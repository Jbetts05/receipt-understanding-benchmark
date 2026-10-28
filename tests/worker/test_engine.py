from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from receipt_bench.storage.local import LocalStorage
from receipt_bench.storage.models import (
    ArtifactInfo,
    EventRecord,
    QueueLease,
    RunRecord,
    TaskCompletion,
    TaskMessage,
    TaskRecord,
    TerminalOutcome,
    Versioned,
)
from receipt_bench.storage.paths import (
    result_execution_attempt,
    result_parse_attempt,
    result_raw,
    result_telemetry,
    run_manifest,
    sample_image,
)
from receipt_bench.storage.protocols import (
    ArtifactRepository,
    EventRepository,
    LeaseLostError,
    RunRepository,
    TaskRepository,
)
from receipt_bench.storage.validation import JsonObject, JsonValue
from receipt_bench.worker import (
    DurableWorker,
    ExecutionError,
    FinalizationOutput,
    InvalidResultError,
    ParsedExecution,
    RawExecution,
    WorkerConfig,
)
from receipt_bench.worker.engine import _ExecutionEnvelope, _telemetry_result_view


def _run(total: int) -> RunRecord:
    now = datetime.now(UTC)
    return RunRecord(
        project_id="cord-v2",
        run_id="run-001",
        manifest_artifact_path=run_manifest("run-001"),
        total_tasks=total,
        pending_tasks=total,
        created_at=now,
        updated_at=now,
        metadata={"dispatch_complete": True},
    )


def _task(sample_id: str, *, provider: str = "azure-openai") -> TaskRecord:
    now = datetime.now(UTC)
    return TaskRecord(
        project_id="cord-v2",
        run_id="run-001",
        sample_id=sample_id,
        solution_id="solution-one",
        image_artifact_path=sample_image("cord-v2", sample_id),
        media_type="image/jpeg",
        provider_keys=(provider,),
        created_at=now,
        updated_at=now,
    )


def test_multi_attempt_cost_is_unknown_if_any_component_is_unpriced() -> None:
    task = _task("sample-cost")
    envelopes = [
        _ExecutionEnvelope(
            attempt=1,
            raw={"attempt": 1},
            telemetry={
                "components": [
                    {
                        "component": "provider",
                        "latency_ms": 10.0,
                        "estimated_cost_usd": 0.1,
                    }
                ],
                "latency_ms": 10.0,
                "estimated_cost_usd": 0.1,
            },
        ),
        _ExecutionEnvelope(
            attempt=2,
            raw={"attempt": 2},
            telemetry={
                "components": [
                    {
                        "component": "provider",
                        "latency_ms": 20.0,
                        "estimated_cost_usd": None,
                    }
                ],
                "latency_ms": 20.0,
                "estimated_cost_usd": 0.2,
            },
        ),
    ]

    telemetry = _telemetry_result_view(task, envelopes)

    assert telemetry["estimated_cost_usd"] is None


def test_execute_failure_without_billable_usage_is_estimated_as_zero_cost() -> None:
    telemetry = _telemetry_result_view(
        _task("sample-no-usage"),
        [
            _ExecutionEnvelope(
                attempt=1,
                raw=None,
                telemetry={},
            )
        ],
    )

    assert telemetry["estimated_cost_usd"] == 0.0


async def _prepare(storage: LocalStorage, tasks: list[TaskRecord]) -> None:
    await storage.artifacts.create_json(run_manifest("run-001"), {"run_id": "run-001"})
    await storage.runs.create(_run(len(tasks)))
    for task in tasks:
        await storage.artifacts.create_bytes(
            task.image_artifact_path,
            f"image:{task.sample_id}".encode(),
            content_type="image/jpeg",
        )
        await storage.tasks.create(task)


class StaticExecutor:
    def __init__(self, storage: LocalStorage | None = None) -> None:
        self.storage = storage
        self.execute_calls = 0
        self.parse_calls = 0

    async def execute(
        self,
        task: TaskRecord,
        image_bytes: bytes,
        media_type: str,
    ) -> RawExecution:
        self.execute_calls += 1
        assert image_bytes == f"image:{task.sample_id}".encode()
        assert media_type == "image/jpeg"
        return RawExecution(
            raw={"provider": "ok", "sample_id": task.sample_id},
            telemetry={"latency_ms": 12.5},
        )

    async def parse(self, task: TaskRecord, raw: object) -> ParsedExecution:
        self.parse_calls += 1
        if self.storage is not None:
            assert await self.storage.artifacts.exists(
                result_raw(task.run_id, task.sample_id, task.solution_id)
            )
        return ParsedExecution(
            parsed={"answer": task.sample_id},
            comparison={"exact": True},
        )


class CountingFinalizer:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(
        self,
        run: RunRecord,
        tasks: tuple[TaskRecord, ...],
    ) -> FinalizationOutput:
        self.calls += 1
        return FinalizationOutput(
            aggregate={
                "run_id": run.run_id,
                "terminal": sum(task.terminal for task in tasks),
            },
            download_bytes=b"deterministic-bundle",
        )


class DuplicateQueue:
    def __init__(self, message: TaskMessage) -> None:
        now = datetime.now(UTC)
        self.leases = [
            QueueLease(
                message_id="message-one",
                pop_receipt="receipt-one",
                message=message,
                dequeue_count=1,
                lease_until=now + timedelta(seconds=30),
            ),
            QueueLease(
                message_id="message-two",
                pop_receipt="receipt-two",
                message=message,
                dequeue_count=1,
                lease_until=now + timedelta(seconds=30),
            ),
        ]
        self.completed: list[str] = []
        self.released: list[str] = []
        self.poisoned: list[str] = []

    async def send(self, message: TaskMessage) -> str:
        del message
        return "message-three"

    async def receive(
        self,
        *,
        max_messages: int,
        visibility_timeout: int,
    ) -> list[QueueLease]:
        del visibility_timeout
        result, self.leases = self.leases[:max_messages], self.leases[max_messages:]
        return result

    async def renew(self, lease: QueueLease, *, visibility_timeout: int) -> QueueLease:
        return replace(
            lease,
            pop_receipt=f"{lease.pop_receipt}-renewed",
            lease_until=datetime.now(UTC) + timedelta(seconds=visibility_timeout),
        )

    async def complete(self, lease: QueueLease) -> None:
        self.completed.append(lease.message_id)

    async def release(self, lease: QueueLease, *, delay: int = 0) -> None:
        del delay
        self.released.append(lease.message_id)

    async def poison(self, lease: QueueLease, *, reason_code: str) -> None:
        del reason_code
        self.poisoned.append(lease.message_id)

    async def send_poison(
        self,
        lease: QueueLease,
        *,
        reason_code: str,
        outbox_id: str,
    ) -> str:
        del reason_code, outbox_id
        self.poisoned.append(lease.message_id)
        return "poison-message"


class SlowExecutor(StaticExecutor):
    async def execute(
        self,
        task: TaskRecord,
        image_bytes: bytes,
        media_type: str,
    ) -> RawExecution:
        await asyncio.sleep(0.12)
        return await super().execute(task, image_bytes, media_type)


class RecordingQueue:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate
        self.renewals = 0
        self.renewed_pairs: list[str] = []
        self.send_count = 0

    async def send(self, message: TaskMessage) -> str:
        self.send_count += 1
        return await self.delegate.send(message)  # type: ignore[attr-defined,no-any-return]

    async def receive(
        self,
        *,
        max_messages: int,
        visibility_timeout: int,
    ) -> list[QueueLease]:
        return await self.delegate.receive(  # type: ignore[attr-defined,no-any-return]
            max_messages=max_messages,
            visibility_timeout=visibility_timeout,
        )

    async def renew(self, lease: QueueLease, *, visibility_timeout: int) -> QueueLease:
        self.renewals += 1
        self.renewed_pairs.append(lease.message.pair_id)
        return await self.delegate.renew(  # type: ignore[attr-defined,no-any-return]
            lease,
            visibility_timeout=visibility_timeout,
        )

    async def complete(self, lease: QueueLease) -> None:
        await self.delegate.complete(lease)  # type: ignore[attr-defined]

    async def release(self, lease: QueueLease, *, delay: int = 0) -> None:
        await self.delegate.release(lease, delay=delay)  # type: ignore[attr-defined]

    async def poison(self, lease: QueueLease, *, reason_code: str) -> None:
        await self.delegate.poison(lease, reason_code=reason_code)  # type: ignore[attr-defined]

    async def send_poison(
        self,
        lease: QueueLease,
        *,
        reason_code: str,
        outbox_id: str,
    ) -> str:
        return await self.delegate.send_poison(  # type: ignore[attr-defined,no-any-return]
            lease,
            reason_code=reason_code,
            outbox_id=outbox_id,
        )


