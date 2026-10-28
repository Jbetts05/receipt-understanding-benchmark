from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

import pytest

from receipt_bench.evaluation.aggregate import (
    ConfidenceInterval,
    MatchCounts,
    SolutionMetadata,
    _bootstrap_ci,
    aggregate_by_dataset_field,
    aggregate_by_solution,
    aggregate_warmups,
    build_report,
    latency_histogram,
)
from receipt_bench.evaluation.pricing import ComponentRate, PricingSnapshot
from receipt_bench.evaluation.warmups import WarmupRecord
from receipt_bench.models import (
    ComponentMeasurement,
    EvaluatedOutput,
    FieldScore,
    ReceiptSample,
    SolutionOutput,
    Usage,
)

_Status = Literal["succeeded", "failed", "invalid"]


def _now() -> datetime:
    return datetime.now(UTC)


def _output(
    sample_id: str,
    solution_id: str,
    status: _Status = "succeeded",
    components: list[ComponentMeasurement] | None = None,
    attempts: int = 1,
) -> SolutionOutput:
    now = _now()
    return SolutionOutput(
        run_id="run-1",
        sample_id=sample_id,
        solution_id=solution_id,
        started_at=now,
        completed_at=now,
        status=status,
        receipt=None,
        raw_artifact=f"artifacts/run-1/raw/{solution_id}/{sample_id}.json",
        attempts=attempts,
        components=components or [],
    )


def _evaluated(
    sample_id: str,
    solution_id: str,
    fields: list[FieldScore],
    status: _Status = "succeeded",
    components: list[ComponentMeasurement] | None = None,
    arithmetic_consistent: bool | None = True,
) -> EvaluatedOutput:
    output = _output(sample_id, solution_id, status=status, components=components)
    return EvaluatedOutput(
        output=output,
        schema_valid=status == "succeeded",
        fields=fields,
        arithmetic_consistent=arithmetic_consistent,
    )


def _field_score(
    field: str, labeled: bool, exact: bool | None, similarity: float | None
) -> FieldScore:
    return FieldScore(
        field=field,
        labeled=labeled,
        exact_match=exact,
        normalized_similarity=similarity,
        expected="expected",
        actual="expected" if exact else "different",
    )


def test_latency_histogram_buckets_equal_width() -> None:
    buckets = latency_histogram([0.0, 10.0, 20.0, 100.0], bucket_count=4)
    assert len(buckets) == 4
    assert sum(bucket.count for bucket in buckets) == 4


def test_latency_histogram_empty_is_empty() -> None:
    assert latency_histogram([]) == []


def test_latency_histogram_single_value_bucket() -> None:
    buckets = latency_histogram([42.0])
    assert len(buckets) == 1
    assert buckets[0].count == 1


def test_aggregate_by_solution_reports_counts_and_rates() -> None:
    evaluated = [
        _evaluated(
            "s1", "solution-a", [_field_score("merchant.name", True, True, 1.0)], status="succeeded"
        ),
        _evaluated("s2", "solution-a", [], status="failed", arithmetic_consistent=None),
        _evaluated("s3", "solution-a", [], status="invalid", arithmetic_consistent=None),
    ]

    aggregates = aggregate_by_solution(evaluated)

    assert len(aggregates) == 1
    solution = aggregates[0]
    assert solution.solution_id == "solution-a"
    assert solution.total_samples == 3
    assert solution.succeeded == 1
    assert solution.failed == 1
    assert solution.invalid == 1
    assert solution.error_rate == pytest.approx(1 / 3)
    assert solution.invalid_rate == pytest.approx(1 / 3)
    assert solution.exact_match_rate == 1.0
    assert solution.overall_quality_score == pytest.approx(1 / 3)
    assert solution.valid_output_rate == pytest.approx(1 / 3)
    assert solution.fully_correct_rate == pytest.approx(1 / 3)


def test_overall_quality_weights_receipts_equally_and_scores_uncaptured_answers_zero() -> None:
    many_correct_fields = [
        _field_score(f"field.{index}", True, True, 1.0) for index in range(10)
    ]
    evaluated = [
        _evaluated("many-labels", "solution-a", many_correct_fields),
        _evaluated(
            "one-label",
            "solution-a",
            [_field_score("amounts.total", True, False, 0.0)],
        ),
        _evaluated(
            "failed",
            "solution-a",
            [],
            status="failed",
            arithmetic_consistent=None,
        ),
    ]

    solution = aggregate_by_solution(evaluated)[0]

    assert solution.overall_quality_score == pytest.approx(1 / 3)
    assert solution.valid_output_rate == pytest.approx(2 / 3)
    assert solution.fully_correct_rate == pytest.approx(1 / 3)
    assert solution.overall_quality_score_ci.lower is not None
    assert solution.overall_quality_score_ci.upper is not None
    assert solution.fully_correct_rate_ci.lower is not None
    assert solution.fully_correct_rate_ci.upper is not None


def test_schema_noncompliance_does_not_erase_scoreable_answer_quality() -> None:
    item = _evaluated(
        "s1",
        "solution-a",
        [_field_score("amounts.total", True, True, 1.0)],
    )
    item = EvaluatedOutput(
        output=item.output,
        schema_valid=False,
        fields=item.fields,
        arithmetic_consistent=item.arithmetic_consistent,
    )

    solution = aggregate_by_solution([item])[0]

    assert solution.schema_valid_rate == 0.0
    assert solution.overall_quality_score == 1.0
    assert solution.valid_output_rate == 1.0
    assert solution.fully_correct_rate == 1.0


def test_aggregate_by_solution_only_counts_labeled_fields_for_quality() -> None:
    evaluated = [
        _evaluated(
            "s1",
            "solution-a",
            [
                _field_score("merchant.name", True, True, 1.0),
                _field_score("merchant.phone", False, False, 0.2),
            ],
        )
    ]

    aggregates = aggregate_by_solution(evaluated)
    solution = aggregates[0]
    assert solution.exact_match_rate == 1.0
    assert solution.coverage == 1.0


