from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from receipt_bench.projects.ocr_evaluation import evaluate_ocr_text
from receipt_bench.projects.registry import ProjectRegistry
from receipt_bench.storage.local import LocalStorage
from receipt_bench.storage.models import RunRecord, TaskRecord
from receipt_bench.storage.paths import (
    result_comparison,
    result_parsed,
    result_raw,
    result_telemetry,
    run_manifest,
)
from receipt_bench.web.catalog import default_solution_catalog
from receipt_bench.web.repositories import RepositoryBundle
from receipt_bench.web.service import ProductService
from receipt_bench.web.settings import WebSettings


def test_standalone_ocr_result_and_aggregate_use_text_metric_family(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        storage = LocalStorage(tmp_path / "storage")
        repositories = RepositoryBundle(
            artifacts=storage.artifacts,
            projects=storage.projects,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
        )
        registry = ProjectRegistry.with_builtins(repository_root=Path.cwd())
        service = ProductService(
            repositories=repositories,
            registry=registry,
            catalog=default_solution_catalog(),
            settings=WebSettings(storage_root=tmp_path / "storage"),
        )
        project = await service.context("sroie", require_available=True)
        sample = next(item for item in project.samples if item.scoring_role == "scored")
        run_id = "run-ocr-metrics"
        solution_id = "azure-vision-read-text"
        now = datetime.now(UTC)
        paths = {
            "raw": result_raw(run_id, sample.id, solution_id),
            "parsed": result_parsed(run_id, sample.id, solution_id),
            "comparison": result_comparison(run_id, sample.id, solution_id),
            "telemetry": result_telemetry(run_id, sample.id, solution_id),
        }
        metric = evaluate_ocr_text(
            project_id="sroie",
            reference="STORE TOTAL 10.00",
            hypothesis="STORE TOTAL 10.00",
        )
        assert sample.gold_path is not None
        await repositories.artifacts.create_json(sample.gold_path, {})
        await repositories.artifacts.create_json(
            paths["raw"],
            {"kind": "ocr-text", "ocr_text": "STORE TOTAL 10.00"},
        )
        await repositories.artifacts.create_json(
            paths["parsed"],
            {"text": "STORE TOTAL 10.00"},
        )
        await repositories.artifacts.create_json(
            paths["comparison"],
            metric.model_dump(mode="json"),
        )
        await repositories.artifacts.create_json(
            paths["telemetry"],
            {
                "components": [
                    {
                        "id": "ocr",
                        "label": "Azure Vision Read standalone",
                        "status": "succeeded",
                        "latency_ms": 125.0,
                        "input_tokens": None,
                        "output_tokens": None,
                        "pages": 1,
                        "estimated_cost_usd": 0.001,
                        "attempt": 1,
                        "input": None,
                        "output": "STORE TOTAL 10.00",
                        "error": None,
                    }
                ],
                "total_tokens": None,
                "total_pages": 1,
                "latency_ms": 125.0,
                "estimated_cost_usd": 0.001,
                "attempts": 1,
                "errors": [],
            },
        )
        task = TaskRecord(
            project_id="sroie",
            run_id=run_id,
            sample_id=sample.id,
            solution_id=solution_id,
            image_artifact_path=sample.image_path,
            media_type=sample.media_type,
            status="succeeded",
            stage="completed",
            raw_artifact_path=paths["raw"],
            parsed_artifact_path=paths["parsed"],
            comparison_artifact_path=paths["comparison"],
            telemetry_artifact_path=paths["telemetry"],
            terminal_event_emitted=True,
            created_at=now,
            updated_at=now,
            metadata={
                "solution_order": 17,
                "compatibility": "full",
                "comparable_quality": True,
            },
        )
        run = RunRecord(
            project_id="sroie",
            run_id=run_id,
            manifest_artifact_path=run_manifest(run_id),
            total_tasks=1,
            pending_tasks=0,
            succeeded_tasks=1,
            status="completed",
            finalizer_completed=True,
            completion_event_emitted=True,
            created_at=now,
            updated_at=now,
            metadata={"dispatch_complete": True},
        )

        result = await service.result_view(
            task,
            project=project,
            sample=sample,
            gold={},
        )
        aggregate = await service._compute_aggregate(project, run, [task])
        row = aggregate.solutions[0]

        assert result.metric_family == "ocr_text"
        assert result.schema_valid is None
        assert result.ocr_metrics is not None
        assert result.ocr_metrics.model_dump(mode="json") == metric.model_dump(
            mode="json"
        )
        assert result.comparisons == []
        assert result.quality_scored
        assert row.metric_family == "ocr_text"
        assert row.normalized_field_f1.value is None
        assert row.reference_token_recall is not None
        assert row.reference_token_recall.value == 1.0
        assert row.character_error_rate is not None
        assert row.character_error_rate.value == 0.0
        assert row.character_error_rate.polarity == "lower_better"
        assert row.median_latency.value == 125.0
        assert row.p95_latency.value == 125.0
        assert row.estimated_cost.value == 0.001
        assert row.total_pages.value == 1
        assert row.total_attempts.value == 1
        assert row.input_tokens.value is None
        assert row.cost_scope == "complete"
        assert aggregate.fields == []

        pending_sample = next(
            item
            for item in project.samples
            if item.scoring_role == "scored" and item.id != sample.id
        )
        pending_task = replace(
            task,
            sample_id=pending_sample.id,
            status="queued",
            stage="queued",
            raw_artifact_path=None,
            parsed_artifact_path=None,
            comparison_artifact_path=None,
            telemetry_artifact_path=None,
            terminal_event_emitted=False,
        )
        partial = await service._compute_aggregate(
            project,
            replace(
                run,
                total_tasks=2,
                pending_tasks=1,
                status="running",
                finalizer_completed=False,
                completion_event_emitted=False,
            ),
            [task, pending_task],
        )
        assert partial.solutions[0].estimated_cost.value == 0.001
        assert len(partial.solutions[0].estimated_cost.evidence) == 1

    asyncio.run(scenario())


def test_standalone_ocr_aggregate_is_unavailable_when_reference_evidence_is_missing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        storage = LocalStorage(tmp_path / "storage")
        repositories = RepositoryBundle(
            artifacts=storage.artifacts,
            projects=storage.projects,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
        )
        service = ProductService(
            repositories=repositories,
            registry=ProjectRegistry.with_builtins(repository_root=Path.cwd()),
            catalog=default_solution_catalog(),
            settings=WebSettings(storage_root=tmp_path / "storage"),
        )
        project = await service.context("sroie", require_available=True)
        sample = next(item for item in project.samples if item.scoring_role == "scored")
        run_id = "run-missing-ocr-reference"
        solution_id = "azure-vision-read-text"
        telemetry_path = result_telemetry(run_id, sample.id, solution_id)
        await repositories.artifacts.create_json(
            telemetry_path,
            {
                "components": [],
                "latency_ms": 75.0,
                "estimated_cost_usd": 0.002,
                "attempts": 1,
                "errors": ["OCR failed"],
            },
        )
        now = datetime.now(UTC)
        task = TaskRecord(
            project_id="sroie",
            run_id=run_id,
            sample_id=sample.id,
            solution_id=solution_id,
            image_artifact_path=sample.image_path,
            media_type=sample.media_type,
            status="failed",
            stage="completed",
            telemetry_artifact_path=telemetry_path,
            error_code="ocr_failed",
            error_message="OCR failed",
            terminal_event_emitted=True,
            created_at=now,
            updated_at=now,
            metadata={
                "solution_order": 17,
                "compatibility": "full",
                "comparable_quality": True,
            },
        )
        run = RunRecord(
            project_id="sroie",
            run_id=run_id,
            manifest_artifact_path=run_manifest(run_id),
            total_tasks=1,
            pending_tasks=0,
            failed_tasks=1,
            status="failed",
            finalizer_completed=True,
            completion_event_emitted=True,
            created_at=now,
            updated_at=now,
            metadata={"dispatch_complete": True},
        )

        aggregate = await service._compute_aggregate(project, run, [task])
        row = aggregate.solutions[0]

        assert row.reference_token_recall is not None
        assert row.reference_token_recall.value is None
        assert len(row.reference_token_recall.evidence) == 1
        assert row.median_latency.value == 75.0
        assert row.median_latency.polarity == "lower_better"
        assert row.estimated_cost.value == 0.002
        assert row.estimated_cost.polarity == "lower_better"

    asyncio.run(scenario())
