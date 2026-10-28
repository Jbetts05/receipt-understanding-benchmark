"""Tests that the generated static HTML report strictly follows the Clawpilot theme."""

from __future__ import annotations

import json
import re
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
from receipt_bench.reporting.html_report import render_html_report, write_html_report

_REQUIRED_CP_VARIABLES = [
    "--cp-bg",
    "--cp-bg-elevated",
    "--cp-surface",
    "--cp-surface-soft",
    "--cp-border",
    "--cp-border-strong",
    "--cp-text",
    "--cp-text-muted",
    "--cp-text-soft",
    "--cp-accent",
    "--cp-accent-hover",
    "--cp-accent-soft",
    "--cp-accent-fg",
    "--cp-success",
    "--cp-danger",
    "--cp-warning",
    "--cp-link",
    "--cp-shadow",
    "--cp-overlay",
    "--cp-panel",
    "--cp-panel-strong",
    "--cp-sheen",
    "--cp-highlight",
]


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
        overall_quality_score=0.8,
        valid_output_rate=1.0,
        fully_correct_rate=0.5,
        overall_quality_score_ci=ConfidenceInterval(lower=0.6, upper=0.95),
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


def _table_headers(html: str, table_id: str) -> list[str]:
    match = re.search(
        rf'<table\b(?=[^>]*\bid="{re.escape(table_id)}")[^>]*>(.*?)</table>',
        html,
        re.DOTALL,
    )
    assert match is not None, f"missing table {table_id}"
    headers = re.findall(r"<th\b[^>]*>(.*?)</th>", match.group(1), re.DOTALL)
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", header)).strip() for header in headers]


def _highlight_html(html: str, metric: str) -> str:
    match = re.search(
        rf'<article\b(?=[^>]*data-metric="{re.escape(metric)}")[^>]*>.*?</article>',
        html,
        re.DOTALL,
    )
    assert match is not None, f"missing highlight for {metric}"
    return re.sub(r"\s+", " ", match.group(0))


def test_default_header_uses_data_driven_benchmark_counts_and_provenance() -> None:
    from dataclasses import replace

    report = _sample_report()
    solutions = [
        replace(report.solutions[0], solution_id=f"solution-{index:02}", total_samples=100)
        for index in range(15)
    ]
    html = render_html_report(replace(report, solutions=solutions))

    assert re.search(r"<strong>100</strong>\s*receipts", html)
    assert re.search(r"<strong>15</strong>\s*solutions", html)
    assert "Source run ID / provenance:" in html
    assert "<code>run-123</code>" in html
    assert 'id="public-data-caveat"' in html
    assert "not guaranteed performance on unseen private receipts" in html


def test_default_comparison_table_has_exactly_six_decision_columns() -> None:
    html = render_html_report(_sample_report())

    assert _table_headers(html, "comparison-table") == [
        "Solution",
        "Overall quality",
        "Valid outputs",
        "Fully correct receipts",
        "Median time",
        "Cost/receipt",
    ]
    assert "const comparisonRows = data.solutions.slice().sort" in html
    assert "return rightQuality - leftQuality" in html
    assert "compareSolutionIds(left.solution_id, right.solution_id)" in html
    assert "solutionCell(row)" in html
    assert "row.lifecycle" in html
    assert "row.kind" in html
    assert "row.model" in html


def test_default_view_defines_quality_and_fully_correct_for_decision_makers() -> None:
    html = re.sub(r"\s+", " ", render_html_report(_sample_report()).lower())

    assert "mean per-receipt field f1" in html
    assert "only labeled golden answers" in html
    assert "unparseable or failed outputs score zero" in html
    assert "each receipt has equal weight" in html
    assert "strict schema compliance is not a gate for answer quality" in html
    assert "fully correct</strong> is binary per receipt" in html
    assert "every labeled gold field must be correct" in html
    assert "denominator is all attempted receipts" in html


