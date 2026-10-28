"""Tests for the sequential deterministic benchmark runner."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from receipt_bench.config import BenchmarkConfig, RetryConfig, SolutionConfig
from receipt_bench.evaluation.errors import RunConfigurationError
from receipt_bench.evaluation.results_io import read_results
from receipt_bench.evaluation.runner import (
    ImageIntegrityError,
    new_run_id,
    run_benchmark,
    shuffled_solution_order,
)
from receipt_bench.evaluation.warmups import WarmupRecord, read_warmups
from receipt_bench.models import ComponentMeasurement, ReceiptSample, Usage
from receipt_bench.providers.base import (
    AdapterResponse,
    InvalidSolutionOutput,
    ProviderResponseError,
)
from tests.conftest import FakeAdapter, build_ground_truth_receipt


def _no_sleep(_delay: float) -> None:
    """A ``sleep`` stand-in for tests: retries must never block on real wall-clock time."""


def _config(
    tmp_path: Path,
    solution_ids: list[str],
    manifest_path: Path,
    *,
    warmup_requests: int = 2,
    random_seed: int = 1234,
    retry: RetryConfig | None = None,
) -> BenchmarkConfig:
    return BenchmarkConfig(
        schema_path=tmp_path / "schema.json",
        dataset_manifest=manifest_path,
        output_root=tmp_path / "runs",
        concurrency=1,
        warmup_requests=warmup_requests,
        random_seed=random_seed,
        retry=retry if retry is not None else RetryConfig(),
        solutions=[
            SolutionConfig(id=solution_id, kind="direct", api="chat")
            for solution_id in solution_ids
        ],
    )


def test_new_run_id_formats_utc_timestamp() -> None:
    moment = datetime(2026, 8, 28, 18, 13, 0, tzinfo=UTC)
    assert new_run_id(moment) == "20260828T181300Z"


def test_new_run_id_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        new_run_id(datetime(2026, 8, 28, 18, 13, 0))


def test_shuffled_solution_order_is_deterministic_per_seed_and_sample() -> None:
    solutions = ["alpha", "beta", "gamma", "delta"]
    first = shuffled_solution_order(solutions, base_seed=42, sample_id="sample-001")
    second = shuffled_solution_order(solutions, base_seed=42, sample_id="sample-001")
    assert first == second
    assert sorted(first) == sorted(solutions)


def test_shuffled_solution_order_varies_by_sample_id() -> None:
    solutions = ["alpha", "beta", "gamma", "delta", "epsilon"]
    a = shuffled_solution_order(solutions, base_seed=42, sample_id="sample-001")
    b = shuffled_solution_order(solutions, base_seed=42, sample_id="sample-002")
    assert a != b


def test_shuffled_solution_order_independent_of_resume_state() -> None:
    """Order must not depend on which pairs already completed (pure function of inputs)."""
    solutions = ["alpha", "beta", "gamma"]
    before_any_runs = shuffled_solution_order(solutions, base_seed=7, sample_id="sample-001")
    # Calling again (as would happen mid-resume, with other state already mutated
    # elsewhere in the process) must yield the identical order: the function takes no
    # mutable/global state.
    after_some_state_change = shuffled_solution_order(
        solutions, base_seed=7, sample_id="sample-001"
    )
    assert before_any_runs == after_some_state_change


def test_run_benchmark_runs_all_pairs_with_warmups_excluded(
    tmp_path: Path, manifest_path: Path
) -> None:
    from receipt_bench.evaluation.manifest import load_manifest

    real_manifest = load_manifest(manifest_path)
    config = _config(tmp_path, ["solution-a", "solution-b"], manifest_path, warmup_requests=2)
    adapter_a = FakeAdapter("solution-a")
    adapter_b = FakeAdapter("solution-b")
    adapters = {"solution-a": adapter_a, "solution-b": adapter_b}

    summary = run_benchmark(
        config=config,
        manifest_path=manifest_path,
        manifest=real_manifest,
        adapters=adapters,
    )

    assert summary.total_pairs == 4
    assert summary.completed_pairs == 4
    assert summary.skipped_pairs == 0

    # 2 warmup calls + 2 measured calls (one per sample) per solution.
    assert len(adapter_a.calls) == 2 + 2
    assert len(adapter_b.calls) == 2 + 2

    results = read_results(summary.results_path)
    assert len(results) == 4
    pairs = {(r.sample_id, r.solution_id) for r in results}
    assert pairs == {
        ("sample-001", "solution-a"),
        ("sample-001", "solution-b"),
        ("sample-002", "solution-a"),
        ("sample-002", "solution-b"),
    }
    assert all(result.status == "succeeded" for result in results)
    assert all(result.attempts == 1 for result in results)

    # Warmups are persisted (not discarded): 2 per solution, with their real usage
    # and cost, in a namespace separate from measured pairs.
    warmups_path = config.output_root / summary.run_id / "warmups.jsonl"
    warmups = read_warmups(warmups_path)
    assert len(warmups) == 4
    by_solution: dict[str, list[WarmupRecord]] = {}
    for record in warmups:
        by_solution.setdefault(record.solution_id, []).append(record)
    assert sorted(by_solution) == ["solution-a", "solution-b"]
    for solution_id, records in by_solution.items():
        assert [r.warmup_index for r in records] == [0, 1]
        for record in records:
            assert record.run_id == summary.run_id
            assert record.components
            assert record.components[0].component == "extraction"
            assert record.components[0].usage.input_tokens == 100
            artifact_path = Path(record.raw_artifact)
            assert artifact_path.is_file()
            assert json.loads(artifact_path.read_text(encoding="utf-8")) == {"ok": True}
            # Warmup artifacts live under their own namespace, never under raw/.
            assert artifact_path.parent.parent.name == "warmups"
            assert artifact_path.parent.name == solution_id


def test_run_benchmark_same_image_bytes_for_all_solutions_of_a_sample(
    tmp_path: Path, make_sample: Callable[..., ReceiptSample]
) -> None:
    from receipt_bench.evaluation.manifest import load_manifest

    sample_one = make_sample(sample_id="sample-001", image_bytes=b"image-one-bytes")
    sample_two = make_sample(sample_id="sample-002", image_bytes=b"image-two-bytes")
    manifest_file = tmp_path / "manifest.jsonl"
    manifest_file.write_text(
        sample_one.model_dump_json() + "\n" + sample_two.model_dump_json() + "\n",
        encoding="utf-8",
    )

    real_manifest = load_manifest(manifest_file)
    config = _config(tmp_path, ["solution-a", "solution-b"], manifest_file, warmup_requests=0)
    adapter_a = FakeAdapter("solution-a")
    adapter_b = FakeAdapter("solution-b")

    run_benchmark(
        config=config,
        manifest_path=manifest_file,
        manifest=real_manifest,
        adapters={"solution-a": adapter_a, "solution-b": adapter_b},
    )

    bytes_by_solution_a = {call[0] for call in adapter_a.calls}
    bytes_by_solution_b = {call[0] for call in adapter_b.calls}
    assert len(bytes_by_solution_a) == 2  # one per distinct sample
    assert bytes_by_solution_a == bytes_by_solution_b


def test_run_benchmark_resume_skips_only_completed_pairs(
    tmp_path: Path, manifest_path: Path
) -> None:
    from receipt_bench.evaluation.manifest import load_manifest

    real_manifest = load_manifest(manifest_path)
    config = _config(tmp_path, ["solution-a", "solution-b"], manifest_path, warmup_requests=0)

    first_adapters = {
        "solution-a": FakeAdapter("solution-a"),
        "solution-b": FakeAdapter("solution-b"),
    }
    first_summary = run_benchmark(
        config=config,
        manifest_path=manifest_path,
        manifest=[real_manifest[0]],
        adapters=first_adapters,
    )
    assert first_summary.completed_pairs == 2
    assert first_summary.skipped_pairs == 0

    second_adapters = {
        "solution-a": FakeAdapter("solution-a"),
        "solution-b": FakeAdapter("solution-b"),
    }
    second_summary = run_benchmark(
        config=config,
        manifest_path=manifest_path,
        manifest=real_manifest,
        adapters=second_adapters,
        run_id=first_summary.run_id,
    )

    assert second_summary.run_id == first_summary.run_id
    assert second_summary.total_pairs == 4
    assert second_summary.skipped_pairs == 2
    assert second_summary.completed_pairs == 2
    # Only the previously-incomplete sample's pairs made new adapter calls.
    assert len(second_adapters["solution-a"].calls) == 1
    assert len(second_adapters["solution-b"].calls) == 1

    all_results = read_results(second_summary.results_path)
    assert len(all_results) == 4


def test_run_benchmark_resume_does_not_rerun_a_solutions_completed_warmups(
    tmp_path: Path, manifest_path: Path
) -> None:
    """Resuming a run must not repeat warmups a solution already completed, even
    when other solutions or measured pairs still have work remaining."""
    from receipt_bench.evaluation.manifest import load_manifest

    real_manifest = load_manifest(manifest_path)
    config = _config(tmp_path, ["solution-a", "solution-b"], manifest_path, warmup_requests=2)

    first_adapters = {
        "solution-a": FakeAdapter("solution-a"),
        "solution-b": FakeAdapter("solution-b"),
    }
    first_summary = run_benchmark(
        config=config,
        manifest_path=manifest_path,
        manifest=[real_manifest[0]],
        adapters=first_adapters,
    )
    # 2 warmups + 1 measured call per solution on the first (partial) run.
    assert len(first_adapters["solution-a"].calls) == 2 + 1
    assert len(first_adapters["solution-b"].calls) == 2 + 1

    second_adapters = {
        "solution-a": FakeAdapter("solution-a"),
        "solution-b": FakeAdapter("solution-b"),
    }
    second_summary = run_benchmark(
        config=config,
        manifest_path=manifest_path,
        manifest=real_manifest,
        adapters=second_adapters,
        run_id=first_summary.run_id,
    )

    # No warmup calls at all on resume: only the still-incomplete measured pair.
    assert len(second_adapters["solution-a"].calls) == 1
    assert len(second_adapters["solution-b"].calls) == 1

    warmups_path = config.output_root / second_summary.run_id / "warmups.jsonl"
    warmups = read_warmups(warmups_path)
    assert len(warmups) == 4  # still exactly 2 per solution, never duplicated
    counts: dict[str, int] = {}
    for record in warmups:
        counts[record.solution_id] = counts.get(record.solution_id, 0) + 1
    assert counts == {"solution-a": 2, "solution-b": 2}


def test_run_benchmark_raises_on_orphaned_warmup_artifact_without_recorded_pair(
    tmp_path: Path, manifest_path: Path
) -> None:
    """A warmup artifact file present without a corresponding ``WarmupRecord`` is
    treated the same as an orphaned measured-pair artifact: refuse to silently
    overwrite it, and raise instead of guessing."""
    from receipt_bench.evaluation.manifest import load_manifest

    real_manifest = load_manifest(manifest_path)
    config = _config(tmp_path, ["solution-a"], manifest_path, warmup_requests=1)
    run_id = "20260101T000000Z"
    orphaned_artifact = config.output_root / run_id / "warmups" / "solution-a" / "0.json"
    orphaned_artifact.parent.mkdir(parents=True, exist_ok=True)
    orphaned_artifact.write_text("{}", encoding="utf-8")

    with pytest.raises(RunConfigurationError, match="warmup artifact"):
        run_benchmark(
            config=config,
            manifest_path=manifest_path,
            manifest=real_manifest,
            adapters={"solution-a": FakeAdapter("solution-a")},
            run_id=run_id,
        )


def test_run_benchmark_retries_provider_response_error_and_records_actual_attempts(
    tmp_path: Path, manifest_path: Path
) -> None:
    """A transient ``ProviderResponseError`` is retried (not immediately recorded as
    failed) up to ``config.retry.max_attempts``; a later success records the true
    number of attempts actually made, not just 1."""
    from receipt_bench.evaluation.manifest import load_manifest

    real_manifest = load_manifest(manifest_path)
    config = _config(tmp_path, ["solution-a"], manifest_path, warmup_requests=0)
    adapter = FakeAdapter(
        "solution-a",
        responses=[
            ProviderResponseError("upstream 500"),
            ProviderResponseError("upstream 500"),
            AdapterResponse(
                receipt=build_ground_truth_receipt(), raw_response={"ok": True}, components=[]
            ),
            AdapterResponse(
                receipt=build_ground_truth_receipt(), raw_response={"ok": True}, components=[]
            ),
        ],
    )

    summary = run_benchmark(
        config=config,
        manifest_path=manifest_path,
        manifest=real_manifest,
        adapters={"solution-a": adapter},
        sleep=_no_sleep,
    )

    results = {r.sample_id: r for r in read_results(summary.results_path)}
    assert results["sample-001"].status == "succeeded"
    assert results["sample-001"].attempts == 3
    assert results["sample-002"].status == "succeeded"
    assert results["sample-002"].attempts == 1


def test_run_benchmark_records_provider_response_error_as_failed_after_exhausting_retries(
    tmp_path: Path, manifest_path: Path
) -> None:
    """Once every one of ``max_attempts`` attempts raises ``ProviderResponseError``,
    the pair is recorded as ``status="failed"`` with ``attempts == max_attempts``."""
    from receipt_bench.evaluation.manifest import load_manifest

    real_manifest = load_manifest(manifest_path)
    retry = RetryConfig(max_attempts=3, initial_delay_seconds=1.0, max_delay_seconds=30.0)
    config = _config(tmp_path, ["solution-a"], manifest_path, warmup_requests=0, retry=retry)
    adapter = FakeAdapter(
        "solution-a",
        responses=[
            ProviderResponseError("upstream 500"),
            ProviderResponseError("upstream 500"),
            ProviderResponseError("upstream 500"),
            AdapterResponse(
                receipt=build_ground_truth_receipt(), raw_response={"ok": True}, components=[]
            ),
        ],
    )

    summary = run_benchmark(
        config=config,
        manifest_path=manifest_path,
        manifest=real_manifest,
        adapters={"solution-a": adapter},
        sleep=_no_sleep,
    )

    results = {r.sample_id: r for r in read_results(summary.results_path)}
    assert results["sample-001"].status == "failed"
    assert results["sample-001"].error_type == "ProviderResponseError"
    assert results["sample-001"].receipt is None
    assert results["sample-001"].attempts == 3
    assert results["sample-002"].status == "succeeded"
    assert results["sample-002"].attempts == 1


def test_run_benchmark_retry_then_success_preserves_ocr_partial_as_retry_overhead(
    tmp_path: Path, manifest_path: Path
) -> None:
    """An OCR-pipeline adapter may raise ``ProviderResponseError`` carrying the
    ``raw_response``/``components`` already incurred by a successful OCR call
    whose subsequent normalize stage failed transport-wise. Once a later
    attempt succeeds, that discarded attempt's real usage must not vanish: its
    components are folded into the ``SolutionOutput`` under a
    ``retry_overhead:<attempt>:`` prefix (so cost/per-component aggregation
    still sees them), and the raw artifact becomes an explicit attempt-indexed
    structure preserving every attempt's raw response verbatim."""
    from receipt_bench.evaluation.manifest import load_manifest

    real_manifest = load_manifest(manifest_path)
    retry = RetryConfig(max_attempts=3, initial_delay_seconds=1.0, max_delay_seconds=30.0)
    config = _config(tmp_path, ["solution-a"], manifest_path, warmup_requests=0, retry=retry)
    partial_ocr = ComponentMeasurement(
        component="ocr", latency_ms=80.0, usage=Usage(input_tokens=1_000)
    )
    adapter = FakeAdapter(
        "solution-a",
        responses=[
            ProviderResponseError(
                "normalize upstream 500",
                raw_response={"ocr": {"text": "partial"}},
                components=[partial_ocr],
            ),
            AdapterResponse(
                receipt=build_ground_truth_receipt(),
                raw_response={"ocr": {"text": "final"}, "normalize": {"ok": True}},
                components=[
                    ComponentMeasurement(
                        component="ocr", latency_ms=75.0, usage=Usage(input_tokens=1_200)
                    ),
                    ComponentMeasurement(
                        component="normalize", latency_ms=40.0, usage=Usage(input_tokens=500)
                    ),
                ],
            ),
            AdapterResponse(
                receipt=build_ground_truth_receipt(), raw_response={"ok": True}, components=[]
            ),
        ],
    )

    summary = run_benchmark(
        config=config,
        manifest_path=manifest_path,
        manifest=real_manifest,
        adapters={"solution-a": adapter},
        sleep=_no_sleep,
    )

    results = {r.sample_id: r for r in read_results(summary.results_path)}
    result = results["sample-001"]
    assert result.status == "succeeded"
    # Two full adapter attempts were made even though only the second succeeded.
    assert result.attempts == 2

    component_names = {c.component for c in result.components}
    assert component_names == {"retry_overhead:1:ocr", "ocr", "normalize"}
    retried = next(c for c in result.components if c.component == "retry_overhead:1:ocr")
    assert retried.usage.input_tokens == 1_000
    assert retried.latency_ms == 80.0
    final_ocr = next(c for c in result.components if c.component == "ocr")
    assert final_ocr.usage.input_tokens == 1_200
    final_normalize = next(c for c in result.components if c.component == "normalize")
    assert final_normalize.usage.input_tokens == 500

    artifact_payload = json.loads(Path(result.raw_artifact).read_text(encoding="utf-8"))
    attempts_payload = artifact_payload["attempts"]
    assert len(attempts_payload) == 2
    assert attempts_payload[0] == {
        "attempt": 1,
        "raw_response": {"ocr": {"text": "partial"}},
        "error_type": "ProviderResponseError",
        "error_message": "normalize upstream 500",
    }
    assert attempts_payload[1] == {
        "attempt": 2,
        "raw_response": {"ocr": {"text": "final"}, "normalize": {"ok": True}},
        "error_type": None,
        "error_message": None,
    }

    assert results["sample-002"].status == "succeeded"
    assert results["sample-002"].attempts == 1


