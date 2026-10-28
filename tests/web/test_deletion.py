from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from receipt_bench.storage.models import RunRecord, TaskRecord, utc_now
from receipt_bench.storage.paths import run_manifest, sample_image

from .conftest import PNG_BYTES, create_project, upload_sample


def _store_terminal_run(app: FastAPI, project_id: str, run_id: str) -> None:
    repositories = app.state.repositories
    now = utc_now()
    run = RunRecord(
        project_id=project_id,
        run_id=run_id,
        manifest_artifact_path=run_manifest(run_id),
        total_tasks=1,
        pending_tasks=0,
        status="completed",
        succeeded_tasks=1,
        finalizer_completed=True,
        completion_event_emitted=True,
        created_at=now,
        updated_at=now,
        metadata={"dispatch_complete": True},
    )
    task = TaskRecord(
        project_id=project_id,
        run_id=run_id,
        sample_id="sample-one",
        solution_id="solution-one",
        image_artifact_path=sample_image(project_id, "sample-one"),
        media_type="image/png",
        status="succeeded",
        terminal_event_emitted=True,
        created_at=now,
        updated_at=now,
    )

    async def store() -> None:
        await repositories.runs.create(run)
        await repositories.tasks.create(task)
        await repositories.artifacts.create_json(run.manifest_artifact_path, {"run": run_id})
        await repositories.events.append(run_id, "run_completed", {"run_id": run_id})

    asyncio.run(store())


def _store_active_run(app: FastAPI, project_id: str, run_id: str) -> None:
    repositories = app.state.repositories
    now = utc_now()
    asyncio.run(
        repositories.runs.create(
            RunRecord(
                project_id=project_id,
                run_id=run_id,
                manifest_artifact_path=run_manifest(run_id),
                total_tasks=1,
                pending_tasks=1,
                status="running",
                created_at=now,
                updated_at=now,
                metadata={"dispatch_complete": True},
            )
        )
    )


def test_delete_protects_builtins_and_rejects_active_runs(
    client: TestClient,
    local_app: FastAPI,
) -> None:
    protected = client.delete("/api/projects/sroie")
    assert protected.status_code == 403
    assert protected.json()["code"] == "protected_project"

    project = create_project(client, name="Active deletion")
    _store_active_run(local_app, project["id"], "run-active-delete")
    run_response = client.delete("/api/runs/run-active-delete")
    assert run_response.status_code == 409
    assert run_response.json()["code"] == "run_active"
    project_response = client.delete(f"/api/projects/{project['id']}")
    assert project_response.status_code == 409
    assert project_response.json()["code"] == "project_has_active_runs"


def test_project_analyzer_cleanup_waits_for_last_schema_reference(
    client: TestClient,
    local_app: FastAPI,
) -> None:
    solution_id = "content-understanding-custom"
    first = create_project(
        client,
        name="Shared schema one",
        solutions=[solution_id],
    )
    second = create_project(
        client,
        name="Shared schema two",
        solutions=[solution_id],
    )
    cleaned: list[str] = []

    async def cleanup(analyzer_id: str) -> None:
        cleaned.append(analyzer_id)

    local_app.state.product_service.set_project_analyzer_cleanup(cleanup)

    assert client.delete(f"/api/projects/{first['id']}").status_code == 204
    assert cleaned == []
    assert client.delete(f"/api/projects/{second['id']}").status_code == 204
    schema_hash = first["schema"]["hash"].removeprefix("sha256:")
    assert cleaned == [f"custom_{schema_hash[:24]}"]


def test_delete_run_is_durable_retry_safe_and_project_scoped(
    client: TestClient,
    local_app: FastAPI,
) -> None:
    first = create_project(client, name="Run delete first")
    second = create_project(client, name="Run delete second")
    _store_terminal_run(local_app, first["id"], "run-delete-first")
    _store_terminal_run(local_app, second["id"], "run-delete-second")
    artifacts = local_app.state.repositories.artifacts
    original_delete_prefix = artifacts.delete_prefix
    calls = 0

    async def fail_once(prefix: str) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated partial deletion")
        return await original_delete_prefix(prefix)

    artifacts.delete_prefix = fail_once
    try:
        with pytest.raises(OSError, match="simulated partial deletion"):
            client.delete("/api/runs/run-delete-first")
    finally:
        artifacts.delete_prefix = original_delete_prefix

    retried = client.delete("/api/runs/run-delete-first")
    assert retried.status_code == 204
    assert client.get("/api/runs/run-delete-first").status_code == 404
    assert asyncio.run(local_app.state.repositories.runs.find("run-delete-second"))
    assert client.delete("/api/runs/run-delete-first").status_code == 404