def test_concurrent_duplicate_delivery_executes_pair_once_and_finalizes_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        queue = DuplicateQueue(
            TaskMessage(
                project_id=task.project_id,
                run_id=task.run_id,
                sample_id=task.sample_id,
                solution_id=task.solution_id,
            )
        )
        executor = SlowExecutor(storage)
        finalizer = CountingFinalizer()
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=queue,
            executor=executor,
            finalizer=finalizer,
            config=WorkerConfig(
                concurrency=2,
                solution_concurrency=2,
                visibility_timeout=10,
                lease_renewal_interval=2,
            ),
            worker_id="worker-one",
        )
        assert await worker.run_once() == 2
        assert executor.execute_calls == 1
        assert executor.parse_calls == 1
        assert finalizer.calls == 1
        run = await storage.runs.get("cord-v2", "run-001")
        assert run.value.pending_tasks == 0
        assert run.value.succeeded_tasks == 1
        assert run.value.finalizer_completed
        assert len(queue.completed) == 1
        assert len(queue.released) == 1

    asyncio.run(scenario())


def test_long_provider_call_renews_visibility_and_task_claim(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        queue = RecordingQueue(storage.queue)
        executor = SlowExecutor(storage)
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=queue,
            executor=executor,
            config=WorkerConfig(
                concurrency=1,
                visibility_timeout=2,
                lease_renewal_interval=0.03,
            ),
            worker_id="worker-one",
        )
        await worker.enqueue([task])
        assert await worker.run_once() == 1
        assert queue.renewals >= 2
        stored = await storage.tasks.get(task.run_id, task.pair_id)
        assert stored.value.status == "succeeded"

    asyncio.run(scenario())


def test_raw_artifact_is_persisted_before_parse_and_invalid_is_terminal(
    tmp_path: Path,
) -> None:
    class InvalidExecutor(StaticExecutor):
        async def parse(self, task: TaskRecord, raw: object) -> ParsedExecution:
            assert self.storage is not None
            assert await self.storage.artifacts.exists(
                result_raw(task.run_id, task.sample_id, task.solution_id)
            )
            raise InvalidResultError()

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        executor = InvalidExecutor(storage)
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=executor,
            config=WorkerConfig(
                concurrency=1,
                visibility_timeout=10,
                lease_renewal_interval=2,
            ),
        )
        await worker.enqueue([task])
        await worker.run_once()
        stored = await storage.tasks.get(task.run_id, task.pair_id)
        assert stored.value.status == "invalid"
        assert stored.value.raw_artifact_path is not None
        run = await storage.runs.get(task.project_id, task.run_id)
        assert run.value.invalid_tasks == 1
        assert run.value.pending_tasks == 0

    asyncio.run(scenario())


def test_failure_intent_survives_crash_after_error_artifact_write(tmp_path: Path) -> None:
    class CrashAfterFailureRaw:
        def __init__(self, delegate: ArtifactRepository) -> None:
            self.delegate = delegate
            self.failed = False

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        async def create_json(
            self,
            path: str,
            value: JsonValue,
            *,
            metadata: JsonObject | None = None,
        ) -> ArtifactInfo:
            created = await self.delegate.create_json(path, value, metadata=metadata)
            if path.endswith("/raw.json") and not self.failed:
                self.failed = True
                raise RuntimeError("injected crash after failure raw write")
            return created

    class NonretryableExecutor(StaticExecutor):
        async def execute(
            self,
            task: TaskRecord,
            image_bytes: bytes,
            media_type: str,
        ) -> RawExecution:
            del task, image_bytes, media_type
            self.execute_calls += 1
            raise ExecutionError(
                "provider_rejected",
                "The provider rejected the request.",
                retryable=False,
                raw={"error": {"code": "provider_rejected"}},
            )

        async def parse(self, task: TaskRecord, raw: object) -> ParsedExecution:
            del task, raw
            self.parse_calls += 1
            raise AssertionError("A durable failure artifact must never be parsed")

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        artifacts = CrashAfterFailureRaw(storage.artifacts)
        executor = NonretryableExecutor()
        worker = DurableWorker(
            artifacts=artifacts,  # type: ignore[arg-type]
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=executor,
            config=WorkerConfig(retry_delay=0),
        )
        await worker.enqueue([task])
        await worker.run_once()

        interrupted = await storage.tasks.get(task.run_id, task.pair_id)
        assert interrupted.value.terminal_intent_outcome == "failed"
        assert not interrupted.value.terminal
        assert await storage.artifacts.get_json(
            result_raw(task.run_id, task.sample_id, task.solution_id)
        ) == {"error": {"code": "provider_rejected"}}

        await worker.run_once()
        terminal = await storage.tasks.get(task.run_id, task.pair_id)
        assert terminal.value.status == "failed"
        assert terminal.value.error_code == "provider_rejected"
        assert executor.execute_calls == 1
        assert executor.parse_calls == 0

    asyncio.run(scenario())


def test_unpaired_surrogate_output_is_terminal_without_retry(tmp_path: Path) -> None:
    class MalformedExecutor(StaticExecutor):
        async def execute(
            self,
            task: TaskRecord,
            image_bytes: bytes,
            media_type: str,
        ) -> RawExecution:
            del task, image_bytes, media_type
            self.execute_calls += 1
            return RawExecution(raw={"malformed": "\ud800"})

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        executor = MalformedExecutor()
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=executor,
            config=WorkerConfig(max_dequeue_count=3),
        )
        await worker.enqueue([task])
        await worker.run_once()

        terminal = await storage.tasks.get(task.run_id, task.pair_id)
        assert terminal.value.status == "invalid"
        assert terminal.value.error_code == "unsafe_raw_output"
        assert terminal.value.attempts == 1
        assert not terminal.value.poisoned
        assert executor.execute_calls == 1

    asyncio.run(scenario())


def test_retry_exhaustion_writes_poison_terminal_and_decrements_once(
    tmp_path: Path,
) -> None:
    class FailingExecutor(StaticExecutor):
        async def execute(
            self,
            task: TaskRecord,
            image_bytes: bytes,
            media_type: str,
        ) -> RawExecution:
            del task, image_bytes, media_type
            self.execute_calls += 1
            raise ExecutionError(
                "provider_unavailable",
                "The provider did not complete the request.",
                retryable=True,
            )

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        executor = FailingExecutor()
        finalizer = CountingFinalizer()
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=executor,
            finalizer=finalizer,
            config=WorkerConfig(
                concurrency=1,
                visibility_timeout=10,
                lease_renewal_interval=2,
                max_dequeue_count=2,
                retry_delay=0,
            ),
        )
        await worker.enqueue([task])
        assert await worker.run_once() == 1
        assert await worker.run_once() == 1
        assert await worker.run_once() == 1

        stored = await storage.tasks.get(task.run_id, task.pair_id)
        assert stored.value.status == "failed"
        assert stored.value.poisoned
        assert stored.value.error_code == "retry_exhausted"
        run = await storage.runs.get(task.project_id, task.run_id)
        assert run.value.pending_tasks == 0
        assert run.value.failed_tasks == 1
        assert run.value.poisoned_tasks == 1
        assert executor.execute_calls == 2
        assert finalizer.calls == 1
        assert len(await storage.queue.poison_messages()) == 1

        restarted = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=executor,
            finalizer=finalizer,
        )
        await restarted.resume()
        run = await storage.runs.get(task.project_id, task.run_id)
        assert run.value.failed_tasks == 1
        assert finalizer.calls == 1

    asyncio.run(scenario())