def test_match_counts_classifies_extra_rows_separately_from_mismatched() -> None:
    scores = [
        _field_score("merchant.name", True, False, 0.2),
        FieldScore(
            field="line_items.__extra__",
            labeled=True,
            exact_match=False,
            normalized_similarity=0.0,
            expected=None,
            actual={"description": "Muffin"},
        ),
    ]

    counts = MatchCounts.from_scores(scores)

    assert counts.mismatched == 1
    assert counts.extra == 1


def test_match_counts_ignores_unlabeled_scores() -> None:
    scores = [
        _field_score("merchant.phone", False, False, 0.1),
        FieldScore(
            field="line_items.__extra__",
            labeled=False,
            exact_match=False,
            normalized_similarity=0.0,
            expected=None,
            actual={"description": "Muffin"},
        ),
    ]

    counts = MatchCounts.from_scores(scores)

    assert counts == MatchCounts(matched=0, mismatched=0, missing=0, extra=0)


def test_match_counts_precision_recall_f1_zero_safe_when_empty() -> None:
    counts = MatchCounts.from_scores([])
    assert counts.precision == 0.0
    assert counts.recall == 0.0
    assert counts.f1 == 0.0


def test_match_counts_precision_recall_f1_correctness() -> None:
    counts = MatchCounts(matched=3, mismatched=0, missing=1, extra=2)
    assert counts.precision == pytest.approx(3 / 5)
    assert counts.recall == pytest.approx(3 / 4)
    assert counts.f1 == pytest.approx(2 * (3 / 5) * (3 / 4) / ((3 / 5) + (3 / 4)))


def test_aggregate_by_solution_reports_precision_recall_f1_schema_and_document_exact() -> None:
    evaluated = [
        _evaluated(
            "s1",
            "solution-a",
            [
                _field_score("merchant.name", True, True, 1.0),
                FieldScore(
                    field="line_items.__extra__",
                    labeled=True,
                    exact_match=False,
                    normalized_similarity=0.0,
                    expected=None,
                    actual={"description": "Muffin"},
                ),
            ],
        ),
        _evaluated("s2", "solution-a", [_field_score("merchant.name", True, False, 0.2)]),
        _evaluated("s3", "solution-a", [_field_score("merchant.name", True, True, 1.0)]),
        _evaluated("s4", "solution-a", [], status="failed", arithmetic_consistent=None),
        _evaluated("s5", "solution-a", [], status="invalid", arithmetic_consistent=None),
    ]

    solution = aggregate_by_solution(evaluated)[0]

    assert solution.matched_count == 2
    assert solution.mismatched_count == 1
    assert solution.missing_count == 0
    assert solution.extra_count == 1
    assert solution.precision == pytest.approx(2 / 4)
    assert solution.recall == pytest.approx(2 / 3)
    assert solution.f1 == pytest.approx(2 * (2 / 4) * (2 / 3) / ((2 / 4) + (2 / 3)))
    # 3 of 5 samples succeeded (schema-valid); only s3 is fully exact with no extras.
    assert solution.schema_valid_rate == pytest.approx(3 / 5)
    assert solution.document_exact_rate == pytest.approx(1 / 3)
    # The single extra row is a repeated-entity (line-item) false positive.
    assert solution.hallucinated_extra_rate == pytest.approx(1.0)


def test_aggregate_by_solution_document_exact_rate_is_zero_with_no_successes() -> None:
    evaluated = [_evaluated("s1", "solution-a", [], status="failed", arithmetic_consistent=None)]
    solution = aggregate_by_solution(evaluated)[0]
    assert solution.document_exact_rate == 0.0
    assert solution.schema_valid_rate == 0.0


def test_aggregate_by_solution_hallucinated_extra_rate_ignores_scalar_mismatches() -> None:
    """Scalar field mismatches must never contribute to hallucinated_extra_rate.

    There are no negative-label masks for scalar fields, so this metric is
    deliberately restricted to repeated entities (line items / payment methods).
    """
    evaluated = [
        _evaluated(
            "s1",
            "solution-a",
            [_field_score("merchant.name", True, False, 0.2)],
        )
    ]
    solution = aggregate_by_solution(evaluated)[0]
    assert solution.hallucinated_extra_rate == 0.0


def test_aggregate_exposes_quality_without_universal_ranking_fields() -> None:
    evaluated = [_evaluated("s1", "solution-a", [_field_score("merchant.name", True, True, 1.0)])]
    solution = aggregate_by_solution(evaluated)[0]
    field_names = {f.name for f in solution.__dataclass_fields__.values()}
    assert solution.overall_quality_score == 1.0
    for forbidden in ("rank", "winner", "recommendation", "composite"):
        assert not any(forbidden in name for name in field_names)


def test_aggregate_by_solution_threads_solution_metadata_and_marks_preview() -> None:
    evaluated = [
        _evaluated("s1", "mistral-ocr-4.0", [_field_score("merchant.name", True, True, 1.0)])
    ]

    solutions = aggregate_by_solution(
        evaluated,
        solution_metadata={
            "mistral-ocr-4.0": SolutionMetadata(
                kind="ocr_pipeline",
                lifecycle="preview",
                deployment="receipt-mistral-ocr-4",
                model="mistral-ocr-4-0",
            )
        },
    )

    solution = solutions[0]
    assert solution.lifecycle == "preview"
    assert solution.kind == "ocr_pipeline"
    assert solution.deployment == "receipt-mistral-ocr-4"
    assert solution.model == "mistral-ocr-4-0"


def test_aggregate_by_solution_defaults_metadata_when_solution_not_in_mapping() -> None:
    evaluated = [_evaluated("s1", "gpt-5.6-sol", [_field_score("merchant.name", True, True, 1.0)])]

    # A solution absent from the mapping (or when no mapping is supplied at all)
    # gets the same defaults SolutionConfig itself would: GA lifecycle, and
    # everything else unknown rather than guessed.
    solution = aggregate_by_solution(
        evaluated, solution_metadata={"some-other-solution": SolutionMetadata(kind="direct")}
    )[0]
    assert solution.lifecycle == "ga"
    assert solution.kind is None
    assert solution.deployment is None
    assert solution.model is None

    solution_without_mapping = aggregate_by_solution(evaluated)[0]
    assert solution_without_mapping.lifecycle == "ga"
    assert solution_without_mapping.kind is None


