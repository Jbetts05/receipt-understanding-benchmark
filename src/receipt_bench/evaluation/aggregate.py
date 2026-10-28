from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import mean
from typing import Literal

from receipt_bench.evaluation.pointers import is_extra_field, is_repeated_entity_field
from receipt_bench.evaluation.pricing import (
    RETRY_OVERHEAD_COMPONENT_PREFIX,
    PricingSnapshot,
    estimate_component_cost,
    rate_for,
)
from receipt_bench.evaluation.warmups import WarmupRecord
from receipt_bench.models import ComponentMeasurement, EvaluatedOutput, FieldScore, ReceiptSample

# Fixed so every run of the same evaluated data produces byte-identical intervals.
_BOOTSTRAP_SEED = 20260828
_BOOTSTRAP_RESAMPLES = 2000
_BOOTSTRAP_CONFIDENCE = 0.95


def _percentile(values: Sequence[float], pct: float) -> float | None:
    """Linear-interpolation percentile (0-100) over a sequence of values."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _safe_mean(values: Sequence[float]) -> float:
    return mean(values) if values else 0.0


def _safe_ratio(numerator: int, denominator: int) -> float:
    """Zero-safe ratio: ``0.0`` rather than a ``ZeroDivisionError`` when empty."""
    return numerator / denominator if denominator else 0.0


@dataclass(frozen=True)
class ConfidenceInterval:
    """A deterministic percentile-bootstrap confidence interval for one point estimate.

    This describes the sampling uncertainty of a single solution's own metric; it
    implies no pairwise significance test, comparison, ranking, or winner between
    solutions. ``lower``/``upper`` are ``None`` when there were zero underlying
    observations to resample (nothing to estimate); with exactly one observation
    there is no variance to estimate, so both bounds equal that single
    observation's statistic value.
    """

    lower: float | None
    upper: float | None


def _bootstrap_ci[T](
    population: Sequence[T],
    statistic: Callable[[Sequence[T]], float],
    *,
    seed: int = _BOOTSTRAP_SEED,
    resamples: int = _BOOTSTRAP_RESAMPLES,
    confidence: float = _BOOTSTRAP_CONFIDENCE,
) -> ConfidenceInterval:
    """Compute a deterministic 95% percentile bootstrap CI for ``statistic``.

    Resampling uses a fixed seed (module default: 20260828) and a fixed resample
    count (module default: 2,000), so the same input population always yields
    byte-identical bounds -- there is no randomness across runs, and no jitter or
    seed variation is ever introduced. Each resample draws ``len(population)``
    observations independently and uniformly, with replacement, from
    ``population``; the interval is the 2.5th/97.5th percentile of the resulting
    distribution of ``statistic`` values (linear interpolation, matching
    ``_percentile`` used elsewhere in this module).
    """
    n = len(population)
    if n == 0:
        return ConfidenceInterval(lower=None, upper=None)
    if n == 1:
        point = statistic(population)
        return ConfidenceInterval(lower=point, upper=point)
    rng = random.Random(seed)
    estimates = [
        statistic([population[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples)
    ]
    alpha = 1.0 - confidence
    return ConfidenceInterval(
        lower=_percentile(estimates, (alpha / 2.0) * 100.0),
        upper=_percentile(estimates, (1.0 - alpha / 2.0) * 100.0),
    )


_ScoreBucket = Literal["matched", "mismatched", "missing", "extra"]


def _classify_score(score: FieldScore) -> _ScoreBucket | None:
    """Classify one labeled ``FieldScore`` into a matched/mismatched/missing/extra bucket.

    Returns ``None`` for unlabeled scores, which never contribute to these counts
    (matching every other label-mask-aware metric in this module). A score whose
    field is a synthetic ``.__extra__`` row (see
    ``receipt_bench.evaluation.pointers.extra_field_name``) is always ``"extra"``:
    it represents an unmatched predicted line item or payment method with no
    ground-truth counterpart, never a "mismatched" scoring of an existing one.
    """
    if not score.labeled:
        return None
    if is_extra_field(score.field):
        return "extra"
    if score.exact_match:
        return "matched"
    if score.expected is not None and score.actual is None:
        return "missing"
    if score.actual is not None:
        return "mismatched"
    return None


@dataclass(frozen=True)
class MatchCounts:
    """Matched / mismatched / missing / extra counts over a set of labeled field scores.

    ``precision``, ``recall``, and ``f1`` are deterministic and zero-safe:
    ``precision = matched / (matched + mismatched + extra)``,
    ``recall = matched / (matched + mismatched + missing)``, and ``f1`` is their
    harmonic mean; each is ``0.0`` (never a division error) when its denominator
    is zero.
    """

    matched: int
    mismatched: int
    missing: int
    extra: int

    @classmethod
    def from_scores(cls, scores: Sequence[FieldScore]) -> MatchCounts:
        matched = mismatched = missing = extra = 0
        for score in scores:
            bucket = _classify_score(score)
            if bucket == "matched":
                matched += 1
            elif bucket == "mismatched":
                mismatched += 1
            elif bucket == "missing":
                missing += 1
            elif bucket == "extra":
                extra += 1
        return cls(matched=matched, mismatched=mismatched, missing=missing, extra=extra)

    @property
    def precision(self) -> float:
        return _safe_ratio(self.matched, self.matched + self.mismatched + self.extra)

    @property
    def recall(self) -> float:
        return _safe_ratio(self.matched, self.matched + self.mismatched + self.missing)

    @property
    def f1(self) -> float:
        precision, recall = self.precision, self.recall
        denominator = precision + recall
        return (2 * precision * recall / denominator) if denominator else 0.0


@dataclass(frozen=True)
class LatencyStats:
    count: int
    mean_ms: float | None
    p50_ms: float | None
    p95_ms: float | None

    @classmethod
    def from_values(cls, values: Sequence[float]) -> LatencyStats:
        if not values:
            return cls(count=0, mean_ms=None, p50_ms=None, p95_ms=None)
        return cls(
            count=len(values),
            mean_ms=mean(values),
            p50_ms=_percentile(values, 50),
            p95_ms=_percentile(values, 95),
        )


@dataclass(frozen=True)
class HistogramBucket:
    label: str
    count: int


def latency_histogram(values: Sequence[float], bucket_count: int = 8) -> list[HistogramBucket]:
    """Bucket a sequence of latencies into a fixed number of equal-width bins."""
    if not values:
        return []
    low, high = min(values), max(values)
    if low == high:
        return [HistogramBucket(label=f"{low:.0f} ms", count=len(values))]
    width = (high - low) / bucket_count
    counts = [0] * bucket_count
    for value in values:
        index = min(bucket_count - 1, int((value - low) / width))
        counts[index] += 1
    return [
        HistogramBucket(
            label=f"{low + index * width:.0f}-{low + (index + 1) * width:.0f} ms",
            count=count,
        )
        for index, count in enumerate(counts)
    ]


@dataclass(frozen=True)
class ComponentAggregate:
    component: str
    sample_count: int
    latency: LatencyStats
    mean_input_tokens: float | None
    mean_output_tokens: float | None
    mean_total_tokens: float | None
    mean_pages: float | None
    mean_contextualization_tokens: float | None
    total_estimated_cost: float | None
    mean_estimated_cost: float | None
    priced_cost_total: float | None = None
    priced_cost_mean: float | None = None


@dataclass(frozen=True)
class SolutionMetadata:
    """Static solution identity threaded from ``BenchmarkConfig.solutions``.

    These are configuration facts (see ``receipt_bench.config.SolutionConfig``),
    not measured behavior: copied through so reports can label solutions (for
    example a "preview" lifecycle badge) without this module importing or
    depending on the config module's own parsing/validation. ``kind`` is the
    adapter kind (``"direct"``, ``"native"``, or ``"ocr_pipeline"``); ``model``/
    ``deployment`` identify the underlying model/deployment where configured
    (``None`` when the solution's config omits one, e.g. many native
    solutions have no separate ``model`` name); ``lifecycle`` defaults to
    ``"ga"`` to match ``SolutionConfig.lifecycle``'s own default.
    """

    kind: str
    lifecycle: str = "ga"
    deployment: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class WarmupAggregate:
    """Warmup-call overhead for one solution, aggregated separately from measured pairs.

    Warmups are real, potentially paid, provider calls (see
    ``receipt_bench.evaluation.runner.run_benchmark``); this type exists purely so
    their usage/cost is visible in reporting instead of silently discarded. It is
    never merged into ``SolutionAggregate``: ``latency`` here is warmup-only
    latency, never counted toward a solution's measured end-to-end latency, and
    warmup calls are never scored (there is no quality/correctness concept for a
    call whose response is never parsed against ground truth).
    """

    solution_id: str
    warmup_count: int
    latency: LatencyStats
    components: list[ComponentAggregate]


@dataclass(frozen=True)
class SolutionAggregate:
    solution_id: str
    total_samples: int
    succeeded: int
    failed: int
    invalid: int
    error_rate: float
    invalid_rate: float
    coverage: float
    exact_match_rate: float
    mean_normalized_similarity: float
    arithmetic_consistency_rate: float | None
    schema_valid_rate: float
    document_exact_rate: float
    matched_count: int
    mismatched_count: int
    missing_count: int
    extra_count: int
    precision: float
    recall: float
    f1: float
    hallucinated_extra_rate: float
    mean_attempts: float
    overall_latency: LatencyStats
    latency_histogram: list[HistogramBucket]
    components: list[ComponentAggregate]
    document_exact_rate_ci: ConfidenceInterval = field(
        default_factory=lambda: ConfidenceInterval(lower=None, upper=None)
    )
    f1_ci: ConfidenceInterval = field(
        default_factory=lambda: ConfidenceInterval(lower=None, upper=None)
    )
    mean_normalized_similarity_ci: ConfidenceInterval = field(
        default_factory=lambda: ConfidenceInterval(lower=None, upper=None)
    )
    mean_latency_ms_ci: ConfidenceInterval = field(
        default_factory=lambda: ConfidenceInterval(lower=None, upper=None)
    )
    priced_cost_total: float | None = None
    priced_cost_mean: float | None = None
    lifecycle: str = "ga"
    kind: str | None = None
    deployment: str | None = None
    model: str | None = None
    overall_quality_score: float = 0.0
    valid_output_rate: float = 0.0
    fully_correct_rate: float = 0.0
    overall_quality_score_ci: ConfidenceInterval = field(
        default_factory=lambda: ConfidenceInterval(lower=None, upper=None)
    )
    fully_correct_rate_ci: ConfidenceInterval = field(
        default_factory=lambda: ConfidenceInterval(lower=None, upper=None)
    )


#: Sentinel used for ``FieldAggregate.language`` when a sample's
#: ``ReceiptSample.source_language`` is not recorded, so that grouping and
#: sorting always operate over plain strings instead of mixing ``None`` in.
_UNKNOWN_LANGUAGE = "unknown"


@dataclass(frozen=True)
class FieldAggregate:
    solution_id: str
    dataset: str
    language: str
    field: str
    total_count: int
    labeled_count: int
    coverage: float
    exact_match_rate: float
    mean_normalized_similarity: float
    missing_count: int
    mismatched_count: int
    matched_count: int
    extra_count: int
    precision: float
    recall: float
    f1: float


@dataclass(frozen=True)
class AggregateReport:
    run_id: str
    generated_at: datetime
    solutions: list[SolutionAggregate]
    fields: list[FieldAggregate]
    pricing_source: str | None = None
    pricing_captured_at: datetime | None = None
    pricing_currency: str | None = None
    warmups: list[WarmupAggregate] = field(default_factory=list)


def _component_aggregate(
    solution_id: str,
    name: str,
    measurements: list[ComponentMeasurement],
    pricing: PricingSnapshot | None,
    normalizer_model: str | None,
) -> ComponentAggregate:
    latencies = [measurement.latency_ms for measurement in measurements]
    input_tokens = [m.usage.input_tokens for m in measurements if m.usage.input_tokens is not None]
    output_tokens = [
        m.usage.output_tokens for m in measurements if m.usage.output_tokens is not None
    ]
    total_tokens = [m.usage.total_tokens for m in measurements if m.usage.total_tokens is not None]
    pages = [m.usage.pages for m in measurements if m.usage.pages is not None]
    contextualization_tokens = [
        m.usage.contextualization_tokens
        for m in measurements
        if m.usage.contextualization_tokens is not None
    ]
    costs_complete = bool(measurements) and all(
        measurement.estimated_cost is not None for measurement in measurements
    )
    costs = [
        measurement.estimated_cost or 0.0
        for measurement in measurements
    ]

    rate = rate_for(pricing, solution_id, name, normalizer_model=normalizer_model)
    priced_cost_values = [
        estimate_component_cost(rate, measurement.usage)
        for measurement in measurements
    ]
    priced_costs_complete = bool(measurements) and all(
        cost is not None for cost in priced_cost_values
    )
    priced_costs = [cost or 0.0 for cost in priced_cost_values]

    return ComponentAggregate(
        component=name,
        sample_count=len(measurements),
        latency=LatencyStats.from_values(latencies),
        mean_input_tokens=_safe_mean(input_tokens) if input_tokens else None,
        mean_output_tokens=_safe_mean(output_tokens) if output_tokens else None,
        mean_total_tokens=_safe_mean(total_tokens) if total_tokens else None,
        mean_pages=_safe_mean(pages) if pages else None,
        mean_contextualization_tokens=(
            _safe_mean(contextualization_tokens) if contextualization_tokens else None
        ),
        total_estimated_cost=sum(costs) if costs_complete else None,
        mean_estimated_cost=_safe_mean(costs) if costs_complete else None,
        priced_cost_total=sum(priced_costs) if priced_costs_complete else None,
        priced_cost_mean=(
            _safe_mean(priced_costs) if priced_costs_complete else None
        ),
    )


def _sample_priced_cost(
    components: list[ComponentMeasurement],
    solution_id: str,
    pricing: PricingSnapshot | None,
    normalizer_model: str | None,
) -> float | None:
    """Total priced cost for one sample's solution output, across every one of
    its recorded components (for example an OCR pipeline's ``ocr`` step and its
    shared ``normalize`` step both contribute), but only when every one of
    those components resolves to a priced cost.

    If any component has no rate for its resolved key, or reports a usage
    dimension the rate card leaves unpriced, the whole sample's cost is
    ``None`` rather than a partial sum of whichever components happened to be
    priced -- a partial total would understate the sample's true cost while
    still looking like a complete number. A sample with no recorded components
    at all (for example a failed pair) has no cost to report and is also
    ``None``.
    """
    if not components:
        return None
    total = 0.0
    for component in components:
        rate = rate_for(
            pricing, solution_id, component.component, normalizer_model=normalizer_model
        )
        cost = estimate_component_cost(rate, component.usage)
        if cost is None:
            return None
        total += cost
    return total


def _labeled_field_scores(items: Sequence[EvaluatedOutput]) -> list[FieldScore]:
    """Flatten one (possibly resampled) bag of documents into their labeled field scores.

    Used as the statistic input for ``f1_ci``/``mean_normalized_similarity_ci``:
    the bootstrap resamples whole *documents* (``EvaluatedOutput`` items), and
    each resampled document still contributes every one of its own labeled
    field scores together. This preserves within-document correlation (a
    single hard document contributes several correlated field errors at once)
    instead of the field-level resampling this replaced, which treated each
    field score as an independent observation and understated document-level
    uncertainty.
    """
    return [score for item in items for score in item.fields if score.labeled]


def _is_document_exact(item: EvaluatedOutput) -> bool:
    """Whether every labeled field score for one succeeded sample is an exact match.

    Labeled ``.__extra__`` rows (hallucinated repeated entities) always have
    ``exact_match=False``, so a sample with any labeled extra entry is never
    counted as document-exact.
    """
    labeled_scores = [score for score in item.fields if score.labeled]
    return bool(labeled_scores) and all(score.exact_match for score in labeled_scores)


def _receipt_quality_score(item: EvaluatedOutput) -> float:
    """Field F1 for one receipt, or zero when no scoreable answer was captured."""
    if item.output.status != "succeeded":
        return 0.0
    labeled_scores = [score for score in item.fields if score.labeled]
    if not labeled_scores:
        return 0.0
    return MatchCounts.from_scores(labeled_scores).f1


def _solution_aggregate(
    solution_id: str,
    items: list[EvaluatedOutput],
    pricing: PricingSnapshot | None,
    normalizer_models: Mapping[str, str],
    metadata: SolutionMetadata | None,
) -> SolutionAggregate:
    total = len(items)
    succeeded = sum(1 for item in items if item.output.status == "succeeded")
    failed = sum(1 for item in items if item.output.status == "failed")
    invalid = sum(1 for item in items if item.output.status == "invalid")
    schema_valid_count = sum(1 for item in items if item.schema_valid)
    document_exact_count = sum(
        1 for item in items if item.output.status == "succeeded" and _is_document_exact(item)
    )
    receipt_quality_scores = [_receipt_quality_score(item) for item in items]

    labeled_scores = [score for item in items for score in item.fields if score.labeled]
    exact_flags = [score.exact_match for score in labeled_scores if score.exact_match is not None]
    similarities = [
        score.normalized_similarity
        for score in labeled_scores
        if score.normalized_similarity is not None
    ]
    covered = [score for score in labeled_scores if score.actual is not None]
    counts = MatchCounts.from_scores(labeled_scores)
    repeated_counts = MatchCounts.from_scores(
        [score for score in labeled_scores if is_repeated_entity_field(score.field)]
    )

    consistency_flags = [
        item.arithmetic_consistent for item in items if item.arithmetic_consistent is not None
    ]

    components_by_name: dict[str, list[ComponentMeasurement]] = defaultdict(list)
    sample_latencies: list[float] = []
    for item in items:
        sample_total = 0.0
        has_component = False
        for component in item.output.components:
            components_by_name[component.component].append(component)
            # A retry-overhead measurement (a retried ProviderResponseError attempt's
            # preserved usage; see runner._retry_overhead_component) is still grouped
            # into its own components_by_name row above -- so its cost/usage is
            # aggregated and priced -- but it is excluded here: it must not inflate
            # this sample's measured end-to-end quality latency, since it did not
            # decide the pair's outcome.
            if component.component.startswith(RETRY_OVERHEAD_COMPONENT_PREFIX):
                continue
            sample_total += component.latency_ms
            has_component = True
        if has_component:
            sample_latencies.append(sample_total)

    normalizer_model = normalizer_models.get(solution_id)
    sample_priced_cost_values = [
        _sample_priced_cost(
            item.output.components,
            solution_id,
            pricing,
            normalizer_model,
        )
        for item in items
    ]
    all_samples_priced = bool(items) and all(
        cost is not None for cost in sample_priced_cost_values
    )
    sample_priced_costs = [cost or 0.0 for cost in sample_priced_cost_values]
    succeeded_items = [item for item in items if item.output.status == "succeeded"]
    document_exact_rate_ci = _bootstrap_ci(
        succeeded_items,
        lambda sample: sum(1 for item in sample if _is_document_exact(item)) / len(sample),
    )
    f1_ci = _bootstrap_ci(
        succeeded_items,
        lambda sample: MatchCounts.from_scores(_labeled_field_scores(sample)).f1,
    )
    mean_normalized_similarity_ci = _bootstrap_ci(
        succeeded_items,
        lambda sample: _safe_mean(
            [
                score.normalized_similarity
                for score in _labeled_field_scores(sample)
                if score.normalized_similarity is not None
            ]
        ),
    )
    mean_latency_ms_ci = _bootstrap_ci(sample_latencies, mean)
    overall_quality_score_ci = _bootstrap_ci(receipt_quality_scores, mean)
    fully_correct_rate_ci = _bootstrap_ci(
        items,
        lambda sample: (
            sum(
                1
                for item in sample
                if item.output.status == "succeeded" and _is_document_exact(item)
            )
            / len(sample)
        ),
    )

    return SolutionAggregate(
        solution_id=solution_id,
        total_samples=total,
        succeeded=succeeded,
        failed=failed,
        invalid=invalid,
        error_rate=failed / total if total else 0.0,
        invalid_rate=invalid / total if total else 0.0,
        coverage=len(covered) / len(labeled_scores) if labeled_scores else 0.0,
        exact_match_rate=(
            sum(1 for flag in exact_flags if flag) / len(exact_flags) if exact_flags else 0.0
        ),
        mean_normalized_similarity=_safe_mean(similarities),
        arithmetic_consistency_rate=(
            sum(1 for flag in consistency_flags if flag) / len(consistency_flags)
            if consistency_flags
            else None
        ),
        schema_valid_rate=schema_valid_count / total if total else 0.0,
        document_exact_rate=document_exact_count / succeeded if succeeded else 0.0,
        matched_count=counts.matched,
        mismatched_count=counts.mismatched,
        missing_count=counts.missing,
        extra_count=counts.extra,
        precision=counts.precision,
        recall=counts.recall,
        f1=counts.f1,
        hallucinated_extra_rate=_safe_ratio(
            repeated_counts.extra,
            repeated_counts.matched + repeated_counts.mismatched + repeated_counts.extra,
        ),
        mean_attempts=_safe_mean([float(item.output.attempts) for item in items]),
        overall_latency=LatencyStats.from_values(sample_latencies),
        latency_histogram=latency_histogram(sample_latencies),
        components=[
            _component_aggregate(
                solution_id, name, components_by_name[name], pricing, normalizer_model
            )
            for name in sorted(components_by_name)
        ],
        document_exact_rate_ci=document_exact_rate_ci,
        f1_ci=f1_ci,
        mean_normalized_similarity_ci=mean_normalized_similarity_ci,
        mean_latency_ms_ci=mean_latency_ms_ci,
        priced_cost_total=(
            sum(sample_priced_costs) if all_samples_priced else None
        ),
        priced_cost_mean=(
            _safe_mean(sample_priced_costs) if all_samples_priced else None
        ),
        lifecycle=metadata.lifecycle if metadata is not None else "ga",
        kind=metadata.kind if metadata is not None else None,
        deployment=metadata.deployment if metadata is not None else None,
        model=metadata.model if metadata is not None else None,
        overall_quality_score=_safe_mean(receipt_quality_scores),
        valid_output_rate=succeeded / total if total else 0.0,
        fully_correct_rate=document_exact_count / total if total else 0.0,
        overall_quality_score_ci=overall_quality_score_ci,
        fully_correct_rate_ci=fully_correct_rate_ci,
    )


def aggregate_by_solution(
    evaluated: Sequence[EvaluatedOutput],
    pricing: PricingSnapshot | None = None,
    normalizer_models: Mapping[str, str] | None = None,
    solution_metadata: Mapping[str, SolutionMetadata] | None = None,
) -> list[SolutionAggregate]:
    """Aggregate evaluated outputs into one summary row per solution.

    ``overall_quality_score`` is a quality-only decision metric: field F1 is
    calculated independently for each receipt over its labeled golden answers,
    failed/invalid outputs contribute zero, and the receipt scores are averaged
    with equal weight. It never mixes quality with latency or cost.

    ``valid_output_rate`` is the fraction of attempts that produced a minimally
    structured, scoreable answer; strict ``schema_valid_rate`` remains a separate
    interoperability diagnostic and does not gate answer quality.
    ``fully_correct_rate`` is the fraction of *all attempted receipts* where every
    labeled answer matched and no labeled repeated-entity extra was predicted.

    The technical ``precision``/``recall``/``f1`` fields are
    computed from ``matched``/``mismatched``/``missing``/``extra`` counts over
    every labeled field (scalar and repeated-entity). ``schema_valid_rate`` is
    the fraction of all samples whose output validated against the receipt
    schema; ``document_exact_rate`` is the fraction of *succeeded* samples where
    every labeled field was an exact match and no labeled extra (hallucinated)
    repeated entity was predicted.

    ``hallucinated_extra_rate`` is deliberately scoped to repeated entities
    (line items and payment methods) only: it is the fraction of predicted
    repeated-entity entries that had no ground-truth counterpart at all. There
    is no equivalent measure for scalar fields, because the datasets carry no
    negative-label masks (no ground truth for "this field should not have been
    populated") -- a wrong scalar value is measured as a mismatch, not a
    hallucination, and this module makes no claim otherwise.

    ``overall_quality_score_ci`` and ``fully_correct_rate_ci`` resample all attempted
    receipts, including zero-valued failed/invalid attempts. The technical
    ``document_exact_rate_ci``, ``f1_ci``, ``mean_normalized_similarity_ci``, and
    ``mean_latency_ms_ci`` are deterministic 95% percentile bootstrap confidence
    intervals (fixed seed, 2,000 resamples; see ``_bootstrap_ci``) describing the
    sampling uncertainty of each solution's own point estimate. These resample
    whole *documents*, not individual field scores or
    component measurements: each resampled document still contributes every one
    of its own labeled field scores (for ``f1_ci``/``mean_normalized_similarity_ci``)
    or its own end-to-end latency (for ``mean_latency_ms_ci``) together, so
    within-document correlation (a single hard document producing several
    correlated field errors, or one slow document dominating several component
    latencies) is preserved in the resample rather than treating fields/
    components as independent observations, which would understate the true
    document-level uncertainty. They are never a pairwise significance test
    between solutions and never imply a ranking or winner. With zero underlying
    observations both bounds are ``None``; with exactly one observation both
    bounds equal that single value.

    When ``pricing`` is supplied, each component's usage is priced from the
    pricing snapshot's rate card (see
    ``receipt_bench.evaluation.pricing.resolve_rate_key`` for how a component
    maps to a rate-card entry); a component with no matching rate (or a rate
    that does not price a usage dimension the component reports) gets
    ``priced_cost_total``/``priced_cost_mean`` of ``None`` rather than an
    inferred value. ``normalizer_models`` maps ``solution_id`` to the shared
    normalizer model backing its "normalize" component (from
    ``SolutionConfig.normalizer``), needed to resolve that component's rate key;
    solutions absent from the mapping, or components other than "normalize",
    are unaffected.

    ``SolutionAggregate.priced_cost_total``/``priced_cost_mean`` are the
    solution-level counterparts, summed/averaged across samples rather than
    components. Every recorded component for every attempted sample must
    resolve to a priced cost; otherwise both solution-level values are
    ``None`` rather than an understated partial total. The per-component
    breakdown is always reported alongside this solution-level total.

    A component measurement preserved from a retried ``ProviderResponseError``
    attempt (named ``retry_overhead:<attempt>:<name>`` by
    ``runner._retry_overhead_component``, rather than being discarded when a
    pair eventually succeeds or exhausts its retries) is aggregated and priced
    as its own distinct component row -- so real, already-incurred cost from a
    retried attempt is never lost from ``priced_cost_total``/``priced_cost_mean``
    or from the per-component breakdown -- but it is excluded from
    ``overall_latency``, ``latency_histogram``, and ``mean_latency_ms_ci``: a
    retried attempt did not decide the pair's outcome, so it must not inflate
    the sample's measured end-to-end quality latency.

    ``solution_metadata`` maps ``solution_id`` to a :class:`SolutionMetadata`
    snapshot (``kind``/``lifecycle``/``deployment``/``model``) copied straight
    from ``BenchmarkConfig.solutions`` (``SolutionConfig``); it is display-only
    identity information, never used in any score or rate computation. A
    solution absent from the mapping gets the same defaults ``SolutionConfig``
    itself would (``lifecycle="ga"``, ``kind``/``deployment``/``model`` all
    ``None``). This is what lets reports mark non-GA solutions (for example a
    ``lifecycle="preview"`` entry) without this module invoking or depending on
    ``receipt_bench.config`` at all.
    """
    resolved_normalizer_models = normalizer_models if normalizer_models is not None else {}
    resolved_solution_metadata = solution_metadata if solution_metadata is not None else {}
    by_solution: dict[str, list[EvaluatedOutput]] = defaultdict(list)
    for item in evaluated:
        by_solution[item.output.solution_id].append(item)
    return [
        _solution_aggregate(
            solution_id,
            by_solution[solution_id],
            pricing,
            resolved_normalizer_models,
            resolved_solution_metadata.get(solution_id),
        )
        for solution_id in sorted(by_solution)
    ]


def _field_aggregate(
    solution_id: str, dataset: str, language: str, field_name: str, scores: list[FieldScore]
) -> FieldAggregate:
    labeled_scores = [score for score in scores if score.labeled]
    exact_flags = [score.exact_match for score in labeled_scores if score.exact_match is not None]
    similarities = [
        score.normalized_similarity
        for score in labeled_scores
        if score.normalized_similarity is not None
    ]
    covered = [score for score in labeled_scores if score.actual is not None]
    counts = MatchCounts.from_scores(labeled_scores)
    return FieldAggregate(
        solution_id=solution_id,
        dataset=dataset,
        language=language,
        field=field_name,
        total_count=len(scores),
        labeled_count=len(labeled_scores),
        coverage=len(covered) / len(labeled_scores) if labeled_scores else 0.0,
        exact_match_rate=(
            sum(1 for flag in exact_flags if flag) / len(exact_flags) if exact_flags else 0.0
        ),
        mean_normalized_similarity=_safe_mean(similarities),
        missing_count=counts.missing,
        mismatched_count=counts.mismatched,
        matched_count=counts.matched,
        extra_count=counts.extra,
        precision=counts.precision,
        recall=counts.recall,
        f1=counts.f1,
    )


def aggregate_by_dataset_field(
    evaluated: Sequence[EvaluatedOutput], samples_by_id: Mapping[str, ReceiptSample]
) -> list[FieldAggregate]:
    """Aggregate field scores into one summary row per ``(solution, dataset, language, field)``.

    ``language`` is ``ReceiptSample.source_language``, so multilingual robustness is
    explicit and filterable rather than folded into a single dataset-wide number: the
    same field on the same dataset gets separate rows per distinct source language.
    Samples with no recorded ``source_language`` are grouped under the literal string
    ``"unknown"`` (never ``None``), so grouping keys and sort order stay well-defined.

    Only fields marked ``labeled`` on a given sample (per the label mask in
    ``ReceiptSample.labeled_fields``) contribute to coverage, quality, missing,
    mismatched, matched, extra, and precision/recall/f1 counts, since unlabeled
    ground truth is not considered reliable. Extra (hallucinated) repeated
    entities are grouped under their own synthetic field, e.g.
    ``"line_items.__extra__"`` (see
    ``receipt_bench.evaluation.pointers.extra_field_name``), so they never
    inflate a real field's mismatched count.

    Note: only ``dataset`` and ``language`` are sliced this way because those are
    the only sample-level dimensions ``ReceiptSample`` carries natively. Image
    quality/complexity metadata is not available in the manifest, so no such
    slices are invented here.
    """
    groups: dict[tuple[str, str, str, str], list[FieldScore]] = defaultdict(list)
    for item in evaluated:
        sample = samples_by_id.get(item.output.sample_id)
        if sample is None:
            raise KeyError(f"No manifest sample found for sample_id {item.output.sample_id!r}")
        language = sample.source_language or _UNKNOWN_LANGUAGE
        for score in item.fields:
            groups[(item.output.solution_id, sample.dataset, language, score.field)].append(score)

    return [
        _field_aggregate(
            solution_id,
            dataset,
            language,
            field_name,
            groups[(solution_id, dataset, language, field_name)],
        )
        for solution_id, dataset, language, field_name in sorted(groups)
    ]


def _warmup_aggregate(
    solution_id: str,
    records: list[WarmupRecord],
    pricing: PricingSnapshot | None,
    normalizer_model: str | None,
) -> WarmupAggregate:
    components_by_name: dict[str, list[ComponentMeasurement]] = defaultdict(list)
    warmup_latencies: list[float] = []
    for record in records:
        record_total = 0.0
        has_component = False
        for component in record.components:
            components_by_name[component.component].append(component)
            record_total += component.latency_ms
            has_component = True
        if has_component:
            warmup_latencies.append(record_total)

    return WarmupAggregate(
        solution_id=solution_id,
        warmup_count=len(records),
        latency=LatencyStats.from_values(warmup_latencies),
        components=[
            _component_aggregate(
                solution_id, name, components_by_name[name], pricing, normalizer_model
            )
            for name in sorted(components_by_name)
        ],
    )


def aggregate_warmups(
    records: Sequence[WarmupRecord],
    pricing: PricingSnapshot | None = None,
    normalizer_models: Mapping[str, str] | None = None,
) -> list[WarmupAggregate]:
    """Aggregate normalized warmup records into one summary row per solution.

    This mirrors ``aggregate_by_solution``'s component/pricing aggregation
    exactly (same ``ComponentAggregate``/``LatencyStats``/pricing resolution),
    but over warmup calls instead of measured pairs, and is always reported as
    a separate, clearly-labeled section: warmup latency and cost are call
    overhead, not measured solution performance, and are never merged into
    ``SolutionAggregate``. A solution with no recorded warmups (including
    ``config.warmup_requests=0`` runs) does not appear in the returned list.
    """
    resolved_normalizer_models = normalizer_models if normalizer_models is not None else {}
    by_solution: dict[str, list[WarmupRecord]] = defaultdict(list)
    for record in records:
        by_solution[record.solution_id].append(record)
    return [
        _warmup_aggregate(
            solution_id,
            by_solution[solution_id],
            pricing,
            resolved_normalizer_models.get(solution_id),
        )
        for solution_id in sorted(by_solution)
    ]


def build_report(
    run_id: str,
    evaluated: Sequence[EvaluatedOutput],
    samples_by_id: Mapping[str, ReceiptSample],
    generated_at: datetime | None = None,
    pricing: PricingSnapshot | None = None,
    normalizer_models: Mapping[str, str] | None = None,
    warmups: Sequence[WarmupRecord] | None = None,
    solution_metadata: Mapping[str, SolutionMetadata] | None = None,
) -> AggregateReport:
    """Build the full aggregate report (per-solution and per-dataset/field) for a run.

    ``pricing`` is an optional, previously-loaded snapshot (see
    ``receipt_bench.evaluation.pricing.load_pricing_snapshot``); when omitted, every
    component's priced cost fields are ``None`` and ``pricing_source``/
    ``pricing_captured_at``/``pricing_currency`` are ``None``. ``normalizer_models``
    maps ``solution_id`` to its shared normalizer model id (from
    ``SolutionConfig.normalizer``), needed to resolve the pricing-snapshot rate
    key for "normalize" components; see ``aggregate_by_solution``.

    ``warmups`` is the run's normalized warmup records (see
    ``receipt_bench.evaluation.warmups.read_warmups``), aggregated separately via
    :func:`aggregate_warmups` into ``AggregateReport.warmups``. Omitted or empty
    when the run had no warmups (e.g. ``config.warmup_requests=0``).

    ``solution_metadata`` maps ``solution_id`` to its :class:`SolutionMetadata`
    (``kind``/``lifecycle``/``deployment``/``model``, copied from
    ``BenchmarkConfig.solutions``); see ``aggregate_by_solution``. This is
    identity/labeling information only (for example, marking a solution
    ``lifecycle="preview"``) and never affects any measured metric.
    """
    return AggregateReport(
        run_id=run_id,
        generated_at=generated_at if generated_at is not None else datetime.now(UTC),
        solutions=aggregate_by_solution(evaluated, pricing, normalizer_models, solution_metadata),
        fields=aggregate_by_dataset_field(evaluated, samples_by_id),
        pricing_source=pricing.source if pricing is not None else None,
        pricing_captured_at=pricing.captured_at if pricing is not None else None,
        pricing_currency=pricing.currency if pricing is not None else None,
        warmups=aggregate_warmups(warmups or [], pricing, normalizer_models),
    )


__all__ = [
    "AggregateReport",
    "ComponentAggregate",
    "ConfidenceInterval",
    "FieldAggregate",
    "HistogramBucket",
    "LatencyStats",
    "MatchCounts",
    "SolutionAggregate",
    "SolutionMetadata",
    "WarmupAggregate",
    "aggregate_by_dataset_field",
    "aggregate_by_solution",
    "aggregate_warmups",
    "build_report",
    "latency_histogram",
]
