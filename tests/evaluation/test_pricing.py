from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from receipt_bench.evaluation.errors import PricingConfigError
from receipt_bench.evaluation.pricing import (
    ComponentRate,
    PricingSnapshot,
    estimate_component_cost,
    load_pricing_snapshot,
    rate_for,
    resolve_rate_key,
    validate_pricing_coverage,
)
from receipt_bench.models import Usage


def test_load_pricing_snapshot_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_pricing_snapshot(tmp_path / "does-not-exist.yaml") is None


def test_load_pricing_snapshot_parses_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text(
        """
        currency: USD
        captured_at: "2026-08-28T17:50:00+01:00"
        region: swedencentral
        source: https://prices.azure.com/api/retail/prices
        rates:
          gpt-5.6-sol:
            input_per_million_tokens: 5.0
            output_per_million_tokens: 30.0
          mistral-ocr-4.0:
            per_thousand_pages: 4.0
          azure-vision-read:
            per_thousand_transactions: 1.0
          gpt-5.4-mini:
            input_per_million_tokens: 0.75
            output_per_million_tokens: 4.5
        """,
        encoding="utf-8",
    )

    snapshot = load_pricing_snapshot(path)

    assert snapshot is not None
    assert snapshot.currency == "USD"
    assert snapshot.region == "swedencentral"
    assert snapshot.source == "https://prices.azure.com/api/retail/prices"
    rate = snapshot.rates["gpt-5.6-sol"]
    assert rate.input_per_million_tokens == 5.0
    assert rate.output_per_million_tokens == 30.0
    assert rate.per_thousand_pages is None
    assert snapshot.rates["mistral-ocr-4.0"].per_thousand_pages == 4.0
    assert snapshot.rates["azure-vision-read"].per_thousand_transactions == 1.0


def test_load_pricing_snapshot_allows_null_rates_for_unmetered_solutions(tmp_path: Path) -> None:
    # Some solutions (e.g. Claude Opus 5) genuinely have no known rate: the field
    # is present but null, never a substituted/inferred value.
    path = tmp_path / "pricing.yaml"
    path.write_text(
        """
        currency: USD
        captured_at: "2026-08-28T17:50:00+01:00"
        region: swedencentral
        source: snapshot
        rates:
          claude-opus-5:
            input_per_million_tokens:
            output_per_million_tokens:
        """,
        encoding="utf-8",
    )

    snapshot = load_pricing_snapshot(path)
    assert snapshot is not None
    rate = snapshot.rates["claude-opus-5"]
    assert rate.input_per_million_tokens is None
    assert rate.output_per_million_tokens is None


def test_load_pricing_snapshot_raises_for_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text("rates: [unterminated", encoding="utf-8")

    with pytest.raises(PricingConfigError):
        load_pricing_snapshot(path)


def test_load_pricing_snapshot_raises_for_invalid_schema(tmp_path: Path) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text(
        """
        currency: USD
        captured_at: "2026-08-28T17:50:00+01:00"
        region: swedencentral
        source: snapshot
        rates:
          solution-a:
            input_per_million_tokens: "not-a-number"
        """,
        encoding="utf-8",
    )

    with pytest.raises(PricingConfigError):
        load_pricing_snapshot(path)


def test_load_pricing_snapshot_raises_for_missing_required_fields(tmp_path: Path) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text("rates: {}\n", encoding="utf-8")

    with pytest.raises(PricingConfigError):
        load_pricing_snapshot(path)


def _snapshot() -> PricingSnapshot:
    return PricingSnapshot(
        currency="USD",
        captured_at=datetime(2026, 8, 28, tzinfo=UTC),
        region="swedencentral",
        source="unit-test snapshot",
        rates={
            "gpt-5.6-sol": ComponentRate(
                input_per_million_tokens=5.0, output_per_million_tokens=30.0
            ),
            "mistral-ocr-4.0": ComponentRate(per_thousand_pages=4.0),
            "azure-vision-read": ComponentRate(per_thousand_transactions=1.0),
            "gpt-5.4-mini": ComponentRate(
                input_per_million_tokens=0.75, output_per_million_tokens=4.5
            ),
            "gpt-5.2": ComponentRate(input_per_million_tokens=1.75, output_per_million_tokens=14.0),
            "text-embedding-3-large": ComponentRate(input_per_million_tokens=0.13),
            "content-understanding-prebuilt-receipt": ComponentRate(
                per_thousand_pages=5.0, contextualization_per_million_tokens=1.0
            ),
        },
    )


def test_resolve_rate_key_uses_solution_id_for_non_normalizer_components() -> None:
    assert resolve_rate_key("gpt-5.6-sol", "direct_extraction", None) == "gpt-5.6-sol"
    assert resolve_rate_key("mistral-ocr-4.0", "ocr", "gpt-5.4-mini") == "mistral-ocr-4.0"