def test_aggregate_by_solution_component_usage_and_cost() -> None:
    components = [
        ComponentMeasurement(
            component="extraction",
            latency_ms=100.0,
            usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
            estimated_cost=0.02,
        )
    ]
    evaluated = [_evaluated("s1", "solution-a", [], components=components)]
    solution = aggregate_by_solution(evaluated)[0]
    assert len(solution.components) == 1
    component = solution.components[0]
    assert component.component == "extraction"
    assert component.mean_input_tokens == 10.0
    assert component.total_estimated_cost == pytest.approx(0.02)
    assert component.latency.mean_ms == 100.0


def test_aggregate_by_solution_priced_cost_is_none_without_pricing_snapshot() -> None:
    components = [
        ComponentMeasurement(
            component="extraction",
            latency_ms=100.0,
            usage=Usage(input_tokens=1_000_000),
        )
    ]
    evaluated = [_evaluated("s1", "solution-a", [], components=components)]
    solution = aggregate_by_solution(evaluated)[0]
    component = solution.components[0]
    assert component.priced_cost_total is None
    assert component.priced_cost_mean is None


def test_aggregate_by_solution_applies_pricing_snapshot_rates() -> None:
    components = [
        ComponentMeasurement(
            component="direct_extraction",
            latency_ms=100.0,
            usage=Usage(input_tokens=1_000_000, output_tokens=500_000),
        )
    ]
    evaluated = [_evaluated("s1", "solution-a", [], components=components)]
    pricing = PricingSnapshot(
        currency="USD",
        captured_at=_now(),
        region="swedencentral",
        source="unit-test snapshot",
        rates={
            "solution-a": ComponentRate(
                input_per_million_tokens=5.0, output_per_million_tokens=15.0
            )
        },
    )

    solution = aggregate_by_solution(evaluated, pricing)[0]
    component = solution.components[0]
    assert component.priced_cost_total == pytest.approx(5.0 + 7.5)
    assert component.priced_cost_mean == pytest.approx(5.0 + 7.5)


def test_aggregate_by_solution_pricing_never_infers_missing_component_rate() -> None:
    components = [
        ComponentMeasurement(
            component="ocr",
            latency_ms=100.0,
            usage=Usage(input_tokens=1_000_000),
        )
    ]
    evaluated = [_evaluated("s1", "solution-b", [], components=components)]
    # Pricing snapshot exists, but not for solution-b's rate key.
    pricing = PricingSnapshot(
        currency="USD",
        captured_at=_now(),
        region="swedencentral",
        source="unit-test snapshot",
        rates={"solution-a": ComponentRate(input_per_million_tokens=5.0)},
    )

    solution = aggregate_by_solution(evaluated, pricing)[0]
    component = solution.components[0]
    assert component.priced_cost_total is None
    assert component.priced_cost_mean is None


def test_aggregate_by_solution_resolves_shared_normalizer_rate_key() -> None:
    components = [
        ComponentMeasurement(
            component="normalize",
            latency_ms=50.0,
            usage=Usage(input_tokens=1_000_000, output_tokens=200_000),
        )
    ]
    evaluated = [_evaluated("s1", "mistral-ocr-4.0", [], components=components)]
    pricing = PricingSnapshot(
        currency="USD",
        captured_at=_now(),
        region="swedencentral",
        source="unit-test snapshot",
        rates={
            "gpt-5.4-mini": ComponentRate(
                input_per_million_tokens=0.75, output_per_million_tokens=4.5
            )
        },
    )

    solution = aggregate_by_solution(
        evaluated, pricing, normalizer_models={"mistral-ocr-4.0": "gpt-5.4-mini"}
    )[0]
    component = solution.components[0]
    assert component.priced_cost_total == pytest.approx(0.75 + 0.9)


def test_aggregate_by_solution_priced_cost_sums_ocr_and_normalization_components() -> None:
    """An OCR pipeline's per-solution priced cost total must include both its
    OCR component and its shared normalization component, not just one."""
    evaluated = [
        _evaluated(
            "s1",
            "mistral-ocr-4.0",
            [],
            components=[
                ComponentMeasurement(
                    component="ocr",
                    latency_ms=80.0,
                    usage=Usage(input_tokens=1_000_000),
                ),
                ComponentMeasurement(
                    component="normalize",
                    latency_ms=50.0,
                    usage=Usage(input_tokens=1_000_000, output_tokens=200_000),
                ),
            ],
        )
    ]
    pricing = PricingSnapshot(
        currency="USD",
        captured_at=_now(),
        region="swedencentral",
        source="unit-test snapshot",
        rates={
            "mistral-ocr-4.0": ComponentRate(input_per_million_tokens=1.0),
            "gpt-5.4-mini": ComponentRate(
                input_per_million_tokens=0.75, output_per_million_tokens=4.5
            ),
        },
    )

    solution = aggregate_by_solution(
        evaluated, pricing, normalizer_models={"mistral-ocr-4.0": "gpt-5.4-mini"}
    )[0]

    # ocr: 1.0 (1M input tokens @ $1.0/M); normalize: 0.75 + 0.9 (per the rate above).
    assert solution.priced_cost_total == pytest.approx(1.0 + 0.75 + 0.9)
    assert solution.priced_cost_mean == pytest.approx(1.0 + 0.75 + 0.9)


def test_aggregate_by_solution_priced_cost_is_none_when_any_sample_component_unpriced() -> None:
    """A sample's cost is not partially summed: if the OCR component prices but
    the normalization component has no rate, the whole sample (and, with no
    other fully-priced sample, the whole solution) is unpriced."""
    evaluated = [
        _evaluated(
            "s1",
            "mistral-ocr-4.0",
            [],
            components=[
                ComponentMeasurement(
                    component="ocr", latency_ms=80.0, usage=Usage(input_tokens=1_000_000)
                ),
                ComponentMeasurement(
                    component="normalize", latency_ms=50.0, usage=Usage(input_tokens=1_000_000)
                ),
            ],
        )
    ]
    pricing = PricingSnapshot(
        currency="USD",
        captured_at=_now(),
        region="swedencentral",
        source="unit-test snapshot",
        # No rate at all for the normalizer model, so "normalize" never prices.
        rates={"mistral-ocr-4.0": ComponentRate(input_per_million_tokens=1.0)},
    )

    solution = aggregate_by_solution(
        evaluated, pricing, normalizer_models={"mistral-ocr-4.0": "gpt-5.4-mini"}
    )[0]

    assert solution.priced_cost_total is None
    assert solution.priced_cost_mean is None
    # The per-component breakdown is unaffected: "ocr" still prices on its own.
    ocr_component = next(c for c in solution.components if c.component == "ocr")
    assert ocr_component.priced_cost_total == pytest.approx(1.0)


