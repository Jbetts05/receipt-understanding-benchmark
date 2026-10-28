from __future__ import annotations

import builtins
from datetime import datetime
from typing import Protocol

from receipt_bench.storage.models import (
    ArtifactInfo,
    EventRecord,
    FinalizerCompletion,
    ProjectRecord,
    QueueLease,
    RunRecord,
    SeedLease,
    TaskClaim,
    TaskCompletion,
    TaskMessage,
    TaskRecord,
    TerminalAccounting,
    TerminalFailureOutcome,
    TerminalOutcome,
    Versioned,
)
from receipt_bench.storage.validation import JsonObject, JsonValue


class StorageError(RuntimeError):
    """Base class for durable storage failures."""


class NotFoundError(StorageError):
    """Raised when a requested durable record does not exist."""


class AlreadyExistsError(StorageError):
    """Raised when a conditional create collides with an existing record."""


class ConcurrencyError(StorageError):
    """Raised when an ETag/version conditional update loses a race."""


class ImmutableArtifactError(AlreadyExistsError):
    """Raised when different bytes target an immutable artifact path."""


class LeaseLostError(ConcurrencyError):
    """Raised when a queue or coordination lease is no longer owned."""


class ArtifactRepository(Protocol):
    async def create_bytes(
        self,
        path: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: JsonObject | None = None,
    ) -> ArtifactInfo: ...

    async def create_json(
        self,
        path: str,
        value: JsonValue,
        *,
        metadata: JsonObject | None = None,
    ) -> ArtifactInfo: ...

    async def get_bytes(self, path: str) -> bytes: ...

    async def get_json(self, path: str) -> JsonValue: ...

    async def get_info(self, path: str) -> ArtifactInfo: ...

    async def exists(self, path: str) -> bool: ...

    async def delete(self, path: str) -> bool: ...

    async def list_prefix(self, prefix: str) -> list[str]: ...

    async def delete_prefix(self, prefix: str) -> int: ...


class ProjectRepository(Protocol):
    async def create(self, project: ProjectRecord) -> Versioned[ProjectRecord]: ...

    async def get(self, owner_scope: str, project_id: str) -> Versioned[ProjectRecord]: ...

    async def replace(
        self,
        project: ProjectRecord,
        *,
        expected_etag: str,
    ) -> Versioned[ProjectRecord]: ...

    async def list(self, owner_scope: str) -> list[Versioned[ProjectRecord]]: ...

    async def resume_delete(self, owner_scope: str, project_id: str) -> bool: ...

    async def delete(self, owner_scope: str, project_id: str) -> None: ...


class RunRepository(Protocol):
    async def create(self, run: RunRecord) -> Versioned[RunRecord]: ...

    async def get(self, project_id: str, run_id: str) -> Versioned[RunRecord]: ...

    async def find(self, run_id: str) -> Versioned[RunRecord]: ...

    async def list(
        self,
        project_id: str,
        *,
        limit: int = 100,
    ) -> builtins.list[Versioned[RunRecord]]: ...

    async def list_all(self, project_id: str) -> builtins.list[Versioned[RunRecord]]: ...

    async def replace(
        self,
        run: RunRecord,
        *,
        expected_etag: str,
    ) -> Versioned[RunRecord]: ...

    async def list_unfinished(self) -> builtins.list[Versioned[RunRecord]]: ...

    async def delete_auxiliary(self, project_id: str, run_id: str) -> None: ...

    async def resume_delete(self, run_id: str) -> bool: ...

    async def delete(self, project_id: str, run_id: str) -> None: ...

    async def record_terminal(
        self,
        project_id: str,
        run_id: str,
        pair_id: str,
        outcome: TerminalOutcome,
        *,
        poisoned: bool = False,
    ) -> TerminalAccounting: ...

    async def claim_finalizer(
        self,
        project_id: str,
        run_id: str,
        *,
        owner: str,
        lease_until: datetime,
    ) -> Versioned[RunRecord] | None: ...

    async def renew_finalizer(
        self,
        project_id: str,
        run_id: str,
        *,
        owner: str,
        lease_until: datetime,
    ) -> Versioned[RunRecord]: ...

    async def complete_finalizer(
        self,
        project_id: str,
        run_id: str,
        *,
        owner: str,
        aggregate_artifact_path: str,
        download_artifact_path: str,
    ) -> FinalizerCompletion: ...

    async def release_finalizer(
        self,
        project_id: str,
        run_id: str,
        *,
        owner: str,
    ) -> Versioned[RunRecord]: ...


