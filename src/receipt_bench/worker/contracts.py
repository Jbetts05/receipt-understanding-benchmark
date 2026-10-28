from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from receipt_bench.storage.models import RunRecord, TaskRecord
from receipt_bench.storage.validation import (
    JsonObject,
    JsonValue,
    bounded_text,
    normalize_json,
    validate_metadata,
)


@dataclass(frozen=True)
class RawExecution:
    """A provider response before any dataset-specific parsing occurs."""

    raw: JsonValue
    telemetry: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", normalize_json(self.raw))
        object.__setattr__(self, "telemetry", validate_metadata(self.telemetry))


@dataclass(frozen=True)
class ParsedExecution:
    """A parsed dataset-native answer and optional deterministic comparison."""

    parsed: JsonValue
    comparison: JsonValue | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "parsed", normalize_json(self.parsed))
        if self.comparison is not None:
            object.__setattr__(self, "comparison", normalize_json(self.comparison))


@dataclass(frozen=True)
class FinalizationOutput:
    aggregate: JsonValue
    download_bytes: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "aggregate", normalize_json(self.aggregate))
        object.__setattr__(self, "download_bytes", bytes(self.download_bytes))


class PairExecutor(Protocol):
    """Integration boundary that keeps provider adapters out of worker/storage code."""

    def execute(
        self,
        task: TaskRecord,
        image_bytes: bytes,
        media_type: str,
    ) -> RawExecution | Awaitable[RawExecution]: ...

    def parse(
        self,
        task: TaskRecord,
        raw: JsonValue,
    ) -> ParsedExecution | Awaitable[ParsedExecution]: ...


class RunFinalizer(Protocol):
    def __call__(
        self,
        run: RunRecord,
        tasks: tuple[TaskRecord, ...],
    ) -> FinalizationOutput | Awaitable[FinalizationOutput]: ...


class ExecutionError(RuntimeError):
    """Safe executor failure that carries no implicit raw exception logging."""

    def __init__(
        self,
        code: str,
        public_message: str,
        *,
        retryable: bool,
        status: Literal["invalid", "failed"] = "failed",
        raw: JsonValue | None = None,
        telemetry: JsonObject | None = None,
    ) -> None:
        bounded_text(code, field="code", limit=256)
        bounded_text(public_message, field="public_message")
        if status not in {"invalid", "failed"}:
            raise ValueError("Execution error status must be invalid or failed")
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.retryable = retryable
        self.status = status
        self.raw = normalize_json(raw) if raw is not None else None
        self.telemetry = validate_metadata(telemetry or {})


class InvalidResultError(ExecutionError):
    def __init__(
        self,
        code: str = "invalid_result",
        public_message: str = "The provider response could not be parsed.",
    ) -> None:
        super().__init__(
            code,
            public_message,
            retryable=False,
            status="invalid",
        )


__all__ = [
    "ExecutionError",
    "FinalizationOutput",
    "InvalidResultError",
    "PairExecutor",
    "ParsedExecution",
    "RawExecution",
    "RunFinalizer",
]