def test_aggregate_by_solution_excludes_retry_overhead_components_from_latency_but_not_cost() -> (
    None
):
    """A component preserved from a retried (non-deciding) ``ProviderResponseError``
    attempt is renamed ``retry_overhead:<attempt>:<name>`` by the runner. It must
    still be aggregated and priced as its own distinct component row -- real,
    already-incurred spend is never dropped -- but its latency must not inflate
    the sample's measured end-to-end quality latency, since it did not decide
    the pair's outcome."""
    evaluated = [
        _evaluated(
            "s1",
            "mistral-ocr-4.0",
            [],
            components=[
                ComponentMeasurement(
                    component="ocr", latency_ms=80.0, usage=Usage(input_tokens=1_000_000)
                ),
                ComponentMeasurement(
                    component="normalize",
                    latency_ms=50.0,
                    usage=Usage(input_tokens=1_000_000, output_tokens=200_000),
                ),
                ComponentMeasurement(
                    component="retry_overhead:1:ocr",
                    latency_ms=999.0,
                    usage=Usage(input_tokens=1_000_000),
                ),
            ],
        )
    ]
    pricing = PricingSnapshot(
        currency="USD",
        captured_at=_now(),
        region="swedencentral",
        source="unit-test snapshot",
        rates={
            "mistral-ocr-4.0": ComponentRate(input_per_million_tokens=1.0),
            "gpt-5.4-mini": ComponentRate(
                input_per_million_tokens=0.75, output_per_million_tokens=4.5
            ),
        },
    )

    solution = aggregate_by_solution(
        evaluated, pricing, normalizer_models={"mistral-ocr-4.0": "gpt-5.4-mini"}
    )[0]

    # The huge retry-overhead latency (999ms) must not appear in measured
    # end-to-end latency: only the deciding attempt's "ocr" (80) + "normalize"
    # (50) contribute.
    assert solution.overall_latency.mean_ms == pytest.approx(130.0)

    # But it still gets its own distinct, transparently-visible component row,
    # priced against the same rate its unprefixed original would resolve to.
    retry_component = next(c for c in solution.components if c.component == "retry_overhead:1:ocr")
    assert retry_component.priced_cost_total == pytest.approx(1.0)

    # And real spend from the retried attempt is folded into the solution's
    # total priced cost alongside the deciding attempt's own components.
    assert solution.priced_cost_total == pytest.approx(1.0 + 0.75 + 0.9 + 1.0)


def test_aggregate_by_solution_prices_model_split_and_service_components_separately() -> None:
    """A native Content Understanding analyzer emits zero-latency ``model:<id>``
    measurements for each contributing model plus one service-level measurement
    (e.g. ``native_extraction``) carrying only pages/contextualization usage.
    Each ``model:<id>`` component must resolve and price against its own
    ``<id>`` rate-card entry independently of the service component, and the
    service component must still resolve against the owning solution id for
    its page/contextualization usage -- with no double counting of token
    usage on the service component."""
    evaluated = [
        _evaluated(
            "s1",
            "content-understanding-prebuilt-receipt",
            [],
            components=[
                ComponentMeasurement(
                    component="native_extraction",
                    latency_ms=250.0,
                    usage=Usage(pages=1, contextualization_tokens=2_000_000),
                ),
                ComponentMeasurement(
                    component="model:gpt-5.2",
                    latency_ms=0.0,
                    usage=Usage(input_tokens=1_000_000, output_tokens=500_000),
                ),
                ComponentMeasurement(
                    component="model:text-embedding-3-large",
                    latency_ms=0.0,
                    usage=Usage(input_tokens=2_000_000),
                ),
            ],
        )
    ]
    pricing = PricingSnapshot(
        currency="USD",
        captured_at=_now(),
        region="swedencentral",
        source="unit-test snapshot",
        rates={
            "content-understanding-prebuilt-receipt": ComponentRate(
                per_thousand_pages=5.0, contextualization_per_million_tokens=1.0
            ),
            "gpt-5.2": ComponentRate(input_per_million_tokens=1.75, output_per_million_tokens=14.0),
            "text-embedding-3-large": ComponentRate(input_per_million_tokens=0.13),
        },
    )

    solution = aggregate_by_solution(evaluated, pricing, normalizer_models={})[0]

    service_component = next(c for c in solution.components if c.component == "native_extraction")
    gpt_component = next(c for c in solution.components if c.component == "model:gpt-5.2")
    embedding_component = next(
        c for c in solution.components if c.component == "model:text-embedding-3-large"
    )

    # Service component: 1 page @ $5/1000 + 2M contextualization tokens @ $1.0/M = 0.005 + 2.0.
    assert service_component.priced_cost_total == pytest.approx(0.005 + 2.0)
    # gpt-5.2 split: 1M input @ $1.75/M + 0.5M output @ $14.0/M = 1.75 + 7.0.
    assert gpt_component.priced_cost_total == pytest.approx(1.75 + 7.0)
    # text-embedding-3-large split: 2M input @ $0.13/M = 0.26.
    assert embedding_component.priced_cost_total == pytest.approx(0.26)

    # Solution-level total sums all three components with no double counting of
    # the model-split token usage onto the service component.
    assert solution.priced_cost_total == pytest.approx(0.005 + 2.0 + 1.75 + 7.0 + 0.26)
    assert solution.priced_cost_mean == pytest.approx(0.005 + 2.0 + 1.75 + 7.0 + 0.26)


