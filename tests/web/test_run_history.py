from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient

from receipt_bench.storage.local import LocalStorage
from receipt_bench.storage.models import RunRecord, Versioned
from receipt_bench.storage.paths import run_manifest
from receipt_bench.storage.protocols import RunRepository
from receipt_bench.web import WebSettings, create_hosted_app, create_local_app
from receipt_bench.web.repositories import RepositoryBundle

from .conftest import create_project


def _run(
    project_id: str,
    run_id: str,
    created_at: datetime,
    *,
    completed: bool = False,
) -> RunRecord:
    return RunRecord(
        project_id=project_id,
        run_id=run_id,
        manifest_artifact_path=run_manifest(run_id),
        total_tasks=6,
        pending_tasks=0 if completed else 6,
        status="completed" if completed else "queued",
        succeeded_tasks=6 if completed else 0,
        finalizer_completed=completed,
        download_artifact_path=f"runs/{run_id}/download.zip" if completed else None,
        created_at=created_at,
        updated_at=created_at + timedelta(seconds=30),
        metadata={
            "execution_mode": "mock",
            "sample_count": 2,
            "solution_count": 3,
            "reference_only_count": 1,
            "dispatch_complete": True,
        },
    )


def _store_runs(storage: LocalStorage, runs: list[RunRecord]) -> None:
    async def store() -> None:
        for run in runs:
            await storage.runs.create(run)

    asyncio.run(store())


def test_local_run_history_persists_with_order_limit_isolation_and_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "history"
    now = datetime(2026, 8, 30, 9, tzinfo=UTC)
    storage = LocalStorage(root)
    _store_runs(
        storage,
        [
            _run("cord-v2", "run-old", now),
            _run("sroie", "run-foreign", now + timedelta(hours=3)),
            _run("cord-v2", "run-middle", now + timedelta(hours=1)),
            _run("cord-v2", "run-new", now + timedelta(hours=2), completed=True),
        ],
    )

    first_app = create_local_app(
        WebSettings(storage_root=root, start_worker=False),
        storage=storage,
    )
    with TestClient(first_app, base_url="http://localhost") as client:
        empty_project = create_project(client, name="Empty history")
        empty = client.get(f"/api/projects/{empty_project['id']}/runs")
        assert empty.status_code == 200
        assert empty.json() == []

        limited = client.get("/api/projects/cord-v2/runs?limit=2")
        assert limited.status_code == 200
        assert limited.headers["cache-control"] == "private, max-age=0, no-store"
        assert [item["run_id"] for item in limited.json()] == ["run-new", "run-middle"]
        assert {item["project_id"] for item in limited.json()} == {"cord-v2"}
        assert limited.json()[0] == {
            "run_id": "run-new",
            "project_id": "cord-v2",
            "status": "completed",
            "created_at": "2026-08-30T11:00:00Z",
            "updated_at": "2026-08-30T11:00:30Z",
            "total_pairs": 6,
            "completed_pairs": 6,
            "execution_mode": "mock",
            "sample_count": 2,
            "solution_count": 3,
            "reference_only_count": 1,
            "download_ready": True,
        }
        assert [item["run_id"] for item in client.get("/api/projects/sroie/runs").json()] == [
            "run-foreign"
        ]
        assert client.get("/api/projects/cord-v2/runs?limit=0").status_code == 422
        assert client.get("/api/projects/cord-v2/runs?limit=101").status_code == 422
        assert client.get("/api/projects/INVALID/runs").status_code == 404

    resumed_app = create_local_app(
        WebSettings(storage_root=root, start_worker=False),
    )
    with TestClient(resumed_app, base_url="http://localhost") as client:
        history = client.get("/api/projects/cord-v2/runs")
        assert history.status_code == 200
        assert [item["run_id"] for item in history.json()] == [
            "run-new",
            "run-middle",
            "run-old",
        ]


class RecordingRunHistoryRepository:
    def __init__(self, delegate: RunRepository) -> None:
        self.delegate = delegate
        self.calls: list[tuple[str, int]] = []

    async def list(
        self,
        project_id: str,
        *,
        limit: int = 100,
    ) -> list[Versioned[RunRecord]]:
        self.calls.append((project_id, limit))
        return await self.delegate.list(project_id, limit=limit)


def test_hosted_history_uses_repository_protocol_and_checks_availability(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path / "hosted")
    _store_runs(
        storage,
        [_run("cord-v2", "run-hosted", datetime(2026, 8, 30, tzinfo=UTC))],
    )
    repositories = RepositoryBundle.from_storage(storage)
    recording = RecordingRunHistoryRepository(repositories.runs)
    repositories = replace(
        repositories,
        runs=cast(RunRepository, recording),
    )
    app = create_hosted_app(
        settings=WebSettings(
            profile="hosted",
            allowed_hosts=("api.example.test",),
            start_worker=False,
        ),
        repositories=repositories,
    )

    with TestClient(app, base_url="https://api.example.test") as client:
        history = client.get("/api/projects/cord-v2/runs?limit=7")
        assert history.status_code == 200
        assert [item["run_id"] for item in history.json()] == ["run-hosted"]
        assert recording.calls == [("cord-v2", 7)]

        restricted = client.get("/api/projects/sroie/runs")
        assert restricted.status_code == 451
        assert restricted.json()["code"] == "rights_restricted"
        assert recording.calls == [("cord-v2", 7)]