def test_resolve_rate_key_resolves_model_prefixed_component_to_exact_model_id() -> None:
    # Zero-latency per-model token splits from a native Content Understanding
    # analyzer resolve straight to their own model id, not to the owning
    # solution's id.
    assert (
        resolve_rate_key("content-understanding-prebuilt-receipt", "model:gpt-5.2", None)
        == "gpt-5.2"
    )
    assert (
        resolve_rate_key("content-understanding-prebuilt-receipt", "model:gpt-5.4-mini", None)
        == "gpt-5.4-mini"
    )
    assert (
        resolve_rate_key("content-understanding-custom", "model:text-embedding-3-large", None)
        == "text-embedding-3-large"
    )


def test_resolve_rate_key_model_prefix_takes_precedence_over_normalizer() -> None:
    # Even when a normalizer model is configured for the owning solution, a
    # "model:<id>" component still resolves to its own <id>, never the
    # normalizer's -- these are unrelated concepts (shared OCR-pipeline
    # normalization step vs. a native analyzer's internally-invoked models).
    key = resolve_rate_key(
        "content-understanding-prebuilt-receipt", "model:gpt-5.4-mini", "some-other-normalizer"
    )
    assert key == "gpt-5.4-mini"


def test_resolve_rate_key_service_component_still_maps_to_solution_id() -> None:
    # The CU service measurement (e.g. "native_extraction") never carries the
    # "model:" prefix, so it still resolves to the owning solution's id for its
    # page/contextualization usage -- it is priced independently of, and never
    # double-counts, the split-out model components.
    key = resolve_rate_key("content-understanding-prebuilt-receipt", "native_extraction", None)
    assert key == "content-understanding-prebuilt-receipt"


def test_resolve_rate_key_uses_normalizer_deployment_id_for_normalize_component() -> None:
    key = resolve_rate_key("mistral-ocr-4.0", "normalize", "gpt-5.4-mini")
    assert key == "gpt-5.4-mini"


def test_resolve_rate_key_falls_back_to_solution_id_when_no_normalizer_configured() -> None:
    assert resolve_rate_key("some-solution", "normalize", None) == "some-solution"


def test_resolve_rate_key_strips_retry_overhead_prefix_before_resolving() -> None:
    # A retried (non-deciding) attempt's component is renamed
    # "retry_overhead:<attempt>:<name>" by the runner so cost/per-component
    # aggregation still sees it, but it must resolve to exactly the same rate
    # key its unprefixed original would.
    assert resolve_rate_key("mistral-ocr-4.0", "retry_overhead:1:ocr", None) == "mistral-ocr-4.0"
    assert (
        resolve_rate_key("mistral-ocr-4.0", "retry_overhead:1:normalize", "gpt-5.4-mini")
        == "gpt-5.4-mini"
    )
    assert (
        resolve_rate_key("gpt-5.6-sol", "retry_overhead:2:direct_extraction", None) == "gpt-5.6-sol"
    )


def test_resolve_rate_key_strips_retry_overhead_prefix_around_nested_model_prefix() -> None:
    # A retried Content Understanding attempt's per-model split component keeps
    # its own further ":"-delimited structure ("model:<id>") after the
    # "retry_overhead:<attempt>:" prefix is stripped -- only the first colon
    # separating the attempt number is consumed.
    assert (
        resolve_rate_key(
            "content-understanding-prebuilt-receipt", "retry_overhead:1:model:gpt-5.2", None
        )
        == "gpt-5.2"
    )
    assert (
        resolve_rate_key(
            "content-understanding-custom",
            "retry_overhead:3:model:text-embedding-3-large",
            None,
        )
        == "text-embedding-3-large"
    )


def test_rate_for_returns_none_when_snapshot_is_none() -> None:
    assert rate_for(None, "gpt-5.6-sol", "direct_extraction") is None


def test_rate_for_returns_none_for_unknown_solution() -> None:
    assert rate_for(_snapshot(), "unknown-solution", "direct_extraction") is None


def test_rate_for_returns_exact_match_for_direct_component() -> None:
    rate = rate_for(_snapshot(), "gpt-5.6-sol", "direct_extraction")
    assert rate is not None
    assert rate.input_per_million_tokens == 5.0


def test_rate_for_resolves_shared_normalizer_rate() -> None:
    rate = rate_for(_snapshot(), "mistral-ocr-4.0", "normalize", normalizer_model="gpt-5.4-mini")
    assert rate is not None
    assert rate.input_per_million_tokens == 0.75


def test_rate_for_never_falls_back_when_normalizer_rate_missing() -> None:
    # No normalizer model configured for this pipeline solution, and the solution
    # itself has no rate-card entry either: no rate should be inferred/reused.
    rate = rate_for(
        _snapshot(), "unconfigured-pipeline-solution", "normalize", normalizer_model=None
    )
    assert rate is None


def test_rate_for_resolves_model_prefixed_component_independently_of_solution() -> None:
    rate = rate_for(_snapshot(), "content-understanding-prebuilt-receipt", "model:gpt-5.2")
    assert rate is not None
    assert rate.input_per_million_tokens == 1.75
    assert rate.output_per_million_tokens == 14.0


