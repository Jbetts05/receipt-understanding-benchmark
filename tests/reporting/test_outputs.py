"""Tests for JSON/CSV aggregate report outputs."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

from receipt_bench.evaluation.aggregate import (
    AggregateReport,
    ComponentAggregate,
    ConfidenceInterval,
    FieldAggregate,
    HistogramBucket,
    LatencyStats,
    SolutionAggregate,
    WarmupAggregate,
)
from receipt_bench.reporting.outputs import write_csv_reports, write_json_report


def _sample_report() -> AggregateReport:
    latency = LatencyStats(count=2, mean_ms=100.0, p50_ms=100.0, p95_ms=150.0)
    component = ComponentAggregate(
        component="extraction",
        sample_count=2,
        latency=latency,
        mean_input_tokens=120.0,
        mean_output_tokens=40.0,
        mean_total_tokens=160.0,
        mean_pages=1.0,
        mean_contextualization_tokens=None,
        total_estimated_cost=0.05,
        mean_estimated_cost=0.025,
    )
    solution = SolutionAggregate(
        solution_id="solution-a",
        total_samples=2,
        succeeded=2,
        failed=0,
        invalid=0,
        error_rate=0.0,
        invalid_rate=0.0,
        coverage=1.0,
        exact_match_rate=0.75,
        mean_normalized_similarity=0.9,
        arithmetic_consistency_rate=1.0,
        schema_valid_rate=1.0,
        document_exact_rate=0.5,
        matched_count=3,
        mismatched_count=1,
        missing_count=0,
        extra_count=0,
        precision=0.75,
        recall=1.0,
        f1=6 / 7,
        hallucinated_extra_rate=0.0,
        mean_attempts=1.0,
        overall_latency=latency,
        latency_histogram=[HistogramBucket(label="100-150 ms", count=2)],
        components=[component],
        document_exact_rate_ci=ConfidenceInterval(lower=0.2, upper=0.8),
        f1_ci=ConfidenceInterval(lower=0.6, upper=1.0),
        mean_normalized_similarity_ci=ConfidenceInterval(lower=0.85, upper=0.95),
        mean_latency_ms_ci=ConfidenceInterval(lower=90.0, upper=110.0),
        priced_cost_total=0.1,
        priced_cost_mean=0.05,
        lifecycle="preview",
        kind="ocr_pipeline",
        deployment="receipt-mistral-ocr-4",
        model="mistral-ocr-4-0",
        overall_quality_score=0.82,
        valid_output_rate=1.0,
        fully_correct_rate=0.5,
        overall_quality_score_ci=ConfidenceInterval(lower=0.75, upper=0.89),
        fully_correct_rate_ci=ConfidenceInterval(lower=0.2, upper=0.8),
    )
    field = FieldAggregate(
        solution_id="solution-a",
        dataset="unit-test",
        language="en",
        field="merchant.name",
        total_count=2,
        labeled_count=2,
        coverage=1.0,
        exact_match_rate=0.5,
        mean_normalized_similarity=0.8,
        missing_count=0,
        mismatched_count=1,
        matched_count=1,
        extra_count=0,
        precision=0.5,
        recall=1.0,
        f1=2 / 3,
    )
    return AggregateReport(
        run_id="run-123",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        solutions=[solution],
        fields=[field],
        warmups=[
            WarmupAggregate(
                solution_id="solution-a",
                warmup_count=2,
                latency=LatencyStats(count=2, mean_ms=50.0, p50_ms=50.0, p95_ms=60.0),
                components=[
                    ComponentAggregate(
                        component="extraction",
                        sample_count=2,
                        latency=LatencyStats(count=2, mean_ms=50.0, p50_ms=50.0, p95_ms=60.0),
                        mean_input_tokens=100.0,
                        mean_output_tokens=30.0,
                        mean_total_tokens=130.0,
                        mean_pages=1.0,
                        mean_contextualization_tokens=None,
                        total_estimated_cost=0.02,
                        mean_estimated_cost=0.01,
                    )
                ],
            )
        ],
    )


def test_write_json_report_round_trips_structure(tmp_path: Path) -> None:
    report = _sample_report()
    path = write_json_report(report, tmp_path / "aggregate.json")

    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["run_id"] == "run-123"
    assert payload["generated_at"] == "2026-01-01T00:00:00+00:00"
    assert len(payload["solutions"]) == 1
    assert payload["solutions"][0]["solution_id"] == "solution-a"
    assert payload["solutions"][0]["components"][0]["component"] == "extraction"
    assert payload["solutions"][0]["priced_cost_total"] == 0.1
    assert payload["solutions"][0]["priced_cost_mean"] == 0.05
    assert payload["solutions"][0]["lifecycle"] == "preview"
    assert payload["solutions"][0]["kind"] == "ocr_pipeline"
    assert payload["solutions"][0]["deployment"] == "receipt-mistral-ocr-4"
    assert payload["solutions"][0]["model"] == "mistral-ocr-4-0"
    assert len(payload["fields"]) == 1
    assert payload["fields"][0]["field"] == "merchant.name"
    assert payload["fields"][0]["dataset"] == "unit-test"
    assert payload["fields"][0]["language"] == "en"
    assert len(payload["warmups"]) == 1
    assert payload["warmups"][0]["solution_id"] == "solution-a"
    assert payload["warmups"][0]["warmup_count"] == 2
    assert payload["warmups"][0]["components"][0]["component"] == "extraction"


def test_write_json_report_has_no_ranking_keys(tmp_path: Path) -> None:
    report = _sample_report()
    path = write_json_report(report, tmp_path / "aggregate.json")
    text = path.read_text(encoding="utf-8").lower()
    for forbidden in ("rank", "winner", "recommendation", "composite_score"):
        assert forbidden not in text


def test_write_csv_reports_produces_five_files_with_expected_headers(tmp_path: Path) -> None:
    report = _sample_report()
    paths = write_csv_reports(report, tmp_path)

    assert set(paths) == {
        "solutions",
        "components",
        "fields",
        "warmups",
        "warmup_components",
    }
    for path in paths.values():
        assert path.is_file()

    with paths["solutions"].open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["solution_id"] == "solution-a"
    assert rows[0]["overall_quality_score"] == "0.82"
    assert rows[0]["valid_output_rate"] == "1.0"
    assert rows[0]["fully_correct_rate"] == "0.5"
    assert rows[0]["exact_match_rate"] == "0.75"
    assert rows[0]["schema_valid_rate"] == "1.0"
    assert rows[0]["document_exact_rate"] == "0.5"
    assert rows[0]["matched_count"] == "3"
    assert rows[0]["mismatched_count"] == "1"
    assert rows[0]["missing_count"] == "0"
    assert rows[0]["extra_count"] == "0"
    assert rows[0]["precision"] == "0.75"
    assert rows[0]["recall"] == "1.0"
    assert rows[0]["hallucinated_extra_rate"] == "0.0"
    assert rows[0]["document_exact_rate_ci_lower"] == "0.2"
    assert rows[0]["document_exact_rate_ci_upper"] == "0.8"
    assert rows[0]["f1_ci_lower"] == "0.6"
    assert rows[0]["f1_ci_upper"] == "1.0"
    assert rows[0]["mean_normalized_similarity_ci_lower"] == "0.85"
    assert rows[0]["mean_normalized_similarity_ci_upper"] == "0.95"
    assert rows[0]["mean_latency_ms_ci_lower"] == "90.0"
    assert rows[0]["mean_latency_ms_ci_upper"] == "110.0"
    assert rows[0]["overall_quality_score_ci_lower"] == "0.75"
    assert rows[0]["overall_quality_score_ci_upper"] == "0.89"
    assert rows[0]["fully_correct_rate_ci_lower"] == "0.2"
    assert rows[0]["fully_correct_rate_ci_upper"] == "0.8"
    assert rows[0]["priced_cost_total"] == "0.1"
    assert rows[0]["priced_cost_mean"] == "0.05"
    assert rows[0]["lifecycle"] == "preview"
    assert rows[0]["kind"] == "ocr_pipeline"
    assert rows[0]["deployment"] == "receipt-mistral-ocr-4"
    assert rows[0]["model"] == "mistral-ocr-4-0"

    with paths["components"].open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["component"] == "extraction"
    assert rows[0]["solution_id"] == "solution-a"
    assert rows[0]["priced_cost_total"] == ""
    assert rows[0]["priced_cost_mean"] == ""

    with paths["fields"].open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["field"] == "merchant.name"
    assert rows[0]["dataset"] == "unit-test"
    assert rows[0]["language"] == "en"
    assert rows[0]["mismatched_count"] == "1"
    assert rows[0]["matched_count"] == "1"
    assert rows[0]["extra_count"] == "0"
    assert rows[0]["precision"] == "0.5"
    assert rows[0]["recall"] == "1.0"

    with paths["warmups"].open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["solution_id"] == "solution-a"
    assert rows[0]["warmup_count"] == "2"
    assert rows[0]["latency_mean_ms"] == "50.0"

    with paths["warmup_components"].open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["solution_id"] == "solution-a"
    assert rows[0]["component"] == "extraction"
    assert rows[0]["mean_input_tokens"] == "100.0"


def test_write_csv_reports_handles_solution_with_no_components(tmp_path: Path) -> None:
    report = _sample_report()
    no_component_solution = SolutionAggregate(
        solution_id="solution-b",
        total_samples=0,
        succeeded=0,
        failed=0,
        invalid=0,
        error_rate=0.0,
        invalid_rate=0.0,
        coverage=0.0,
        exact_match_rate=0.0,
        mean_normalized_similarity=0.0,
        arithmetic_consistency_rate=None,
        schema_valid_rate=0.0,
        document_exact_rate=0.0,
        matched_count=0,
        mismatched_count=0,
        missing_count=0,
        extra_count=0,
        precision=0.0,
        recall=0.0,
        f1=0.0,
        hallucinated_extra_rate=0.0,
        mean_attempts=0.0,
        overall_latency=LatencyStats(count=0, mean_ms=None, p50_ms=None, p95_ms=None),
        latency_histogram=[],
        components=[],
    )
    report.solutions.append(no_component_solution)

    paths = write_csv_reports(report, tmp_path)
    with paths["solutions"].open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    no_data_row = next(row for row in rows if row["solution_id"] == "solution-b")
    # Zero underlying observations: bootstrap CI bounds are blank, not inferred.
    assert no_data_row["document_exact_rate_ci_lower"] == ""
    assert no_data_row["document_exact_rate_ci_upper"] == ""
    assert no_data_row["mean_latency_ms_ci_lower"] == ""
    with paths["components"].open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1  # only solution-a contributed a component row


def test_write_csv_reports_writes_empty_warmup_files_when_no_warmups_recorded(
    tmp_path: Path,
) -> None:
    report = _sample_report()
    report.warmups.clear()

    paths = write_csv_reports(report, tmp_path)

    with paths["warmups"].open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == []
    with paths["warmup_components"].open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == []
