from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from receipt_bench.demo.api_models import (
    ComparisonMode,
    DemoSolution,
    ErrorView,
    ExecutionMode,
    RunSnapshot,
    SolutionResult,
)
from receipt_bench.demo.domain import DemoInput
from receipt_bench.models import NonFiniteNumberError, ensure_finite_numbers


@dataclass(frozen=True)
class UploadedReceipt:
    token: str
    filename: str
    media_type: str
    data: bytes
    sha256: str
    created_monotonic: float


class UploadCapacityError(RuntimeError):
    """Raised when accepting an upload would exceed an in-memory store cap."""


@dataclass(frozen=True)
class UploadReservation:
    token: str


class UploadStore:
    def __init__(
        self,
        *,
        ttl_seconds: int,
        max_items: int,
        max_total_bytes: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_items = max_items
        self._max_total_bytes = max_total_bytes
        self._clock = clock
        self._items: dict[str, UploadedReceipt] = {}
        self._total_bytes = 0
        self._reservations: dict[str, int] = {}
        self._reserved_bytes = 0
        self._lock = asyncio.Lock()

    def _prune_unlocked(self) -> int:
        cutoff = self._clock() - self._ttl_seconds
        expired = [token for token, item in self._items.items() if item.created_monotonic < cutoff]
        for token in expired:
            self._total_bytes -= len(self._items.pop(token).data)
        return len(expired)

    def _ensure_item_capacity_unlocked(self) -> None:
        if len(self._items) + len(self._reservations) >= self._max_items:
            raise UploadCapacityError(
                f"Pending or in-flight upload limit reached ({self._max_items} items). "
                "Start a run or wait for uploads to expire."
            )

    def _ensure_byte_capacity_unlocked(self, additional_bytes: int) -> None:
        if self._total_bytes + self._reserved_bytes + additional_bytes > self._max_total_bytes:
            raise UploadCapacityError(
                "Pending or in-flight upload memory limit reached. "
                "Start a run or wait for uploads to expire."
            )

    async def reserve(self, expected_bytes: int) -> UploadReservation:
        if expected_bytes < 0:
            raise ValueError("Upload reservation size cannot be negative")
        async with self._lock:
            self._prune_unlocked()
            self._ensure_item_capacity_unlocked()
            self._ensure_byte_capacity_unlocked(expected_bytes)
            token = secrets.token_urlsafe(24)
            while token in self._reservations:
                token = secrets.token_urlsafe(24)
            self._reservations[token] = expected_bytes
            self._reserved_bytes += expected_bytes
            return UploadReservation(token=token)

    async def resize_reservation(
        self,
        reservation: UploadReservation,
        expected_bytes: int,
    ) -> None:
        if expected_bytes < 0:
            raise ValueError("Upload reservation size cannot be negative")
        async with self._lock:
            self._prune_unlocked()
            current = self._reservations.get(reservation.token)
            if current is None:
                raise ValueError("Upload reservation is invalid or has been consumed")
            if expected_bytes <= current:
                return
            additional_bytes = expected_bytes - current
            self._ensure_byte_capacity_unlocked(additional_bytes)
            self._reservations[reservation.token] = expected_bytes
            self._reserved_bytes += additional_bytes

    async def commit(
        self,
        *,
        reservation: UploadReservation,
        filename: str,
        media_type: str,
        data: bytes,
        sha256: str,
    ) -> UploadedReceipt:
        async with self._lock:
            self._prune_unlocked()
            reserved_bytes = self._reservations.get(reservation.token)
            if reserved_bytes is None:
                raise ValueError("Upload reservation is invalid or has been consumed")
            if len(data) > reserved_bytes:
                raise ValueError("Upload data exceeds its reserved capacity")
            token = secrets.token_urlsafe(24)
            while token in self._items:
                token = secrets.token_urlsafe(24)
            item = UploadedReceipt(
                token=token,
                filename=filename,
                media_type=media_type,
                data=data,
                sha256=sha256,
                created_monotonic=self._clock(),
            )
            self._reservations.pop(reservation.token)
            self._reserved_bytes -= reserved_bytes
            self._items[token] = item
            self._total_bytes += len(data)
            return item

    async def release_reservation(self, reservation: UploadReservation) -> None:
        async with self._lock:
            reserved_bytes = self._reservations.pop(reservation.token, None)
            if reserved_bytes is not None:
                self._reserved_bytes -= reserved_bytes

    async def add(
        self,
        *,
        filename: str,
        media_type: str,
        data: bytes,
        sha256: str,
    ) -> UploadedReceipt:
        reservation = await self.reserve(len(data))
        try:
            return await self.commit(
                reservation=reservation,
                filename=filename,
                media_type=media_type,
                data=data,
                sha256=sha256,
            )
        finally:
            await self.release_reservation(reservation)

    async def take_many(self, tokens: list[str]) -> list[UploadedReceipt]:
        if len(tokens) != len(set(tokens)):
            raise KeyError("Upload tokens must be unique")
        async with self._lock:
            self._prune_unlocked()
            missing = [token for token in tokens if token not in self._items]
            if missing:
                raise KeyError("One or more upload tokens are invalid or expired")
            items = [self._items.pop(token) for token in tokens]
            self._total_bytes -= sum(len(item.data) for item in items)
            return items

    async def prune_expired(self) -> int:
        async with self._lock:
            return self._prune_unlocked()

    async def discard(self, token: str) -> bool:
        async with self._lock:
            item = self._items.pop(token, None)
            if item is None:
                return False
            self._total_bytes -= len(item.data)
            return True

    async def usage(self) -> tuple[int, int]:
        async with self._lock:
            return len(self._items), self._total_bytes

    async def reservation_usage(self) -> tuple[int, int]:
        async with self._lock:
            return len(self._reservations), self._reserved_bytes

    async def close(self) -> None:
        async with self._lock:
            self._items.clear()
            self._total_bytes = 0
            self._reservations.clear()
            self._reserved_bytes = 0


@dataclass(frozen=True)
class RunEvent:
    sequence: int
    event: str
    data: dict[str, Any]


RunStatus = Literal["running", "completed", "failed"]


@dataclass
class RunRecord:
    run_id: str
    comparison_mode: ComparisonMode
    execution_mode: ExecutionMode
    inputs: list[DemoInput]
    solutions: list[DemoSolution]
    manifest_bytes: bytes
    total_pairs: int
    created_monotonic: float = field(default_factory=time.monotonic)
    status: RunStatus = "running"
    results: dict[tuple[str, str], SolutionResult] = field(default_factory=dict)
    events: list[RunEvent] = field(default_factory=list)
    error: ErrorView | None = None
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)

    @property
    def is_mock(self) -> bool:
        return self.execution_mode == "mock"

    @property
    def completed_pairs(self) -> int:
        return len(self.results)

    def ordered_results(self) -> list[SolutionResult]:
        receipt_order = {
            input_receipt.receipt_id: index for index, input_receipt in enumerate(self.inputs)
        }
        solution_order = {solution.id: solution.order for solution in self.solutions}
        return sorted(
            self.results.values(),
            key=lambda result: (
                receipt_order.get(result.receipt_id, 10**9),
                solution_order.get(result.solution_id, 10**9),
            ),
        )

    def snapshot(self) -> RunSnapshot:
        return RunSnapshot(
            run_id=self.run_id,
            status=self.status,
            comparison_mode=self.comparison_mode,
            execution_mode=self.execution_mode,
            mock=self.is_mock,
            total_pairs=self.total_pairs,
            completed_pairs=self.completed_pairs,
            receipt_order=[item.receipt_id for item in self.inputs],
            solution_order=[solution.id for solution in self.solutions],
            results=self.ordered_results(),
            error=self.error,
        )

    async def publish(self, event: str, data: dict[str, Any]) -> None:
        ensure_finite_numbers(data)
        async with self.condition:
            self.events.append(RunEvent(sequence=len(self.events) + 1, event=event, data=data))
            self.condition.notify_all()

    async def add_result(self, result: SolutionResult) -> None:
        try:
            ensure_finite_numbers(result)
        except NonFiniteNumberError:
            result = SolutionResult(
                receipt_id=result.receipt_id,
                sample_id=result.sample_id,
                solution_id=result.solution_id,
                solution_order=result.solution_order,
                status="invalid",
                mock=result.mock,
                schema_valid=False,
                latency_ms=0.0,
                error=ErrorView(
                    code="non_finite_provider_output",
                    message="The provider response contained a non-finite numeric value.",
                ),
            )
        async with self.condition:
            self.results[(result.receipt_id, result.solution_id)] = result
            self.events.append(
                RunEvent(
                    sequence=len(self.events) + 1,
                    event="solution_completed",
                    data={
                        "completed_pairs": self.completed_pairs,
                        "total_pairs": self.total_pairs,
                        "result": result.model_dump(mode="json"),
                    },
                )
            )
            self.condition.notify_all()

    async def complete(self) -> None:
        async with self.condition:
            self.status = "completed"
            self.events.append(
                RunEvent(
                    sequence=len(self.events) + 1,
                    event="run_completed",
                    data={
                        "completed_pairs": self.completed_pairs,
                        "total_pairs": self.total_pairs,
                    },
                )
            )
            self.condition.notify_all()

    async def fail(self, error: ErrorView) -> None:
        async with self.condition:
            self.status = "failed"
            self.error = error
            self.events.append(
                RunEvent(
                    sequence=len(self.events) + 1,
                    event="run_failed",
                    data={"error": error.model_dump(mode="json")},
                )
            )
            self.condition.notify_all()

    def release_input_bytes(self) -> None:
        self.inputs = [
            DemoInput(
                receipt_id=item.receipt_id,
                sample_id=item.sample_id,
                title=item.title,
                filename=item.filename,
                image_bytes=b"",
                media_type=item.media_type,
                sha256=item.sha256,
                sample=item.sample,
            )
            for item in self.inputs
        ]