def test_execution_attempt_evidence_survives_retry_then_success(tmp_path: Path) -> None:
    class RetryThenSucceed(StaticExecutor):
        async def execute(
            self,
            task: TaskRecord,
            image_bytes: bytes,
            media_type: str,
        ) -> RawExecution:
            del image_bytes, media_type
            self.execute_calls += 1
            component: JsonObject = {
                "component": "provider",
                "latency_ms": float(self.execute_calls),
                "usage": {"total_tokens": self.execute_calls},
                "estimated_cost_usd": self.execute_calls / 10,
            }
            telemetry: JsonObject = {
                "components": [component],
                "total_tokens": self.execute_calls,
                "latency_ms": float(self.execute_calls),
                "estimated_cost_usd": self.execute_calls / 10,
            }
            if self.execute_calls == 1:
                raise ExecutionError(
                    "provider_busy",
                    "The provider is temporarily busy.",
                    retryable=True,
                    raw={"partial": task.sample_id},
                    telemetry=telemetry,
                )
            return RawExecution(
                raw={"answer": task.sample_id},
                telemetry=telemetry,
            )

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        executor = RetryThenSucceed()
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=executor,
            config=WorkerConfig(max_dequeue_count=2, retry_delay=0),
        )
        await worker.enqueue([task])
        assert await worker.run_once() == 1
        assert await worker.run_once() == 1

        first = await storage.artifacts.get_json(
            result_execution_attempt(task.run_id, task.sample_id, task.solution_id, 1)
        )
        second = await storage.artifacts.get_json(
            result_execution_attempt(task.run_id, task.sample_id, task.solution_id, 2)
        )
        assert isinstance(first, dict)
        assert first["raw"] == {"partial": task.sample_id}
        first_telemetry = first["telemetry"]
        assert isinstance(first_telemetry, dict)
        assert first_telemetry["total_tokens"] == 1
        assert first["error"] == {
            "code": "provider_busy",
            "message": "The provider is temporarily busy.",
            "retryable": True,
            "status": "failed",
        }
        assert isinstance(second, dict)
        assert second["raw"] == {"answer": task.sample_id}
        assert second["error"] is None

        terminal = await storage.tasks.get(task.run_id, task.pair_id)
        assert terminal.value.status == "succeeded"
        final_raw = await storage.artifacts.get_json(
            terminal.value.raw_artifact_path or ""
        )
        assert isinstance(final_raw, dict)
        final_attempts = final_raw["attempts"]
        assert isinstance(final_attempts, list)
        assert len(final_attempts) == 2
        assert isinstance(final_attempts[0], dict)
        assert final_attempts[0]["raw"] == {"partial": task.sample_id}
        assert isinstance(final_attempts[1], dict)
        assert final_attempts[1]["raw"] == {"answer": task.sample_id}
        telemetry = await storage.artifacts.get_json(
            terminal.value.telemetry_artifact_path or ""
        )
        assert isinstance(telemetry, dict)
        assert telemetry["attempts"] == 2
        assert telemetry["total_tokens"] == 3
        assert telemetry["latency_ms"] == 3.0
        assert telemetry["estimated_cost_usd"] == pytest.approx(0.3)
        components = telemetry["components"]
        assert isinstance(components, list)
        first_component, second_component = components
        assert isinstance(first_component, dict)
        assert isinstance(second_component, dict)
        assert first_component["component"] == "retry_overhead:1:provider"
        assert first_component["attempt"] == 1
        assert second_component["component"] == "provider"
        assert second_component["attempt"] == 2

    asyncio.run(scenario())


def test_retry_exhaustion_preserves_every_execution_envelope(tmp_path: Path) -> None:
    class BillableFailure(StaticExecutor):
        async def execute(
            self,
            task: TaskRecord,
            image_bytes: bytes,
            media_type: str,
        ) -> RawExecution:
            del task, image_bytes, media_type
            self.execute_calls += 1
            raise ExecutionError(
                "provider_busy",
                "The provider is temporarily busy.",
                retryable=True,
                raw={"attempt": self.execute_calls, "partial": True},
                telemetry={
                    "components": [
                        {
                            "component": "provider",
                            "latency_ms": 4.0,
                            "usage": {"total_tokens": 5},
                            "estimated_cost_usd": 0.25,
                        }
                    ],
                    "total_tokens": 5,
                    "latency_ms": 4.0,
                    "estimated_cost_usd": 0.25,
                },
            )

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        executor = BillableFailure()
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=executor,
            config=WorkerConfig(max_dequeue_count=2, retry_delay=0),
        )
        await worker.enqueue([task])
        for _ in range(3):
            assert await worker.run_once() == 1

        for attempt in (1, 2):
            envelope = await storage.artifacts.get_json(
                result_execution_attempt(
                    task.run_id,
                    task.sample_id,
                    task.solution_id,
                    attempt,
                )
            )
            assert isinstance(envelope, dict)
            assert envelope["raw"] == {"attempt": attempt, "partial": True}
            attempt_telemetry = envelope["telemetry"]
            assert isinstance(attempt_telemetry, dict)
            assert attempt_telemetry["estimated_cost_usd"] == 0.25

        terminal = await storage.tasks.get(task.run_id, task.pair_id)
        assert terminal.value.status == "failed"
        assert terminal.value.error_code == "retry_exhausted"
        raw = await storage.artifacts.get_json(terminal.value.raw_artifact_path or "")
        assert isinstance(raw, dict)
        raw_attempts = raw["attempts"]
        assert isinstance(raw_attempts, list)
        assert all(isinstance(item, dict) for item in raw_attempts)
        assert [
            item["attempt"] for item in raw_attempts if isinstance(item, dict)
        ] == [1, 2]
        telemetry = await storage.artifacts.get_json(
            terminal.value.telemetry_artifact_path or ""
        )
        assert isinstance(telemetry, dict)
        assert telemetry["total_tokens"] == 10
        assert telemetry["latency_ms"] == 8.0
        assert telemetry["estimated_cost_usd"] == 0.5
        execution_attempts = telemetry["execution_attempts"]
        assert isinstance(execution_attempts, list)
        assert len(execution_attempts) == 2

    asyncio.run(scenario())


def test_crash_after_derived_raw_recovers_atomic_execution_envelope(
    tmp_path: Path,
) -> None:
    class CrashAfterDerivedRaw:
        def __init__(self, delegate: ArtifactRepository) -> None:
            self.delegate = delegate
            self.failed = False

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        async def create_json(
            self,
            path: str,
            value: JsonValue,
            *,
            metadata: JsonObject | None = None,
        ) -> ArtifactInfo:
            created = await self.delegate.create_json(path, value, metadata=metadata)
            if path.endswith("/raw.json") and not self.failed:
                self.failed = True
                raise RuntimeError("injected crash between derived result writes")
            return created

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        executor = StaticExecutor()
        worker = DurableWorker(
            artifacts=CrashAfterDerivedRaw(storage.artifacts),  # type: ignore[arg-type]
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=executor,
            config=WorkerConfig(max_dequeue_count=2, retry_delay=0),
        )
        await worker.enqueue([task])
        assert await worker.run_once() == 1
        assert executor.execute_calls == 1
        assert executor.parse_calls == 0
        assert not await storage.artifacts.exists(
            result_telemetry(task.run_id, task.sample_id, task.solution_id)
        )

        envelope = await storage.artifacts.get_json(
            result_execution_attempt(task.run_id, task.sample_id, task.solution_id, 1)
        )
        assert isinstance(envelope, dict)
        assert envelope["raw"] == {
            "provider": "ok",
            "sample_id": task.sample_id,
        }
        assert envelope["telemetry"] == {"latency_ms": 12.5}
        assert envelope["error"] is None

        restarted = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=executor,
            config=WorkerConfig(max_dequeue_count=2, retry_delay=0),
        )
        assert await restarted.run_once() == 1
        terminal = await storage.tasks.get(task.run_id, task.pair_id)
        assert terminal.value.status == "succeeded"
        assert executor.execute_calls == 1
        assert executor.parse_calls == 1
        assert await storage.artifacts.get_json(
            terminal.value.telemetry_artifact_path or ""
        ) == {"latency_ms": 12.5, "estimated_cost_usd": 0.0}

    asyncio.run(scenario())


