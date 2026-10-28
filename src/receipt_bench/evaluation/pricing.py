"""Optional pricing-snapshot support for estimating component costs.

Rates come from an optional ``configs/pricing.yaml`` snapshot (see
``docs/pricing.md`` for the human-readable rate card and provenance). This
module never creates, requires, or infers that file's contents:

* If the file does not exist, :func:`load_pricing_snapshot` returns ``None``
  and every priced cost stays ``None`` throughout aggregation.
* If the file exists but has no rate entry for a component's resolved rate
  key, :func:`rate_for` returns ``None`` for that component -- it is never
  substituted with another component's or solution's rate, and never
  defaulted to zero.
* If a rate entry exists but does not price a usage dimension that a
  measurement actually reports (e.g. the measurement has ``pages`` but the
  rate entry has neither ``per_thousand_pages`` nor ``per_thousand_transactions``
  set), :func:`estimate_component_cost` returns ``None`` for that measurement
  rather than silently treating the missing rate as free. A missing rate is
  reported as ``None``, never inferred.

Rate entries are keyed by a single flat string: the deployment/adapter ID that
actually incurs the billable usage (not a nested ``solution``/``component``
mapping, and not an abstract component name), because the underlying Azure
Retail Prices meters are per *model/deployment*, not per solution or component
label:

* Most components (``direct_extraction``, ``native_extraction``, ``ocr``) are
  priced directly under their owning solution's ``id`` (see
  ``configs/solutions.yaml``), since that solution has exactly one billable
  model/service backing that component (e.g. ``gpt-5.6-sol``,
  ``mistral-large-3``, ``mistral-ocr-4.0``, ``document-intelligence-receipt``,
  ``azure-vision-read``, ``content-understanding-custom``).
* The shared "normalize" step used by every OCR/ingestion pipeline solution
  (``kind: ocr_pipeline`` in ``configs/solutions.yaml``) is priced under the
  normalizer's own deployment id (e.g. ``gpt-5.4-mini``) rather than the owning
  solution's id, since multiple pipeline solutions share the same normalizer
  deployment and should not duplicate its rate entry per solution.
* A component measurement preserved from a retried ``ProviderResponseError``
  attempt (named ``"retry_overhead:<attempt>:<name>"`` by
  ``runner._retry_overhead_component``, so it is never mixed into a sample's
  measured end-to-end latency) is priced exactly like its unprefixed
  ``<name>`` would be: it is the same underlying model/service call, just one
  that needed retrying, and its real cost is still owed.

:func:`resolve_rate_key` implements that mapping; :func:`rate_for` uses it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import Field, ValidationError

from receipt_bench.evaluation.errors import PricingConfigError
from receipt_bench.models import StrictModel, Usage

_NORMALIZER_COMPONENT = "normalize"
_MODEL_COMPONENT_PREFIX = "model:"

#: Public so ``runner.py`` (which builds these component names for retried
#: ``ProviderResponseError`` attempts) and ``aggregate.py`` (which excludes
#: them from measured end-to-end latency) share one literal instead of each
#: hardcoding the same string.
RETRY_OVERHEAD_COMPONENT_PREFIX = "retry_overhead:"


class ComponentRate(StrictModel):
    """Per-unit USD rates for one pricing-snapshot rate-card entry.

    ``input_per_million_tokens``, ``output_per_million_tokens``, and
    ``contextualization_per_million_tokens`` are USD per 1,000,000 tokens.
    ``per_thousand_pages`` and ``per_thousand_transactions`` are USD per 1,000
    pages/images or transactions, respectively (the two are mutually
    interchangeable units for a single-page-per-sample benchmark; a rate card
    sets whichever one matches its own meter's terminology). Any rate left
    unset means "no known rate for this dimension" -- it is not treated as
    zero.
    """

    input_per_million_tokens: float | None = None
    output_per_million_tokens: float | None = None
    per_thousand_pages: float | None = None
    per_thousand_transactions: float | None = None
    contextualization_per_million_tokens: float | None = None
    long_context_threshold_input_tokens: int | None = Field(default=None, ge=1)
    long_context_input_per_million_tokens: float | None = None
    long_context_output_per_million_tokens: float | None = None


class PricingSnapshot(StrictModel):
    """A committed pricing snapshot: its provenance plus a flat rate-key -> rate map."""

    currency: str
    captured_at: datetime
    region: str
    source: str
    rates: dict[str, ComponentRate] = Field(default_factory=dict)


def load_pricing_snapshot(path: Path) -> PricingSnapshot | None:
    """Load an optional pricing snapshot from ``path``.

    Returns ``None`` when ``path`` does not exist: pricing is entirely optional
    and its absence is never an error. Raises ``PricingConfigError`` for a
    present-but-malformed file (invalid YAML, or a structure that does not match
    ``PricingSnapshot``) -- a broken snapshot is never silently ignored.
    """
    if not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PricingConfigError(f"{path}: invalid YAML ({exc})") from exc
    try:
        return PricingSnapshot.model_validate(raw)
    except ValidationError as exc:
        raise PricingConfigError(f"{path}: invalid pricing snapshot ({exc})") from exc


def validate_pricing_coverage(
    snapshot: PricingSnapshot | None,
    required_rate_keys: Iterable[str],
) -> None:
    """Require every runtime rate key to resolve to at least one priced dimension."""

    if snapshot is None:
        raise PricingConfigError("The hosted runtime requires a pricing snapshot")
    missing: list[str] = []
    for key in sorted(set(required_rate_keys)):
        rate = snapshot.rates.get(key)
        if rate is None or all(
            value is None
            for name, value in rate.model_dump().items()
            if name != "long_context_threshold_input_tokens"
        ):
            missing.append(key)
    if missing:
        raise PricingConfigError(
            "Pricing snapshot has no usable rate for: " + ", ".join(missing)
        )


def resolve_rate_key(solution_id: str, component: str, normalizer_model: str | None) -> str:
    """Resolve the pricing-snapshot rate key for one component measurement.

    A component named ``"model:<model-id>"`` (the zero-latency per-model token
    splits that Content Understanding's native analyzers emit for the models
    they internally invoke, e.g. ``"model:gpt-5.2"``, ``"model:gpt-5.4-mini"``,
    ``"model:text-embedding-3-large"``; see
    ``providers/content_understanding.py``) resolves *exactly* to that
    ``<model-id>``, regardless of the owning solution or normalizer -- each
    contributing model is priced independently under its own rate-card entry.
    This also means the CU *service* measurement (``native_extraction``, which
    never carries a ``model:`` prefix and never duplicates the split-out token
    counts) is not double-counted: it still resolves to the owning solution's
    id and is priced only for the usage it actually reports (pages/
    contextualization tokens), never for the token usage already billed to the
    ``model:<id>`` entries.

    The shared normalizer step (``component == "normalize"``) is priced under
    its normalizer deployment's own id (``SolutionConfig.normalizer`` in
    ``configs/solutions.yaml``, e.g. ``gpt-5.4-mini``) when the owning solution
    names one; every other component is priced directly under its own
    ``solution_id``. This is a deterministic key-naming lookup, not a rate
    inference: it never invents a price, only decides which committed
    rate-card entry (if any) applies.

    A component named ``"retry_overhead:<attempt>:<name>"`` (a retried
    ``ProviderResponseError`` attempt's measurement, preserved rather than
    discarded so its real cost is not lost; see
    ``runner._retry_overhead_component``) resolves *exactly* like its
    unprefixed ``<name>`` would: the ``retry_overhead:<attempt>:`` segment is
    stripped first, then the remainder (which may itself start with
    ``"model:"``, e.g. a retried Content Understanding attempt) is resolved
    the normal way. A retried attempt is priced identically to the attempt
    that decided the pair's outcome -- it is the same underlying
    model/service call, just one that needed retrying.
    """
    resolved = component
    if resolved.startswith(RETRY_OVERHEAD_COMPONENT_PREFIX):
        remainder = resolved[len(RETRY_OVERHEAD_COMPONENT_PREFIX) :]
        _, _, original = remainder.partition(":")
        resolved = original or remainder
    if resolved.startswith(_MODEL_COMPONENT_PREFIX):
        return resolved[len(_MODEL_COMPONENT_PREFIX) :]
    if resolved == _NORMALIZER_COMPONENT and normalizer_model:
        return normalizer_model
    return solution_id


def rate_for(
    snapshot: PricingSnapshot | None,
    solution_id: str,
    component: str,
    *,
    normalizer_model: str | None = None,
) -> ComponentRate | None:
    """Look up the rate card for one component measurement.

    Returns ``None`` when there is no snapshot, or the snapshot has no rate
    entry for the resolved key (see :func:`resolve_rate_key`). Never falls back
    to a rate for a different solution or component.
    """
    if snapshot is None:
        return None
    key = resolve_rate_key(solution_id, component, normalizer_model)
    return snapshot.rates.get(key)


def estimate_component_cost(rate: ComponentRate | None, usage: Usage) -> float | None:
    """Estimate one component measurement's cost from its usage and a rate card.

    Returns ``None`` when ``rate`` is ``None`` (no rate available for this
    component), or when the measurement reports usage for a dimension (input,
    output, or contextualization tokens, or pages) that the rate card leaves
    unpriced. A usage dimension that is simply absent from the measurement
    (``None``) contributes nothing and does not require a rate.

    ``pages`` is priced by whichever of ``per_thousand_pages`` /
    ``per_thousand_transactions`` the rate card sets (a rate card is expected to
    set at most one of the two, matching whichever meter name its underlying
    Azure Retail Prices entry used); if neither is set, a present ``pages``
    value makes the whole estimate ``None`` rather than being silently ignored.
    """
    if rate is None:
        return None

    total = 0.0
    long_context = (
        rate.long_context_threshold_input_tokens is not None
        and usage.input_tokens is not None
        and usage.input_tokens >= rate.long_context_threshold_input_tokens
    )
    input_rate = (
        rate.long_context_input_per_million_tokens
        if long_context
        else rate.input_per_million_tokens
    )
    output_rate = (
        rate.long_context_output_per_million_tokens
        if long_context
        else rate.output_per_million_tokens
    )
    token_dimensions = (
        (usage.input_tokens, input_rate),
        (usage.output_tokens, output_rate),
        (usage.contextualization_tokens, rate.contextualization_per_million_tokens),
    )
    for amount, unit_rate in token_dimensions:
        if amount is None:
            continue
        if unit_rate is None:
            return None
        total += (amount / 1_000_000.0) * unit_rate

    if usage.pages is not None:
        page_rate = (
            rate.per_thousand_pages
            if rate.per_thousand_pages is not None
            else rate.per_thousand_transactions
        )
        if page_rate is None:
            return None
        total += (usage.pages / 1_000.0) * page_rate

    return total


__all__ = [
    "ComponentRate",
    "PricingSnapshot",
    "estimate_component_cost",
    "load_pricing_snapshot",
    "rate_for",
    "resolve_rate_key",
    "validate_pricing_coverage",
]