def test_highlights_report_all_ties_in_deterministic_order() -> None:
    from dataclasses import replace

    report = _sample_report()
    base = report.solutions[0]
    solution_b = replace(
        base,
        solution_id="solution-b",
        overall_quality_score=0.9,
        valid_output_rate=0.95,
        overall_latency=replace(base.overall_latency, p50_ms=200.0),
        priced_cost_mean=None,
    )
    solution_c = replace(
        base,
        solution_id="solution-c",
        overall_quality_score=0.8,
        valid_output_rate=0.8,
        overall_latency=replace(base.overall_latency, p50_ms=100.0),
        priced_cost_mean=0.02,
    )
    solution_a = replace(
        base,
        solution_id="solution-a",
        overall_quality_score=0.9,
        valid_output_rate=0.95,
        overall_latency=replace(base.overall_latency, p50_ms=100.0),
        priced_cost_mean=0.02,
    )
    html = render_html_report(
        replace(report, solutions=[solution_b, solution_c, solution_a])
    )

    expected_ties = {
        "overall_quality_score": ("solution-a", "solution-b"),
        "overall_latency.p50_ms": ("solution-a", "solution-c"),
        "priced_cost_mean": ("solution-a", "solution-c"),
        "valid_output_rate": ("solution-a", "solution-b"),
    }
    for metric, names in expected_ties.items():
        card = _highlight_html(html, metric)
        assert 'data-highlight-count="2"' in card
        assert "Tie on this benchmark:" in card
        assert card.index(names[0]) < card.index(names[1])

    cost_card = _highlight_html(html, "priced_cost_mean")
    assert "$0.020" in cost_card
    assert "solution-b" not in cost_card
    assert "unavailable prices are excluded" in cost_card.lower()


def test_unavailable_cost_is_neither_selected_nor_displayed_as_zero() -> None:
    from dataclasses import replace

    report = _sample_report()
    no_cost_solution = replace(report.solutions[0], priced_cost_mean=None)
    html = render_html_report(replace(report, solutions=[no_cost_solution]))
    card = _highlight_html(html, "priced_cost_mean")

    assert 'data-highlight-count="0"' in card
    assert "Unavailable" in card
    assert "No measured value on this benchmark." in card
    assert 'if (number === null) return "Unavailable";' in html


def test_large_highlight_tie_is_summarized_instead_of_listing_every_solution() -> None:
    from dataclasses import replace

    report = _sample_report()
    base = report.solutions[0]
    tied = [replace(base, solution_id=f"solution-{index}") for index in range(5)]

    html = render_html_report(replace(report, solutions=tied))
    card = _highlight_html(html, "valid_output_rate")

    assert 'data-highlight-count="5"' in card
    assert "5 solutions" in card
    assert "solution-0, solution-1" not in card


def test_advanced_analysis_is_closed_and_contains_all_technical_sections() -> None:
    html = render_html_report(_sample_report())
    opening = re.search(r'<details\b[^>]*id="advanced-analysis"[^>]*>', html)
    assert opening is not None
    assert not re.search(r"\bopen(?:\s|=|>)", opening.group(0))
    details_end = html.index("</details>", opening.end())
    advanced_html = html[opening.start() : details_end]

    for section_id in (
        "methodology-section",
        "solutions-section",
        "components-section",
        "distributions-section",
        "fields-section",
        "warmups-section",
    ):
        assert f'id="{section_id}"' in advanced_html
    assert len(_table_headers(advanced_html, "solutions-table")) >= 29
    assert 'id="component-filter"' in advanced_html
    assert "renderSolutions" in html
    assert "renderComponents" in html
    assert "renderFields" in html
    assert "renderWarmups" in html


def test_theme_detection_script_is_first_script_in_document() -> None:
    html = render_html_report(_sample_report())
    script_positions = [m.start() for m in re.finditer(r"<script", html)]
    assert script_positions, "expected at least one <script> tag"
    first_script_end = html.index("</script>", script_positions[0])
    first_script_body = html[script_positions[0] : first_script_end]
    assert "scoutTheme" in first_script_body
    assert "data-theme" in first_script_body
    assert "prefers-color-scheme: dark" in first_script_body
    # It must precede every other <script> tag in the document.
    assert script_positions[0] == min(script_positions)
    for later_position in script_positions[1:]:
        assert later_position > first_script_end