def test_changed_parser_retry_uses_fenced_candidates_until_atomic_finish(
    tmp_path: Path,
) -> None:
    class ChangingParser(StaticExecutor):
        async def parse(self, task: TaskRecord, raw: object) -> ParsedExecution:
            del task, raw
            self.parse_calls += 1
            return ParsedExecution(
                parsed={"parser_version": self.parse_calls},
                comparison={"parser_version": self.parse_calls},
            )

    class CrashFirstFinish:
        def __init__(self, delegate: TaskRepository) -> None:
            self.delegate = delegate
            self.failed = False
            self.candidates: list[tuple[str, str, str | None]] = []

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        async def finish(
            self,
            run_id: str,
            pair_id: str,
            *,
            owner: str | None,
            outcome: TerminalOutcome,
            raw_artifact_path: str,
            parsed_artifact_path: str,
            telemetry_artifact_path: str,
            comparison_artifact_path: str | None = None,
            error_code: str | None = None,
            error_message: str | None = None,
            poisoned: bool = False,
        ) -> TaskCompletion:
            self.candidates.append(
                (
                    parsed_artifact_path,
                    telemetry_artifact_path,
                    comparison_artifact_path,
                )
            )
            if outcome == "succeeded" and not self.failed:
                self.failed = True
                raise RuntimeError("injected crash before atomic task completion")
            return await self.delegate.finish(
                run_id,
                pair_id,
                owner=owner,
                outcome=outcome,
                raw_artifact_path=raw_artifact_path,
                parsed_artifact_path=parsed_artifact_path,
                telemetry_artifact_path=telemetry_artifact_path,
                comparison_artifact_path=comparison_artifact_path,
                error_code=error_code,
                error_message=error_message,
                poisoned=poisoned,
            )

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        tasks = CrashFirstFinish(storage.tasks)
        executor = ChangingParser()
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=tasks,  # type: ignore[arg-type]
            events=storage.events,
            queue=storage.queue,
            executor=executor,
            config=WorkerConfig(max_dequeue_count=2, retry_delay=0),
        )
        await worker.enqueue([task])
        assert await worker.run_once() == 1

        interrupted = await storage.tasks.get(task.run_id, task.pair_id)
        assert interrupted.value.parsed_artifact_path is None
        assert interrupted.value.comparison_artifact_path is None
        assert interrupted.value.telemetry_artifact_path is None
        first_parsed, first_telemetry, first_comparison = tasks.candidates[0]
        assert "/task-attempts/a000001-" in first_parsed
        assert "/task-attempts/a000001-" in first_telemetry
        assert first_comparison is not None
        assert "/task-attempts/a000001-" in first_comparison
        assert await storage.artifacts.get_json(first_parsed) == {"parser_version": 1}

        assert await worker.run_once() == 1
        terminal = await storage.tasks.get(task.run_id, task.pair_id)
        second_parsed, second_telemetry, second_comparison = tasks.candidates[1]
        assert terminal.value.status == "succeeded"
        assert terminal.value.parsed_artifact_path == second_parsed
        assert terminal.value.telemetry_artifact_path == second_telemetry
        assert terminal.value.comparison_artifact_path == second_comparison
        assert second_parsed != first_parsed
        assert second_telemetry != first_telemetry
        assert second_comparison != first_comparison
        assert "/task-attempts/a000002-" in second_parsed
        assert await storage.artifacts.get_json(second_parsed) == {"parser_version": 2}
        assert executor.execute_calls == 1
        assert executor.parse_calls == 2

        base = result_raw(task.run_id, task.sample_id, task.solution_id)
        assert not await storage.artifacts.exists(base.replace("raw.json", "parsed.json"))
        assert not await storage.artifacts.exists(base.replace("raw.json", "comparison.json"))
        assert not await storage.artifacts.exists(
            result_telemetry(task.run_id, task.sample_id, task.solution_id)
        )

    asyncio.run(scenario())


def test_crashed_success_candidate_cannot_bypass_retry_exhaustion(
    tmp_path: Path,
) -> None:
    class CrashSuccessfulFinish:
        def __init__(self, delegate: TaskRepository) -> None:
            self.delegate = delegate
            self.stale_parsed_path: str | None = None

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        async def finish(
            self,
            run_id: str,
            pair_id: str,
            *,
            owner: str | None,
            outcome: TerminalOutcome,
            raw_artifact_path: str,
            parsed_artifact_path: str,
            telemetry_artifact_path: str,
            comparison_artifact_path: str | None = None,
            error_code: str | None = None,
            error_message: str | None = None,
            poisoned: bool = False,
        ) -> TaskCompletion:
            if outcome == "succeeded" and self.stale_parsed_path is None:
                self.stale_parsed_path = parsed_artifact_path
                raise RuntimeError("injected crash before successful finish")
            return await self.delegate.finish(
                run_id,
                pair_id,
                owner=owner,
                outcome=outcome,
                raw_artifact_path=raw_artifact_path,
                parsed_artifact_path=parsed_artifact_path,
                telemetry_artifact_path=telemetry_artifact_path,
                comparison_artifact_path=comparison_artifact_path,
                error_code=error_code,
                error_message=error_message,
                poisoned=poisoned,
            )

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        tasks = CrashSuccessfulFinish(storage.tasks)
        executor = StaticExecutor()
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=tasks,  # type: ignore[arg-type]
            events=storage.events,
            queue=storage.queue,
            executor=executor,
            config=WorkerConfig(max_dequeue_count=1, retry_delay=0),
        )
        await worker.enqueue([task])
        assert await worker.run_once() == 1
        assert await worker.run_once() == 1

        terminal = await storage.tasks.get(task.run_id, task.pair_id)
        assert terminal.value.status == "failed"
        assert terminal.value.error_code == "retry_exhausted"
        assert terminal.value.poisoned
        assert tasks.stale_parsed_path is not None
        assert terminal.value.parsed_artifact_path != tasks.stale_parsed_path
        assert "/task-attempts/a000002-" in (terminal.value.parsed_artifact_path or "")
        assert executor.execute_calls == 1
        assert executor.parse_calls == 1

    asyncio.run(scenario())


def test_retryable_parser_errors_use_durable_attempt_budget(tmp_path: Path) -> None:
    class RetryableParser(StaticExecutor):
        async def parse(self, task: TaskRecord, raw: object) -> ParsedExecution:
            del task, raw
            self.parse_calls += 1
            raise ExecutionError(
                "parser_temporarily_unavailable",
                "The parser is temporarily unavailable.",
                retryable=True,
                raw={"parse_attempt": self.parse_calls},
                telemetry={
                    "components": [
                        {
                            "component": "parser",
                            "latency_ms": 2.0,
                            "usage": {"total_tokens": 3},
                            "estimated_cost_usd": 0.2,
                        }
                    ],
                    "total_tokens": 3,
                    "latency_ms": 2.0,
                    "estimated_cost_usd": 0.2,
                },
            )

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        executor = RetryableParser(storage)
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=executor,
            config=WorkerConfig(max_dequeue_count=2, retry_delay=0),
        )
        await worker.enqueue([task])
        assert await worker.run_once() == 1
        assert await worker.run_once() == 1
        assert await worker.run_once() == 1

        terminal = await storage.tasks.get(task.run_id, task.pair_id)
        assert terminal.value.status == "failed"
        assert terminal.value.error_code == "retry_exhausted"
        assert terminal.value.attempts == 3
        assert terminal.value.poisoned
        assert executor.execute_calls == 1
        assert executor.parse_calls == 2
        for attempt in (1, 2):
            evidence = await storage.artifacts.get_json(
                result_parse_attempt(
                    task.run_id,
                    task.sample_id,
                    task.solution_id,
                    attempt,
                )
            )
            assert isinstance(evidence, dict)
            assert evidence["phase"] == "parse"
            assert evidence["raw"] == {"parse_attempt": attempt}
            parse_telemetry = evidence["telemetry"]
            assert isinstance(parse_telemetry, dict)
            assert parse_telemetry["estimated_cost_usd"] == 0.2
        telemetry = await storage.artifacts.get_json(
            terminal.value.telemetry_artifact_path or ""
        )
        assert isinstance(telemetry, dict)
        assert telemetry["total_tokens"] == 6
        assert telemetry["latency_ms"] == 16.5
        assert telemetry["estimated_cost_usd"] == 0.4

    asyncio.run(scenario())


def test_restart_resumes_from_existing_raw_without_repeating_paid_call(
    tmp_path: Path,
) -> None:
    class ParseOnlyExecutor(StaticExecutor):
        async def execute(
            self,
            task: TaskRecord,
            image_bytes: bytes,
            media_type: str,
        ) -> RawExecution:
            del task, image_bytes, media_type
            raise AssertionError("Existing raw output must prevent another provider call")

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        await storage.artifacts.create_json(
            result_raw(task.run_id, task.sample_id, task.solution_id),
            {"provider": "already-paid"},
        )
        await storage.artifacts.create_json(
            result_telemetry(task.run_id, task.sample_id, task.solution_id),
            {"latency_ms": 9.0},
        )

        executor = ParseOnlyExecutor(storage)
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=executor,
            config=WorkerConfig(
                concurrency=1,
                visibility_timeout=10,
                lease_renewal_interval=2,
            ),
        )
        await worker.resume()
        await worker.run_once()
        stored = await storage.tasks.get(task.run_id, task.pair_id)
        assert stored.value.status == "succeeded"
        assert executor.execute_calls == 0
        assert executor.parse_calls == 1

    asyncio.run(scenario())