def test_run_benchmark_retry_exhaustion_marks_every_ocr_partial_as_retry_overhead(
    tmp_path: Path, manifest_path: Path
) -> None:
    """When every attempt (including the last) raises ``ProviderResponseError``
    with partial OCR components, there is no deciding/measured attempt: all
    attempts' components must be tagged ``retry_overhead:<attempt>:`` (none
    should be counted as measured, primary latency), while every attempt's raw
    response is still preserved verbatim in the attempt-indexed artifact."""
    from receipt_bench.evaluation.manifest import load_manifest

    real_manifest = load_manifest(manifest_path)
    retry = RetryConfig(max_attempts=2, initial_delay_seconds=1.0, max_delay_seconds=30.0)
    config = _config(tmp_path, ["solution-a"], manifest_path, warmup_requests=0, retry=retry)
    adapter = FakeAdapter(
        "solution-a",
        responses=[
            ProviderResponseError(
                "normalize upstream 500",
                raw_response={"ocr": {"text": "attempt-1"}},
                components=[
                    ComponentMeasurement(
                        component="ocr", latency_ms=80.0, usage=Usage(input_tokens=1_000)
                    )
                ],
            ),
            ProviderResponseError(
                "normalize upstream 502",
                raw_response={"ocr": {"text": "attempt-2"}},
                components=[
                    ComponentMeasurement(
                        component="ocr", latency_ms=82.0, usage=Usage(input_tokens=1_100)
                    )
                ],
            ),
            AdapterResponse(
                receipt=build_ground_truth_receipt(), raw_response={"ok": True}, components=[]
            ),
        ],
    )

    summary = run_benchmark(
        config=config,
        manifest_path=manifest_path,
        manifest=real_manifest,
        adapters={"solution-a": adapter},
        sleep=_no_sleep,
    )

    results = {r.sample_id: r for r in read_results(summary.results_path)}
    result = results["sample-001"]
    assert result.status == "failed"
    assert result.error_type == "ProviderResponseError"
    assert result.error_message == "normalize upstream 502"
    assert result.attempts == 2

    # No attempt succeeded, so every attempt's components are retry overhead --
    # none may be mistaken for a measured, primary-latency component.
    component_names = {c.component for c in result.components}
    assert component_names == {"retry_overhead:1:ocr", "retry_overhead:2:ocr"}

    artifact_payload = json.loads(Path(result.raw_artifact).read_text(encoding="utf-8"))
    attempts_payload = artifact_payload["attempts"]
    assert len(attempts_payload) == 2
    assert attempts_payload[0] == {
        "attempt": 1,
        "raw_response": {"ocr": {"text": "attempt-1"}},
        "error_type": "ProviderResponseError",
        "error_message": "normalize upstream 500",
    }
    assert attempts_payload[1] == {
        "attempt": 2,
        "raw_response": {"ocr": {"text": "attempt-2"}},
        "error_type": "ProviderResponseError",
        "error_message": "normalize upstream 502",
    }

    assert results["sample-002"].status == "succeeded"
    assert results["sample-002"].attempts == 1