def test_all_required_cp_css_variables_present_in_light_and_dark_blocks() -> None:
    html = render_html_report(_sample_report())
    root_match = re.search(r":root\s*\{(.*?)\}", html, re.DOTALL)
    dark_match = re.search(r'html\[data-theme="dark"\]\s*\{(.*?)\}', html, re.DOTALL)
    assert root_match is not None
    assert dark_match is not None
    root_block = root_match.group(1)
    dark_block = dark_match.group(1)
    for variable in _REQUIRED_CP_VARIABLES:
        assert f"{variable}:" in root_block, f"missing {variable} in :root"
        assert f"{variable}:" in dark_block, f"missing {variable} in dark block"


def test_uses_only_cp_variables_for_component_colors() -> None:
    html = render_html_report(_sample_report())
    style_match = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
    assert style_match is not None
    style_body = style_match.group(1)
    # Strip the two variable-definition blocks; everything else must reference var(--cp-*)
    # rather than hardcoding new hex colors for components.
    without_root = re.sub(r":root\s*\{.*?\}", "", style_body, flags=re.DOTALL)
    without_dark = re.sub(r'html\[data-theme="dark"\]\s*\{.*?\}', "", without_root, flags=re.DOTALL)
    hex_colors = re.findall(r"#[0-9a-fA-F]{3,8}\b", without_dark)
    assert hex_colors == []


def test_typography_uses_required_font_stack() -> None:
    html = render_html_report(_sample_report())
    assert '"Segoe UI", Aptos, Calibri' in html


def test_no_external_network_assets() -> None:
    html = render_html_report(_sample_report())
    assert "<link" not in html
    assert "http://" not in html
    assert "https://" not in html
    assert "cdn." not in html.lower()


def test_report_contains_no_ranking_or_winner_language() -> None:
    html = render_html_report(_sample_report())
    lowered = re.sub(r"\s+", " ", html.lower())
    # The template explicitly disclaims ranking/winner behavior; that disclaimer is the
    # only permitted appearance of these words, so scrub it before asserting absence.
    assert "does not compute a composite score, rank solutions, or declare a winner" in lowered
    scrubbed = lowered.replace(
        "does not compute a composite score, rank solutions, or declare a winner", ""
    )
    assert "winner" not in scrubbed
    assert "recommendation" not in scrubbed
    assert "composite score" not in scrubbed


def test_report_includes_public_dataset_training_data_caveat() -> None:
    html = render_html_report(_sample_report())
    assert 'id="methodology-caveat"' in html
    lowered = re.sub(r"\s+", " ", html.lower())
    assert "sroie" in lowered
    assert "cord" in lowered
    assert "training data" in lowered
    assert "reproducible" in lowered
    assert "generaliz" in lowered


def test_report_includes_unpriced_failure_cost_caveat() -> None:
    html = render_html_report(_sample_report())
    assert 'id="cost-failure-caveat"' in html
    lowered = re.sub(r"\s+", " ", html.lower())
    assert "no usage payload" in lowered
    assert "cannot be reconstructed" in lowered
    assert "unpriced" in lowered
    assert "retry and attempt counts" in lowered


def test_report_marks_preview_solution_with_badge() -> None:
    """SolutionAggregate.lifecycle/kind/deployment/model must be threaded into the
    solutions table so a non-GA solution (e.g. lifecycle="preview") is clearly
    labeled, without affecting row order or introducing any ranking language.
    """
    html = render_html_report(_sample_report())
    assert 'id="lifecycle-note"' in html
    assert "badge-preview" in html
    assert "row.lifecycle" in html
    assert "row.model" in html
    assert '"lifecycle": "preview"' in html
    assert '"kind": "ocr_pipeline"' in html
    assert '"model": "mistral-ocr-4-0"' in html
    assert "<th>Lifecycle</th>" in html
    assert "<th>Model</th>" in html


def test_inline_data_payload_round_trips_report_contents() -> None:
    report = _sample_report()
    html = render_html_report(report)
    match = re.search(
        r'<script id="report-data" type="application/json">(.*?)</script>', html, re.DOTALL
    )
    assert match is not None
    payload = json.loads(match.group(1))
    assert payload["run_id"] == report.run_id
    assert payload["solutions"][0]["solution_id"] == "solution-a"
    assert payload["solutions"][0]["overall_quality_score"] == 0.8
    assert payload["solutions"][0]["valid_output_rate"] == 1.0
    assert payload["solutions"][0]["fully_correct_rate"] == 0.5
    assert payload["solutions"][0]["priced_cost_total"] == 0.1
    assert payload["solutions"][0]["priced_cost_mean"] == 0.05
    assert payload["fields"][0]["field"] == "merchant.name"
    assert payload["fields"][0]["language"] == "en"