def test_rate_for_resolves_each_model_split_component_to_its_own_rate() -> None:
    gpt_rate = rate_for(_snapshot(), "content-understanding-custom", "model:gpt-5.4-mini")
    embedding_rate = rate_for(
        _snapshot(), "content-understanding-custom", "model:text-embedding-3-large"
    )
    assert gpt_rate is not None
    assert gpt_rate.input_per_million_tokens == 0.75
    assert embedding_rate is not None
    assert embedding_rate.input_per_million_tokens == 0.13
    assert embedding_rate.output_per_million_tokens is None


def test_rate_for_resolves_cu_service_component_to_solution_id_not_a_model() -> None:
    rate = rate_for(_snapshot(), "content-understanding-prebuilt-receipt", "native_extraction")
    assert rate is not None
    assert rate.per_thousand_pages == 5.0
    assert rate.contextualization_per_million_tokens == 1.0
    # The service rate carries no token rate at all -- proof it is a distinct
    # rate-card entry from any "model:<id>" split, not an accidental merge.
    assert rate.input_per_million_tokens is None
    assert rate.output_per_million_tokens is None


def test_estimate_component_cost_returns_none_without_rate() -> None:
    usage = Usage(input_tokens=1_000_000)
    assert estimate_component_cost(None, usage) is None


def test_estimate_component_cost_computes_token_costs() -> None:
    rate = ComponentRate(input_per_million_tokens=5.0, output_per_million_tokens=15.0)
    usage = Usage(input_tokens=1_000_000, output_tokens=500_000)
    cost = estimate_component_cost(rate, usage)
    assert cost == pytest.approx(5.0 + 7.5)


def test_estimate_component_cost_computes_per_thousand_pages_cost() -> None:
    rate = ComponentRate(per_thousand_pages=4.0)
    usage = Usage(pages=250)
    assert estimate_component_cost(rate, usage) == pytest.approx(1.0)


def test_estimate_component_cost_computes_per_thousand_transactions_cost() -> None:
    rate = ComponentRate(per_thousand_transactions=1.0)
    usage = Usage(pages=1)
    assert estimate_component_cost(rate, usage) == pytest.approx(0.001)


def test_estimate_component_cost_includes_contextualization_tokens() -> None:
    rate = ComponentRate(contextualization_per_million_tokens=1.25)
    usage = Usage(contextualization_tokens=2_000_000)
    assert estimate_component_cost(rate, usage) == pytest.approx(2.5)


def test_estimate_component_cost_skips_absent_usage_dimensions() -> None:
    rate = ComponentRate(input_per_million_tokens=5.0)
    usage = Usage(input_tokens=1_000_000)
    # output/pages/contextualization are None on usage -- nothing to price, no rate needed.
    assert estimate_component_cost(rate, usage) == pytest.approx(5.0)


def test_estimate_component_cost_returns_none_when_rate_missing_for_present_dimension() -> None:
    # Usage reports pages, but the rate card has neither per_thousand_pages nor
    # per_thousand_transactions: never infer it as free.
    rate = ComponentRate(input_per_million_tokens=5.0)
    usage = Usage(input_tokens=1_000_000, pages=2)
    assert estimate_component_cost(rate, usage) is None


def test_estimate_component_cost_returns_none_for_unmetered_solution() -> None:
    # Claude Opus 5 / Grok 4.6-style rate card entry: present but null.
    rate = ComponentRate(input_per_million_tokens=None, output_per_million_tokens=None)
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert estimate_component_cost(rate, usage) is None


def test_estimate_component_cost_zero_usage_with_full_rate_is_zero() -> None:
    rate = ComponentRate(input_per_million_tokens=5.0, output_per_million_tokens=15.0)
    usage = Usage(input_tokens=0, output_tokens=0)
    assert estimate_component_cost(rate, usage) == pytest.approx(0.0)


def test_estimate_component_cost_applies_long_context_tier_to_whole_request() -> None:
    rate = ComponentRate(
        input_per_million_tokens=2.0,
        output_per_million_tokens=6.0,
        long_context_threshold_input_tokens=200_000,
        long_context_input_per_million_tokens=4.0,
        long_context_output_per_million_tokens=12.0,
    )

    assert estimate_component_cost(
        rate,
        Usage(input_tokens=199_999, output_tokens=100_000),
    ) == pytest.approx(0.399998 + 0.6)
    assert estimate_component_cost(
        rate,
        Usage(input_tokens=200_000, output_tokens=100_000),
    ) == pytest.approx(0.8 + 1.2)


def test_validate_pricing_coverage_rejects_missing_or_empty_rate_entries() -> None:
    snapshot = PricingSnapshot(
        currency="USD",
        captured_at=datetime(2026, 8, 31, tzinfo=UTC),
        region="swedencentral",
        source="unit-test snapshot",
        rates={
            "priced": ComponentRate(input_per_million_tokens=2.0),
            "empty": ComponentRate(),
        },
    )

    validate_pricing_coverage(snapshot, {"priced"})
    with pytest.raises(PricingConfigError, match="empty, missing"):
        validate_pricing_coverage(snapshot, {"priced", "empty", "missing"})