def test_run_benchmark_retry_uses_exponential_backoff_capped_at_max_delay(
    tmp_path: Path, manifest_path: Path
) -> None:
    from receipt_bench.evaluation.manifest import load_manifest

    real_manifest = load_manifest(manifest_path)
    retry = RetryConfig(max_attempts=4, initial_delay_seconds=1.0, max_delay_seconds=3.0)
    config = _config(tmp_path, ["solution-a"], manifest_path, warmup_requests=0, retry=retry)
    adapter = FakeAdapter(
        "solution-a",
        responses=[
            ProviderResponseError("upstream 500"),
            ProviderResponseError("upstream 500"),
            ProviderResponseError("upstream 500"),
            AdapterResponse(
                receipt=build_ground_truth_receipt(), raw_response={"ok": True}, components=[]
            ),
            AdapterResponse(
                receipt=build_ground_truth_receipt(), raw_response={"ok": True}, components=[]
            ),
        ],
    )
    delays: list[float] = []

    run_benchmark(
        config=config,
        manifest_path=manifest_path,
        manifest=real_manifest,
        adapters={"solution-a": adapter},
        sleep=delays.append,
    )

    # 1.0, then 2.0 (doubling), then capped at max_delay_seconds=3.0 instead of 4.0.
    assert delays == [1.0, 2.0, 3.0]