def test_filterable_solution_and_field_controls_present() -> None:
    html = render_html_report(_sample_report())
    assert 'id="solution-filter"' in html
    assert 'id="solution-select"' in html
    assert 'id="dataset-select"' in html
    assert 'id="language-select"' in html
    assert 'id="field-filter"' in html


def test_html_report_surfaces_language_column_and_note() -> None:
    html = render_html_report(_sample_report())
    assert "<th>Language</th>" in html
    assert "row.language" in html
    lowered = re.sub(r"\s+", " ", html.lower())
    assert "source_language" in lowered
    assert "image quality" in lowered


def test_write_html_report_writes_file(tmp_path: Path) -> None:
    report = _sample_report()
    path = write_html_report(report, tmp_path / "report.html")
    assert path.is_file()
    assert path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_html_report_notes_absent_pricing_snapshot_by_default() -> None:
    html = render_html_report(_sample_report())
    assert "No pricing snapshot was loaded" in html


def test_html_report_surfaces_pricing_source_and_timestamp_when_present() -> None:
    from dataclasses import replace

    report = replace(
        _sample_report(),
        pricing_source="unit-test snapshot",
        pricing_captured_at=datetime(2026, 8, 1, tzinfo=UTC),
        pricing_currency="USD",
    )
    html = render_html_report(report)
    assert "unit-test snapshot" in html
    assert "2026-08-01T00:00:00+00:00" in html
    assert "USD" in html


def test_html_report_surfaces_new_precision_recall_and_document_metric_columns() -> None:
    html = render_html_report(_sample_report())
    for header in (
        "Schema valid",
        "Document exact",
        "Precision",
        "Recall",
        "F1",
        "Hallucinated extra",
    ):
        assert f"<th>{header}</th>" in html


def test_html_report_surfaces_priced_cost_total_and_mean_columns() -> None:
    html = render_html_report(_sample_report())
    assert "<th>Priced cost total</th>" in html
    assert "<th>Priced cost mean</th>" in html
    assert "row.priced_cost_total" in html
    assert "row.priced_cost_mean" in html


def test_html_report_notes_priced_cost_is_all_or_nothing_per_sample() -> None:
    html = render_html_report(_sample_report())
    lowered = re.sub(r"\s+", " ", html.lower())
    assert "priced_cost_total" in lowered
    assert "priced_cost_mean" in lowered
    assert "ocr" in lowered and "normaliz" in lowered


def test_html_report_notes_hallucination_measure_is_limited_to_repeated_entities() -> None:
    html = render_html_report(_sample_report())
    lowered = re.sub(r"\s+", " ", html.lower())
    assert "hallucinated_extra_rate" in lowered
    assert "no equivalent hallucination" in lowered
    assert "no negative-label" in lowered or "negative-label masks" in lowered


def test_html_report_surfaces_bootstrap_ci_columns_and_note() -> None:
    html = render_html_report(_sample_report())
    for header in (
        "Overall quality 95% CI",
        "Fully correct 95% CI",
        "Document exact 95% CI",
        "F1 95% CI",
        "Similarity 95% CI",
        "Mean latency 95% CI (ms)",
    ):
        assert f"<th>{header}</th>" in html
    lowered = re.sub(r"\s+", " ", html.lower())
    assert "deterministic percentile bootstrap" in lowered
    assert "fixed seed 20260828" in lowered
    assert "2,000 resamples" in lowered
    assert "not a pairwise significance test" in lowered


def test_html_report_ci_note_never_uses_ranking_or_winner_language() -> None:
    html = render_html_report(_sample_report())
    lowered = re.sub(r"\s+", " ", html.lower())
    ci_note_match = re.search(
        r"95% confidence intervals for document exact rate.*?collapses to that single value\.",
        lowered,
    )
    assert ci_note_match is not None
    ci_note = ci_note_match.group(0)
    assert "winner" not in ci_note
    assert "ranking" not in ci_note
    assert "rank " not in ci_note
    assert "recommendation" not in ci_note
    assert "composite score" not in ci_note


