from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import logging
import secrets
import time
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from receipt_bench.storage.models import (
    FinalizerCompletion,
    QueueLease,
    RunRecord,
    TaskMessage,
    TaskRecord,
    TerminalFailureOutcome,
    TerminalOutcome,
    utc_now,
)
from receipt_bench.storage.paths import (
    finalizer_aggregate,
    finalizer_download,
    result_attempt_comparison,
    result_attempt_parsed,
    result_attempt_telemetry,
    result_execution_attempt,
    result_parse_attempt,
    result_raw,
    result_telemetry,
)
from receipt_bench.storage.protocols import (
    ArtifactRepository,
    ConcurrencyError,
    EventRepository,
    LeaseLostError,
    NotFoundError,
    RunRepository,
    TaskQueue,
    TaskRepository,
)
from receipt_bench.storage.validation import (
    JsonObject,
    JsonValue,
    StrictJsonError,
    bounded_text,
    normalize_json,
    require_json_object,
    strict_json_bytes,
    validate_id,
    validate_metadata,
)
from receipt_bench.worker.contracts import (
    ExecutionError,
    FinalizationOutput,
    PairExecutor,
    ParsedExecution,
    RawExecution,
    RunFinalizer,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ExecutionAttemptError:
    code: str
    message: str
    retryable: bool
    status: TerminalFailureOutcome

    def __post_init__(self) -> None:
        code = bounded_text(self.code, field="error code", limit=256)
        message = bounded_text(self.message, field="error message", limit=2_048)
        if code is None or message is None:
            raise StrictJsonError("Execution attempt error fields cannot be null")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "message", message)
        if self.status not in {"invalid", "failed"}:
            raise StrictJsonError("Execution attempt error status is invalid")

    def as_json(self) -> JsonObject:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "status": self.status,
        }

    @classmethod
    def from_json(cls, value: JsonValue) -> _ExecutionAttemptError:
        document = require_json_object(value, field="execution attempt error")
        code = document.get("code")
        message = document.get("message")
        retryable = document.get("retryable")
        status = document.get("status")
        if (
            not isinstance(code, str)
            or not isinstance(message, str)
            or not isinstance(retryable, bool)
            or status not in {"invalid", "failed"}
        ):
            raise StrictJsonError("Execution attempt error has invalid fields")
        return cls(
            code=code,
            message=message,
            retryable=retryable,
            status=cast(TerminalFailureOutcome, status),
        )


@dataclass(frozen=True)
class _ExecutionEnvelope:
    attempt: int
    raw: JsonValue
    telemetry: JsonObject
    error: _ExecutionAttemptError | None = None
    phase: Literal["execute", "parse"] = "execute"

    def __post_init__(self) -> None:
        if self.attempt < 1:
            raise StrictJsonError("Execution attempt must be at least 1")
        if self.phase not in {"execute", "parse"}:
            raise StrictJsonError("Execution envelope phase is invalid")
        object.__setattr__(self, "raw", normalize_json(self.raw))
        object.__setattr__(self, "telemetry", validate_metadata(self.telemetry))

    def as_json(self) -> JsonObject:
        return {
            "schema_version": "1.0",
            "attempt": self.attempt,
            "phase": self.phase,
            "raw": self.raw,
            "telemetry": self.telemetry,
            "error": self.error.as_json() if self.error is not None else None,
        }

    @classmethod
    def from_json(
        cls,
        value: JsonValue,
        *,
        expected_attempt: int,
    ) -> _ExecutionEnvelope:
        document = require_json_object(value, field="execution envelope")
        if document.get("schema_version") != "1.0":
            raise StrictJsonError("Execution envelope schema_version is invalid")
        attempt = document.get("attempt")
        if (
            not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt != expected_attempt
        ):
            raise StrictJsonError("Execution envelope attempt does not match its path")
        phase = document.get("phase", "execute")
        if phase not in {"execute", "parse"}:
            raise StrictJsonError("Execution envelope phase is invalid")
        if "raw" not in document:
            raise StrictJsonError("Execution envelope is missing raw evidence")
        telemetry = require_json_object(
            document.get("telemetry"),
            field="execution envelope telemetry",
        )
        error_value = document.get("error")
        error = (
            None
            if error_value is None
            else _ExecutionAttemptError.from_json(error_value)
        )
        return cls(
            attempt=attempt,
            raw=document["raw"],
            telemetry=telemetry,
            error=error,
            phase=cast(Literal["execute", "parse"], phase),
        )


@dataclass(frozen=True)
class WorkerConfig:
    concurrency: int = 4
    dispatch_concurrency: int = 16
    provider_concurrency: int = 2
    solution_concurrency: int = 1
    provider_limits: Mapping[str, int] = field(default_factory=dict)
    solution_limits: Mapping[str, int] = field(default_factory=dict)
    visibility_timeout: int = 60
    lease_renewal_interval: float = 20.0
    max_dequeue_count: int = 3
    retry_delay: int = 5
    idle_poll_interval: float = 1.0
    finalizer_lease_seconds: int = 60
    finalizer_renewal_interval: float | None = None
    dispatch_stale_seconds: int = 300
    dispatch_reconciliation_interval: float = 60.0

    def __post_init__(self) -> None:
        if self.concurrency < 1 or self.concurrency > 32:
            raise ValueError("Worker concurrency must be between 1 and 32")
        if self.dispatch_concurrency < 1 or self.dispatch_concurrency > 64:
            raise ValueError("Dispatch concurrency must be between 1 and 64")
        if self.provider_concurrency < 1 or self.solution_concurrency < 1:
            raise ValueError("Provider and solution concurrency must be positive")
        if self.visibility_timeout < 2:
            raise ValueError("Queue visibility timeout must be at least two seconds")
        if not 0 < self.lease_renewal_interval < self.visibility_timeout:
            raise ValueError("Lease renewal interval must be shorter than visibility timeout")
        if self.max_dequeue_count < 1:
            raise ValueError("max_dequeue_count must be positive")
        if self.retry_delay < 0 or self.idle_poll_interval <= 0:
            raise ValueError("Worker delays are invalid")
        if self.finalizer_lease_seconds < 1:
            raise ValueError("Finalizer lease must be positive")
        if self.finalizer_renewal_interval is not None and not (
            0 < self.finalizer_renewal_interval < self.finalizer_lease_seconds
        ):
            raise ValueError("Finalizer renewal interval must be shorter than its lease")
        if self.dispatch_stale_seconds < self.visibility_timeout + self.retry_delay:
            raise ValueError(
                "Dispatch reconciliation must outlive queue visibility plus retry delay"
            )
        if self.dispatch_reconciliation_interval <= 0:
            raise ValueError("Dispatch reconciliation interval must be positive")
        for key, limit in self.provider_limits.items():
            validate_id(key, field="provider limit key")
            if limit < 1:
                raise ValueError("Provider concurrency limits must be positive")
        for key, limit in self.solution_limits.items():
            validate_id(key, field="solution limit key")
            if limit < 1:
                raise ValueError("Solution concurrency limits must be positive")


@dataclass
class _LeaseState:
    lease: QueueLease
    lost: BaseException | None = None
    queue_lost: BaseException | None = None
    lost_event: asyncio.Event = field(default_factory=asyncio.Event)
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _FinalizerState:
    lost: BaseException | None = None
    lost_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class _QueueAction:
    kind: Literal["complete", "retry", "poison"]
    project_id: str
    run_id: str
    delay: int = 0
    reason_code: str = "worker_failure"
    maybe_finalize: bool = False