def test_run_benchmark_records_invalid_output_distinctly_from_provider_failure(
    tmp_path: Path, manifest_path: Path
) -> None:
    from receipt_bench.evaluation.manifest import load_manifest

    real_manifest = load_manifest(manifest_path)
    config = _config(tmp_path, ["solution-a"], manifest_path, warmup_requests=0)
    adapter = FakeAdapter(
        "solution-a",
        responses=[
            ValueError("could not parse JSON body"),
            AdapterResponse(
                receipt=build_ground_truth_receipt(), raw_response={"ok": True}, components=[]
            ),
        ],
    )

    summary = run_benchmark(
        config=config,
        manifest_path=manifest_path,
        manifest=real_manifest,
        adapters={"solution-a": adapter},
    )

    results = {r.sample_id: r for r in read_results(summary.results_path)}
    assert results["sample-001"].status == "invalid"
    assert results["sample-001"].error_type == "ValueError"
    # Semantic parsing failures are never retried, even though config.retry allows it.
    assert results["sample-001"].attempts == 1


def test_run_benchmark_preserves_raw_response_and_components_for_invalid_solution_output(
    tmp_path: Path, manifest_path: Path
) -> None:
    """``InvalidSolutionOutput`` carries the raw response and component measurements
    from a call that transport-succeeded but could not be parsed/validated; these must
    be written to the (immutable) raw artifact and preserved on the recorded
    ``SolutionOutput`` instead of being discarded, and the pair must not be retried."""
    import json

    from receipt_bench.evaluation.manifest import load_manifest

    real_manifest = load_manifest(manifest_path)
    config = _config(tmp_path, ["solution-a"], manifest_path, warmup_requests=0)
    raw_response = {"choices": [{"message": {"content": "not-json"}}]}
    components = [
        ComponentMeasurement(
            component="extraction",
            latency_ms=87.0,
            usage=Usage(input_tokens=120, output_tokens=10, total_tokens=130),
            estimated_cost=0.004,
        )
    ]
    adapter = FakeAdapter(
        "solution-a",
        responses=[
            InvalidSolutionOutput(
                "could not parse model output into CanonicalReceipt",
                raw_response=raw_response,
                components=components,
            ),
            AdapterResponse(
                receipt=build_ground_truth_receipt(), raw_response={"ok": True}, components=[]
            ),
        ],
    )

    summary = run_benchmark(
        config=config,
        manifest_path=manifest_path,
        manifest=real_manifest,
        adapters={"solution-a": adapter},
    )

    results = {r.sample_id: r for r in read_results(summary.results_path)}
    invalid_result = results["sample-001"]
    assert invalid_result.status == "invalid"
    assert invalid_result.error_type == "InvalidSolutionOutput"
    assert invalid_result.receipt is None
    assert invalid_result.attempts == 1
    assert [component.component for component in invalid_result.components] == ["extraction"]
    assert invalid_result.components[0].usage.input_tokens == 120

    artifact_payload = json.loads(Path(invalid_result.raw_artifact).read_text(encoding="utf-8"))
    assert artifact_payload == raw_response
    # Only one call was made for the invalid pair: no retry for a semantic failure.
    assert len(adapter.calls) == 2