def test_inline_data_payload_includes_confidence_interval_bounds() -> None:
    report = _sample_report()
    html = render_html_report(report)
    match = re.search(
        r'<script id="report-data" type="application/json">(.*?)</script>', html, re.DOTALL
    )
    assert match is not None
    payload = json.loads(match.group(1))
    solution_row = payload["solutions"][0]
    assert solution_row["document_exact_rate_ci"] == {"lower": 0.2, "upper": 0.8}
    assert solution_row["f1_ci"] == {"lower": 0.6, "upper": 1.0}
    assert solution_row["mean_normalized_similarity_ci"] == {"lower": 0.85, "upper": 0.95}
    assert solution_row["mean_latency_ms_ci"] == {"lower": 90.0, "upper": 110.0}
    assert solution_row["overall_quality_score_ci"] == {"lower": 0.6, "upper": 0.95}
    assert solution_row["fully_correct_rate_ci"] == {"lower": 0.2, "upper": 0.8}


def test_html_report_renders_blank_dash_for_null_confidence_interval() -> None:
    from dataclasses import replace

    report = _sample_report()
    no_data_solution = replace(
        report.solutions[0],
        solution_id="solution-no-data",
        document_exact_rate_ci=ConfidenceInterval(lower=None, upper=None),
    )
    report = replace(report, solutions=[*report.solutions, no_data_solution])
    html = render_html_report(report)
    payload_match = re.search(
        r'<script id="report-data" type="application/json">(.*?)</script>', html, re.DOTALL
    )
    assert payload_match is not None
    payload = json.loads(payload_match.group(1))
    no_data_row = next(
        row for row in payload["solutions"] if row["solution_id"] == "solution-no-data"
    )
    assert no_data_row["document_exact_rate_ci"] == {"lower": None, "upper": None}


def test_html_report_includes_warmups_section_and_table() -> None:
    html = render_html_report(_sample_report())
    assert 'id="warmups-section"' in html
    assert 'id="warmups-table"' in html
    assert 'id="warmup-filter"' in html
    assert "renderWarmups" in html


def test_html_report_warmup_note_explains_exclusion_and_avoids_forbidden_words() -> None:
    html = render_html_report(_sample_report())
    lowered = re.sub(r"\s+", " ", html.lower())
    warmup_note_match = re.search(
        r"warmup calls are real, potentially paid.*?rather than silently discarded\.",
        lowered,
    )
    assert warmup_note_match is not None
    warmup_note = warmup_note_match.group(0)
    assert "never scored" in warmup_note
    assert "excluded from every quality metric" in warmup_note
    assert "measured end-to-end latency" in warmup_note
    assert "winner" not in warmup_note
    assert "ranking" not in warmup_note
    assert "rank " not in warmup_note
    assert "recommendation" not in warmup_note
    assert "composite score" not in warmup_note


def test_inline_data_payload_includes_warmup_aggregates() -> None:
    report = _sample_report()
    html = render_html_report(report)
    match = re.search(
        r'<script id="report-data" type="application/json">(.*?)</script>', html, re.DOTALL
    )
    assert match is not None
    payload = json.loads(match.group(1))
    assert len(payload["warmups"]) == 1
    warmup_row = payload["warmups"][0]
    assert warmup_row["solution_id"] == "solution-a"
    assert warmup_row["warmup_count"] == 2
    assert warmup_row["components"][0]["component"] == "extraction"


def test_html_report_renders_placeholder_when_no_warmups_recorded() -> None:
    from dataclasses import replace

    report = replace(_sample_report(), warmups=[])
    html = render_html_report(report)
    assert "renderWarmups" in html
    # The payload itself is empty; the placeholder row is rendered client-side by JS,
    # so we only assert the data the JS relies on is correctly empty.
    match = re.search(
        r'<script id="report-data" type="application/json">(.*?)</script>', html, re.DOTALL
    )
    assert match is not None
    payload = json.loads(match.group(1))
    assert payload["warmups"] == []