def test_restart_repairs_terminal_task_counter_and_runs_single_finalizer(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        raw_path = result_raw(task.run_id, task.sample_id, task.solution_id)
        telemetry_path = result_telemetry(task.run_id, task.sample_id, task.solution_id)
        parsed_path = f"runs/{task.run_id}/results/{task.sample_id}/{task.solution_id}/parsed.json"
        await storage.artifacts.create_json(raw_path, {"provider": "ok"})
        await storage.artifacts.create_json(telemetry_path, {})
        await storage.artifacts.create_json(parsed_path, {"answer": "ok"})
        await storage.tasks.finish(
            task.run_id,
            task.pair_id,
            owner=None,
            outcome="succeeded",
            raw_artifact_path=raw_path,
            parsed_artifact_path=parsed_path,
            telemetry_artifact_path=telemetry_path,
        )
        assert (await storage.runs.get(task.project_id, task.run_id)).value.pending_tasks == 1

        finalizer = CountingFinalizer()
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=StaticExecutor(),
            finalizer=finalizer,
        )
        await asyncio.gather(worker.resume(), worker.resume())
        run = await storage.runs.get(task.project_id, task.run_id)
        assert run.value.pending_tasks == 0
        assert run.value.finalizer_completed
        assert finalizer.calls == 1

    asyncio.run(scenario())


def test_provider_semaphore_limits_parallel_calls_across_samples(tmp_path: Path) -> None:
    class TrackingExecutor(StaticExecutor):
        def __init__(self, storage: LocalStorage) -> None:
            super().__init__(storage)
            self.active = 0
            self.max_active = 0

        async def execute(
            self,
            task: TaskRecord,
            image_bytes: bytes,
            media_type: str,
        ) -> RawExecution:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.03)
            try:
                return await super().execute(task, image_bytes, media_type)
            finally:
                self.active -= 1

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        tasks = [_task("sample-001"), _task("sample-002")]
        await _prepare(storage, tasks)
        executor = TrackingExecutor(storage)
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=executor,
            config=WorkerConfig(
                concurrency=2,
                provider_concurrency=1,
                solution_concurrency=2,
                visibility_timeout=10,
                lease_renewal_interval=2,
            ),
        )
        await worker.enqueue(tasks)
        await worker.run_once()
        assert executor.max_active == 1

    asyncio.run(scenario())


def test_waiting_for_capacity_renews_every_received_lease(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        tasks = [_task("sample-001"), _task("sample-002")]
        await _prepare(storage, tasks)
        queue = RecordingQueue(storage.queue)
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=queue,
            executor=SlowExecutor(storage),
            config=WorkerConfig(
                concurrency=2,
                provider_concurrency=1,
                solution_concurrency=1,
                visibility_timeout=2,
                lease_renewal_interval=0.02,
            ),
        )
        await worker.enqueue(tasks)
        await worker.run_once()
        assert {task.pair_id for task in tasks} <= set(queue.renewed_pairs)

    asyncio.run(scenario())


def test_queue_renewal_failure_keeps_provider_fenced_by_task_claim(
    tmp_path: Path,
) -> None:
    class MutableClock:
        def __init__(self) -> None:
            self.now = datetime(2026, 8, 30, tzinfo=UTC)

        def __call__(self) -> datetime:
            return self.now

        def advance(self, seconds: int) -> None:
            self.now += timedelta(seconds=seconds)

    class FailingRenewQueue(RecordingQueue):
        async def renew(
            self,
            lease: QueueLease,
            *,
            visibility_timeout: int,
        ) -> QueueLease:
            del lease, visibility_timeout
            raise LeaseLostError("injected renewal loss")

    class CancellableExecutor(StaticExecutor):
        def __init__(self, clock: MutableClock) -> None:
            super().__init__()
            self.clock = clock
            self.started = False
            self.completed = False
            self.cancelled = False

        async def execute(
            self,
            task: TaskRecord,
            image_bytes: bytes,
            media_type: str,
        ) -> RawExecution:
            self.started = True
            try:
                for _ in range(3):
                    await asyncio.sleep(0.04)
                    self.clock.advance(1)
                await asyncio.sleep(0.04)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            self.completed = True
            return await super().execute(task, image_bytes, media_type)

    async def scenario() -> None:
        clock = MutableClock()
        storage = LocalStorage(tmp_path, clock=clock)
        task = _task("sample-001")
        await _prepare(storage, [task])
        queue = FailingRenewQueue(storage.queue)
        executor = CancellableExecutor(clock)
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=queue,
            executor=executor,
            config=WorkerConfig(
                concurrency=1,
                visibility_timeout=2,
                lease_renewal_interval=0.02,
            ),
            clock=clock,
        )
        await worker.enqueue([task])
        await worker.run_once()
        stored = await storage.tasks.get(task.run_id, task.pair_id)
        assert executor.started
        assert executor.completed
        assert not executor.cancelled
        assert stored.value.status == "succeeded"
        assert await storage.artifacts.exists(
            result_raw(task.run_id, task.sample_id, task.solution_id)
        )
        assert await worker.run_once() == 1
        assert executor.execute_calls == 1

    asyncio.run(scenario())


def test_process_cancellation_awaits_child_cleanup_before_stopping_renewal(
    tmp_path: Path,
) -> None:
    class CleanupExecutor(StaticExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.cleanup_started = asyncio.Event()
            self.allow_cleanup = asyncio.Event()

        async def execute(
            self,
            task: TaskRecord,
            image_bytes: bytes,
            media_type: str,
        ) -> RawExecution:
            del task, image_bytes, media_type
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cleanup_started.set()
                await self.allow_cleanup.wait()
                raise

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        queue = RecordingQueue(storage.queue)
        executor = CleanupExecutor()
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=queue,
            executor=executor,
            config=WorkerConfig(
                visibility_timeout=2,
                lease_renewal_interval=0.02,
            ),
        )
        await worker.enqueue([task])
        running = asyncio.create_task(worker.run_once())
        await executor.entered.wait()
        running.cancel()
        await executor.cleanup_started.wait()
        renewals_before_cleanup = queue.renewals
        await asyncio.sleep(0.06)
        assert not running.done()
        assert queue.renewals > renewals_before_cleanup

        executor.allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(scenario())


def test_retry_budget_uses_durable_task_attempts_not_dequeue_count(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        queue = DuplicateQueue(
            TaskMessage(
                project_id=task.project_id,
                run_id=task.run_id,
                sample_id=task.sample_id,
                solution_id=task.solution_id,
            )
        )
        queue.leases = [replace(queue.leases[0], dequeue_count=99)]
        executor = StaticExecutor(storage)
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=queue,
            executor=executor,
            config=WorkerConfig(max_dequeue_count=2),
        )
        await worker.run_once()
        stored = await storage.tasks.get(task.run_id, task.pair_id)
        assert stored.value.status == "succeeded"
        assert stored.value.attempts == 1
        assert executor.execute_calls == 1
        assert not queue.poisoned

    asyncio.run(scenario())


def test_resume_reuses_recent_dispatch_outbox_without_duplicate_send(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        queue = RecordingQueue(storage.queue)
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=queue,
            executor=StaticExecutor(storage),
        )
        await worker.enqueue([task])
        dispatched = await storage.tasks.get(task.run_id, task.pair_id)
        assert dispatched.value.dispatch_token is not None
        await asyncio.gather(worker.resume(), worker.resume())
        assert queue.send_count == 1
        assert (await storage.tasks.get(task.run_id, task.pair_id)).value.attempts == 0

    asyncio.run(scenario())


def test_resume_heals_pending_dispatch_after_send_confirmation_crash(
    tmp_path: Path,
) -> None:
    class CrashAfterSend(RecordingQueue):
        async def send(self, message: TaskMessage) -> str:
            await super().send(message)
            raise RuntimeError("injected crash after queue send")

    async def scenario() -> None:
        now = datetime(2026, 8, 30, tzinfo=UTC)
        clock_value = [now]

        def clock() -> datetime:
            return clock_value[0]

        storage = LocalStorage(tmp_path, clock=clock)
        task = _task("sample-001")
        await _prepare(storage, [task])
        config = WorkerConfig(
            visibility_timeout=2,
            lease_renewal_interval=0.5,
            retry_delay=0,
            dispatch_stale_seconds=2,
        )
        crashing = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=CrashAfterSend(storage.queue),
            executor=StaticExecutor(storage),
            config=config,
            clock=clock,
        )
        with pytest.raises(RuntimeError, match="injected crash"):
            await crashing.enqueue([task])
        pending = await storage.tasks.get(task.run_id, task.pair_id)
        assert pending.value.dispatch_token is not None
        assert pending.value.dispatch_message_id is None

        clock_value[0] += timedelta(seconds=3)
        queue = RecordingQueue(storage.queue)
        restarted = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=queue,
            executor=StaticExecutor(storage),
            config=config,
            clock=clock,
        )
        await restarted.resume()
        ambiguous = await storage.tasks.get(task.run_id, task.pair_id)
        assert ambiguous.value.dispatch_token == pending.value.dispatch_token
        assert ambiguous.value.dispatch_message_id is None
        assert queue.send_count == 1
        assert len(list((storage.queue.root / "messages").glob("*.json"))) == 1
        await restarted.run_once()
        completed = await storage.tasks.get(task.run_id, task.pair_id)
        assert completed.value.status == "succeeded"
        assert completed.value.attempts == 1
        assert completed.value.dispatch_message_id is not None

    asyncio.run(scenario())


