from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from receipt_bench.projects.models import (
    ArtifactReference,
    Project,
    ProjectSchema,
    Result,
    ResultArtifacts,
    RunManifest,
    Sample,
    Task,
)


def test_run_manifest_pins_contracts_and_rejects_non_scored_samples(
    sroie_project: Project,
) -> None:
    manifest = RunManifest.for_project(
        run_id="run-001",
        project=sroie_project,
        created_at=datetime.now(UTC),
        sample_ids=["sroie-001"],
        solution_ids=["gpt-5.4-mini"],
    )

    assert manifest.schema_ref.content_hash == sroie_project.project_schema.content_hash
    assert manifest.sample_ids == ("sroie-001",)
    assert manifest.solutions[0].comparable_quality
    for sample_id in ("sroie-012", "sroie-015"):
        with pytest.raises(ValueError, match="Non-scored"):
            RunManifest.for_project(
                run_id="run-002",
                project=sroie_project,
                created_at=datetime.now(UTC),
                sample_ids=[sample_id],
                solution_ids=["gpt-5.4-mini"],
            )


def test_contracts_reject_non_finite_nested_values() -> None:
    with pytest.raises(ValidationError, match="finite"):
        ProjectSchema(
            schema_id="example",
            version="1.0.0",
            content_hash="0" * 64,
            source=ArtifactReference(kind="schema", path="schemas/example.json"),
            document={"nested": [{"value": float("nan")}]},
        )


def test_excluded_sample_requires_exclusion_reason(sroie_project: Project) -> None:
    payload = sroie_project.sample("sroie-015").model_dump(mode="json")
    payload["exclusion_reason"] = None

    with pytest.raises(ValidationError, match="Excluded samples require"):
        Sample.model_validate(payload)


def test_successful_result_requires_parsed_and_comparison_artifacts() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError, match="parsed and comparison"):
        Result(
            run_id="run-001",
            project_id="sroie",
            sample_id="sroie-001",
            solution_id="gpt-5.4-mini",
            status="succeeded",
            attempts=1,
            started_at=now,
            completed_at=now,
            artifacts=ResultArtifacts(
                telemetry=ArtifactReference(
                    kind="telemetry", path="runs/run-001/telemetry.json"
                )
            ),
        )


def test_task_uses_stable_table_pair_key() -> None:
    now = datetime.now(UTC)
    task = Task(
        task_id="sroie-001|gpt-5.4-mini",
        run_id="run-001",
        project_id="sroie",
        sample_id="sroie-001",
        solution_id="gpt-5.4-mini",
        status="queued",
        stage="pending",
        created_at=now,
        updated_at=now,
    )
    assert task.task_id == "sroie-001|gpt-5.4-mini"

    with pytest.raises(ValidationError, match="stable pair key"):
        Task(
            task_id="wrong",
            run_id="run-001",
            project_id="sroie",
            sample_id="sroie-001",
            solution_id="gpt-5.4-mini",
            status="queued",
            stage="pending",
            created_at=now,
            updated_at=now,
        )