def test_run_benchmark_warmup_provider_error_is_not_retried_and_propagates(
    tmp_path: Path, manifest_path: Path
) -> None:
    from receipt_bench.evaluation.manifest import load_manifest

    real_manifest = load_manifest(manifest_path)
    config = _config(tmp_path, ["solution-a"], manifest_path, warmup_requests=1)
    adapter = FakeAdapter("solution-a", responses=[ProviderResponseError("warmup upstream 500")])

    with pytest.raises(ProviderResponseError, match="warmup upstream 500"):
        run_benchmark(
            config=config,
            manifest_path=manifest_path,
            manifest=real_manifest,
            adapters={"solution-a": adapter},
        )
    # Exactly one call: the failed warmup was never retried.
    assert len(adapter.calls) == 1
    # A failed warmup leaves no persisted record: the aborting exception itself is
    # the visible signal, not a silently-dropped partial artifact.
    assert not (config.output_root).exists() or not any((config.output_root).rglob("warmups.jsonl"))


def test_run_benchmark_records_pydantic_validation_error_as_invalid(
    tmp_path: Path, manifest_path: Path
) -> None:
    from receipt_bench.evaluation.manifest import load_manifest
    from receipt_bench.models import CanonicalReceipt

    real_manifest = load_manifest(manifest_path)
    config = _config(tmp_path, ["solution-a"], manifest_path, warmup_requests=0)

    validation_error: ValidationError
    try:
        CanonicalReceipt.model_validate({"schema_version": "1.0", "amounts": {"subtotal": "n/a"}})
    except ValidationError as exc:
        validation_error = exc

    adapter = FakeAdapter("solution-a", responses=[validation_error, validation_error])

    summary = run_benchmark(
        config=config,
        manifest_path=manifest_path,
        manifest=real_manifest,
        adapters={"solution-a": adapter},
    )

    results = read_results(summary.results_path)
    assert all(result.status == "invalid" for result in results)
    assert all(result.attempts == 1 for result in results)
    assert all(result.error_type == "ValidationError" for result in results)


