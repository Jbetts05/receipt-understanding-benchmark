from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from receipt_bench.storage.local import LocalStorage
from receipt_bench.storage.models import TaskRecord
from receipt_bench.web import WebSettings, create_local_app
from receipt_bench.web.repositories import RepositoryBundle

from .conftest import (
    create_project,
    create_run,
    upload_sample,
    wait_for_terminal,
)


def _event_ids(body: str) -> list[int]:
    return [
        int(line.removeprefix("id: "))
        for line in body.splitlines()
        if line.startswith("id: ")
    ]


def test_durable_sse_replay_resume_terminal_and_heartbeat(tmp_path: Path) -> None:
    app = create_local_app(
        WebSettings(
            storage_root=tmp_path / "completed",
            start_worker=True,
            sse_poll_interval=0.01,
            sse_heartbeat_interval=0.02,
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        project = create_project(client)
        sample = upload_sample(
            client,
            project["id"],
            gold={"merchant": "SSE", "total": 9},
        )
        accepted = create_run(client, project, [sample["id"]], ["gpt-5.4-mini"])
        snapshot = wait_for_terminal(client, accepted["run_id"])

        deadline = time.monotonic() + 2
        replay = client.get(f"{accepted['event_url']}?follow=false")
        while "event: run_completed" not in replay.text and time.monotonic() < deadline:
            time.sleep(0.01)
            replay = client.get(f"{accepted['event_url']}?follow=false")
        assert replay.status_code == 200
        ids = _event_ids(replay.text)
        assert ids == sorted(ids)
        assert len(ids) == len(set(ids))
        assert "event: run_started" in replay.text
        assert "event: task_progress" in replay.text
        assert "event: task_completed" in replay.text
        assert "event: run_completed" in replay.text
        assert int(snapshot["event_cursor"]) <= ids[-1]
        for line in replay.text.splitlines():
            if line.startswith("data: "):
                payload = json.loads(line.removeprefix("data: "))
                assert isinstance(payload["type"], str)
                if payload["type"] == "task_completed":
                    assert "result" not in payload
                    assert payload["task"]["partial_output"] is None

        resumed = client.get(
            f"{accepted['event_url']}?follow=false",
            headers={"Last-Event-ID": str(ids[0])},
        )
        assert all(identifier > ids[0] for identifier in _event_ids(resumed.text))
        terminal = client.get(accepted["event_url"])
        assert terminal.status_code == 200
        assert terminal.text.rstrip().endswith("}")

    idle_app = create_local_app(
        WebSettings(
            storage_root=tmp_path / "idle",
            start_worker=False,
            sse_poll_interval=0.01,
            sse_heartbeat_interval=0.02,
        )
    )
    with TestClient(idle_app, base_url="http://localhost") as client:
        project = create_project(client, name="Heartbeat")
        sample = upload_sample(client, project["id"])
        accepted = create_run(client, project, [sample["id"]], ["gpt-5.4-mini"])
        heartbeat = client.get(f"{accepted['event_url']}?cursor=1&follow=false")
        assert heartbeat.status_code == 200
        assert heartbeat.text == ": heartbeat\n\n"


def test_local_repositories_resume_after_restart(tmp_path: Path) -> None:
    storage_root = tmp_path / "persistent"
    initial_app = create_local_app(
        WebSettings(storage_root=storage_root, start_worker=False)
    )
    with TestClient(initial_app, base_url="http://localhost") as client:
        project = create_project(client, name="Persistent project")
        sample = upload_sample(
            client,
            project["id"],
            gold={"merchant": "Restart", "total": 3},
        )
        accepted = create_run(
            client,
            project,
            [sample["id"]],
            ["gpt-5.4-mini"],
            idempotency_key="survive-restart",
        )
        assert client.get(accepted["snapshot_url"]).json()["tasks"][0]["status"] == "queued"

    resumed_app = create_local_app(
        WebSettings(
            storage_root=storage_root,
            start_worker=True,
            sse_poll_interval=0.01,
            sse_heartbeat_interval=0.02,
        )
    )
    with TestClient(resumed_app, base_url="http://localhost") as client:
        projects = client.get("/api/projects").json()
        assert project["id"] in {item["id"] for item in projects}
        snapshot = wait_for_terminal(client, accepted["run_id"])
        assert snapshot["manifest"]["status"] == "completed"
        assert snapshot["results"][0]["parsed_output"] == {
            "merchant": "Restart",
            "total": 3,
        }
        repeated_project = client.post(
            "/api/projects",
            json={
                "name": "Persistent project",
                "description": "Offline API test project",
                "schema": {
                    "type": "object",
                    "properties": {
                        "merchant": {"type": "string"},
                        "total": {"type": "number"},
                    },
                    "required": ["merchant", "total"],
                    "additionalProperties": False,
                },
                "prompt": "Return the merchant and total as strict JSON.",
                "selected_solution_ids": ["gpt-5.4-mini"],
            },
        )
        assert repeated_project.status_code == 201
        assert repeated_project.json()["id"] == project["id"]


def test_project_record_is_published_only_after_required_artifacts(
    tmp_path: Path,
) -> None:
    class FailingArtifacts:
        def __init__(self, delegate: object) -> None:
            self.delegate = delegate

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        async def create_json(
            self,
            path: str,
            value: object,
            *,
            metadata: object | None = None,
        ) -> object:
            if "/prompts/" in path:
                raise RuntimeError("injected artifact failure")
            return await self.delegate.create_json(  # type: ignore[attr-defined,no-any-return]
                path,
                value,
                metadata=metadata,
            )

    storage = LocalStorage(tmp_path / "project-publish")
    base = RepositoryBundle.from_storage(storage)
    repositories = RepositoryBundle(
        artifacts=FailingArtifacts(base.artifacts),  # type: ignore[arg-type]
        projects=base.projects,
        runs=base.runs,
        tasks=base.tasks,
        events=base.events,
        queue=base.queue,
    )
    app = create_local_app(
        WebSettings(storage_root=tmp_path / "project-publish", start_worker=False),
        repositories=repositories,
    )
    with TestClient(
        app,
        base_url="http://localhost",
        raise_server_exceptions=False,
    ) as client:
        response = client.post(
            "/api/projects",
            json={
                "name": "Interrupted project",
                "description": "",
                "schema": {
                    "type": "object",
                    "properties": {"total": {"type": "number"}},
                    "additionalProperties": False,
                },
                "prompt": "Extract total.",
                "selected_solution_ids": ["gpt-5.4-mini"],
            },
        )
        assert response.status_code == 500

    assert asyncio.run(storage.projects.list("public")) == []


def test_startup_recovers_run_interrupted_before_dispatch_completion(
    tmp_path: Path,
) -> None:
    class FailingDispatcher:
        async def enqueue(
            self,
            tasks: list[TaskRecord] | tuple[TaskRecord, ...],
        ) -> None:
            del tasks
            raise RuntimeError("injected dispatch interruption")

    class CapturingDispatcher:
        def __init__(self) -> None:
            self.tasks: list[TaskRecord] = []

        async def enqueue(
            self,
            tasks: list[TaskRecord] | tuple[TaskRecord, ...],
        ) -> None:
            self.tasks.extend(tasks)

    storage_root = tmp_path / "run-recovery"
    storage = LocalStorage(storage_root)
    initial = create_local_app(
        WebSettings(storage_root=storage_root, start_worker=False),
        storage=storage,
        dispatcher=FailingDispatcher(),
    )
    with TestClient(
        initial,
        base_url="http://localhost",
        raise_server_exceptions=False,
    ) as client:
        project = create_project(client, name="Recover run setup")
        sample = upload_sample(
            client,
            project["id"],
            gold={"merchant": "Recovery", "total": 8},
        )
        interrupted = client.post(
            f"/api/projects/{project['id']}/runs",
            headers={"Idempotency-Key": "recover-initialization"},
            json={
                "sample_ids": [sample["id"]],
                "solution_ids": ["gpt-5.4-mini"],
                "schema_version": project["schema"]["version"],
                "prompt_version": project["prompt"]["version"],
                "execution_mode": "mock",
            },
        )
        assert interrupted.status_code == 500
        assert client.get(f"/api/projects/{project['id']}/runs").json() == []

    interrupted_runs = asyncio.run(storage.runs.list(project["id"]))
    assert len(interrupted_runs) == 1
    interrupted_run_id = interrupted_runs[0].value.run_id
    dispatcher = CapturingDispatcher()
    resumed = create_local_app(
        WebSettings(storage_root=storage_root, start_worker=False),
        storage=LocalStorage(storage_root),
        dispatcher=dispatcher,
    )
    with TestClient(resumed, base_url="http://localhost") as client:
        history = client.get(f"/api/projects/{project['id']}/runs").json()
        assert [item["run_id"] for item in history] == [interrupted_run_id]
        snapshot = client.get(f"/api/runs/{interrupted_run_id}").json()
        assert len(snapshot["tasks"]) == 1
        assert snapshot["tasks"][0]["status"] == "queued"
        assert [task.pair_id for task in dispatcher.tasks] == [
            f"{sample['id']}|gpt-5.4-mini"
        ]