def test_aggregate_by_solution_is_unpriced_when_model_split_component_is_unpriced() -> None:
    """If the CU service component prices but one of its model-split components
    has no rate-card entry (e.g. a new embedding model not yet captured in the
    pricing snapshot), the whole sample -- and thus the solution -- stays
    unpriced rather than reporting a partial total."""
    evaluated = [
        _evaluated(
            "s1",
            "content-understanding-prebuilt-receipt",
            [],
            components=[
                ComponentMeasurement(
                    component="native_extraction",
                    latency_ms=250.0,
                    usage=Usage(pages=1),
                ),
                ComponentMeasurement(
                    component="model:gpt-5.2",
                    latency_ms=0.0,
                    usage=Usage(input_tokens=1_000_000),
                ),
                ComponentMeasurement(
                    component="model:text-embedding-3-large",
                    latency_ms=0.0,
                    usage=Usage(input_tokens=2_000_000),
                ),
            ],
        )
    ]
    pricing = PricingSnapshot(
        currency="USD",
        captured_at=_now(),
        region="swedencentral",
        source="unit-test snapshot",
        rates={
            "content-understanding-prebuilt-receipt": ComponentRate(per_thousand_pages=5.0),
            "gpt-5.2": ComponentRate(input_per_million_tokens=1.75),
            # No rate entry at all for "text-embedding-3-large".
        },
    )

    solution = aggregate_by_solution(evaluated, pricing, normalizer_models={})[0]

    assert solution.priced_cost_total is None
    assert solution.priced_cost_mean is None
    # Per-component breakdown is unaffected: the priced components still show
    # their own cost independently of the solution-level all-or-nothing rule.
    gpt_component = next(c for c in solution.components if c.component == "model:gpt-5.2")
    assert gpt_component.priced_cost_total == pytest.approx(1.75)


def test_aggregate_by_solution_cost_is_unavailable_if_any_sample_is_unpriced() -> None:
    """A solution never presents its priced subset as the complete run cost."""
    evaluated = [
        _evaluated(
            "s1",
            "solution-a",
            [],
            components=[
                ComponentMeasurement(
                    component="extraction", latency_ms=10.0, usage=Usage(input_tokens=1_000_000)
                )
            ],
        ),
        _evaluated(
            "s2",
            "solution-a",
            [],
            components=[
                ComponentMeasurement(
                    component="extraction",
                    latency_ms=10.0,
                    # pages is priced by neither per_thousand_pages nor
                    # per_thousand_transactions below, so this sample's
                    # estimate is None even though input_tokens alone would
                    # have priced fine.
                    usage=Usage(input_tokens=1_000_000, pages=3),
                )
            ],
        ),
    ]
    pricing = PricingSnapshot(
        currency="USD",
        captured_at=_now(),
        region="swedencentral",
        source="unit-test snapshot",
        # No page rate set, so s2's "pages" usage makes its estimate None.
        rates={"solution-a": ComponentRate(input_per_million_tokens=2.0)},
    )

    solution = aggregate_by_solution(evaluated, pricing)[0]

    assert solution.priced_cost_total is None
    assert solution.priced_cost_mean is None


def test_aggregate_by_solution_priced_cost_total_is_none_without_pricing_snapshot() -> None:
    evaluated = [
        _evaluated(
            "s1",
            "solution-a",
            [],
            components=[
                ComponentMeasurement(
                    component="extraction", latency_ms=10.0, usage=Usage(input_tokens=1_000_000)
                )
            ],
        )
    ]

    solution = aggregate_by_solution(evaluated)[0]

    assert solution.priced_cost_total is None
    assert solution.priced_cost_mean is None


def test_aggregate_by_solution_cost_is_unavailable_for_unmeasured_failure() -> None:
    """A failed sample without telemetry prevents a complete cost claim."""
    pricing = PricingSnapshot(
        currency="USD",
        captured_at=_now(),
        region="swedencentral",
        source="unit-test snapshot",
        rates={"solution-a": ComponentRate(input_per_million_tokens=2.0)},
    )
    evaluated = [
        _evaluated(
            "s1",
            "solution-a",
            [],
            components=[
                ComponentMeasurement(
                    component="extraction", latency_ms=10.0, usage=Usage(input_tokens=1_000_000)
                )
            ],
        ),
        _evaluated("s2", "solution-a", [], status="failed", components=[]),
    ]

    solution = aggregate_by_solution(evaluated, pricing)[0]

    assert solution.priced_cost_total is None
    assert solution.priced_cost_mean is None


def _warmup_record(
    solution_id: str,
    warmup_index: int,
    components: list[ComponentMeasurement] | None = None,
) -> WarmupRecord:
    now = _now()
    return WarmupRecord(
        run_id="run-1",
        solution_id=solution_id,
        warmup_index=warmup_index,
        started_at=now,
        completed_at=now,
        raw_artifact=f"artifacts/run-1/warmups/{solution_id}/{warmup_index}.json",
        components=components or [],
    )


def test_aggregate_warmups_returns_empty_list_for_no_records() -> None:
    assert aggregate_warmups([]) == []


def test_aggregate_warmups_groups_by_solution_and_counts() -> None:
    records = [
        _warmup_record("solution-a", 0),
        _warmup_record("solution-a", 1),
        _warmup_record("solution-b", 0),
    ]

    warmups = aggregate_warmups(records)

    assert [w.solution_id for w in warmups] == ["solution-a", "solution-b"]
    assert warmups[0].warmup_count == 2
    assert warmups[1].warmup_count == 1


def test_aggregate_warmups_computes_latency_and_components_separately_per_solution() -> None:
    components_a = [
        ComponentMeasurement(
            component="extraction",
            latency_ms=100.0,
            usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
            estimated_cost=0.02,
        )
    ]
    records = [
        _warmup_record("solution-a", 0, components=components_a),
        _warmup_record("solution-a", 1, components=components_a),
    ]

    warmup = aggregate_warmups(records)[0]

    assert warmup.latency.mean_ms == 100.0
    assert len(warmup.components) == 1
    assert warmup.components[0].component == "extraction"
    assert warmup.components[0].mean_input_tokens == 10.0
    assert warmup.components[0].total_estimated_cost == pytest.approx(0.04)