def test_delete_custom_project_cascades_without_cross_project_deletion(
    client: TestClient,
    local_app: FastAPI,
) -> None:
    first = create_project(client, name="Cascade first")
    second = create_project(client, name="Cascade second")
    upload_sample(client, first["id"], filename="first.png")
    second_sample = upload_sample(client, second["id"], filename="second.png")
    _store_terminal_run(local_app, first["id"], "run-cascade-first")
    _store_terminal_run(local_app, second["id"], "run-cascade-second")

    artifacts = local_app.state.repositories.artifacts
    original_delete_prefix = artifacts.delete_prefix

    async def fail_project_artifacts(prefix: str) -> int:
        if prefix == f"projects/{first['id']}/":
            raise OSError("simulated project artifact failure")
        return await original_delete_prefix(prefix)

    artifacts.delete_prefix = fail_project_artifacts
    try:
        with pytest.raises(OSError, match="simulated project artifact failure"):
            client.delete(f"/api/projects/{first['id']}")
    finally:
        artifacts.delete_prefix = original_delete_prefix
    assert asyncio.run(
        local_app.state.repositories.projects.get("public", first["id"])
    )
    assert client.get("/api/runs/run-cascade-first").status_code == 404

    response = client.delete(f"/api/projects/{first['id']}")
    assert response.status_code == 204
    assert client.get(f"/api/projects/{first['id']}").status_code == 404
    assert client.get("/api/runs/run-cascade-first").status_code == 404
    assert client.get(f"/api/projects/{second['id']}").status_code == 200
    assert asyncio.run(local_app.state.repositories.runs.find("run-cascade-second"))
    assert (
        client.get(
            f"/api/projects/{second['id']}/samples/{second_sample['id']}/image"
        ).status_code
        == 200
    )
    assert client.delete(f"/api/projects/{first['id']}").status_code == 404


def test_run_created_during_project_deletion_fence_is_removed(
    client: TestClient,
    local_app: FastAPI,
) -> None:
    project = create_project(client, name="Run deletion fence")
    sample = upload_sample(client, project["id"], filename="race.png")
    repositories = local_app.state.repositories
    original_create = repositories.runs.create

    async def create_after_project_removed(run: RunRecord):
        stored = await original_create(run)
        await repositories.projects.delete("public", project["id"])
        return stored

    repositories.runs.create = create_after_project_removed
    try:
        response = client.post(
            f"/api/projects/{project['id']}/runs",
            json={
                "sample_ids": [sample["id"]],
                "solution_ids": ["gpt-5.4-mini"],
                "schema_version": project["schema"]["version"],
                "prompt_version": project["prompt"]["version"],
                "execution_mode": "mock",
            },
        )
    finally:
        repositories.runs.create = original_create

    assert response.status_code == 409
    assert response.json()["code"] == "project_deleting"
    assert asyncio.run(repositories.runs.list_all(project["id"])) == []


def test_project_deletion_rechecks_runs_after_setting_fence(
    client: TestClient,
    local_app: FastAPI,
) -> None:
    project = create_project(client, name="Deletion inventory fence")
    repositories = local_app.state.repositories
    original_replace = repositories.projects.replace
    inserted = False

    async def replace_and_race(record, *, expected_etag: str):
        nonlocal inserted
        stored = await original_replace(record, expected_etag=expected_etag)
        if record.metadata.get("deletion_pending") is True and not inserted:
            inserted = True
            now = utc_now()
            await repositories.runs.create(
                RunRecord(
                    project_id=project["id"],
                    run_id="run-raced-delete",
                    manifest_artifact_path=run_manifest("run-raced-delete"),
                    total_tasks=1,
                    pending_tasks=1,
                    status="running",
                    created_at=now,
                    updated_at=now,
                    metadata={"dispatch_complete": True},
                )
            )
        return stored

    repositories.projects.replace = replace_and_race
    try:
        response = client.delete(f"/api/projects/{project['id']}")
    finally:
        repositories.projects.replace = original_replace

    assert response.status_code == 409
    assert response.json()["code"] == "project_has_active_runs"
    stored = asyncio.run(repositories.projects.get("public", project["id"]))
    assert stored.value.metadata["deletion_pending"] is True