class TaskRepository(Protocol):
    async def create(self, task: TaskRecord) -> Versioned[TaskRecord]: ...

    async def get(self, run_id: str, pair_id: str) -> Versioned[TaskRecord]: ...

    async def replace(
        self,
        task: TaskRecord,
        *,
        expected_etag: str,
    ) -> Versioned[TaskRecord]: ...

    async def list_for_run(self, run_id: str) -> list[Versioned[TaskRecord]]: ...

    async def list_resumable(self) -> list[Versioned[TaskRecord]]: ...

    async def delete_for_run(self, run_id: str) -> None: ...

    async def claim(
        self,
        run_id: str,
        pair_id: str,
        *,
        owner: str,
        lease_until: datetime,
        dequeue_count: int | None = None,
        dispatch_token: str | None = None,
        message_id: str | None = None,
    ) -> TaskClaim: ...

    async def renew_claim(
        self,
        run_id: str,
        pair_id: str,
        *,
        owner: str,
        lease_until: datetime,
    ) -> Versioned[TaskRecord]: ...

    async def release_claim(
        self,
        run_id: str,
        pair_id: str,
        *,
        owner: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> Versioned[TaskRecord]: ...

    async def record_raw(
        self,
        run_id: str,
        pair_id: str,
        *,
        owner: str,
        raw_artifact_path: str,
        telemetry_artifact_path: str,
    ) -> Versioned[TaskRecord]: ...

    async def record_terminal_intent(
        self,
        run_id: str,
        pair_id: str,
        *,
        owner: str,
        outcome: TerminalFailureOutcome,
        error_code: str,
        error_message: str,
        poisoned: bool = False,
    ) -> Versioned[TaskRecord]: ...

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
    ) -> TaskCompletion: ...


class EventRepository(Protocol):
    async def latest_sequence(self, run_id: str) -> int: ...

    async def append(
        self,
        run_id: str,
        event: str,
        data: JsonObject,
        *,
        idempotency_key: str | None = None,
    ) -> EventRecord: ...

    async def read(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[EventRecord]: ...


class TaskQueue(Protocol):
    async def send(self, message: TaskMessage) -> str: ...

    async def receive(
        self,
        *,
        max_messages: int,
        visibility_timeout: int,
    ) -> list[QueueLease]: ...

    async def renew(self, lease: QueueLease, *, visibility_timeout: int) -> QueueLease: ...

    async def complete(self, lease: QueueLease) -> None: ...

    async def release(self, lease: QueueLease, *, delay: int = 0) -> None: ...

    async def poison(self, lease: QueueLease, *, reason_code: str) -> None: ...

    async def send_poison(
        self,
        lease: QueueLease,
        *,
        reason_code: str,
        outbox_id: str,
    ) -> str: ...


class SeedCoordinator(Protocol):
    async def is_complete(self, seed_id: str) -> bool: ...

    async def acquire(
        self,
        seed_id: str,
        *,
        owner: str,
        lease_seconds: int,
    ) -> SeedLease | None: ...

    async def renew(self, lease: SeedLease, *, lease_seconds: int) -> SeedLease: ...

    async def complete(self, lease: SeedLease, marker: JsonObject) -> None: ...

    async def release(self, lease: SeedLease) -> None: ...


__all__ = [
    "AlreadyExistsError",
    "ArtifactRepository",
    "ConcurrencyError",
    "EventRepository",
    "ImmutableArtifactError",
    "LeaseLostError",
    "NotFoundError",
    "ProjectRepository",
    "RunRepository",
    "SeedCoordinator",
    "StorageError",
    "TaskQueue",
    "TaskRepository",
]
