from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from receipt_bench.artifacts import ArtifactStore, safe_segment
from receipt_bench.config import BenchmarkConfig, RetryConfig
from receipt_bench.evaluation.errors import RunConfigurationError
from receipt_bench.evaluation.manifest import load_manifest
from receipt_bench.evaluation.pricing import RETRY_OVERHEAD_COMPONENT_PREFIX
from receipt_bench.evaluation.results_io import append_result, existing_pairs
from receipt_bench.evaluation.run_plan import RunPlan, ensure_run_plan
from receipt_bench.evaluation.warmups import (
    WarmupRecord,
    append_warmup,
    completed_warmup_counts,
)
from receipt_bench.models import ComponentMeasurement, ReceiptSample, SolutionOutput
from receipt_bench.providers.base import (
    AdapterResponse,
    InvalidSolutionOutput,
    ProviderResponseError,
    SolutionAdapter,
)

_MEDIA_TYPES_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".pdf": "application/pdf",
}


class ImageIntegrityError(RuntimeError):
    """Raised when a sample's image bytes do not match its recorded ``image_sha256``."""


def new_run_id(now: datetime | None = None) -> str:
    """Build a UTC-timestamped run id, for example ``20260828T181300Z``."""
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("Run id timestamp must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _sample_seed(base_seed: int, sample_id: str) -> int:
    """Derive a deterministic per-sample seed from the committed run seed.

    Deriving the seed from ``(base_seed, sample_id)`` alone (rather than, say, seeding
    once and shuffling sequentially per sample) means every sample's solution order is
    reproducible independent of which other samples have already run. That is what
    makes resume-after-interruption produce byte-identical orderings to a full run.
    """
    digest = hashlib.sha256(f"{base_seed}:{sample_id}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big")


def shuffled_solution_order(solution_ids: list[str], base_seed: int, sample_id: str) -> list[str]:
    """Deterministically shuffle the solution execution order for one sample."""
    rng = random.Random(_sample_seed(base_seed, sample_id))
    order = list(solution_ids)
    rng.shuffle(order)
    return order


def media_type_for(path: Path) -> str:
    """Infer an image media type from a file extension."""
    media_type = _MEDIA_TYPES_BY_SUFFIX.get(path.suffix.lower())
    if media_type is not None:
        return media_type
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed is None:
        raise ValueError(f"Cannot determine media type for image: {path}")
    return guessed


def resolve_image_path(manifest_path: Path, sample: ReceiptSample) -> Path:
    """Resolve a sample's ``image_path`` relative to the manifest file's directory."""
    candidate = Path(sample.image_path)
    if candidate.is_absolute():
        return candidate
    return (manifest_path.parent / candidate).resolve()


def load_image_bytes(manifest_path: Path, sample: ReceiptSample) -> bytes:
    """Read a sample's image bytes and verify them against ``image_sha256``."""
    path = resolve_image_path(manifest_path, sample)
    if not path.is_file():
        raise FileNotFoundError(f"Image not found for sample {sample.sample_id}: {path}")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != sample.image_sha256:
        raise ImageIntegrityError(
            f"Image checksum mismatch for sample {sample.sample_id}: "
            f"expected {sample.image_sha256}, got {digest} (path: {path})"
        )
    return data


def _artifact_path(root: Path, run_id: str, solution_id: str, sample_id: str) -> Path:
    """Mirror ``ArtifactStore``'s path layout without duplicating its write logic."""
    return (
        root
        / safe_segment(run_id)
        / "raw"
        / safe_segment(solution_id)
        / f"{safe_segment(sample_id)}.json"
    )


def _warmup_artifact_path(root: Path, run_id: str, solution_id: str, index: int) -> Path:
    """Path for one warmup call's raw response, under a namespace distinct from
    ``raw/`` (measured pairs) so warmup and measured artifacts are never
    confused, listed together, or accidentally overwritten by one another."""
    return root / safe_segment(run_id) / "warmups" / safe_segment(solution_id) / f"{index}.json"


def _write_warmup_artifact(
    root: Path, run_id: str, solution_id: str, index: int, payload: Any
) -> Path:
    """Write one warmup's raw response, atomically, mirroring
    ``ArtifactStore.write_json``'s write-then-rename pattern but under the
    separate ``warmups/`` namespace (see :func:`_warmup_artifact_path`)."""
    target = _warmup_artifact_path(root, run_id, solution_id, index)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    total_pairs: int
    completed_pairs: int
    skipped_pairs: int
    results_path: Path


def run_benchmark(
    *,
    config: BenchmarkConfig,
    manifest_path: Path,
    manifest: list[ReceiptSample],
    planned_sample_ids: Sequence[str] | None = None,
    adapters: Mapping[str, SolutionAdapter],
    run_id: str | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] | None = None,
) -> RunSummary:
    """Run every configured solution against every manifest sample, sequentially.

    Determinism guarantees:

    * The run id is a UTC timestamp (or the caller-supplied ``run_id`` when resuming).
    * ``config.random_seed`` is the sole source of randomness, and each sample's
      solution execution order is shuffled deterministically from it (see
      :func:`shuffled_solution_order`).
    * Samples are processed in ``sample_id`` order.
    * Each solution receives exactly ``config.warmup_requests`` warmup calls (against
      the first sample's image, in ``sample_id`` order) before any measured pair runs.
      Warmup calls are real (potentially paid) provider calls made directly against the
      adapter with **no retry**: they exist only to absorb one-time provider/connection
      warm-up cost, so a warmup failure aborts the run immediately rather than being
      retried or masked. A successful warmup's raw response and component measurements
      (usage/cost) are persisted -- via :func:`_write_warmup_artifact` under a
      ``warmups/<solution_id>/`` namespace distinct from measured pairs' ``raw/``, and
      as a normalized ``WarmupRecord`` appended to this run's ``warmups.jsonl`` -- so
      real spend incurred by warmups is never silently discarded from reporting. They
      are still never scored and never counted toward any solution's measured
      end-to-end latency or quality metrics: see
      ``receipt_bench.evaluation.aggregate.aggregate_warmups`` for their separate,
      parallel aggregation path. Resuming skips only the warmups a solution already has
      recorded (by count, not by re-checking content), never re-running them.
    * All solutions for a given sample receive the exact same ``image_bytes`` object.
    * Raw provider responses are written once per pair via ``ArtifactStore`` and are
      never overwritten for a pair that already has a normalized result recorded.
    * Resuming (passing an existing ``run_id``) skips only the ``(sample_id,
      solution_id)`` pairs already present in that run's ``results.jsonl``.

    At the adapter-call boundary, a ``ProviderResponseError`` (a transport/provider
    failure) is retried up to ``config.retry.max_attempts`` times, with a deterministic
    exponential backoff (``config.retry.initial_delay_seconds * 2**(attempt - 1)``,
    capped at ``config.retry.max_delay_seconds``, never jittered/randomized) between
    attempts; only once every attempt has raised ``ProviderResponseError`` is the pair
    recorded as ``status="failed"``. A ``ValueError`` or Pydantic ``ValidationError``
    (invalid semantic output, e.g. malformed JSON or a schema violation) is a
    deterministic parsing failure, not a transient transport error, so it is never
    retried and is recorded as ``status="invalid"`` after exactly one attempt. This
    includes ``InvalidSolutionOutput`` (a ``ValueError`` subclass raised by an adapter
    for a *successful* call whose response could not be parsed/validated): its raw
    response and any component measurements already collected are preserved on the
    recorded artifact/``SolutionOutput`` rather than discarded, since the call itself
    consumed real latency/tokens. Neither failure mode is silently swallowed, and no
    other exception type is caught here: adapter misconfiguration
    (``AdapterConfigurationError``) and any unexpected exception propagate and abort the
    run. Provider-internal transport retries (e.g. an SDK's own HTTP retry policy)
    remain the provider's own concern; this is an orchestration-level retry bound only
    to ``ProviderResponseError``, on top of whatever the provider already does.

    ``ProviderResponseError`` may optionally carry a partial ``raw_response``/
    ``components`` (for example an OCR pipeline whose OCR stage succeeded but whose
    normalizer stage then failed transport-wise), representing real, already-incurred
    provider usage from *that* attempt alone. Every attempt's partial data is preserved,
    not just the attempt that decided the pair's final outcome:

    * The raw artifact stays a bare raw-response object (unchanged from a pair that
      never needed a retry) when the pair was decided in exactly one attempt. Once more
      than one attempt was made, the raw artifact becomes an explicit, attempt-indexed
      structure -- ``{"attempts": [{"attempt": 1, "raw_response": ..., "error_type":
      ..., "error_message": ...}, ..., {"attempt": N, "raw_response": ...}]}`` -- so
      every attempt's own raw response (``None`` when that attempt failed before any
      usable response existed) is visible and immutable, never overwritten by a later
      attempt's.
    * Every attempt's component measurements are preserved in ``SolutionOutput.components``.
      A retried (non-final, or -- for an exhausted failure -- every) attempt's components
      are renamed under a ``retry_overhead:<attempt>:`` prefix (see
      :func:`_retry_overhead_component`) before being combined with the deciding attempt's
      own (unprefixed) components: this keeps their real cost/usage visible to aggregation
      and pricing (``pricing.resolve_rate_key`` resolves the prefix back to the same rate
      as the unprefixed component) without inflating the primary component's latency
      distribution or the sample's measured end-to-end quality latency, both of which
      ``aggregate.py`` computes only from unprefixed components.
    * ``attempts`` on the recorded ``SolutionOutput`` remains the number of full adapter
      attempts made, regardless of how many of them are folded into the artifact/components
      above.

    ``sleep`` defaults to :func:`time.sleep` and is exposed purely so callers (tests)
    can inject a fast/no-op stand-in; it does not affect determinism of the retry
    *count* or *delay values*, only whether the process actually blocks for them.
    """
    if not manifest:
        raise RunConfigurationError("Manifest contains no samples to benchmark")

    solution_ids = [solution.id for solution in config.solutions]
    missing_adapters = [sid for sid in solution_ids if sid not in adapters]
    if missing_adapters:
        raise RunConfigurationError(
            f"No adapter registered for configured solution(s): {missing_adapters}"
        )

    resolved_sleep = sleep if sleep is not None else time.sleep
    resolved_run_id = run_id or new_run_id(now)
    store = ArtifactStore(config.output_root)
    results_path = config.output_root / resolved_run_id / "results.jsonl"
    completed_pairs_set = existing_pairs(results_path)
    warmups_path = config.output_root / resolved_run_id / "warmups.jsonl"
    completed_warmups = completed_warmup_counts(warmups_path)

    ordered_samples = sorted(manifest, key=lambda item: item.sample_id)
    plan_sample_ids = tuple(
        sorted(
            planned_sample_ids
            if planned_sample_ids is not None
            else (
                sample.sample_id
                for sample in load_manifest(manifest_path)
            )
        )
    )
    plan = ensure_run_plan(
        config.output_root / resolved_run_id / "run-plan.json",
        RunPlan(
            run_id=resolved_run_id,
            sample_ids=plan_sample_ids,
            solution_ids=tuple(solution_ids),
        ),
    )
    unplanned_samples = sorted(
        {sample.sample_id for sample in ordered_samples} - set(plan.sample_ids)
    )
    if unplanned_samples:
        raise RunConfigurationError(
            f"Execution contains samples outside the immutable run plan: {unplanned_samples[:5]}"
        )
    unexpected_completed = sorted(completed_pairs_set - plan.expected_pairs)
    if unexpected_completed:
        raise RunConfigurationError(
            "Existing results contain pairs outside the immutable run plan: "
            f"{unexpected_completed[:5]}"
        )

    if config.warmup_requests > 0:
        warmup_sample = ordered_samples[0]
        warmup_bytes = load_image_bytes(manifest_path, warmup_sample)
        warmup_media_type = media_type_for(resolve_image_path(manifest_path, warmup_sample))
        for solution_id in solution_ids:
            adapter = adapters[solution_id]
            already_recorded = completed_warmups.get(solution_id, 0)
            for warmup_index in range(already_recorded, config.warmup_requests):
                warmup_artifact_target = _warmup_artifact_path(
                    config.output_root, resolved_run_id, solution_id, warmup_index
                )
                if warmup_artifact_target.exists():
                    raise RunConfigurationError(
                        "Refusing to overwrite an existing warmup artifact that is not "
                        f"yet recorded as complete: {warmup_artifact_target}. Warmup "
                        "artifacts are immutable once written; inspect and clear this "
                        "run directory before resuming."
                    )
                # Fail fast: warmups are never retried, see the docstring above. A
                # failure here (ProviderResponseError, ValueError, or
                # InvalidSolutionOutput) propagates immediately and aborts the run;
                # nothing is persisted for this specific failed attempt, but the
                # aborting exception itself is a visible, non-silent signal.
                started_at = datetime.now(UTC)
                response = adapter.execute(warmup_bytes, warmup_media_type)
                completed_at = datetime.now(UTC)
                artifact_path = _write_warmup_artifact(
                    config.output_root,
                    resolved_run_id,
                    solution_id,
                    warmup_index,
                    response.raw_response,
                )
                append_warmup(
                    warmups_path,
                    WarmupRecord(
                        run_id=resolved_run_id,
                        solution_id=solution_id,
                        warmup_index=warmup_index,
                        started_at=started_at,
                        completed_at=completed_at,
                        raw_artifact=str(artifact_path),
                        components=list(response.components),
                    ),
                )

    total_pairs = len(ordered_samples) * len(solution_ids)
    completed_this_run = 0
    skipped_pairs = 0

    for sample in ordered_samples:
        image_bytes = load_image_bytes(manifest_path, sample)
        media_type = media_type_for(resolve_image_path(manifest_path, sample))
        order = shuffled_solution_order(solution_ids, config.random_seed, sample.sample_id)
        for solution_id in order:
            pair = (sample.sample_id, solution_id)
            if pair in completed_pairs_set:
                skipped_pairs += 1
                continue

            artifact_target = _artifact_path(
                config.output_root, resolved_run_id, solution_id, sample.sample_id
            )
            if artifact_target.exists():
                raise RunConfigurationError(
                    "Refusing to overwrite an existing raw artifact for a pair that is "
                    f"not yet recorded as complete: {artifact_target}. Raw artifacts are "
                    "immutable once written; inspect and clear this run directory before "
                    "retrying."
                )

            output = _execute_pair(
                store=store,
                run_id=resolved_run_id,
                sample=sample,
                solution_id=solution_id,
                adapter=adapters[solution_id],
                image_bytes=image_bytes,
                media_type=media_type,
                retry=config.retry,
                sleep=resolved_sleep,
            )
            append_result(results_path, output)
            completed_pairs_set.add(pair)
            completed_this_run += 1

    return RunSummary(
        run_id=resolved_run_id,
        total_pairs=total_pairs,
        completed_pairs=completed_this_run,
        skipped_pairs=skipped_pairs,
        results_path=results_path,
    )