class DurableWorker:
    """Queue worker with durable pair idempotency and restart-safe accounting."""

    def __init__(
        self,
        *,
        artifacts: ArtifactRepository,
        runs: RunRepository,
        tasks: TaskRepository,
        events: EventRepository,
        queue: TaskQueue,
        executor: PairExecutor,
        finalizer: RunFinalizer | None = None,
        config: WorkerConfig | None = None,
        worker_id: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._artifacts = artifacts
        self._runs = runs
        self._tasks = tasks
        self._events = events
        self._queue = queue
        self._executor = executor
        self._finalizer = finalizer
        self._config = config or WorkerConfig()
        self._worker_id = validate_id(
            worker_id or f"worker-{secrets.token_hex(8)}",
            field="worker_id",
        )
        self._clock = clock
        self._global_slots = asyncio.Semaphore(self._config.concurrency)
        self._provider_slots: dict[str, asyncio.Semaphore] = {}
        self._solution_slots: dict[str, asyncio.Semaphore] = {}
        self._last_dispatch_reconciliation = time.monotonic()

    async def enqueue(self, tasks: list[TaskRecord] | tuple[TaskRecord, ...]) -> None:
        slots = asyncio.Semaphore(self._config.dispatch_concurrency)

        async def dispatch(task: TaskRecord) -> None:
            async with slots:
                await self._ensure_dispatched(task.run_id, task.pair_id)

        await asyncio.gather(*(dispatch(task) for task in tasks))

    async def resume(self) -> None:
        """Repair durable outboxes, counters, dispatches, and finalizers."""

        await self._reconcile_dispatches()
        self._last_dispatch_reconciliation = time.monotonic()
        await self._resume_ready_finalizers()

    async def run_once(self) -> int:
        leases = await self._queue.receive(
            max_messages=self._config.concurrency,
            visibility_timeout=self._config.visibility_timeout,
        )
        if leases:
            async with asyncio.TaskGroup() as group:
                for lease in leases:
                    group.create_task(self._process_lease(lease))
        now = time.monotonic()
        if (
            now - self._last_dispatch_reconciliation
            >= self._config.dispatch_reconciliation_interval
        ):
            await self._reconcile_dispatches()
            self._last_dispatch_reconciliation = now
        await self._resume_ready_finalizers()
        return len(leases)

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.resume()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning(
                    "Worker startup recovery failed; retrying type=%s",
                    type(exc).__name__,
                )
                await self._wait_for_retry(stop)
                continue
            break
        while not stop.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning(
                    "Worker poll failed; retrying type=%s",
                    type(exc).__name__,
                )
                await self._wait_for_retry(stop)
                continue
            if processed == 0:
                await self._wait_for_retry(stop)

    async def _wait_for_retry(self, stop: asyncio.Event) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(
                stop.wait(),
                timeout=self._config.idle_poll_interval,
            )

    async def _resume_ready_finalizers(self) -> None:
        for stored in await self._runs.list_unfinished():
            try:
                if stored.value.finalizer_completed:
                    await self._emit_run_completion(stored.value)
                elif stored.value.pending_tasks == 0:
                    await self._try_finalize(stored.value.project_id, stored.value.run_id)
            except Exception as exc:
                LOGGER.error(
                    "Run recovery stopped for run=%s type=%s",
                    stored.value.run_id,
                    type(exc).__name__,
                )

    async def _reconcile_dispatches(self) -> None:
        for stored in await self._tasks.list_resumable():
            try:
                if stored.value.terminal:
                    await self._heal_terminal(stored.value)
                else:
                    await self._ensure_dispatched(
                        stored.value.run_id,
                        stored.value.pair_id,
                    )
            except Exception as exc:
                LOGGER.error(
                    "Task dispatch reconciliation stopped for run=%s pair=%s type=%s",
                    stored.value.run_id,
                    stored.value.pair_id,
                    type(exc).__name__,
                )

    async def _ensure_dispatched(self, run_id: str, task_pair_id: str) -> bool:
        confirm_message_id = False
        for _ in range(32):
            current = await self._tasks.get(run_id, task_pair_id)
            task = current.value
            if task.terminal:
                return False
            now = self._now()
            active_claim = task.claim_lease_until is not None and task.claim_lease_until > now
            recent_dispatch = (
                task.dispatch_updated_at is not None
                and task.dispatch_updated_at
                + timedelta(seconds=self._config.dispatch_stale_seconds)
                > now
            )
            if active_claim:
                return False
            if task.dispatch_token is not None and task.dispatch_message_id is not None:
                return False
            elif task.dispatch_token is not None:
                if recent_dispatch:
                    return False
                token = task.dispatch_token
                updated = replace(
                    task,
                    dispatch_updated_at=now,
                    updated_at=now,
                )
            else:
                token = validate_id(
                    f"d{secrets.token_hex(15)}",
                    field="dispatch_token",
                )
                confirm_message_id = True
                updated = replace(
                    task,
                    status="queued",
                    stage="queued",
                    dispatch_token=token,
                    dispatch_message_id=None,
                    dispatch_updated_at=now,
                    claim_owner=None,
                    claim_lease_until=None,
                    updated_at=now,
                )
            if updated != task:
                try:
                    await self._tasks.replace(updated, expected_etag=current.etag)
                except ConcurrencyError:
                    confirm_message_id = False
                    continue
            message_id = await self._queue.send(
                TaskMessage(
                    project_id=updated.project_id,
                    run_id=updated.run_id,
                    sample_id=updated.sample_id,
                    solution_id=updated.solution_id,
                    dispatch_token=token,
                )
            )
            if not confirm_message_id:
                return True
            for _ in range(32):
                sent = await self._tasks.get(run_id, task_pair_id)
                if sent.value.dispatch_token != token:
                    return True
                if sent.value.dispatch_message_id == message_id:
                    return True
                if sent.value.dispatch_message_id is not None:
                    return True
                try:
                    await self._tasks.replace(
                        replace(
                            sent.value,
                            dispatch_message_id=message_id,
                            dispatch_updated_at=self._now(),
                            updated_at=self._now(),
                        ),
                        expected_etag=sent.etag,
                    )
                except ConcurrencyError:
                    continue
                return True
            raise RuntimeError("Could not confirm durable task dispatch")
        raise RuntimeError("Could not prepare durable task dispatch")

    @asynccontextmanager
    async def _capacity(self, task: TaskRecord) -> AsyncIterator[None]:
        provider_slots = [
            self._provider_slots.setdefault(
                key,
                asyncio.Semaphore(
                    self._config.provider_limits.get(
                        key,
                        self._config.provider_concurrency,
                    )
                ),
            )
            for key in sorted(task.provider_keys)
        ]
        solution_slot = self._solution_slots.setdefault(
            task.solution_id,
            asyncio.Semaphore(
                self._config.solution_limits.get(
                    task.solution_id,
                    self._config.solution_concurrency,
                )
            ),
        )
        slots = [self._global_slots, *provider_slots, solution_slot]
        acquired: list[asyncio.Semaphore] = []
        try:
            for slot in slots:
                await slot.acquire()
                acquired.append(slot)
            yield
        finally:
            for slot in reversed(acquired):
                slot.release()

    async def _process_lease(self, initial_lease: QueueLease) -> None:
        message = initial_lease.message
        try:
            stored_task = await self._tasks.get(message.run_id, message.pair_id)
        except NotFoundError:
            await self._safe_poison_queue(initial_lease, "task_not_found")
            return
        task = stored_task.value
        if (
            task.project_id != message.project_id
            or task.sample_id != message.sample_id
            or task.solution_id != message.solution_id
        ):
            await self._safe_poison_queue(initial_lease, "task_identity_mismatch")
            return
        if task.dispatch_token != message.dispatch_token:
            await self._safe_complete_queue(initial_lease)
            return
        if (
            task.dispatch_message_id is not None
            and task.dispatch_message_id != initial_lease.message_id
        ):
            await self._safe_complete_queue(initial_lease)
            return
        if task.terminal:
            await self._heal_terminal(task)
            await self._safe_complete_queue(initial_lease)
            await self._try_finalize(task.project_id, task.run_id)
            return

        claim_owner = self._claim_owner(initial_lease)
        try:
            claim = await self._tasks.claim(
                task.run_id,
                task.pair_id,
                owner=claim_owner,
                lease_until=initial_lease.lease_until,
                dispatch_token=message.dispatch_token,
                message_id=initial_lease.message_id,
            )
        except LeaseLostError:
            return
        if not claim.acquired:
            claimed_task = claim.task.value
            if claimed_task.terminal:
                await self._heal_terminal(claimed_task)
                await self._safe_complete_queue(initial_lease)
                await self._try_finalize(task.project_id, task.run_id)
            elif claimed_task.dispatch_token != message.dispatch_token or (
                claimed_task.dispatch_message_id is not None
                and claimed_task.dispatch_message_id != initial_lease.message_id
            ):
                await self._safe_complete_queue(initial_lease)
            else:
                await self._safe_release_queue(initial_lease, delay=1)
            return

        state = _LeaseState(initial_lease)
        stop_renewal = asyncio.Event()
        renewer = asyncio.create_task(
            self._renew_loop(
                state,
                stop_renewal,
                claim_owner=claim_owner,
            )
        )
        processing = asyncio.create_task(
            self._process_claimed(
                claim.task.value,
                claim_owner=claim_owner,
                state=state,
            )
        )
        lost_waiter = asyncio.create_task(state.lost_event.wait())
        action: _QueueAction | None = None
        try:
            done, _ = await asyncio.wait(
                {processing, lost_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lost_waiter in done and state.lost is not None:
                processing.cancel()
                with suppress(asyncio.CancelledError):
                    await processing
                return
            action = await processing
        except asyncio.CancelledError:
            processing.cancel()
            lost_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await processing
            with suppress(asyncio.CancelledError):
                await lost_waiter
            raise
        except Exception as exc:
            LOGGER.error(
                "Worker pair processing stopped for run=%s pair=%s type=%s",
                task.run_id,
                task.pair_id,
                type(exc).__name__,
            )
            if state.lost is None:
                try:
                    current = await self._tasks.get(task.run_id, task.pair_id)
                    if current.value.terminal:
                        await self._heal_terminal(current.value)
                        action = _QueueAction(
                            "complete",
                            current.value.project_id,
                            current.value.run_id,
                            maybe_finalize=True,
                        )
                    else:
                        await self._tasks.release_claim(
                            task.run_id,
                            task.pair_id,
                            owner=claim_owner,
                            error_code="worker_failure",
                            error_message="The worker could not complete this attempt.",
                        )
                        action = _QueueAction(
                            "retry",
                            task.project_id,
                            task.run_id,
                            delay=self._config.retry_delay,
                        )
                except Exception as recovery_exc:
                    LOGGER.error(
                        "Worker recovery stopped for run=%s pair=%s type=%s",
                        task.run_id,
                        task.pair_id,
                        type(recovery_exc).__name__,
                    )
        finally:
            stop_renewal.set()
            lost_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await lost_waiter
            await renewer

        if state.lost is not None or action is None:
            return
        lease = state.lease
        if action.kind in {"complete", "poison"}:
            await self._safe_complete_queue(lease)
        else:
            await self._safe_release_queue(lease, delay=action.delay)
        if action.maybe_finalize:
            await self._try_finalize(action.project_id, action.run_id)

    async def _process_claimed(
        self,
        task: TaskRecord,
        *,
        claim_owner: str,
        state: _LeaseState,
    ) -> _QueueAction:
        self._raise_if_lease_lost(state)
        if task.terminal_intent_outcome is not None:
            return await self._resume_terminal_intent(
                task,
                claim_owner=claim_owner,
                state=state,
            )
        async with self._capacity(task):
            self._raise_if_lease_lost(state)
            return await self._execute_claimed(
                task,
                claim_owner=claim_owner,
                attempt=task.attempts,
                state=state,
            )

    async def _execute_claimed(
        self,
        task: TaskRecord,
        *,
        claim_owner: str,
        attempt: int,
        state: _LeaseState,
    ) -> _QueueAction:
        self._raise_if_lease_lost(state)
        await self._events.append(
            task.run_id,
            "task_started",
            {
                "sample_id": task.sample_id,
                "solution_id": task.solution_id,
                "attempt": attempt,
                "dequeue_count": state.lease.dequeue_count,
            },
        )
        self._raise_if_lease_lost(state)
        envelopes = await self._load_execution_envelopes(task, through_attempt=attempt)
        if not envelopes:
            legacy = await self._migrate_legacy_execution(task, attempt=attempt)
            if legacy is not None:
                envelopes.append(legacy)

        evidence = await self._load_attempt_evidence(
            task,
            through_attempt=attempt,
            executions=envelopes,
        )
        latest = evidence[-1] if evidence else None
        if latest is not None and latest.error is not None and not latest.error.retryable:
            return await self._terminal_attempt_error(
                task,
                claim_owner=claim_owner,
                envelope=latest,
                state=state,
            )
        envelope = next(
            (item for item in reversed(envelopes) if item.error is None),
            None,
        )
        if attempt > self._config.max_dequeue_count:
            return await self._poison_terminal(
                task,
                claim_owner=claim_owner,
                state=state,
                reason_code="retry_exhausted",
            )

        if envelope is None:
            envelope = await self._execute_attempt(task, attempt=attempt, state=state)
            envelopes.append(envelope)

        if envelope.error is not None:
            if envelope.error.retryable:
                return await self._retry_claimed(
                    task,
                    claim_owner=claim_owner,
                    error=envelope.error,
                    attempt=attempt,
                    state=state,
                )
            return await self._terminal_attempt_error(
                task,
                claim_owner=claim_owner,
                envelope=envelope,
                state=state,
            )

        raw = envelope.raw
        raw_path = result_raw(task.run_id, task.sample_id, task.solution_id)
        await self._artifacts.create_json(raw_path, _raw_result_view(envelopes))
        self._raise_if_lease_lost(state)
        await self._events.append(
            task.run_id,
            "raw_persisted",
            {
                "sample_id": task.sample_id,
                "solution_id": task.solution_id,
                "path": raw_path,
                "execution_envelope_path": result_execution_attempt(
                    task.run_id,
                    task.sample_id,
                    task.solution_id,
                    envelope.attempt,
                ),
            },
            idempotency_key=f"raw-persisted:{task.pair_id}:{envelope.attempt}",
        )
        self._raise_if_lease_lost(state)

        try:
            parsed = await _invoke(self._executor.parse, task, raw)
            self._raise_if_lease_lost(state)
            if not isinstance(parsed, ParsedExecution):
                raise TypeError("PairExecutor.parse must return ParsedExecution")
        except LeaseLostError:
            raise
        except ExecutionError as exc:
            if exc.retryable:
                return await self._retry_claimed(
                    task,
                    claim_owner=claim_owner,
                    error=exc,
                    attempt=attempt,
                    state=state,
                )
            await self._persist_parse_error(task, attempt=attempt, error=exc)
            return await self._terminal_failure(
                task,
                claim_owner=claim_owner,
                outcome=exc.status,
                error_code=exc.code,
                error_message=exc.public_message,
                raw_value=exc.raw,
                telemetry=exc.telemetry,
                state=state,
            )
        except (StrictJsonError, OverflowError):
            parse_error = _ExecutionAttemptError(
                code="unsafe_parsed_output",
                message="The parsed response cannot be stored as strict JSON.",
                retryable=False,
                status="invalid",
            )
            await self._persist_parse_attempt(
                task,
                attempt=attempt,
                raw=None,
                telemetry={},
                error=parse_error,
            )
            return await self._terminal_failure(
                task,
                claim_owner=claim_owner,
                outcome="invalid",
                error_code="unsafe_parsed_output",
                error_message="The parsed response cannot be stored as strict JSON.",
                raw_value=None,
                telemetry=None,
                state=state,
            )
        except NotFoundError:
            parse_error = _ExecutionAttemptError(
                code="parse_failure",
                message="The provider response could not be parsed.",
                retryable=False,
                status="invalid",
            )
            await self._persist_parse_attempt(
                task,
                attempt=attempt,
                raw=None,
                telemetry={},
                error=parse_error,
            )
            return await self._terminal_failure(
                task,
                claim_owner=claim_owner,
                outcome="invalid",
                error_code="parse_failure",
                error_message="The provider response could not be parsed.",
                raw_value=None,
                telemetry=None,
                state=state,
            )

        attempt_id = self._result_attempt_id(task, claim_owner)
        parsed_path = result_attempt_parsed(
            task.run_id,
            task.sample_id,
            task.solution_id,
            attempt_id,
        )
        comparison_path: str | None = None
        self._raise_if_lease_lost(state)
        await self._artifacts.create_json(parsed_path, parsed.parsed)
        if parsed.comparison is not None:
            comparison_path = result_attempt_comparison(
                task.run_id,
                task.sample_id,
                task.solution_id,
                attempt_id,
            )
            await self._artifacts.create_json(comparison_path, parsed.comparison)
        evidence = await self._load_attempt_evidence(
            task,
            through_attempt=attempt,
            executions=envelopes,
        )
        raw_path, telemetry_path = await self._materialize_result_views(
            task,
            evidence,
            attempt_id=attempt_id,
        )
        self._raise_if_lease_lost(state)
        return await self._finish_terminal(
            task,
            claim_owner=claim_owner,
            outcome="succeeded",
            raw_path=raw_path,
            parsed_path=parsed_path,
            telemetry_path=telemetry_path,
            comparison_path=comparison_path,
            state=state,
        )

    async def _execute_attempt(
        self,
        task: TaskRecord,
        *,
        attempt: int,
        state: _LeaseState,
    ) -> _ExecutionEnvelope:
        try:
            image_bytes = await self._artifacts.get_bytes(task.image_artifact_path)
            execution = await _invoke(
                self._executor.execute,
                task,
                image_bytes,
                task.media_type,
            )
            self._raise_if_lease_lost(state)
            if not isinstance(execution, RawExecution):
                raise TypeError("PairExecutor.execute must return RawExecution")
            envelope = _ExecutionEnvelope(
                attempt=attempt,
                raw=execution.raw,
                telemetry=execution.telemetry,
            )
        except LeaseLostError:
            raise
        except ExecutionError as exc:
            envelope = _ExecutionEnvelope(
                attempt=attempt,
                raw=exc.raw,
                telemetry=exc.telemetry,
                error=_ExecutionAttemptError(
                    code=exc.code,
                    message=exc.public_message,
                    retryable=exc.retryable,
                    status=exc.status,
                ),
            )
        except (StrictJsonError, OverflowError):
            envelope = _ExecutionEnvelope(
                attempt=attempt,
                raw=None,
                telemetry={},
                error=_ExecutionAttemptError(
                    code="unsafe_raw_output",
                    message=(
                        "The provider returned data that cannot be stored as strict JSON."
                    ),
                    retryable=False,
                    status="invalid",
                ),
            )
        except NotFoundError:
            envelope = _ExecutionEnvelope(
                attempt=attempt,
                raw=None,
                telemetry={},
                error=_ExecutionAttemptError(
                    code="executor_failure",
                    message="The provider call did not complete.",
                    retryable=True,
                    status="failed",
                ),
            )
        self._raise_if_lease_lost(state)
        await self._artifacts.create_json(
            result_execution_attempt(
                task.run_id,
                task.sample_id,
                task.solution_id,
                attempt,
            ),
            envelope.as_json(),
        )
        return envelope

    async def _load_execution_envelopes(
        self,
        task: TaskRecord,
        *,
        through_attempt: int,
    ) -> list[_ExecutionEnvelope]:
        envelopes: list[_ExecutionEnvelope] = []
        for attempt in range(1, through_attempt + 1):
            path = result_execution_attempt(
                task.run_id,
                task.sample_id,
                task.solution_id,
                attempt,
            )
            if not await self._artifacts.exists(path):
                continue
            envelopes.append(
                _ExecutionEnvelope.from_json(
                    await self._artifacts.get_json(path),
                    expected_attempt=attempt,
                )
            )
            if envelopes[-1].phase != "execute":
                raise StrictJsonError("Execution attempt artifact has the wrong phase")
        return envelopes

    async def _load_attempt_evidence(
        self,
        task: TaskRecord,
        *,
        through_attempt: int,
        executions: list[_ExecutionEnvelope] | None = None,
    ) -> list[_ExecutionEnvelope]:
        evidence = (
            list(executions)
            if executions is not None
            else await self._load_execution_envelopes(
                task,
                through_attempt=through_attempt,
            )
        )
        for attempt in range(1, through_attempt + 1):
            path = result_parse_attempt(
                task.run_id,
                task.sample_id,
                task.solution_id,
                attempt,
            )
            if not await self._artifacts.exists(path):
                continue
            envelope = _ExecutionEnvelope.from_json(
                await self._artifacts.get_json(path),
                expected_attempt=attempt,
            )
            if envelope.phase != "parse":
                raise StrictJsonError("Parse attempt artifact has the wrong phase")
            evidence.append(envelope)
        return sorted(
            evidence,
            key=lambda item: (item.attempt, 0 if item.phase == "execute" else 1),
        )

    async def _persist_parse_error(
        self,
        task: TaskRecord,
        *,
        attempt: int,
        error: ExecutionError,
    ) -> _ExecutionEnvelope:
        return await self._persist_parse_attempt(
            task,
            attempt=attempt,
            raw=error.raw,
            telemetry=error.telemetry,
            error=_ExecutionAttemptError(
                code=error.code,
                message=error.public_message,
                retryable=error.retryable,
                status=error.status,
            ),
        )

    async def _persist_parse_attempt(
        self,
        task: TaskRecord,
        *,
        attempt: int,
        raw: JsonValue,
        telemetry: JsonObject,
        error: _ExecutionAttemptError,
    ) -> _ExecutionEnvelope:
        envelope = _ExecutionEnvelope(
            attempt=attempt,
            raw=raw,
            telemetry=telemetry,
            error=error,
            phase="parse",
        )
        await self._artifacts.create_json(
            result_parse_attempt(
                task.run_id,
                task.sample_id,
                task.solution_id,
                attempt,
            ),
            envelope.as_json(),
        )
        return envelope

    async def _migrate_legacy_execution(
        self,
        task: TaskRecord,
        *,
        attempt: int,
        error: _ExecutionAttemptError | None = None,
    ) -> _ExecutionEnvelope | None:
        raw_path = task.raw_artifact_path or result_raw(
            task.run_id,
            task.sample_id,
            task.solution_id,
        )
        if not await self._artifacts.exists(raw_path):
            return None
        raw = await self._artifacts.get_json(raw_path)
        telemetry_path = task.telemetry_artifact_path or result_telemetry(
            task.run_id,
            task.sample_id,
            task.solution_id,
        )
        if await self._artifacts.exists(telemetry_path):
            telemetry = require_json_object(
                await self._artifacts.get_json(telemetry_path),
                field="legacy execution telemetry",
            )
        else:
            telemetry = {"evidence_incomplete": True}
            if error is None:
                error = _ExecutionAttemptError(
                    code="missing_execution_telemetry",
                    message="The stored provider response has no matching telemetry.",
                    retryable=False,
                    status="failed",
                )
        envelope = _ExecutionEnvelope(
            attempt=attempt,
            raw=raw,
            telemetry=telemetry,
            error=error,
        )
        await self._artifacts.create_json(
            result_execution_attempt(
                task.run_id,
                task.sample_id,
                task.solution_id,
                attempt,
            ),
            envelope.as_json(),
        )
        return envelope

    async def _materialize_result_views(
        self,
        task: TaskRecord,
        envelopes: list[_ExecutionEnvelope],
        *,
        attempt_id: str,
    ) -> tuple[str, str]:
        if not envelopes:
            raise ValueError("Result views require at least one execution envelope")
        ordered = sorted(envelopes, key=lambda item: item.attempt)
        raw_path = result_raw(task.run_id, task.sample_id, task.solution_id)
        telemetry_path = result_attempt_telemetry(
            task.run_id,
            task.sample_id,
            task.solution_id,
            attempt_id,
        )
        await self._artifacts.create_json(raw_path, _raw_result_view(ordered))
        await self._artifacts.create_json(
            telemetry_path,
            _telemetry_result_view(task, ordered),
        )
        return raw_path, telemetry_path

    async def _terminal_attempt_error(
        self,
        task: TaskRecord,
        *,
        claim_owner: str,
        envelope: _ExecutionEnvelope,
        state: _LeaseState,
    ) -> _QueueAction:
        error = envelope.error
        if error is None:
            raise ValueError("A successful execution envelope is not terminal")
        return await self._terminal_failure(
            task,
            claim_owner=claim_owner,
            outcome=error.status,
            error_code=error.code,
            error_message=error.message,
            raw_value=envelope.raw,
            telemetry=envelope.telemetry,
            state=state,
        )

    async def _retry_claimed(
        self,
        task: TaskRecord,
        *,
        claim_owner: str,
        error: ExecutionError | _ExecutionAttemptError,
        attempt: int,
        state: _LeaseState,
    ) -> _QueueAction:
        message = (
            error.public_message
            if isinstance(error, ExecutionError)
            else error.message
        )
        self._raise_if_lease_lost(state)
        if isinstance(error, ExecutionError):
            await self._persist_parse_error(task, attempt=attempt, error=error)
            self._raise_if_lease_lost(state)
        await self._tasks.release_claim(
            task.run_id,
            task.pair_id,
            owner=claim_owner,
            error_code=error.code,
            error_message=message,
        )
        await self._events.append(
            task.run_id,
            "task_retry",
            {
                "sample_id": task.sample_id,
                "solution_id": task.solution_id,
                "error_code": error.code,
                "attempt": attempt,
                "dequeue_count": state.lease.dequeue_count,
            },
        )
        return _QueueAction(
            "retry",
            task.project_id,
            task.run_id,
            delay=self._config.retry_delay,
        )

    async def _resume_terminal_intent(
        self,
        task: TaskRecord,
        *,
        claim_owner: str,
        state: _LeaseState,
    ) -> _QueueAction:
        outcome = task.terminal_intent_outcome
        error_code = task.terminal_intent_error_code
        error_message = task.terminal_intent_error_message
        if outcome not in {"invalid", "failed"} or error_code is None or error_message is None:
            raise RuntimeError("Stored terminal intent is incomplete")
        return await self._terminal_failure(
            task,
            claim_owner=claim_owner,
            outcome=outcome,
            error_code=error_code,
            error_message=error_message,
            raw_value=None,
            telemetry=None,
            poisoned=task.terminal_intent_poisoned,
            state=state,
        )

    async def _terminal_failure(
        self,
        task: TaskRecord,
        *,
        claim_owner: str,
        outcome: Literal["invalid", "failed"],
        error_code: str,
        error_message: str,
        raw_value: JsonValue | None,
        telemetry: JsonObject | None,
        poisoned: bool = False,
        state: _LeaseState,
    ) -> _QueueAction:
        self._raise_if_lease_lost(state)
        intent = await self._tasks.record_terminal_intent(
            task.run_id,
            task.pair_id,
            owner=claim_owner,
            outcome=outcome,
            error_code=error_code,
            error_message=error_message,
            poisoned=poisoned,
        )
        task = intent.value
        self._raise_if_lease_lost(state)
        envelopes = await self._load_attempt_evidence(
            task,
            through_attempt=task.attempts,
        )
        if not envelopes:
            fallback_error = _ExecutionAttemptError(
                code=error_code,
                message=error_message,
                retryable=False,
                status=outcome,
            )
            legacy = await self._migrate_legacy_execution(
                task,
                attempt=max(task.attempts, 1),
                error=fallback_error,
            )
            if legacy is not None:
                envelopes.append(legacy)
            else:
                fallback = _ExecutionEnvelope(
                    attempt=max(task.attempts, 1),
                    raw=(
                        raw_value
                        if raw_value is not None
                        else {"error": {"code": error_code}}
                    ),
                    telemetry=telemetry or {"evidence_incomplete": True},
                    error=fallback_error,
                )
                await self._artifacts.create_json(
                    result_execution_attempt(
                        task.run_id,
                        task.sample_id,
                        task.solution_id,
                        fallback.attempt,
                    ),
                    fallback.as_json(),
                )
                envelopes.append(fallback)
        attempt_id = self._result_attempt_id(task, claim_owner)
        raw_path, telemetry_path = await self._materialize_result_views(
            task,
            envelopes,
            attempt_id=attempt_id,
        )
        self._raise_if_lease_lost(state)
        parsed_path = result_attempt_parsed(
            task.run_id,
            task.sample_id,
            task.solution_id,
            attempt_id,
        )
        await self._artifacts.create_json(
            parsed_path,
            {
                "status": outcome,
                "error": {"code": error_code, "message": error_message},
            },
        )
        action = await self._finish_terminal(
            task,
            claim_owner=claim_owner,
            outcome=outcome,
            raw_path=raw_path,
            parsed_path=parsed_path,
            telemetry_path=telemetry_path,
            error_code=error_code,
            error_message=error_message,
            poisoned=poisoned,
            state=state,
        )
        if not poisoned:
            return action
        return _QueueAction(
            "poison",
            action.project_id,
            action.run_id,
            reason_code=error_code,
            maybe_finalize=action.maybe_finalize,
        )

    async def _finish_terminal(
        self,
        task: TaskRecord,
        *,
        claim_owner: str | None,
        outcome: TerminalOutcome,
        raw_path: str,
        parsed_path: str,
        telemetry_path: str,
        comparison_path: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        poisoned: bool = False,
        state: _LeaseState | None = None,
    ) -> _QueueAction:
        if state is not None:
            self._raise_if_lease_lost(state)
        completion = await self._tasks.finish(
            task.run_id,
            task.pair_id,
            owner=claim_owner,
            outcome=outcome,
            raw_artifact_path=raw_path,
            parsed_artifact_path=parsed_path,
            telemetry_artifact_path=telemetry_path,
            comparison_artifact_path=comparison_path,
            error_code=error_code,
            error_message=error_message,
            poisoned=poisoned,
        )
        terminal_task = completion.task.value
        accounting = await self._runs.record_terminal(
            terminal_task.project_id,
            terminal_task.run_id,
            terminal_task.pair_id,
            outcome,
            poisoned=poisoned,
        )
        await self._emit_task_completion(
            terminal_task,
            pending_tasks=accounting.run.value.pending_tasks,
        )
        return _QueueAction(
            "complete",
            task.project_id,
            task.run_id,
            maybe_finalize=accounting.run.value.pending_tasks == 0,
        )

    async def _poison_terminal(
        self,
        task: TaskRecord,
        *,
        claim_owner: str,
        state: _LeaseState,
        reason_code: str,
    ) -> _QueueAction:
        stable_outbox_id = validate_id(
            "p"
            + hashlib.sha256(
                (f"{task.run_id}\n{task.pair_id}\n{reason_code}").encode()
            ).hexdigest()[:31],
            field="poison_outbox_id",
        )
        outbox_id = stable_outbox_id
        dispatched_task: TaskRecord | None = None
        for _ in range(32):
            current = await self._tasks.get(task.run_id, task.pair_id)
            self._assert_claim_owned(current.value, claim_owner)
            if current.value.poison_outbox_id is not None:
                if current.value.poison_reason_code != reason_code:
                    raise ConcurrencyError("Task already has a different durable poison handoff")
                outbox_id = current.value.poison_outbox_id
            else:
                outbox_id = stable_outbox_id
            same_outbox = current.value.poison_outbox_id == outbox_id
            prepared = replace(
                current.value,
                poison_outbox_id=outbox_id,
                poison_source_message_id=(
                    current.value.poison_source_message_id
                    if same_outbox
                    else state.lease.message_id
                ),
                poison_reason_code=reason_code,
                poison_message_id=current.value.poison_message_id if same_outbox else None,
                poison_dispatched=current.value.poison_dispatched if same_outbox else False,
                updated_at=self._now(),
            )
            if prepared == current.value:
                dispatched_task = prepared
                break
            try:
                stored = await self._tasks.replace(
                    prepared,
                    expected_etag=current.etag,
                )
            except ConcurrencyError:
                continue
            dispatched_task = stored.value
            break
        if dispatched_task is None:
            raise ConcurrencyError("Could not prepare durable poison outbox")

        if not dispatched_task.poison_dispatched:
            self._raise_if_lease_lost(state)
            async with state.operation_lock:
                self._raise_if_lease_lost(state)
                poison_message_id = await self._queue.send_poison(
                    state.lease,
                    reason_code=reason_code,
                    outbox_id=outbox_id,
                )
            self._raise_if_lease_lost(state)
            for _ in range(32):
                current = await self._tasks.get(task.run_id, task.pair_id)
                self._assert_claim_owned(current.value, claim_owner)
                if current.value.poison_outbox_id != outbox_id:
                    raise ConcurrencyError("Poison outbox identity changed")
                if current.value.poison_dispatched:
                    dispatched_task = current.value
                    break
                try:
                    stored = await self._tasks.replace(
                        replace(
                            current.value,
                            poison_message_id=poison_message_id,
                            poison_dispatched=True,
                            updated_at=self._now(),
                        ),
                        expected_etag=current.etag,
                    )
                except ConcurrencyError:
                    continue
                dispatched_task = stored.value
                break
            else:
                raise ConcurrencyError("Could not confirm durable poison dispatch")

        self._raise_if_lease_lost(state)
        return await self._terminal_failure(
            dispatched_task,
            claim_owner=claim_owner,
            outcome="failed",
            error_code=reason_code,
            error_message="The task exhausted its durable retry limit.",
            raw_value={"error": {"code": reason_code}},
            telemetry={},
            poisoned=True,
            state=state,
        )

    async def _heal_terminal(self, task: TaskRecord) -> None:
        if task.status not in {"succeeded", "invalid", "failed"}:
            raise ValueError("Cannot heal a non-terminal task")
        outcome = cast(TerminalOutcome, task.status)
        accounting = await self._runs.record_terminal(
            task.project_id,
            task.run_id,
            task.pair_id,
            outcome,
            poisoned=task.poisoned,
        )
        await self._emit_task_completion(
            task,
            pending_tasks=accounting.run.value.pending_tasks,
        )

    async def _emit_task_completion(
        self,
        task: TaskRecord,
        *,
        pending_tasks: int,
    ) -> None:
        for _ in range(32):
            current = await self._tasks.get(task.run_id, task.pair_id)
            terminal = current.value
            if not terminal.terminal or terminal.terminal_event_emitted:
                return
            if terminal.terminal_pending_tasks is None:
                try:
                    stored = await self._tasks.replace(
                        replace(
                            terminal,
                            terminal_pending_tasks=pending_tasks,
                            updated_at=self._now(),
                        ),
                        expected_etag=current.etag,
                    )
                except ConcurrencyError:
                    continue
                terminal = stored.value
            await self._events.append(
                terminal.run_id,
                "task_completed",
                {
                    "sample_id": terminal.sample_id,
                    "solution_id": terminal.solution_id,
                    "status": terminal.status,
                    "error_code": terminal.error_code,
                    "pending_tasks": terminal.terminal_pending_tasks,
                    "poisoned": terminal.poisoned,
                },
                idempotency_key=f"task-terminal:{terminal.pair_id}",
            )
            latest = await self._tasks.get(terminal.run_id, terminal.pair_id)
            if latest.value.terminal_event_emitted:
                return
            try:
                await self._tasks.replace(
                    replace(
                        latest.value,
                        terminal_event_emitted=True,
                        updated_at=self._now(),
                    ),
                    expected_etag=latest.etag,
                )
            except ConcurrencyError:
                continue
            return
        raise ConcurrencyError("Could not confirm durable task completion event")

    async def _renew_loop(
        self,
        state: _LeaseState,
        stop: asyncio.Event,
        *,
        claim_owner: str,
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self._config.lease_renewal_interval,
                )
                return
            except TimeoutError:
                pass
            claim_lease_until = self._now() + timedelta(
                seconds=self._config.visibility_timeout
            )
            if state.queue_lost is None:
                try:
                    async with state.operation_lock:
                        renewed = await self._queue.renew(
                            state.lease,
                            visibility_timeout=self._config.visibility_timeout,
                        )
                        state.lease = renewed
                    claim_lease_until = renewed.lease_until
                except Exception as exc:
                    state.queue_lost = exc
                    LOGGER.warning(
                        "Worker queue lease renewal failed; continuing under "
                        "the durable task claim run=%s pair=%s type=%s",
                        state.lease.message.run_id,
                        state.lease.message.pair_id,
                        type(exc).__name__,
                    )
            try:
                await self._tasks.renew_claim(
                    state.lease.message.run_id,
                    state.lease.message.pair_id,
                    owner=claim_owner,
                    lease_until=claim_lease_until,
                )
            except LeaseLostError as exc:
                try:
                    current = await self._tasks.get(
                        state.lease.message.run_id,
                        state.lease.message.pair_id,
                    )
                except Exception:
                    current = None
                if current is not None and current.value.terminal:
                    return
                state.lost = exc
                state.lost_event.set()
                LOGGER.warning(
                    "Worker task claim renewal stopped for run=%s pair=%s type=%s",
                    state.lease.message.run_id,
                    state.lease.message.pair_id,
                    type(exc).__name__,
                )
                return
            except Exception as exc:
                state.lost = exc
                state.lost_event.set()
                LOGGER.warning(
                    "Worker task claim renewal stopped for run=%s pair=%s type=%s",
                    state.lease.message.run_id,
                    state.lease.message.pair_id,
                    type(exc).__name__,
                )
                return

    async def _try_finalize(self, project_id: str, run_id: str) -> bool:
        current = await self._runs.get(project_id, run_id)
        if current.value.metadata.get("dispatch_complete") is not True:
            return False
        stored_tasks = await self._tasks.list_for_run(run_id)
        if (
            len(stored_tasks) != current.value.total_tasks
            or any(
                not stored.value.terminal
                or not stored.value.terminal_event_emitted
                for stored in stored_tasks
            )
        ):
            return False
        finalizer_owner = validate_id(
            "f"
            + hashlib.sha256(
                f"{self._worker_id}\n{secrets.token_hex(16)}".encode()
            ).hexdigest()[:31],
            field="finalizer_owner",
        )
        lease_until = self._now() + timedelta(seconds=self._config.finalizer_lease_seconds)
        claimed = await self._runs.claim_finalizer(
            project_id,
            run_id,
            owner=finalizer_owner,
            lease_until=lease_until,
        )
        if claimed is None:
            try:
                current = await self._runs.get(project_id, run_id)
                if current.value.finalizer_completed:
                    await self._emit_run_completion(current.value)
            except NotFoundError:
                pass
            return False
        state = _FinalizerState()
        stop_renewal = asyncio.Event()
        renewer = asyncio.create_task(
            self._renew_finalizer_loop(
                project_id,
                run_id,
                owner=finalizer_owner,
                state=state,
                stop=stop_renewal,
            )
        )
        processing = asyncio.create_task(
            self._finalize_claimed(
                claimed.value,
                owner=finalizer_owner,
                state=state,
            )
        )
        lost_waiter = asyncio.create_task(state.lost_event.wait())
        try:
            done, _ = await asyncio.wait(
                {processing, lost_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lost_waiter in done and state.lost is not None:
                processing.cancel()
                with suppress(asyncio.CancelledError):
                    await processing
                return False
            completed = await processing
        except asyncio.CancelledError:
            processing.cancel()
            lost_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await processing
            with suppress(asyncio.CancelledError):
                await lost_waiter
            raise
        except Exception as exc:
            LOGGER.error(
                "Run finalization stopped for run=%s type=%s",
                run_id,
                type(exc).__name__,
            )
            with suppress(LeaseLostError, NotFoundError):
                await self._runs.release_finalizer(
                    project_id,
                    run_id,
                    owner=finalizer_owner,
                )
            return False
        finally:
            stop_renewal.set()
            lost_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await lost_waiter
            await renewer
        if state.lost is not None:
            return False
        try:
            await self._emit_run_completion(completed.run.value)
        except Exception as exc:
            LOGGER.error(
                "Run completion event recovery stopped for run=%s type=%s",
                run_id,
                type(exc).__name__,
            )
        return completed.applied

    async def _finalize_claimed(
        self,
        run: RunRecord,
        *,
        owner: str,
        state: _FinalizerState,
    ) -> FinalizerCompletion:
        self._raise_if_finalizer_lost(state)
        stored_tasks = await self._tasks.list_for_run(run.run_id)
        task_values = tuple(stored.value for stored in stored_tasks)
        if any(not task.terminal for task in task_values):
            raise RuntimeError("Cannot finalize a run with non-terminal tasks")
        if self._finalizer is None:
            output = await _invoke(_default_finalization, run, task_values)
        else:
            output = await _invoke(self._finalizer, run, task_values)
        self._raise_if_finalizer_lost(state)
        if not isinstance(output, FinalizationOutput):
            raise TypeError("RunFinalizer must return FinalizationOutput")
        aggregate_path = finalizer_aggregate(run.run_id, owner)
        download_path = finalizer_download(run.run_id, owner)
        await self._artifacts.create_json(aggregate_path, output.aggregate)
        await self._artifacts.create_bytes(
            download_path,
            output.download_bytes,
            content_type="application/zip",
        )
        self._raise_if_finalizer_lost(state)
        return await self._runs.complete_finalizer(
            run.project_id,
            run.run_id,
            owner=owner,
            aggregate_artifact_path=aggregate_path,
            download_artifact_path=download_path,
        )

    async def _renew_finalizer_loop(
        self,
        project_id: str,
        run_id: str,
        *,
        owner: str,
        state: _FinalizerState,
        stop: asyncio.Event,
    ) -> None:
        interval = self._config.finalizer_renewal_interval
        if interval is None:
            interval = max(0.1, self._config.finalizer_lease_seconds / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                await self._runs.renew_finalizer(
                    project_id,
                    run_id,
                    owner=owner,
                    lease_until=self._now()
                    + timedelta(seconds=self._config.finalizer_lease_seconds),
                )
            except LeaseLostError as exc:
                try:
                    current = await self._runs.get(project_id, run_id)
                except Exception:
                    current = None
                if current is not None and current.value.finalizer_completed:
                    return
                state.lost = exc
                state.lost_event.set()
                LOGGER.warning(
                    "Finalizer lease renewal stopped for run=%s type=%s",
                    run_id,
                    type(exc).__name__,
                )
                return
            except Exception as exc:
                state.lost = exc
                state.lost_event.set()
                LOGGER.warning(
                    "Finalizer lease renewal stopped for run=%s type=%s",
                    run_id,
                    type(exc).__name__,
                )
                return

    async def _emit_run_completion(self, run: RunRecord) -> None:
        if not run.finalizer_completed or run.completion_event_emitted:
            return
        await self._events.append(
            run.run_id,
            "run_completed",
            {
                "run_id": run.run_id,
                "succeeded_tasks": run.succeeded_tasks,
                "invalid_tasks": run.invalid_tasks,
                "failed_tasks": run.failed_tasks,
                "poisoned_tasks": run.poisoned_tasks,
                "aggregate_artifact_path": run.aggregate_artifact_path,
                "download_artifact_path": run.download_artifact_path,
            },
            idempotency_key="run-terminal",
        )
        for _ in range(32):
            current = await self._runs.get(run.project_id, run.run_id)
            if current.value.completion_event_emitted:
                return
            try:
                await self._runs.replace(
                    replace(
                        current.value,
                        completion_event_emitted=True,
                        updated_at=self._now(),
                    ),
                    expected_etag=current.etag,
                )
            except ConcurrencyError:
                continue
            return
        raise ConcurrencyError("Could not confirm durable run completion event")

    def _assert_claim_owned(self, task: TaskRecord, owner: str) -> None:
        if (
            task.terminal
            or task.claim_owner != owner
            or task.claim_lease_until is None
            or task.claim_lease_until <= self._now()
        ):
            raise LeaseLostError("Task claim is not owned")

    def _raise_if_lease_lost(self, state: _LeaseState) -> None:
        if state.lost is not None or (
            state.queue_lost is None and state.lease.lease_until <= self._now()
        ):
            raise LeaseLostError("Worker lease was lost")

    @staticmethod
    def _raise_if_finalizer_lost(state: _FinalizerState) -> None:
        if state.lost is not None:
            raise LeaseLostError("Finalizer lease was lost")

    async def _safe_complete_queue(self, lease: QueueLease) -> None:
        with suppress(LeaseLostError, NotFoundError):
            await self._queue.complete(lease)

    async def _safe_release_queue(self, lease: QueueLease, *, delay: int) -> None:
        with suppress(LeaseLostError, NotFoundError):
            await self._queue.release(lease, delay=delay)

    async def _safe_poison_queue(self, lease: QueueLease, reason_code: str) -> None:
        with suppress(LeaseLostError, NotFoundError):
            await self._queue.poison(lease, reason_code=reason_code)

    def _claim_owner(self, lease: QueueLease) -> str:
        digest = hashlib.sha256(
            f"{self._worker_id}\n{lease.message_id}\n{lease.pop_receipt}".encode()
        ).hexdigest()[:31]
        return validate_id(f"w{digest}", field="claim_owner")

    @staticmethod
    def _result_attempt_id(task: TaskRecord, claim_owner: str) -> str:
        return validate_id(
            f"a{task.attempts:06d}-{claim_owner}",
            field="result_attempt_id",
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Worker clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _raw_result_view(envelopes: list[_ExecutionEnvelope]) -> JsonValue:
    executions = [item for item in envelopes if item.phase == "execute"]
    if not executions:
        executions = envelopes
    if len(executions) == 1:
        envelope = executions[0]
        if envelope.error is None or envelope.raw is not None:
            return envelope.raw
        return {
            "error": {
                "code": envelope.error.code,
                "message": envelope.error.message,
            }
        }
    return {
        "attempts": [
            {
                "attempt": envelope.attempt,
                "raw": envelope.raw,
                "error": (
                    envelope.error.as_json() if envelope.error is not None else None
                ),
            }
            for envelope in executions
        ]
    }


def _telemetry_result_view(
    task: TaskRecord,
    envelopes: list[_ExecutionEnvelope],
) -> JsonObject:
    if len(envelopes) == 1:
        result = dict(envelopes[0].telemetry)
        if (
            envelopes[0].phase == "execute"
            and result.get("estimated_cost_usd") is None
            and not result.get("components")
        ):
            result["estimated_cost_usd"] = 0.0
        return result

    winner = next(
        (
            item
            for item in reversed(envelopes)
            if item.phase == "execute" and item.error is None
        ),
        None,
    )
    base = dict((winner or envelopes[-1]).telemetry)
    components: list[JsonValue] = []
    errors: list[JsonValue] = []
    for envelope in envelopes:
        raw_components = envelope.telemetry.get("components")
        if isinstance(raw_components, list):
            for value in raw_components:
                if not isinstance(value, dict):
                    continue
                component = dict(value)
                component["attempt"] = envelope.attempt
                if winner is not envelope:
                    prefix = f"retry_overhead:{envelope.attempt}:"
                    for key in ("component", "id"):
                        name = component.get(key)
                        if isinstance(name, str) and not name.startswith(prefix):
                            component[key] = f"{prefix}{name}"
                components.append(component)
        raw_errors = envelope.telemetry.get("errors")
        if isinstance(raw_errors, list):
            errors.extend(value for value in raw_errors if isinstance(value, str))
        if envelope.error is not None:
            errors.append(f"{envelope.error.code}: {envelope.error.message}")

    base["components"] = components
    base["attempts"] = max(item.attempt for item in envelopes)
    base["errors"] = errors
    base["execution_attempts"] = [
        {
            "attempt": envelope.attempt,
            "phase": envelope.phase,
            "artifact_path": (
                result_execution_attempt(
                    task.run_id,
                    task.sample_id,
                    task.solution_id,
                    envelope.attempt,
                )
                if envelope.phase == "execute"
                else result_parse_attempt(
                    task.run_id,
                    task.sample_id,
                    task.solution_id,
                    envelope.attempt,
                )
            ),
            "error": envelope.error.as_json() if envelope.error is not None else None,
        }
        for envelope in envelopes
    ]
    for field_name in (
        "total_tokens",
        "total_pages",
        "latency_ms",
        "estimated_cost_usd",
    ):
        total, present, unknown = _sum_telemetry_field(envelopes, field_name)
        if unknown:
            base[field_name] = None
        elif present:
            base[field_name] = total
    return base


def _sum_telemetry_field(
    envelopes: list[_ExecutionEnvelope],
    field_name: str,
) -> tuple[int | float, bool, bool]:
    total: int | float = 0
    present = False
    unknown = False
    for envelope in envelopes:
        components = envelope.telemetry.get("components")
        if field_name not in envelope.telemetry:
            if field_name == "estimated_cost_usd" and (
                components is not None
                and (not isinstance(components, list) or bool(components))
            ):
                unknown = True
            elif field_name == "estimated_cost_usd" and envelope.phase == "execute":
                present = True
            continue
        value = envelope.telemetry[field_name]
        if value is None:
            if (
                field_name == "estimated_cost_usd"
                and envelope.phase == "execute"
                and not components
            ):
                present = True
            else:
                unknown = True
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            total += value
            present = True
        else:
            unknown = True
        if field_name == "estimated_cost_usd" and (
            (
                isinstance(components, list)
                and any(
                    not isinstance(component, dict)
                    or component.get("estimated_cost_usd") is None
                    for component in components
                )
            )
            or (components is not None and not isinstance(components, list))
        ):
            unknown = True
    return total, present, unknown


async def _resolve[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await cast(Awaitable[T], value)
    return value


async def _invoke[**P, T](
    callback: Callable[P, T | Awaitable[T]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    if inspect.iscoroutinefunction(callback):
        return await cast(Awaitable[T], callback(*args, **kwargs))
    thread_call = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))
    cancelled: asyncio.CancelledError | None = None
    while not thread_call.done():
        try:
            await asyncio.shield(thread_call)
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    try:
        result = thread_call.result()
    except BaseException:
        if cancelled is not None:
            raise cancelled from None
        raise
    if cancelled is not None:
        if inspect.isawaitable(result):
            child = asyncio.ensure_future(cast(Awaitable[T], result))
            child.cancel()
            try:
                await child
            except BaseException:
                raise cancelled from None
        raise cancelled
    return await _resolve(result)


def _default_finalization(
    run: RunRecord,
    tasks: tuple[TaskRecord, ...],
) -> FinalizationOutput:
    ordered = sorted(tasks, key=lambda task: task.pair_id)
    aggregate: JsonObject = {
        "run_id": run.run_id,
        "project_id": run.project_id,
        "total_tasks": run.total_tasks,
        "succeeded_tasks": run.succeeded_tasks,
        "invalid_tasks": run.invalid_tasks,
        "failed_tasks": run.failed_tasks,
        "poisoned_tasks": run.poisoned_tasks,
        "results": [
            {
                "sample_id": task.sample_id,
                "solution_id": task.solution_id,
                "status": task.status,
                "raw_artifact_path": task.raw_artifact_path,
                "parsed_artifact_path": task.parsed_artifact_path,
                "comparison_artifact_path": task.comparison_artifact_path,
                "telemetry_artifact_path": task.telemetry_artifact_path,
                "error_code": task.error_code,
                "poisoned": task.poisoned,
            }
            for task in ordered
        ],
    }
    aggregate_bytes = strict_json_bytes(normalize_json(aggregate), newline=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("aggregate.json", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16
        archive.writestr(info, aggregate_bytes)
    return FinalizationOutput(aggregate=aggregate, download_bytes=buffer.getvalue())


__all__ = ["DurableWorker", "WorkerConfig"]
