from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from receipt_bench.projects.registry import ProjectRegistry
from receipt_bench.storage.local import LocalStorage
from receipt_bench.storage.paths import (
    project_prompt,
    project_record,
    project_schema,
    run_manifest,
    sample_gold,
)
from receipt_bench.web import WebSettings, create_local_app
from receipt_bench.web.repositories import RepositoryBundle
from receipt_bench.web.runtime import _ocr_artifact_path


def test_builtin_seed_persists_complete_private_artifacts_and_marker(
    tmp_path: Path,
) -> None:
    repository_root = Path.cwd()
    registry = ProjectRegistry.with_builtins(repository_root=repository_root)
    storage_root = tmp_path / "seeded-storage"
    settings = WebSettings(
        storage_root=storage_root,
        repository_root=repository_root,
        start_worker=False,
        seed_builtin_projects=True,
        seed_cord=True,
        seed_sroie=True,
        sroie_rights_evidence="approved-for-test-hosting",
    )
    app = create_local_app(settings, registry=registry)

    with TestClient(app, base_url="http://localhost") as client:
        samples = client.get("/api/projects/cord-v2/samples")
        assert samples.status_code == 200
        first = samples.json()[0]
        image = client.get(
            f"/api/projects/cord-v2/samples/{first['id']}/image"
        )
        assert image.status_code == 200
        assert image.headers["content-type"] in {"image/jpeg", "image/png"}
        assert image.content
        project_view = client.get("/api/projects/cord-v2").json()
        scored = next(
            sample
            for sample in samples.json()
            if sample["scoring_role"] == "scored"
        )
        accepted = client.post(
            "/api/projects/cord-v2/runs",
            json={
                "sample_ids": [scored["id"]],
                "solution_ids": ["gpt-5.4-mini", "azure-vision-read"],
                "schema_version": project_view["schema"]["version"],
                "prompt_version": project_view["prompt"]["version"],
                "execution_mode": "mock",
            },
        )
        assert accepted.status_code == 202

    artifact_root = storage_root / "artifacts"
    project = registry.get("cord-v2")
    assert (artifact_root / project_schema(project.project_id)).is_file()
    assert (
        artifact_root
        / project_prompt(project.project_id, project.prompts[0].version)
    ).is_file()
    assert (artifact_root / project_record(project.project_id)).is_file()
    manifest = json.loads(
        (artifact_root / run_manifest(accepted.json()["run_id"])).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["solution_prompts"]["gpt-5.4-mini"]["mode"] == "direct-multimodal"
    assert manifest["solution_prompts"]["azure-vision-read"]["mode"] == "ocr-normalizer"
    assert (
        manifest["solution_prompts"]["gpt-5.4-mini"]["hash"]
        != manifest["solution_prompts"]["azure-vision-read"]["hash"]
    )
    for sample in project.samples:
        assert (artifact_root / sample_gold(project.project_id, sample.sample_id)).is_file()
        assert (
            artifact_root / _ocr_artifact_path(project.project_id, sample.sample_id)
        ).read_text(encoding="utf-8")

    markers = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (storage_root / "coordination" / "seeds").glob("*.json")
    ]
    assert len(markers) == 2
    assert {marker["status"] for marker in markers} == {"completed"}
    assert {
        marker["marker"]["project_id"]: marker["marker"]["sample_count"]
        for marker in markers
    } == {"cord-v2": 20, "sroie": 80}

    second_settings = settings.model_copy(
        update={"repository_root": tmp_path / "source-no-longer-present"}
    )
    second = create_local_app(second_settings, registry=registry)
    with TestClient(second, base_url="http://localhost") as client:
        assert client.get("/api/projects/cord-v2").status_code == 200


def test_builtin_seed_requires_durable_coordinator(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "storage")
    repositories = RepositoryBundle(
        artifacts=storage.artifacts,
        projects=storage.projects,
        runs=storage.runs,
        tasks=storage.tasks,
        events=storage.events,
        queue=storage.queue,
    )
    app = create_local_app(
        WebSettings(
            storage_root=tmp_path / "storage",
            repository_root=Path.cwd(),
            start_worker=False,
            seed_builtin_projects=True,
            seed_cord=True,
        ),
        repositories=repositories,
    )

    with (
        pytest.raises(RuntimeError, match="durable seed coordinator"),
        TestClient(app, base_url="http://localhost"),
    ):
        pass