def test_aggregate_warmups_applies_pricing_snapshot_rates() -> None:
    components = [
        ComponentMeasurement(
            component="direct_extraction",
            latency_ms=50.0,
            usage=Usage(input_tokens=1_000_000, output_tokens=500_000),
        )
    ]
    records = [_warmup_record("solution-a", 0, components=components)]
    pricing = PricingSnapshot(
        currency="USD",
        captured_at=_now(),
        region="swedencentral",
        source="unit-test snapshot",
        rates={
            "solution-a": ComponentRate(
                input_per_million_tokens=5.0, output_per_million_tokens=15.0
            )
        },
    )

    warmup = aggregate_warmups(records, pricing)[0]

    assert warmup.components[0].priced_cost_total == pytest.approx(5.0 + 7.5)


def test_aggregate_warmups_never_infers_missing_rate() -> None:
    components = [
        ComponentMeasurement(component="ocr", latency_ms=50.0, usage=Usage(input_tokens=1_000_000))
    ]
    records = [_warmup_record("solution-b", 0, components=components)]
    pricing = PricingSnapshot(
        currency="USD",
        captured_at=_now(),
        region="swedencentral",
        source="unit-test snapshot",
        rates={"solution-a": ComponentRate(input_per_million_tokens=5.0)},
    )

    warmup = aggregate_warmups(records, pricing)[0]

    assert warmup.components[0].priced_cost_total is None


def test_build_report_includes_warmups_when_supplied(
    make_sample: Callable[..., ReceiptSample],
) -> None:
    sample = make_sample(sample_id="s1")
    samples_by_id: dict[str, ReceiptSample] = {sample.sample_id: sample}
    evaluated = [_evaluated("s1", "solution-a", [_field_score("merchant.name", True, True, 1.0)])]
    warmups = [_warmup_record("solution-a", 0), _warmup_record("solution-a", 1)]

    report = build_report("run-123", evaluated, samples_by_id, warmups=warmups)

    assert len(report.warmups) == 1
    assert report.warmups[0].solution_id == "solution-a"
    assert report.warmups[0].warmup_count == 2


def test_build_report_warmups_defaults_to_empty_when_omitted(
    make_sample: Callable[..., ReceiptSample],
) -> None:
    sample = make_sample(sample_id="s1")
    samples_by_id: dict[str, ReceiptSample] = {sample.sample_id: sample}
    evaluated = [_evaluated("s1", "solution-a", [_field_score("merchant.name", True, True, 1.0)])]

    report = build_report("run-123", evaluated, samples_by_id)

    assert report.warmups == []


def test_aggregate_by_dataset_field_groups_by_solution_dataset_field(
    make_sample: Callable[..., ReceiptSample],
) -> None:
    sample = make_sample(sample_id="s1", dataset="unit-test")
    samples_by_id = {sample.sample_id: sample}
    evaluated = [
        _evaluated("s1", "solution-a", [_field_score("merchant.name", True, True, 1.0)]),
    ]

    fields = aggregate_by_dataset_field(evaluated, samples_by_id)

    assert len(fields) == 1
    row = fields[0]
    assert row.solution_id == "solution-a"
    assert row.dataset == "unit-test"
    assert row.language == "en"
    assert row.field == "merchant.name"
    assert row.exact_match_rate == 1.0
    assert row.missing_count == 0
    assert row.mismatched_count == 0


def test_aggregate_by_dataset_field_splits_rows_by_source_language(
    make_sample: Callable[..., ReceiptSample],
) -> None:
    """Two samples with the same solution/dataset/field but different
    ``source_language`` must produce separate field aggregate rows, so
    multilingual robustness is visible rather than blended together."""
    english_sample = make_sample(sample_id="s1")
    spanish_sample = make_sample(sample_id="s2").model_copy(update={"source_language": "es"})
    samples_by_id = {
        english_sample.sample_id: english_sample,
        spanish_sample.sample_id: spanish_sample,
    }
    evaluated = [
        _evaluated("s1", "solution-a", [_field_score("merchant.name", True, True, 1.0)]),
        _evaluated("s2", "solution-a", [_field_score("merchant.name", True, False, 0.2)]),
    ]

    fields = aggregate_by_dataset_field(evaluated, samples_by_id)
    by_language = {row.language: row for row in fields}

    assert set(by_language) == {"en", "es"}
    assert by_language["en"].exact_match_rate == 1.0
    assert by_language["es"].exact_match_rate == 0.0
    # Every row still reports the same solution/dataset/field; only the
    # language differs, so nothing was silently merged across languages.
    for row in fields:
        assert row.solution_id == "solution-a"
        assert row.dataset == "unit-test"
        assert row.field == "merchant.name"


def test_aggregate_by_dataset_field_groups_missing_source_language_as_unknown(
    make_sample: Callable[..., ReceiptSample],
) -> None:
    sample = make_sample(sample_id="s1").model_copy(update={"source_language": None})
    samples_by_id = {sample.sample_id: sample}
    evaluated = [
        _evaluated("s1", "solution-a", [_field_score("merchant.name", True, True, 1.0)]),
    ]

    fields = aggregate_by_dataset_field(evaluated, samples_by_id)

    assert len(fields) == 1
    assert fields[0].language == "unknown"


def test_aggregate_by_dataset_field_counts_missing_and_mismatched(
    make_sample: Callable[..., ReceiptSample],
) -> None:
    sample = make_sample(sample_id="s1")
    samples_by_id = {sample.sample_id: sample}
    evaluated = [
        _evaluated(
            "s1",
            "solution-a",
            [
                FieldScore(
                    field="merchant.name",
                    labeled=True,
                    exact_match=False,
                    normalized_similarity=0.4,
                    expected="Contoso",
                    actual="Fabrikam",
                ),
                FieldScore(
                    field="transaction.date",
                    labeled=True,
                    exact_match=None,
                    normalized_similarity=None,
                    expected="2026-08-28",
                    actual=None,
                ),
            ],
        ),
    ]

    fields = aggregate_by_dataset_field(evaluated, samples_by_id)
    by_field = {row.field: row for row in fields}
    assert by_field["merchant.name"].mismatched_count == 1
    assert by_field["transaction.date"].missing_count == 1