def test_ambiguous_duplicate_dispatch_is_first_claim_wins(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        now = datetime.now(UTC)
        task = replace(
            _task("sample-001"),
            dispatch_token="dlogical",
            dispatch_updated_at=now - timedelta(seconds=600),
        )
        await _prepare(storage, [task])
        message = TaskMessage(
            project_id=task.project_id,
            run_id=task.run_id,
            sample_id=task.sample_id,
            solution_id=task.solution_id,
            dispatch_token="dlogical",
        )
        queue = DuplicateQueue(message)
        executor = SlowExecutor(storage)
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=queue,
            executor=executor,
            config=WorkerConfig(concurrency=2, dispatch_stale_seconds=65),
        )

        await worker.resume()
        still_ambiguous = await storage.tasks.get(task.run_id, task.pair_id)
        assert still_ambiguous.value.dispatch_message_id is None
        await worker.run_once()

        completed = await storage.tasks.get(task.run_id, task.pair_id)
        assert completed.value.status == "succeeded"
        assert completed.value.dispatch_token == "dlogical"
        assert completed.value.dispatch_message_id in {"message-one", "message-two"}
        assert completed.value.attempts == 1
        assert executor.execute_calls == 1

    asyncio.run(scenario())


def test_poison_outbox_redelivery_heals_failed_dispatch_confirmation(
    tmp_path: Path,
) -> None:
    class FailOnePoisonConfirmation:
        def __init__(self, delegate: TaskRepository) -> None:
            self.delegate = delegate
            self.failed = False

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        async def replace(
            self,
            task: TaskRecord,
            *,
            expected_etag: str,
        ) -> Versioned[TaskRecord]:
            if task.poison_dispatched and not self.failed:
                self.failed = True
                raise RuntimeError("injected crash before outbox confirmation")
            return await self.delegate.replace(
                task,
                expected_etag=expected_etag,
            )

    class RetryableExecutor(StaticExecutor):
        async def execute(
            self,
            task: TaskRecord,
            image_bytes: bytes,
            media_type: str,
        ) -> RawExecution:
            del task, image_bytes, media_type
            self.execute_calls += 1
            raise ExecutionError("temporary", "retry", retryable=True)

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        tasks = FailOnePoisonConfirmation(storage.tasks)
        executor = RetryableExecutor()
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=tasks,  # type: ignore[arg-type]
            events=storage.events,
            queue=storage.queue,
            executor=executor,
            config=WorkerConfig(
                max_dequeue_count=1,
                retry_delay=0,
                visibility_timeout=10,
                lease_renewal_interval=2,
            ),
        )
        await worker.enqueue([task])
        await worker.run_once()
        await worker.run_once()
        pending = await storage.tasks.get(task.run_id, task.pair_id)
        assert pending.value.poison_outbox_id is not None
        assert not pending.value.poison_dispatched
        assert not pending.value.terminal

        await worker.run_once()
        terminal = await storage.tasks.get(task.run_id, task.pair_id)
        assert terminal.value.poisoned
        assert terminal.value.poison_dispatched
        assert len(await storage.queue.poison_messages()) == 1
        assert executor.execute_calls == 1

    asyncio.run(scenario())


def test_missing_terminal_events_are_recovered_exactly_once(tmp_path: Path) -> None:
    class FailingTerminalEvents:
        def __init__(self, delegate: EventRepository, blocked: str) -> None:
            self.delegate = delegate
            self.blocked = blocked

        async def append(
            self,
            run_id: str,
            event: str,
            data: JsonObject,
            *,
            idempotency_key: str | None = None,
        ) -> EventRecord:
            if event == self.blocked:
                raise RuntimeError("injected event outage")
            return await self.delegate.append(
                run_id,
                event,
                data,
                idempotency_key=idempotency_key,
            )

        async def read(
            self,
            run_id: str,
            *,
            after_sequence: int = 0,
            limit: int = 100,
        ) -> list[EventRecord]:
            return await self.delegate.read(
                run_id,
                after_sequence=after_sequence,
                limit=limit,
            )

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        finalizer = CountingFinalizer()
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=FailingTerminalEvents(storage.events, "task_completed"),
            queue=storage.queue,
            executor=StaticExecutor(storage),
            finalizer=finalizer,
        )
        await worker.enqueue([task])
        await worker.run_once()
        terminal = await storage.tasks.get(task.run_id, task.pair_id)
        assert terminal.value.terminal
        assert not terminal.value.terminal_event_emitted

        restarted = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=StaticExecutor(storage),
            finalizer=finalizer,
        )
        await asyncio.gather(restarted.resume(), restarted.resume())
        events = await storage.events.read(task.run_id)
        assert sum(event.event == "task_completed" for event in events) == 1
        assert sum(event.event == "run_completed" for event in events) == 1
        assert finalizer.calls == 1

    asyncio.run(scenario())


def test_finalization_waits_for_dispatch_publication(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        current = await storage.runs.get(task.project_id, task.run_id)
        await storage.runs.replace(
            replace(
                current.value,
                metadata={"dispatch_complete": False},
            ),
            expected_etag=current.etag,
        )
        finalizer = CountingFinalizer()
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=StaticExecutor(storage),
            finalizer=finalizer,
        )

        await worker.enqueue([task])
        await worker.run_once()
        assert finalizer.calls == 0
        terminal = await storage.tasks.get(task.run_id, task.pair_id)
        assert terminal.value.terminal_event_emitted

        current = await storage.runs.get(task.project_id, task.run_id)
        await storage.runs.replace(
            replace(
                current.value,
                metadata={"dispatch_complete": True},
            ),
            expected_etag=current.etag,
        )
        await worker.resume()

        assert finalizer.calls == 1
        assert (
            await storage.runs.get(task.project_id, task.run_id)
        ).value.finalizer_completed

    asyncio.run(scenario())


def test_finalizer_renews_lease_during_long_work(tmp_path: Path) -> None:
    class RecordingRuns:
        def __init__(self, delegate: RunRepository) -> None:
            self.delegate = delegate
            self.renewals = 0

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        async def renew_finalizer(
            self,
            project_id: str,
            run_id: str,
            *,
            owner: str,
            lease_until: datetime,
        ) -> Versioned[RunRecord]:
            self.renewals += 1
            return await self.delegate.renew_finalizer(
                project_id,
                run_id,
                owner=owner,
                lease_until=lease_until,
            )

    class SlowFinalizer(CountingFinalizer):
        async def __call__(
            self,
            run: RunRecord,
            tasks: tuple[TaskRecord, ...],
        ) -> FinalizationOutput:
            await asyncio.sleep(0.12)
            return await super().__call__(run, tasks)

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        runs = RecordingRuns(storage.runs)
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=runs,  # type: ignore[arg-type]
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=StaticExecutor(storage),
            finalizer=SlowFinalizer(),
            config=WorkerConfig(
                finalizer_lease_seconds=2,
                finalizer_renewal_interval=0.02,
            ),
        )
        await worker.enqueue([task])
        await worker.run_once()
        assert runs.renewals >= 2
        assert (await storage.runs.get(task.project_id, task.run_id)).value.finalizer_completed

    asyncio.run(scenario())


def test_nondeterministic_finalizer_retry_uses_new_immutable_paths(tmp_path: Path) -> None:
    class CrashAfterDownloadWrite:
        def __init__(self, delegate: ArtifactRepository) -> None:
            self.delegate = delegate
            self.failed = False

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        async def create_bytes(
            self,
            path: str,
            data: bytes,
            *,
            content_type: str = "application/octet-stream",
            metadata: JsonObject | None = None,
        ) -> ArtifactInfo:
            created = await self.delegate.create_bytes(
                path,
                data,
                content_type=content_type,
                metadata=metadata,
            )
            if path.endswith("/download.zip") and "/finalizers/" in path and not self.failed:
                self.failed = True
                raise RuntimeError("injected crash after finalizer artifacts")
            return created

    class NondeterministicFinalizer:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(
            self,
            run: RunRecord,
            tasks: tuple[TaskRecord, ...],
        ) -> FinalizationOutput:
            del run, tasks
            self.calls += 1
            return FinalizationOutput(
                aggregate={"attempt": self.calls},
                download_bytes=f"bundle-{self.calls}".encode(),
            )

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        artifacts = CrashAfterDownloadWrite(storage.artifacts)
        finalizer = NondeterministicFinalizer()
        worker = DurableWorker(
            artifacts=artifacts,  # type: ignore[arg-type]
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=StaticExecutor(storage),
            finalizer=finalizer,
        )
        await worker.enqueue([task])
        await worker.run_once()

        run = (await storage.runs.get(task.project_id, task.run_id)).value
        assert run.finalizer_completed
        assert finalizer.calls == 2
        assert run.aggregate_artifact_path is not None
        assert run.download_artifact_path is not None
        assert "/finalizers/" in run.aggregate_artifact_path
        assert await storage.artifacts.get_json(run.aggregate_artifact_path) == {"attempt": 2}
        assert await storage.artifacts.get_bytes(run.download_artifact_path) == b"bundle-2"
        attempts = list(
            (tmp_path / "artifacts" / "runs" / task.run_id / "finalizers").glob(
                "*/aggregate.json"
            )
        )
        assert len(attempts) == 2

    asyncio.run(scenario())


