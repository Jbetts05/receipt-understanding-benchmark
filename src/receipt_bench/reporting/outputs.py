from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from receipt_bench.evaluation.aggregate import AggregateReport

_SOLUTION_FIELDS = [
    "solution_id",
    "overall_quality_score",
    "valid_output_rate",
    "fully_correct_rate",
    "total_samples",
    "succeeded",
    "failed",
    "invalid",
    "error_rate",
    "invalid_rate",
    "coverage",
    "exact_match_rate",
    "mean_normalized_similarity",
    "arithmetic_consistency_rate",
    "schema_valid_rate",
    "document_exact_rate",
    "matched_count",
    "mismatched_count",
    "missing_count",
    "extra_count",
    "precision",
    "recall",
    "f1",
    "hallucinated_extra_rate",
    "mean_attempts",
    "overall_latency_count",
    "overall_latency_mean_ms",
    "overall_latency_p50_ms",
    "overall_latency_p95_ms",
    "document_exact_rate_ci_lower",
    "document_exact_rate_ci_upper",
    "f1_ci_lower",
    "f1_ci_upper",
    "mean_normalized_similarity_ci_lower",
    "mean_normalized_similarity_ci_upper",
    "mean_latency_ms_ci_lower",
    "mean_latency_ms_ci_upper",
    "overall_quality_score_ci_lower",
    "overall_quality_score_ci_upper",
    "fully_correct_rate_ci_lower",
    "fully_correct_rate_ci_upper",
    "priced_cost_total",
    "priced_cost_mean",
    "lifecycle",
    "kind",
    "deployment",
    "model",
]

_COMPONENT_FIELDS = [
    "solution_id",
    "component",
    "sample_count",
    "latency_count",
    "latency_mean_ms",
    "latency_p50_ms",
    "latency_p95_ms",
    "mean_input_tokens",
    "mean_output_tokens",
    "mean_total_tokens",
    "mean_pages",
    "mean_contextualization_tokens",
    "total_estimated_cost",
    "mean_estimated_cost",
    "priced_cost_total",
    "priced_cost_mean",
]

_FIELD_FIELDS = [
    "solution_id",
    "dataset",
    "language",
    "field",
    "total_count",
    "labeled_count",
    "coverage",
    "exact_match_rate",
    "mean_normalized_similarity",
    "missing_count",
    "mismatched_count",
    "matched_count",
    "extra_count",
    "precision",
    "recall",
    "f1",
]

_WARMUP_FIELDS = [
    "solution_id",
    "warmup_count",
    "latency_count",
    "latency_mean_ms",
    "latency_p50_ms",
    "latency_p95_ms",
]

_WARMUP_COMPONENT_FIELDS = [
    "solution_id",
    "component",
    "sample_count",
    "latency_count",
    "latency_mean_ms",
    "latency_p50_ms",
    "latency_p95_ms",
    "mean_input_tokens",
    "mean_output_tokens",
    "mean_total_tokens",
    "mean_pages",
    "mean_contextualization_tokens",
    "total_estimated_cost",
    "mean_estimated_cost",
    "priced_cost_total",
    "priced_cost_mean",
]