def test_aggregate_by_dataset_field_reports_precision_recall_f1(
    make_sample: Callable[..., ReceiptSample],
) -> None:
    sample_a = make_sample(sample_id="s1")
    sample_b = make_sample(sample_id="s2")
    samples_by_id = {sample_a.sample_id: sample_a, sample_b.sample_id: sample_b}
    evaluated = [
        _evaluated("s1", "solution-a", [_field_score("merchant.name", True, True, 1.0)]),
        _evaluated("s2", "solution-a", [_field_score("merchant.name", True, False, 0.2)]),
    ]

    fields = aggregate_by_dataset_field(evaluated, samples_by_id)
    row = next(row for row in fields if row.field == "merchant.name")

    assert row.matched_count == 1
    assert row.mismatched_count == 1
    assert row.extra_count == 0
    assert row.precision == pytest.approx(1 / 2)
    assert row.recall == pytest.approx(1 / 2)
    assert row.f1 == pytest.approx(1 / 2)


def test_aggregate_by_dataset_field_extra_row_gets_its_own_aggregate(
    make_sample: Callable[..., ReceiptSample],
) -> None:
    sample = make_sample(sample_id="s1")
    samples_by_id = {sample.sample_id: sample}
    evaluated = [
        _evaluated(
            "s1",
            "solution-a",
            [
                FieldScore(
                    field="line_items.__extra__",
                    labeled=True,
                    exact_match=False,
                    normalized_similarity=0.0,
                    expected=None,
                    actual={"description": "Muffin"},
                )
            ],
        ),
    ]

    fields = aggregate_by_dataset_field(evaluated, samples_by_id)
    row = next(row for row in fields if row.field == "line_items.__extra__")

    assert row.extra_count == 1
    assert row.mismatched_count == 0
    assert row.matched_count == 0
    # No matches or misses ever appear for the synthetic extra-row field, so its
    # precision is always zero and its recall is trivially zero-safe (no denominator).
    assert row.precision == 0.0
    assert row.recall == 0.0
    assert row.f1 == 0.0


def test_aggregate_by_dataset_field_raises_for_unknown_sample() -> None:
    evaluated = [_evaluated("missing-sample", "solution-a", [])]
    try:
        aggregate_by_dataset_field(evaluated, {})
    except KeyError:
        pass
    else:
        raise AssertionError("Expected KeyError for unknown sample_id")


def test_build_report_contains_run_id_and_sections(
    make_sample: Callable[..., ReceiptSample],
) -> None:
    sample = make_sample(sample_id="s1")
    samples_by_id: dict[str, ReceiptSample] = {sample.sample_id: sample}
    evaluated = [_evaluated("s1", "solution-a", [_field_score("merchant.name", True, True, 1.0)])]

    report = build_report("run-123", evaluated, samples_by_id)

    assert report.run_id == "run-123"
    assert len(report.solutions) == 1
    assert len(report.fields) == 1
    assert report.pricing_source is None
    assert report.pricing_captured_at is None
    assert report.pricing_currency is None


def test_build_report_reports_pricing_source_and_timestamp_when_supplied(
    make_sample: Callable[..., ReceiptSample],
) -> None:
    sample = make_sample(sample_id="s1")
    samples_by_id: dict[str, ReceiptSample] = {sample.sample_id: sample}
    evaluated = [_evaluated("s1", "solution-a", [_field_score("merchant.name", True, True, 1.0)])]
    captured_at = _now()
    pricing = PricingSnapshot(
        currency="USD",
        captured_at=captured_at,
        region="swedencentral",
        source="unit-test snapshot",
        rates={},
    )

    report = build_report("run-123", evaluated, samples_by_id, pricing=pricing)

    assert report.pricing_source == "unit-test snapshot"
    assert report.pricing_captured_at == captured_at
    assert report.pricing_currency == "USD"


def test_build_report_threads_solution_metadata_into_solutions(
    make_sample: Callable[..., ReceiptSample],
) -> None:
    sample = make_sample(sample_id="s1")
    samples_by_id: dict[str, ReceiptSample] = {sample.sample_id: sample}
    evaluated = [
        _evaluated("s1", "grok-4.6", [_field_score("merchant.name", True, True, 1.0)]),
        _evaluated("s1", "gpt-5.6-sol", [_field_score("merchant.name", True, True, 1.0)]),
    ]

    report = build_report(
        "run-123",
        evaluated,
        samples_by_id,
        solution_metadata={
            "grok-4.6": SolutionMetadata(
                kind="direct", lifecycle="preview", deployment="receipt-grok-46", model="grok-4.6"
            ),
            "gpt-5.6-sol": SolutionMetadata(kind="direct", model="gpt-5.6-sol"),
        },
    )

    by_id = {solution.solution_id: solution for solution in report.solutions}
    assert by_id["grok-4.6"].lifecycle == "preview"
    assert by_id["grok-4.6"].model == "grok-4.6"
    assert by_id["gpt-5.6-sol"].lifecycle == "ga"


def test_bootstrap_ci_returns_null_bounds_for_empty_population() -> None:
    ci = _bootstrap_ci([], statistic=lambda sample: sum(sample) / len(sample))
    assert ci == ConfidenceInterval(lower=None, upper=None)


def test_bootstrap_ci_degenerates_to_point_estimate_for_single_observation() -> None:
    ci = _bootstrap_ci([0.42], statistic=lambda sample: sum(sample) / len(sample))
    assert ci.lower == pytest.approx(0.42)
    assert ci.upper == pytest.approx(0.42)


def test_bootstrap_ci_is_deterministic_across_repeated_calls() -> None:
    population = [1.0, 2.0, 3.0, 4.0, 5.0, 10.0, 0.5]
    first = _bootstrap_ci(population, statistic=lambda sample: sum(sample) / len(sample))
    second = _bootstrap_ci(population, statistic=lambda sample: sum(sample) / len(sample))
    assert first == second


def test_bootstrap_ci_bounds_are_sane_around_point_estimate() -> None:
    population = [1.0, 2.0, 3.0, 4.0, 5.0, 10.0, 0.5]
    point_estimate = sum(population) / len(population)
    ci = _bootstrap_ci(population, statistic=lambda sample: sum(sample) / len(sample))
    assert ci.lower is not None
    assert ci.upper is not None
    assert ci.lower <= point_estimate <= ci.upper
    assert ci.lower < ci.upper