def test_finalizer_cancellation_waits_for_sync_child_before_renewal_stops(
    tmp_path: Path,
) -> None:
    class RecordingRuns:
        def __init__(self, delegate: RunRepository) -> None:
            self.delegate = delegate
            self.renewals = 0

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        async def renew_finalizer(
            self,
            project_id: str,
            run_id: str,
            *,
            owner: str,
            lease_until: datetime,
        ) -> Versioned[RunRecord]:
            self.renewals += 1
            return await self.delegate.renew_finalizer(
                project_id,
                run_id,
                owner=owner,
                lease_until=lease_until,
            )

    class BlockingSyncFinalizer:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.release = threading.Event()

        def __call__(
            self,
            run: RunRecord,
            tasks: tuple[TaskRecord, ...],
        ) -> FinalizationOutput:
            del run, tasks
            self.entered.set()
            self.release.wait(timeout=5)
            return FinalizationOutput(aggregate={"finished": True}, download_bytes=b"bundle")

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        await _prepare(storage, [])
        runs = RecordingRuns(storage.runs)
        finalizer = BlockingSyncFinalizer()
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=runs,  # type: ignore[arg-type]
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=StaticExecutor(),
            finalizer=finalizer,
            config=WorkerConfig(
                finalizer_lease_seconds=2,
                finalizer_renewal_interval=0.02,
            ),
        )
        running = asyncio.create_task(worker.resume())
        while not finalizer.entered.is_set():
            await asyncio.sleep(0)
        running.cancel()
        renewals_before_cleanup = runs.renewals
        await asyncio.sleep(0.06)
        assert not running.done()
        assert runs.renewals > renewals_before_cleanup
        running.cancel()
        await asyncio.sleep(0.02)
        assert not running.done()

        finalizer.release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        run = await storage.runs.get("cord-v2", "run-001")
        assert not run.value.finalizer_completed

    asyncio.run(scenario())


def test_confirmed_dispatch_remains_valid_while_stale_behind_backlog(
    tmp_path: Path,
) -> None:
    class MutableClock:
        def __init__(self) -> None:
            self.now = datetime(2026, 8, 30, tzinfo=UTC)

        def __call__(self) -> datetime:
            return self.now

        def advance(self, seconds: int) -> None:
            self.now += timedelta(seconds=seconds)

    async def scenario() -> None:
        clock = MutableClock()
        storage = LocalStorage(tmp_path, clock=clock)
        task = _task("sample-001")
        await _prepare(storage, [task])
        first = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=StaticExecutor(storage),
            clock=clock,
            config=WorkerConfig(
                visibility_timeout=2,
                lease_renewal_interval=0.5,
                retry_delay=0,
                dispatch_stale_seconds=2,
            ),
        )
        await first.enqueue([task])
        original = await storage.tasks.get(task.run_id, task.pair_id)
        assert original.value.dispatch_message_id is not None
        clock.advance(3)

        queue = RecordingQueue(storage.queue)
        restarted = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=queue,
            executor=StaticExecutor(storage),
            clock=clock,
            config=WorkerConfig(
                visibility_timeout=2,
                lease_renewal_interval=0.5,
                retry_delay=0,
                dispatch_stale_seconds=2,
            ),
        )
        await restarted.resume()
        unchanged = await storage.tasks.get(task.run_id, task.pair_id)
        assert unchanged.value.dispatch_token == original.value.dispatch_token
        assert unchanged.value.dispatch_message_id == original.value.dispatch_message_id
        assert unchanged.value.dispatch_updated_at == original.value.dispatch_updated_at
        assert unchanged.value.attempts == 0
        assert queue.send_count == 0
        await restarted.run_once()
        completed = await storage.tasks.get(task.run_id, task.pair_id)
        assert completed.value.status == "succeeded"
        assert completed.value.attempts == 1

    asyncio.run(scenario())


def test_stale_dispatch_never_consumes_retry_budget_or_poison_task(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime.now(UTC)
        storage = LocalStorage(tmp_path)
        task = replace(
            _task("sample-001"),
            dispatch_token="dcurrent",
            dispatch_updated_at=now,
        )
        await _prepare(storage, [task])
        queue = DuplicateQueue(
            TaskMessage(
                project_id=task.project_id,
                run_id=task.run_id,
                sample_id=task.sample_id,
                solution_id=task.solution_id,
                dispatch_token="dstale",
            )
        )
        queue.leases = [replace(queue.leases[0], dequeue_count=999)]
        executor = StaticExecutor(storage)
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=queue,
            executor=executor,
            config=WorkerConfig(max_dequeue_count=1),
        )
        await worker.run_once()
        stored = await storage.tasks.get(task.run_id, task.pair_id)
        assert stored.value.attempts == 0
        assert not stored.value.terminal
        assert queue.completed == ["message-one"]
        assert not queue.poisoned
        assert executor.execute_calls == 0

    asyncio.run(scenario())


def test_finalizer_lease_loss_cancels_work_and_prevents_completion(
    tmp_path: Path,
) -> None:
    class FailingRuns:
        def __init__(self, delegate: RunRepository) -> None:
            self.delegate = delegate

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        async def renew_finalizer(
            self,
            project_id: str,
            run_id: str,
            *,
            owner: str,
            lease_until: datetime,
        ) -> Versioned[RunRecord]:
            del project_id, run_id, owner, lease_until
            raise LeaseLostError("injected finalizer lease loss")

    class CancellableFinalizer(CountingFinalizer):
        def __init__(self) -> None:
            super().__init__()
            self.started = False
            self.cancelled = False

        async def __call__(
            self,
            run: RunRecord,
            tasks: tuple[TaskRecord, ...],
        ) -> FinalizationOutput:
            del run, tasks
            self.calls += 1
            self.started = True
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("finalizer should have been cancelled")

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        finalizer = CancellableFinalizer()
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=FailingRuns(storage.runs),  # type: ignore[arg-type]
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=StaticExecutor(storage),
            finalizer=finalizer,
            config=WorkerConfig(
                finalizer_lease_seconds=2,
                finalizer_renewal_interval=0.02,
            ),
        )
        await worker.enqueue([task])
        await worker.run_once()
        run = await storage.runs.get(task.project_id, task.run_id)
        assert not finalizer.started or finalizer.cancelled
        assert not run.value.finalizer_completed
        assert run.value.status == "finalizing"

    asyncio.run(scenario())


def test_missing_run_completion_event_is_recovered_once(tmp_path: Path) -> None:
    class FailRunEvent:
        def __init__(self, delegate: EventRepository) -> None:
            self.delegate = delegate

        async def append(
            self,
            run_id: str,
            event: str,
            data: JsonObject,
            *,
            idempotency_key: str | None = None,
        ) -> EventRecord:
            if event == "run_completed":
                raise RuntimeError("injected event outage")
            return await self.delegate.append(
                run_id,
                event,
                data,
                idempotency_key=idempotency_key,
            )

        async def read(
            self,
            run_id: str,
            *,
            after_sequence: int = 0,
            limit: int = 100,
        ) -> list[EventRecord]:
            return await self.delegate.read(
                run_id,
                after_sequence=after_sequence,
                limit=limit,
            )

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=FailRunEvent(storage.events),
            queue=storage.queue,
            executor=StaticExecutor(storage),
        )
        await worker.enqueue([task])
        await worker.run_once()
        run = await storage.runs.get(task.project_id, task.run_id)
        assert run.value.finalizer_completed
        assert not run.value.completion_event_emitted

        restarted = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=StaticExecutor(storage),
        )
        await asyncio.gather(restarted.resume(), restarted.resume())
        events = await storage.events.read(task.run_id)
        assert sum(event.event == "run_completed" for event in events) == 1
        assert (await storage.runs.get(task.project_id, task.run_id)).value.completion_event_emitted

    asyncio.run(scenario())