class ActiveRunLimitError(RuntimeError):
    """Raised when a new run would exceed the application-wide active-run cap."""


class RunReservationConflictError(RuntimeError):
    """Raised when another request is already creating the same deterministic run."""


@dataclass(frozen=True)
class RunReservation:
    run_id: str
    token: str | None

    @property
    def is_existing(self) -> bool:
        return self.token is None


class RunStore:
    def __init__(self, *, ttl_seconds: int, max_runs: int, max_active_runs: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_runs = max_runs
        self._max_active_runs = max_active_runs
        self._records: dict[str, RunRecord] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._reservations: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _prune_unlocked(self) -> None:
        cutoff = time.monotonic() - self._ttl_seconds
        expired = [
            run_id
            for run_id, record in self._records.items()
            if record.status != "running" and record.created_monotonic < cutoff
        ]
        for run_id in expired:
            self._records.pop(run_id, None)
            self._tasks.pop(run_id, None)

        completed = sorted(
            (record for record in self._records.values() if record.status != "running"),
            key=lambda record: record.created_monotonic,
        )
        while len(self._records) > self._max_runs and completed:
            oldest = completed.pop(0)
            self._records.pop(oldest.run_id, None)
            self._tasks.pop(oldest.run_id, None)

    def _ensure_active_capacity_unlocked(self) -> None:
        active_count = sum(record.status == "running" for record in self._records.values())
        if active_count + len(self._reservations) >= self._max_active_runs:
            raise ActiveRunLimitError(
                f"Active run limit reached ({self._max_active_runs}). "
                "Wait for a running comparison to finish."
            )

    async def reserve(self, run_id: str) -> RunReservation:
        async with self._lock:
            self._prune_unlocked()
            if run_id in self._records:
                return RunReservation(run_id=run_id, token=None)
            if run_id in self._reservations.values():
                raise RunReservationConflictError(
                    "An identical deterministic run is already being prepared."
                )
            self._ensure_active_capacity_unlocked()
            token = secrets.token_urlsafe(24)
            self._reservations[token] = run_id
            return RunReservation(run_id=run_id, token=token)

    async def release_reservation(self, reservation: RunReservation) -> None:
        if reservation.token is None:
            return
        async with self._lock:
            if self._reservations.get(reservation.token) == reservation.run_id:
                self._reservations.pop(reservation.token)

    async def create(self, record: RunRecord, reservation: RunReservation) -> bool:
        async with self._lock:
            self._prune_unlocked()
            if reservation.run_id != record.run_id:
                raise ValueError("Run reservation does not match the record")
            if reservation.token is None:
                if record.run_id not in self._records:
                    raise ValueError("Existing-run reservation no longer exists")
                return False
            if self._reservations.get(reservation.token) != record.run_id:
                raise ValueError("Run reservation is invalid or has already been consumed")
            try:
                if record.run_id in self._records:
                    return False
                self._records[record.run_id] = record
                return True
            finally:
                self._reservations.pop(reservation.token, None)

    async def attach_task(self, run_id: str, task: asyncio.Task[None]) -> None:
        async with self._lock:
            self._tasks[run_id] = task

    async def get(self, run_id: str) -> RunRecord:
        async with self._lock:
            self._prune_unlocked()
            record = self._records.get(run_id)
            if record is None:
                raise KeyError(run_id)
            return record

    async def close(self) -> None:
        async with self._lock:
            tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            self._tasks.clear()
            self._records.clear()
            self._reservations.clear()


__all__ = [
    "ActiveRunLimitError",
    "RunEvent",
    "RunRecord",
    "RunReservation",
    "RunReservationConflictError",
    "RunStore",
    "UploadCapacityError",
    "UploadReservation",
    "UploadStore",
    "UploadedReceipt",
]