@dataclass(frozen=True)
class _AttemptRecord:
    """One adapter-call attempt's outcome, preserved regardless of whether it was
    the pair's final, deciding attempt.

    ``error_type is None`` marks the attempt that succeeded (there is at most one,
    and it is always the last record in a ``_RetryOutcome.attempts`` list). Every
    other attempt raised ``ProviderResponseError`` and was retried (or, for the
    last record of an exhausted-failure outcome, was the attempt that gave up);
    its ``raw_response``/``components`` are whatever partial data that specific
    call already collected before failing (both may be empty -- "a hard failure
    before any usable response exists has legitimately nothing to attach", per
    ``ProviderResponseError``'s own docstring).
    """

    attempt: int
    raw_response: dict[str, Any] | None
    components: list[ComponentMeasurement]
    error_type: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class _RetryOutcome:
    """The result of running the bounded retry loop for one adapter call.

    ``attempts`` always lists every attempt made, in order, including the one
    that decided the pair's outcome. ``response`` is the successful
    ``AdapterResponse`` when the last attempt succeeded, and ``None`` when every
    attempt (up to ``retry.max_attempts``) raised ``ProviderResponseError``.
    """

    attempts: list[_AttemptRecord]
    response: AdapterResponse | None


def _execute_with_retry(
    adapter: SolutionAdapter,
    image_bytes: bytes,
    media_type: str,
    retry: RetryConfig,
    sleep: Callable[[float], None],
) -> _RetryOutcome:
    """Call ``adapter.execute``, retrying only a transport-level ``ProviderResponseError``.

    Retries are bounded by ``retry.max_attempts`` and separated by a deterministic
    exponential backoff (``retry.initial_delay_seconds * 2**(attempt - 1)``, capped at
    ``retry.max_delay_seconds``; never jittered/randomized, so behavior is reproducible
    given the same config). A ``ValueError``/pydantic ``ValidationError`` (including
    ``InvalidSolutionOutput``) represents a deterministic semantic parsing failure, not
    a transient transport error: it is not caught here at all, so it always propagates
    from the adapter's first and only attempt with no retry.

    Every attempt made -- whether it raised ``ProviderResponseError`` or eventually
    succeeded -- is recorded in the returned ``_RetryOutcome.attempts``, so no partial
    data from a retried attempt is ever silently dropped; this function never raises
    ``ProviderResponseError`` itself, it only reports the outcome.
    """
    records: list[_AttemptRecord] = []
    attempt = 0
    while True:
        attempt += 1
        try:
            response = adapter.execute(image_bytes, media_type)
        except ProviderResponseError as exc:
            records.append(
                _AttemptRecord(
                    attempt=attempt,
                    raw_response=exc.raw_response,
                    components=list(exc.components),
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            )
            if attempt >= retry.max_attempts:
                return _RetryOutcome(attempts=records, response=None)
            delay = min(retry.initial_delay_seconds * (2 ** (attempt - 1)), retry.max_delay_seconds)
            sleep(delay)
            continue
        records.append(
            _AttemptRecord(
                attempt=attempt,
                raw_response=response.raw_response,
                components=list(response.components),
            )
        )
        return _RetryOutcome(attempts=records, response=response)


def _retry_overhead_component(
    attempt: int, component: ComponentMeasurement
) -> ComponentMeasurement:
    """Rename one retried (non-final) attempt's component measurement so it is
    tracked as retry overhead: distinct from the deciding attempt's own
    same-named component. This preserves its real cost/usage -- and prices it,
    via ``pricing.resolve_rate_key``'s matching prefix handling -- without
    mixing it into the primary component's latency distribution or the
    sample's measured end-to-end quality latency, both of which
    ``aggregate.py`` computes only from unprefixed components.
    """
    return component.model_copy(
        update={"component": f"{RETRY_OVERHEAD_COMPONENT_PREFIX}{attempt}:{component.component}"}
    )


def _build_raw_artifact_payload(attempts: list[_AttemptRecord]) -> Any:
    """Build the raw-artifact payload for one pair, given every attempt made.

    Exactly one attempt (no retry occurred) keeps writing the bare raw response
    -- or, for a hard failure with no raw response at all, a small
    error-description dict -- unchanged from a pair that never needed retrying.
    More than one attempt writes an explicit, attempt-indexed structure
    (``{"attempts": [...]}``) listing every attempt in order, each with its own
    raw response (``None`` when that attempt failed before any usable response
    existed) and, for a failed attempt, its error type/message -- so no earlier
    attempt's real, potentially billable response is ever discarded or
    silently overwritten by a later one.
    """
    if len(attempts) == 1:
        record = attempts[0]
        if record.error_type is None or record.raw_response is not None:
            return record.raw_response
        return {"error_type": record.error_type, "error_message": record.error_message}
    return {
        "attempts": [
            {
                "attempt": record.attempt,
                "raw_response": record.raw_response,
                "error_type": record.error_type,
                "error_message": record.error_message,
            }
            for record in attempts
        ]
    }


def _build_pair_components(attempts: list[_AttemptRecord]) -> list[ComponentMeasurement]:
    """Combine every attempt's component measurements for one pair.

    With exactly one attempt, its components are used unchanged -- the common,
    non-retried case, matching prior behavior exactly. With more than one
    attempt, every attempt except the last is a retried attempt whose
    components are renamed under the ``retry_overhead:<attempt>:`` prefix (see
    :func:`_retry_overhead_component`). The last attempt's own components are
    appended unprefixed only if it succeeded; if every attempt (including the
    last) raised ``ProviderResponseError``, there is no winning attempt, so its
    components are renamed under the same overhead prefix too.
    """
    if len(attempts) == 1:
        return list(attempts[0].components)
    last = attempts[-1]
    retried_attempts = attempts[:-1] if last.error_type is None else attempts
    combined = [
        _retry_overhead_component(record.attempt, component)
        for record in retried_attempts
        for component in record.components
    ]
    if last.error_type is None:
        combined.extend(last.components)
    return combined


def _execute_pair(
    *,
    store: ArtifactStore,
    run_id: str,
    sample: ReceiptSample,
    solution_id: str,
    adapter: SolutionAdapter,
    image_bytes: bytes,
    media_type: str,
    retry: RetryConfig,
    sleep: Callable[[float], None],
) -> SolutionOutput:
    started_at = datetime.now(UTC)
    try:
        outcome = _execute_with_retry(adapter, image_bytes, media_type, retry, sleep)
    except InvalidSolutionOutput as exc:
        # The call itself succeeded (and consumed real latency/tokens/pages) but its
        # response could not be parsed/validated: preserve the raw response and any
        # component measurements already collected, rather than discarding them.
        return _record_failure(
            store,
            run_id,
            sample,
            solution_id,
            started_at,
            status="invalid",
            error_type=type(exc).__name__,
            error_message=str(exc),
            attempts=1,
            raw_payload=exc.raw_response,
            components=exc.components,
        )
    except (ValueError, ValidationError) as exc:
        return _record_failure(
            store,
            run_id,
            sample,
            solution_id,
            started_at,
            status="invalid",
            error_type=type(exc).__name__,
            error_message=str(exc),
            attempts=1,
            raw_payload={"error_type": type(exc).__name__, "error_message": str(exc)},
        )

    attempts_made = len(outcome.attempts)
    raw_payload = _build_raw_artifact_payload(outcome.attempts)
    components = _build_pair_components(outcome.attempts)

    if outcome.response is not None:
        completed_at = datetime.now(UTC)
        artifact_path = store.write_json(run_id, solution_id, sample.sample_id, raw_payload)
        return SolutionOutput(
            run_id=run_id,
            sample_id=sample.sample_id,
            solution_id=solution_id,
            started_at=started_at,
            completed_at=completed_at,
            status="succeeded",
            receipt=outcome.response.receipt,
            raw_artifact=str(artifact_path),
            attempts=attempts_made,
            components=components,
        )

    # Every attempt raised ProviderResponseError: the last one decided this pair's
    # final (failed) status, so its error type/message is what gets recorded.
    last = outcome.attempts[-1]
    return _record_failure(
        store,
        run_id,
        sample,
        solution_id,
        started_at,
        status="failed",
        error_type=last.error_type or "ProviderResponseError",
        error_message=last.error_message or "",
        attempts=attempts_made,
        raw_payload=raw_payload,
        components=components,
    )


def _record_failure(
    store: ArtifactStore,
    run_id: str,
    sample: ReceiptSample,
    solution_id: str,
    started_at: datetime,
    *,
    status: Literal["failed", "invalid"],
    error_type: str,
    error_message: str,
    attempts: int,
    raw_payload: Any,
    components: list[ComponentMeasurement] | None = None,
) -> SolutionOutput:
    completed_at = datetime.now(UTC)
    artifact_path = store.write_json(run_id, solution_id, sample.sample_id, raw_payload)
    return SolutionOutput(
        run_id=run_id,
        sample_id=sample.sample_id,
        solution_id=solution_id,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        receipt=None,
        raw_artifact=str(artifact_path),
        error_type=error_type,
        error_message=error_message,
        attempts=attempts,
        components=components or [],
    )


__all__ = [
    "ImageIntegrityError",
    "RunSummary",
    "load_image_bytes",
    "media_type_for",
    "new_run_id",
    "resolve_image_path",
    "run_benchmark",
    "shuffled_solution_order",
]