def test_confirmed_poison_outbox_survives_redelivery(tmp_path: Path) -> None:
    class FailFirstPoisonFinish:
        def __init__(self, delegate: TaskRepository) -> None:
            self.delegate = delegate
            self.failed = False

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        async def finish(
            self,
            run_id: str,
            pair_id: str,
            *,
            owner: str | None,
            outcome: TerminalOutcome,
            raw_artifact_path: str,
            parsed_artifact_path: str,
            telemetry_artifact_path: str,
            comparison_artifact_path: str | None = None,
            error_code: str | None = None,
            error_message: str | None = None,
            poisoned: bool = False,
        ) -> TaskCompletion:
            if poisoned and not self.failed:
                self.failed = True
                raise RuntimeError("injected crash after poison confirmation")
            return await self.delegate.finish(
                run_id,
                pair_id,
                owner=owner,
                outcome=outcome,
                raw_artifact_path=raw_artifact_path,
                parsed_artifact_path=parsed_artifact_path,
                telemetry_artifact_path=telemetry_artifact_path,
                comparison_artifact_path=comparison_artifact_path,
                error_code=error_code,
                error_message=error_message,
                poisoned=poisoned,
            )

    class RetryableExecutor(StaticExecutor):
        async def execute(
            self,
            task: TaskRecord,
            image_bytes: bytes,
            media_type: str,
        ) -> RawExecution:
            del task, image_bytes, media_type
            self.execute_calls += 1
            raise ExecutionError("temporary", "retry", retryable=True)

    async def scenario() -> None:
        now = datetime.now(UTC)
        clock_value = [now]

        def clock() -> datetime:
            return clock_value[0]

        storage = LocalStorage(tmp_path, clock=clock)
        task = _task("sample-001")
        await _prepare(storage, [task])
        tasks = FailFirstPoisonFinish(storage.tasks)
        config = WorkerConfig(
            max_dequeue_count=1,
            retry_delay=0,
            visibility_timeout=2,
            lease_renewal_interval=0.5,
            dispatch_stale_seconds=2,
        )
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=tasks,  # type: ignore[arg-type]
            events=storage.events,
            queue=storage.queue,
            executor=RetryableExecutor(),
            config=config,
            clock=clock,
        )
        await worker.enqueue([task])
        await worker.run_once()
        await worker.run_once()

        pending = await storage.tasks.get(task.run_id, task.pair_id)
        assert pending.value.poison_dispatched
        assert not pending.value.terminal
        outbox_id = pending.value.poison_outbox_id
        source_message_id = pending.value.poison_source_message_id
        assert outbox_id is not None
        assert source_message_id is not None
        restarted = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=RetryableExecutor(),
            config=config,
            clock=clock,
        )
        await restarted.resume()
        redelivered = await storage.tasks.get(task.run_id, task.pair_id)
        assert redelivered.value.dispatch_message_id == source_message_id
        await restarted.run_once()

        terminal = await storage.tasks.get(task.run_id, task.pair_id)
        assert terminal.value.terminal
        assert terminal.value.poisoned
        assert terminal.value.poison_outbox_id == outbox_id
        assert terminal.value.poison_source_message_id == source_message_id
        assert len(await storage.queue.poison_messages()) == 1

    asyncio.run(scenario())


def test_run_forever_retries_startup_and_poll_failures(tmp_path: Path) -> None:
    class FailFirstRuns:
        def __init__(self, delegate: RunRepository) -> None:
            self.delegate = delegate
            self.list_calls = 0

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        async def list_unfinished(self) -> list[Versioned[RunRecord]]:
            self.list_calls += 1
            if self.list_calls == 1:
                raise RuntimeError("injected startup storage outage")
            return await self.delegate.list_unfinished()

    class FailFirstPoll:
        def __init__(self, delegate: object, stop: asyncio.Event) -> None:
            self.delegate = delegate
            self.stop = stop
            self.receive_calls = 0

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        async def receive(
            self,
            *,
            max_messages: int,
            visibility_timeout: int,
        ) -> list[QueueLease]:
            del max_messages, visibility_timeout
            self.receive_calls += 1
            if self.receive_calls == 1:
                raise RuntimeError("injected queue outage")
            self.stop.set()
            return []

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        stop = asyncio.Event()
        runs = FailFirstRuns(storage.runs)
        queue = FailFirstPoll(storage.queue, stop)
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=runs,  # type: ignore[arg-type]
            tasks=storage.tasks,
            events=storage.events,
            queue=queue,  # type: ignore[arg-type]
            executor=StaticExecutor(),
            config=WorkerConfig(idle_poll_interval=0.001),
        )
        await worker.run_forever(stop)
        assert runs.list_calls >= 3
        assert queue.receive_calls == 2

    asyncio.run(scenario())


def test_run_forever_preserves_cancellation(tmp_path: Path) -> None:
    class BlockingQueue:
        def __init__(self, delegate: object) -> None:
            self.delegate = delegate
            self.entered = asyncio.Event()
            self.block = asyncio.Event()

        def __getattr__(self, name: str) -> object:
            return getattr(self.delegate, name)

        async def receive(
            self,
            *,
            max_messages: int,
            visibility_timeout: int,
        ) -> list[QueueLease]:
            del max_messages, visibility_timeout
            self.entered.set()
            await self.block.wait()
            return []

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        queue = BlockingQueue(storage.queue)
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=queue,  # type: ignore[arg-type]
            executor=StaticExecutor(),
        )
        running = asyncio.create_task(worker.run_forever(asyncio.Event()))
        await queue.entered.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(scenario())


def test_nonempty_batch_also_reconciles_ready_finalizers(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        await _prepare(storage, [])
        await storage.queue.send(
            TaskMessage(
                project_id="cord-v2",
                run_id="missing-run",
                sample_id="missing-sample",
                solution_id="missing-solution",
            )
        )
        finalizer = CountingFinalizer()
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=StaticExecutor(),
            finalizer=finalizer,
        )

        assert await worker.run_once() == 1
        run = await storage.runs.get("cord-v2", "run-001")
        assert run.value.finalizer_completed
        assert run.value.completion_event_emitted
        assert finalizer.calls == 1

    asyncio.run(scenario())


def test_batches_and_idle_polls_do_not_rescan_entire_dispatch_backlog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        tasks = [_task("sample-001"), _task("sample-002")]
        await _prepare(storage, tasks)
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=StaticExecutor(),
            config=WorkerConfig(
                concurrency=1,
                dispatch_reconciliation_interval=600,
            ),
        )
        await worker.enqueue(tasks)
        scans = 0
        original = storage.tasks.list_resumable

        async def count_scans() -> list[Versioned[TaskRecord]]:
            nonlocal scans
            scans += 1
            return await original()

        monkeypatch.setattr(storage.tasks, "list_resumable", count_scans)

        assert await worker.run_once() == 1
        assert await worker.run_once() == 1
        assert await worker.run_once() == 0
        assert scans == 0

    asyncio.run(scenario())


def test_synchronous_executor_parser_and_finalizer_run_off_loop_thread(
    tmp_path: Path,
) -> None:
    class SyncExecutor:
        def __init__(self) -> None:
            self.execute_thread: int | None = None
            self.parse_thread: int | None = None

        def execute(
            self,
            task: TaskRecord,
            image_bytes: bytes,
            media_type: str,
        ) -> RawExecution:
            del task, image_bytes, media_type
            self.execute_thread = threading.get_ident()
            return RawExecution(raw={"ok": True}, telemetry={})

        def parse(self, task: TaskRecord, raw: object) -> ParsedExecution:
            del task, raw
            self.parse_thread = threading.get_ident()
            return ParsedExecution(parsed={"ok": True})

    class SyncFinalizer:
        def __init__(self) -> None:
            self.thread: int | None = None

        def __call__(
            self,
            run: RunRecord,
            tasks: tuple[TaskRecord, ...],
        ) -> FinalizationOutput:
            del run, tasks
            self.thread = threading.get_ident()
            return FinalizationOutput(aggregate={"ok": True}, download_bytes=b"bundle")

    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        task = _task("sample-001")
        await _prepare(storage, [task])
        executor = SyncExecutor()
        finalizer = SyncFinalizer()
        event_loop_thread = threading.get_ident()
        worker = DurableWorker(
            artifacts=storage.artifacts,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            executor=executor,
            finalizer=finalizer,
        )
        await worker.enqueue([task])
        await worker.run_once()

        assert executor.execute_thread is not None
        assert executor.parse_thread is not None
        assert finalizer.thread is not None
        assert executor.execute_thread != event_loop_thread
        assert executor.parse_thread != event_loop_thread
        assert finalizer.thread != event_loop_thread

    asyncio.run(scenario())