def _json_default(value: object) -> str:
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        result = isoformat()
        return str(result)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def write_json_report(report: AggregateReport, path: Path) -> Path:
    """Write the full aggregate report as a single JSON document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(report)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )
    return path


def write_csv_reports(report: AggregateReport, output_dir: Path) -> dict[str, Path]:
    """Write the aggregate report as five CSVs: solutions, components, fields,
    warmups, and warmup_components.

    ``warmups``/``warmup_components`` cover warmup-call overhead only (see
    ``receipt_bench.evaluation.aggregate.WarmupAggregate``); they are always
    written, even when empty (a run with ``config.warmup_requests=0`` produces
    header-only files), so downstream tooling never needs to special-case their
    absence.

    Returns the paths written, keyed by ``"solutions"``, ``"components"``,
    ``"fields"``, ``"warmups"``, and ``"warmup_components"``.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    solutions_path = output_dir / "solutions.csv"
    components_path = output_dir / "components.csv"
    fields_path = output_dir / "fields.csv"
    warmups_path = output_dir / "warmups.csv"
    warmup_components_path = output_dir / "warmup_components.csv"

    with solutions_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_SOLUTION_FIELDS)
        writer.writeheader()
        for solution in report.solutions:
            row: dict[str, Any] = {
                "solution_id": solution.solution_id,
                "overall_quality_score": solution.overall_quality_score,
                "valid_output_rate": solution.valid_output_rate,
                "fully_correct_rate": solution.fully_correct_rate,
                "total_samples": solution.total_samples,
                "succeeded": solution.succeeded,
                "failed": solution.failed,
                "invalid": solution.invalid,
                "error_rate": solution.error_rate,
                "invalid_rate": solution.invalid_rate,
                "coverage": solution.coverage,
                "exact_match_rate": solution.exact_match_rate,
                "mean_normalized_similarity": solution.mean_normalized_similarity,
                "arithmetic_consistency_rate": solution.arithmetic_consistency_rate,
                "schema_valid_rate": solution.schema_valid_rate,
                "document_exact_rate": solution.document_exact_rate,
                "matched_count": solution.matched_count,
                "mismatched_count": solution.mismatched_count,
                "missing_count": solution.missing_count,
                "extra_count": solution.extra_count,
                "precision": solution.precision,
                "recall": solution.recall,
                "f1": solution.f1,
                "hallucinated_extra_rate": solution.hallucinated_extra_rate,
                "mean_attempts": solution.mean_attempts,
                "overall_latency_count": solution.overall_latency.count,
                "overall_latency_mean_ms": solution.overall_latency.mean_ms,
                "overall_latency_p50_ms": solution.overall_latency.p50_ms,
                "overall_latency_p95_ms": solution.overall_latency.p95_ms,
                "document_exact_rate_ci_lower": solution.document_exact_rate_ci.lower,
                "document_exact_rate_ci_upper": solution.document_exact_rate_ci.upper,
                "f1_ci_lower": solution.f1_ci.lower,
                "f1_ci_upper": solution.f1_ci.upper,
                "mean_normalized_similarity_ci_lower": (
                    solution.mean_normalized_similarity_ci.lower
                ),
                "mean_normalized_similarity_ci_upper": (
                    solution.mean_normalized_similarity_ci.upper
                ),
                "mean_latency_ms_ci_lower": solution.mean_latency_ms_ci.lower,
                "mean_latency_ms_ci_upper": solution.mean_latency_ms_ci.upper,
                "overall_quality_score_ci_lower": solution.overall_quality_score_ci.lower,
                "overall_quality_score_ci_upper": solution.overall_quality_score_ci.upper,
                "fully_correct_rate_ci_lower": solution.fully_correct_rate_ci.lower,
                "fully_correct_rate_ci_upper": solution.fully_correct_rate_ci.upper,
                "priced_cost_total": solution.priced_cost_total,
                "priced_cost_mean": solution.priced_cost_mean,
                "lifecycle": solution.lifecycle,
                "kind": solution.kind,
                "deployment": solution.deployment,
                "model": solution.model,
            }
            writer.writerow(row)

    with components_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_COMPONENT_FIELDS)
        writer.writeheader()
        for solution in report.solutions:
            for component in solution.components:
                writer.writerow(
                    {
                        "solution_id": solution.solution_id,
                        "component": component.component,
                        "sample_count": component.sample_count,
                        "latency_count": component.latency.count,
                        "latency_mean_ms": component.latency.mean_ms,
                        "latency_p50_ms": component.latency.p50_ms,
                        "latency_p95_ms": component.latency.p95_ms,
                        "mean_input_tokens": component.mean_input_tokens,
                        "mean_output_tokens": component.mean_output_tokens,
                        "mean_total_tokens": component.mean_total_tokens,
                        "mean_pages": component.mean_pages,
                        "mean_contextualization_tokens": component.mean_contextualization_tokens,
                        "total_estimated_cost": component.total_estimated_cost,
                        "mean_estimated_cost": component.mean_estimated_cost,
                        "priced_cost_total": component.priced_cost_total,
                        "priced_cost_mean": component.priced_cost_mean,
                    }
                )

    with fields_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FIELD_FIELDS)
        writer.writeheader()
        for field_row in report.fields:
            writer.writerow(
                {
                    "solution_id": field_row.solution_id,
                    "dataset": field_row.dataset,
                    "language": field_row.language,
                    "field": field_row.field,
                    "total_count": field_row.total_count,
                    "labeled_count": field_row.labeled_count,
                    "coverage": field_row.coverage,
                    "exact_match_rate": field_row.exact_match_rate,
                    "mean_normalized_similarity": field_row.mean_normalized_similarity,
                    "missing_count": field_row.missing_count,
                    "mismatched_count": field_row.mismatched_count,
                    "matched_count": field_row.matched_count,
                    "extra_count": field_row.extra_count,
                    "precision": field_row.precision,
                    "recall": field_row.recall,
                    "f1": field_row.f1,
                }
            )

    with warmups_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_WARMUP_FIELDS)
        writer.writeheader()
        for warmup in report.warmups:
            writer.writerow(
                {
                    "solution_id": warmup.solution_id,
                    "warmup_count": warmup.warmup_count,
                    "latency_count": warmup.latency.count,
                    "latency_mean_ms": warmup.latency.mean_ms,
                    "latency_p50_ms": warmup.latency.p50_ms,
                    "latency_p95_ms": warmup.latency.p95_ms,
                }
            )

    with warmup_components_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_WARMUP_COMPONENT_FIELDS)
        writer.writeheader()
        for warmup in report.warmups:
            for component in warmup.components:
                writer.writerow(
                    {
                        "solution_id": warmup.solution_id,
                        "component": component.component,
                        "sample_count": component.sample_count,
                        "latency_count": component.latency.count,
                        "latency_mean_ms": component.latency.mean_ms,
                        "latency_p50_ms": component.latency.p50_ms,
                        "latency_p95_ms": component.latency.p95_ms,
                        "mean_input_tokens": component.mean_input_tokens,
                        "mean_output_tokens": component.mean_output_tokens,
                        "mean_total_tokens": component.mean_total_tokens,
                        "mean_pages": component.mean_pages,
                        "mean_contextualization_tokens": (
                            component.mean_contextualization_tokens
                        ),
                        "total_estimated_cost": component.total_estimated_cost,
                        "mean_estimated_cost": component.mean_estimated_cost,
                        "priced_cost_total": component.priced_cost_total,
                        "priced_cost_mean": component.priced_cost_mean,
                    }
                )

    return {
        "solutions": solutions_path,
        "components": components_path,
        "fields": fields_path,
        "warmups": warmups_path,
        "warmup_components": warmup_components_path,
    }


__all__ = ["write_csv_reports", "write_json_report"]