def test_run_benchmark_raises_for_empty_manifest(tmp_path: Path, manifest_path: Path) -> None:
    config = _config(tmp_path, ["solution-a"], manifest_path)
    with pytest.raises(RunConfigurationError, match="no samples"):
        run_benchmark(
            config=config,
            manifest_path=manifest_path,
            manifest=[],
            adapters={"solution-a": FakeAdapter("solution-a")},
        )


def test_run_benchmark_raises_for_missing_adapter_coverage(
    tmp_path: Path, manifest_path: Path
) -> None:
    from receipt_bench.evaluation.manifest import load_manifest

    real_manifest = load_manifest(manifest_path)
    config = _config(tmp_path, ["solution-a", "solution-b"], manifest_path)
    with pytest.raises(RunConfigurationError, match="solution-b"):
        run_benchmark(
            config=config,
            manifest_path=manifest_path,
            manifest=real_manifest,
            adapters={"solution-a": FakeAdapter("solution-a")},
        )


def test_run_benchmark_raises_image_integrity_error_on_checksum_mismatch(
    tmp_path: Path, manifest_path: Path
) -> None:
    from receipt_bench.evaluation.manifest import load_manifest

    real_manifest = load_manifest(manifest_path)
    # Corrupt the first sample's image bytes on disk after the manifest recorded its hash.
    image_path = manifest_path.parent / real_manifest[0].image_path
    image_path.write_bytes(b"corrupted-bytes")

    config = _config(tmp_path, ["solution-a"], manifest_path, warmup_requests=0)
    with pytest.raises(ImageIntegrityError, match="checksum mismatch"):
        run_benchmark(
            config=config,
            manifest_path=manifest_path,
            manifest=real_manifest,
            adapters={"solution-a": FakeAdapter("solution-a")},
        )


def test_run_benchmark_refuses_to_overwrite_existing_artifact_for_incomplete_pair(
    tmp_path: Path, manifest_path: Path
) -> None:
    from receipt_bench.artifacts import safe_segment
    from receipt_bench.evaluation.manifest import load_manifest

    real_manifest = load_manifest(manifest_path)
    config = _config(tmp_path, ["solution-a"], manifest_path, warmup_requests=0)
    run_id = "20260101T000000Z"

    artifact_path = (
        config.output_root
        / safe_segment(run_id)
        / "raw"
        / safe_segment("solution-a")
        / f"{safe_segment('sample-001')}.json"
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("{}", encoding="utf-8")

    with pytest.raises(RunConfigurationError, match="immutable"):
        run_benchmark(
            config=config,
            manifest_path=manifest_path,
            manifest=real_manifest,
            adapters={"solution-a": FakeAdapter("solution-a")},
            run_id=run_id,
        )