def test_bootstrap_ci_constant_population_collapses_bounds_to_that_constant() -> None:
    ci = _bootstrap_ci([7.0, 7.0, 7.0, 7.0], statistic=lambda sample: sum(sample) / len(sample))
    assert ci.lower == pytest.approx(7.0)
    assert ci.upper == pytest.approx(7.0)


def test_aggregate_by_solution_populates_bootstrap_confidence_intervals() -> None:
    evaluated = [
        _evaluated(
            "s1",
            "solution-a",
            [_field_score("merchant.name", True, True, 1.0)],
            components=[
                ComponentMeasurement(
                    component="model",
                    latency_ms=100.0,
                    usage=Usage(),
                )
            ],
        ),
        _evaluated(
            "s2",
            "solution-a",
            [_field_score("merchant.name", True, False, 0.5)],
            components=[
                ComponentMeasurement(
                    component="model",
                    latency_ms=150.0,
                    usage=Usage(),
                )
            ],
        ),
        _evaluated(
            "s3",
            "solution-a",
            [_field_score("merchant.name", True, True, 1.0)],
            components=[
                ComponentMeasurement(
                    component="model",
                    latency_ms=200.0,
                    usage=Usage(),
                )
            ],
        ),
    ]

    solution = aggregate_by_solution(evaluated)[0]

    for ci in (
        solution.document_exact_rate_ci,
        solution.f1_ci,
        solution.mean_normalized_similarity_ci,
        solution.mean_latency_ms_ci,
    ):
        assert ci.lower is not None
        assert ci.upper is not None
        assert ci.lower <= ci.upper

    assert solution.mean_latency_ms_ci.lower is not None
    assert solution.mean_latency_ms_ci.upper is not None
    assert solution.mean_latency_ms_ci.lower <= 150.0 <= solution.mean_latency_ms_ci.upper


def test_aggregate_by_solution_confidence_intervals_are_null_with_no_data() -> None:
    evaluated = [_evaluated("s1", "solution-a", [], status="failed", arithmetic_consistent=None)]

    solution = aggregate_by_solution(evaluated)[0]

    assert solution.document_exact_rate_ci == ConfidenceInterval(lower=None, upper=None)
    assert solution.f1_ci == ConfidenceInterval(lower=None, upper=None)
    assert solution.mean_normalized_similarity_ci == ConfidenceInterval(lower=None, upper=None)
    assert solution.mean_latency_ms_ci == ConfidenceInterval(lower=None, upper=None)


def test_aggregate_by_solution_confidence_intervals_degenerate_with_single_sample() -> None:
    evaluated = [
        _evaluated(
            "s1",
            "solution-a",
            [_field_score("merchant.name", True, True, 1.0)],
            components=[
                ComponentMeasurement(
                    component="model",
                    latency_ms=120.0,
                    usage=Usage(),
                )
            ],
        )
    ]

    solution = aggregate_by_solution(evaluated)[0]

    assert solution.document_exact_rate_ci == ConfidenceInterval(lower=1.0, upper=1.0)
    assert solution.f1_ci == ConfidenceInterval(lower=1.0, upper=1.0)
    assert solution.mean_normalized_similarity_ci == ConfidenceInterval(lower=1.0, upper=1.0)
    assert solution.mean_latency_ms_ci == ConfidenceInterval(lower=120.0, upper=120.0)


def test_aggregate_by_solution_bootstrap_resamples_documents_not_flattened_fields() -> None:
    """Regression test: f1_ci/mean_normalized_similarity_ci must resample whole
    documents, not each document's field scores independently.

    With two multi-field documents whose fields are perfectly correlated
    within each document (one document's fields are all matched, the other's
    are all mismatched), document-cluster resampling of two documents drawn
    with replacement can only ever produce a matched-fraction of 0.0, 0.5, or
    1.0. Field-level resampling (the prior, buggy behaviour) would instead be
    free to mix individual fields from both documents into fractions like 0.25
    or 0.75, which become impossible once resampling is document-clustered --
    those intermediate fractions never appearing in the observed bounds is
    itself evidence that resampling now clusters by document.
    """
    all_matched_doc = _evaluated(
        "s1",
        "solution-a",
        [
            _field_score("merchant.name", True, True, 1.0),
            _field_score("total", True, True, 1.0),
        ],
    )
    all_mismatched_doc = _evaluated(
        "s2",
        "solution-a",
        [
            _field_score("merchant.name", True, False, 0.0),
            _field_score("total", True, False, 0.0),
        ],
    )
    evaluated = [all_matched_doc, all_mismatched_doc]

    solution = aggregate_by_solution(evaluated)[0]

    allowed_fractions = {0.0, 0.5, 1.0}
    assert solution.f1_ci.lower in allowed_fractions
    assert solution.f1_ci.upper in allowed_fractions
    assert solution.mean_normalized_similarity_ci.lower in allowed_fractions
    assert solution.mean_normalized_similarity_ci.upper in allowed_fractions

    # A wide interval (not collapsed to one value) confirms both documents'
    # outcomes genuinely appear across the 2,000 resamples.
    assert solution.f1_ci.lower is not None
    assert solution.f1_ci.upper is not None
    assert solution.f1_ci.lower < solution.f1_ci.upper
    similarity_ci = solution.mean_normalized_similarity_ci
    assert similarity_ci.lower is not None
    assert similarity_ci.upper is not None
    assert similarity_ci.lower < similarity_ci.upper

    # Cross-check against independently bootstrapping the two succeeded
    # documents themselves (not their flattened field scores) with the same
    # deterministic helper: an exact match proves aggregate_by_solution truly
    # uses documents, not fields, as its resampling unit.
    expected_f1_ci = _bootstrap_ci(
        [all_matched_doc, all_mismatched_doc],
        lambda sample: (
            MatchCounts.from_scores(
                [score for item in sample for score in item.fields if score.labeled]
            ).f1
        ),
    )
    assert solution.f1_ci == expected_f1_ci