def test_upload_race_cleans_only_its_staged_artifacts(
    client: TestClient,
    local_app: FastAPI,
) -> None:
    project = create_project(client, name="Upload deletion fence")
    first = upload_sample(client, project["id"], filename="first.png")
    repositories = local_app.state.repositories
    before = asyncio.run(
        repositories.artifacts.list_prefix(f"projects/{project['id']}/")
    )
    original_create_bytes = repositories.artifacts.create_bytes
    fenced = False

    async def create_bytes_and_fence(path: str, data: bytes, **kwargs):
        nonlocal fenced
        info = await original_create_bytes(path, data, **kwargs)
        if "/uploads/" in path and not fenced:
            fenced = True
            current = await repositories.projects.get("public", project["id"])
            await repositories.projects.replace(
                replace(
                    current.value,
                    metadata={
                        **current.value.metadata,
                        "deletion_pending": True,
                    },
                ),
                expected_etag=current.etag,
            )
        return info

    repositories.artifacts.create_bytes = create_bytes_and_fence
    try:
        response = client.post(
            f"/api/projects/{project['id']}/samples",
            files={"image": ("second.png", PNG_BYTES + b"second", "image/png")},
        )
    finally:
        repositories.artifacts.create_bytes = original_create_bytes

    assert response.status_code == 409
    assert response.json()["code"] == "project_deleting"
    after = asyncio.run(
        repositories.artifacts.list_prefix(f"projects/{project['id']}/")
    )
    assert after == before
    assert (
        client.get(
            f"/api/projects/{project['id']}/samples/{first['id']}/image"
        ).status_code
        == 200
    )


def test_sample_limit_rejection_writes_no_artifacts(
    client: TestClient,
    local_app: FastAPI,
) -> None:
    service = local_app.state.product_service
    original_settings = service.settings
    service.settings = original_settings.model_copy(
        update={"max_samples_per_project": 1}
    )
    try:
        project = create_project(client, name="Bounded uploads")
        upload_sample(client, project["id"], filename="first.png")
        repositories = local_app.state.repositories
        before = asyncio.run(
            repositories.artifacts.list_prefix(f"projects/{project['id']}/")
        )

        response = client.post(
            f"/api/projects/{project['id']}/samples",
            files={"image": ("second.png", PNG_BYTES + b"second", "image/png")},
        )

        assert response.status_code == 409
        assert response.json()["code"] == "sample_limit"
        after = asyncio.run(
            repositories.artifacts.list_prefix(f"projects/{project['id']}/")
        )
        assert after == before
    finally:
        service.settings = original_settings


def test_ambiguous_sample_index_commit_keeps_referenced_upload_artifacts(
    client: TestClient,
    local_app: FastAPI,
) -> None:
    project = create_project(client, name="Ambiguous upload commit")
    repositories = local_app.state.repositories
    original_replace = repositories.projects.replace
    raised = False

    async def replace_then_timeout(record, *, expected_etag: str):
        nonlocal raised
        stored = await original_replace(record, expected_etag=expected_etag)
        if record.metadata.get("sample_index_path") is not None and not raised:
            raised = True
            raise OSError("simulated ambiguous commit response")
        return stored

    repositories.projects.replace = replace_then_timeout
    try:
        response = client.post(
            f"/api/projects/{project['id']}/samples",
            files={"image": ("ambiguous.png", PNG_BYTES, "image/png")},
        )
    finally:
        repositories.projects.replace = original_replace

    assert response.status_code == 201, response.text
    sample = response.json()
    assert (
        client.get(
            f"/api/projects/{project['id']}/samples/{sample['id']}/image"
        ).status_code
        == 200
    )
