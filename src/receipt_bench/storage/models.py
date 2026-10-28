from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from receipt_bench.storage.validation import (
    JsonObject,
    JsonValue,
    bounded_text,
    pair_id,
    require_json_object,
    validate_artifact_path,
    validate_id,
    validate_metadata,
)

RunStatus = Literal["queued", "running", "finalizing", "completed", "failed"]
TaskStatus = Literal["queued", "processing", "succeeded", "invalid", "failed"]
TerminalOutcome = Literal["succeeded", "invalid", "failed"]
TerminalFailureOutcome = Literal["invalid", "failed"]


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def datetime_text(value: datetime) -> str:
    return ensure_utc(value, field_name="datetime").isoformat().replace("+00:00", "Z")


def parse_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO timestamp") from exc
    return ensure_utc(parsed, field_name=field_name)


@dataclass(frozen=True)
class Versioned[T]:
    value: T
    etag: str

    def __post_init__(self) -> None:
        if not self.etag:
            raise ValueError("ETag must be non-empty")


@dataclass(frozen=True)
class ArtifactInfo:
    path: str
    etag: str
    sha256: str
    size: int
    content_type: str
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_artifact_path(self.path)
        if self.size < 0:
            raise ValueError("Artifact size cannot be negative")
        if len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256):
            raise ValueError("Artifact SHA-256 must be lowercase hexadecimal")
        bounded_text(self.content_type, field="content_type", limit=256)
        object.__setattr__(self, "metadata", validate_metadata(self.metadata))


@dataclass(frozen=True)
class ProjectRecord:
    owner_scope: str
    project_id: str
    name: str
    description: str
    schema_artifact_path: str
    prompt_artifact_path: str
    prompt_version: str
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_id(self.owner_scope, field="owner_scope")
        validate_id(self.project_id, field="project_id")
        validate_id(self.prompt_version, field="prompt_version")
        validate_artifact_path(self.schema_artifact_path)
        validate_artifact_path(self.prompt_artifact_path)
        bounded_text(self.name, field="name", limit=512)
        bounded_text(self.description, field="description", limit=8_192)
        object.__setattr__(self, "created_at", ensure_utc(self.created_at, field_name="created_at"))
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at, field_name="updated_at"))
        object.__setattr__(self, "metadata", validate_metadata(self.metadata))

    def to_dict(self) -> JsonObject:
        return {
            "owner_scope": self.owner_scope,
            "project_id": self.project_id,
            "name": self.name,
            "description": self.description,
            "schema_artifact_path": self.schema_artifact_path,
            "prompt_artifact_path": self.prompt_artifact_path,
            "prompt_version": self.prompt_version,
            "created_at": datetime_text(self.created_at),
            "updated_at": datetime_text(self.updated_at),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: object) -> ProjectRecord:
        data = require_json_object(value, field="project")
        return cls(
            owner_scope=_required_str(data, "owner_scope"),
            project_id=_required_str(data, "project_id"),
            name=_required_str(data, "name"),
            description=_required_str(data, "description"),
            schema_artifact_path=_required_str(data, "schema_artifact_path"),
            prompt_artifact_path=_required_str(data, "prompt_artifact_path"),
            prompt_version=_required_str(data, "prompt_version"),
            created_at=parse_datetime(data.get("created_at"), field_name="created_at"),
            updated_at=parse_datetime(data.get("updated_at"), field_name="updated_at"),
            metadata=_object(data.get("metadata", {}), "metadata"),
        )


@dataclass(frozen=True)
class RunRecord:
    project_id: str
    run_id: str
    manifest_artifact_path: str
    total_tasks: int
    pending_tasks: int
    status: RunStatus = "queued"
    succeeded_tasks: int = 0
    invalid_tasks: int = 0
    failed_tasks: int = 0
    poisoned_tasks: int = 0
    finalizer_owner: str | None = None
    finalizer_lease_until: datetime | None = None
    finalizer_completed: bool = False
    completion_event_emitted: bool = False
    aggregate_artifact_path: str | None = None
    download_artifact_path: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_id(self.project_id, field="project_id")
        validate_id(self.run_id, field="run_id")
        validate_artifact_path(self.manifest_artifact_path)
        if self.aggregate_artifact_path is not None:
            validate_artifact_path(self.aggregate_artifact_path)
        if self.download_artifact_path is not None:
            validate_artifact_path(self.download_artifact_path)
        if self.status not in {"queued", "running", "finalizing", "completed", "failed"}:
            raise ValueError("Unsupported run status")
        counts = (
            self.total_tasks,
            self.pending_tasks,
            self.succeeded_tasks,
            self.invalid_tasks,
            self.failed_tasks,
            self.poisoned_tasks,
        )
        if any(count < 0 for count in counts):
            raise ValueError("Run counts cannot be negative")
        terminal = self.succeeded_tasks + self.invalid_tasks + self.failed_tasks
        if self.pending_tasks + terminal != self.total_tasks:
            raise ValueError("Run pending and terminal counts must equal total_tasks")
        if self.poisoned_tasks > self.failed_tasks:
            raise ValueError("Poisoned tasks must be a subset of failed tasks")
        if self.finalizer_owner is not None:
            validate_id(self.finalizer_owner, field="finalizer_owner")
        if (self.finalizer_owner is None) != (self.finalizer_lease_until is None):
            raise ValueError("Finalizer owner and lease must be set together")
        if self.finalizer_lease_until is not None:
            object.__setattr__(
                self,
                "finalizer_lease_until",
                ensure_utc(self.finalizer_lease_until, field_name="finalizer_lease_until"),
            )
        if self.finalizer_completed and self.pending_tasks != 0:
            raise ValueError("A finalized run cannot have pending tasks")
        if self.completion_event_emitted and not self.finalizer_completed:
            raise ValueError("A run completion event requires a finalized run")
        object.__setattr__(self, "created_at", ensure_utc(self.created_at, field_name="created_at"))
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at, field_name="updated_at"))
        object.__setattr__(self, "metadata", validate_metadata(self.metadata))

    def to_dict(self) -> JsonObject:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "manifest_artifact_path": self.manifest_artifact_path,
            "total_tasks": self.total_tasks,
            "pending_tasks": self.pending_tasks,
            "status": self.status,
            "succeeded_tasks": self.succeeded_tasks,
            "invalid_tasks": self.invalid_tasks,
            "failed_tasks": self.failed_tasks,
            "poisoned_tasks": self.poisoned_tasks,
            "finalizer_owner": self.finalizer_owner,
            "finalizer_lease_until": (
                datetime_text(self.finalizer_lease_until)
                if self.finalizer_lease_until is not None
                else None
            ),
            "finalizer_completed": self.finalizer_completed,
            "completion_event_emitted": self.completion_event_emitted,
            "aggregate_artifact_path": self.aggregate_artifact_path,
            "download_artifact_path": self.download_artifact_path,
            "created_at": datetime_text(self.created_at),
            "updated_at": datetime_text(self.updated_at),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: object) -> RunRecord:
        data = require_json_object(value, field="run")
        status = _required_str(data, "status")
        if status not in {"queued", "running", "finalizing", "completed", "failed"}:
            raise ValueError("Unsupported run status")
        lease_value = data.get("finalizer_lease_until")
        return cls(
            project_id=_required_str(data, "project_id"),
            run_id=_required_str(data, "run_id"),
            manifest_artifact_path=_required_str(data, "manifest_artifact_path"),
            total_tasks=_required_int(data, "total_tasks"),
            pending_tasks=_required_int(data, "pending_tasks"),
            status=status,  # type: ignore[arg-type]
            succeeded_tasks=_required_int(data, "succeeded_tasks"),
            invalid_tasks=_required_int(data, "invalid_tasks"),
            failed_tasks=_required_int(data, "failed_tasks"),
            poisoned_tasks=_required_int(data, "poisoned_tasks"),
            finalizer_owner=_optional_str(data, "finalizer_owner"),
            finalizer_lease_until=(
                parse_datetime(lease_value, field_name="finalizer_lease_until")
                if lease_value is not None
                else None
            ),
            finalizer_completed=_required_bool(data, "finalizer_completed"),
            completion_event_emitted=_optional_bool(
                data,
                "completion_event_emitted",
                default=False,
            ),
            aggregate_artifact_path=_optional_str(data, "aggregate_artifact_path"),
            download_artifact_path=_optional_str(data, "download_artifact_path"),
            created_at=parse_datetime(data.get("created_at"), field_name="created_at"),
            updated_at=parse_datetime(data.get("updated_at"), field_name="updated_at"),
            metadata=_object(data.get("metadata", {}), "metadata"),
        )


@dataclass(frozen=True)
class TaskRecord:
    project_id: str
    run_id: str
    sample_id: str
    solution_id: str
    image_artifact_path: str
    media_type: str
    provider_keys: tuple[str, ...] = ()
    status: TaskStatus = "queued"
    stage: str = "queued"
    attempts: int = 0
    dispatch_token: str | None = None
    dispatch_message_id: str | None = None
    dispatch_updated_at: datetime | None = None
    claim_owner: str | None = None
    claim_lease_until: datetime | None = None
    raw_artifact_path: str | None = None
    parsed_artifact_path: str | None = None
    comparison_artifact_path: str | None = None
    telemetry_artifact_path: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    poisoned: bool = False
    poison_outbox_id: str | None = None
    poison_source_message_id: str | None = None
    poison_reason_code: str | None = None
    poison_message_id: str | None = None
    poison_dispatched: bool = False
    terminal_intent_outcome: TerminalFailureOutcome | None = None
    terminal_intent_error_code: str | None = None
    terminal_intent_error_message: str | None = None
    terminal_intent_poisoned: bool = False
    terminal_pending_tasks: int | None = None
    terminal_event_emitted: bool = False
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_id(self.project_id, field="project_id")
        validate_id(self.run_id, field="run_id")
        validate_id(self.sample_id, field="sample_id")
        validate_id(self.solution_id, field="solution_id")
        validate_artifact_path(self.image_artifact_path)
        for provider_key in self.provider_keys:
            validate_id(provider_key, field="provider_key")
        if len(set(self.provider_keys)) != len(self.provider_keys):
            raise ValueError("Provider keys must be unique")
        if self.status not in {"queued", "processing", "succeeded", "invalid", "failed"}:
            raise ValueError("Unsupported task status")
        if self.attempts < 0:
            raise ValueError("Task attempts cannot be negative")
        if self.dispatch_token is not None:
            validate_id(self.dispatch_token, field="dispatch_token")
        if self.dispatch_message_id is not None:
            validate_id(self.dispatch_message_id, field="dispatch_message_id")
        if self.dispatch_message_id is not None and self.dispatch_token is None:
            raise ValueError("A dispatch message ID requires a dispatch token")
        if (self.dispatch_token is None) != (self.dispatch_updated_at is None):
            raise ValueError("Dispatch token and timestamp must be set together")
        if self.dispatch_updated_at is not None:
            object.__setattr__(
                self,
                "dispatch_updated_at",
                ensure_utc(self.dispatch_updated_at, field_name="dispatch_updated_at"),
            )
        if self.claim_owner is not None:
            validate_id(self.claim_owner, field="claim_owner")
        if (self.claim_owner is None) != (self.claim_lease_until is None):
            raise ValueError("Claim owner and lease must be set together")
        if self.claim_lease_until is not None:
            object.__setattr__(
                self,
                "claim_lease_until",
                ensure_utc(self.claim_lease_until, field_name="claim_lease_until"),
            )
        terminal = self.status in {"succeeded", "invalid", "failed"}
        if terminal and self.claim_owner is not None:
            raise ValueError("Terminal tasks cannot retain a claim")
        if self.poisoned and self.status != "failed":
            raise ValueError("Only failed tasks can be poison-terminal")
        poison_identity = (
            self.poison_outbox_id,
            self.poison_source_message_id,
            self.poison_reason_code,
        )
        if any(value is not None for value in poison_identity) and not all(
            value is not None for value in poison_identity
        ):
            raise ValueError("Poison outbox identity fields must be set together")
        for field_name, value in (
            ("poison_outbox_id", self.poison_outbox_id),
            ("poison_source_message_id", self.poison_source_message_id),
            ("poison_reason_code", self.poison_reason_code),
            ("poison_message_id", self.poison_message_id),
        ):
            if value is not None:
                validate_id(value, field=field_name)
        if self.poison_dispatched and (
            self.poison_outbox_id is None or self.poison_message_id is None
        ):
            raise ValueError("A dispatched poison message requires durable outbox IDs")
        terminal_intent = (
            self.terminal_intent_outcome,
            self.terminal_intent_error_code,
            self.terminal_intent_error_message,
        )
        if any(value is not None for value in terminal_intent) and not all(
            value is not None for value in terminal_intent
        ):
            raise ValueError("Terminal intent outcome and error fields must be set together")
        if self.terminal_intent_outcome not in {None, "invalid", "failed"}:
            raise ValueError("Unsupported terminal intent outcome")
        if self.terminal_intent_poisoned and self.terminal_intent_outcome != "failed":
            raise ValueError("Only failed terminal intent can be poisoned")
        bounded_text(
            self.terminal_intent_error_code,
            field="terminal_intent_error_code",
            limit=256,
        )
        bounded_text(
            self.terminal_intent_error_message,
            field="terminal_intent_error_message",
        )
        if self.terminal_pending_tasks is not None and (
            not terminal or self.terminal_pending_tasks < 0
        ):
            raise ValueError("Terminal pending-task count requires a terminal task")
        if self.terminal_event_emitted and not terminal:
            raise ValueError("A task completion event requires a terminal task")
        for path in (
            self.raw_artifact_path,
            self.parsed_artifact_path,
            self.comparison_artifact_path,
            self.telemetry_artifact_path,
        ):
            if path is not None:
                validate_artifact_path(path)
        bounded_text(self.media_type, field="media_type", limit=256)
        bounded_text(self.stage, field="stage", limit=128)
        bounded_text(self.error_code, field="error_code", limit=256)
        bounded_text(self.error_message, field="error_message")
        object.__setattr__(self, "created_at", ensure_utc(self.created_at, field_name="created_at"))
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at, field_name="updated_at"))
        object.__setattr__(self, "metadata", validate_metadata(self.metadata))

    @property
    def pair_id(self) -> str:
        return pair_id(self.sample_id, self.solution_id)

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "invalid", "failed"}

    def to_dict(self) -> JsonObject:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "sample_id": self.sample_id,
            "solution_id": self.solution_id,
            "pair_id": self.pair_id,
            "image_artifact_path": self.image_artifact_path,
            "media_type": self.media_type,
            "provider_keys": list(self.provider_keys),
            "status": self.status,
            "stage": self.stage,
            "attempts": self.attempts,
            "dispatch_token": self.dispatch_token,
            "dispatch_message_id": self.dispatch_message_id,
            "dispatch_updated_at": (
                datetime_text(self.dispatch_updated_at)
                if self.dispatch_updated_at is not None
                else None
            ),
            "claim_owner": self.claim_owner,
            "claim_lease_until": (
                datetime_text(self.claim_lease_until)
                if self.claim_lease_until is not None
                else None
            ),
            "raw_artifact_path": self.raw_artifact_path,
            "parsed_artifact_path": self.parsed_artifact_path,
            "comparison_artifact_path": self.comparison_artifact_path,
            "telemetry_artifact_path": self.telemetry_artifact_path,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "poisoned": self.poisoned,
            "poison_outbox_id": self.poison_outbox_id,
            "poison_source_message_id": self.poison_source_message_id,
            "poison_reason_code": self.poison_reason_code,
            "poison_message_id": self.poison_message_id,
            "poison_dispatched": self.poison_dispatched,
            "terminal_intent_outcome": self.terminal_intent_outcome,
            "terminal_intent_error_code": self.terminal_intent_error_code,
            "terminal_intent_error_message": self.terminal_intent_error_message,
            "terminal_intent_poisoned": self.terminal_intent_poisoned,
            "terminal_pending_tasks": self.terminal_pending_tasks,
            "terminal_event_emitted": self.terminal_event_emitted,
            "created_at": datetime_text(self.created_at),
            "updated_at": datetime_text(self.updated_at),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: object) -> TaskRecord:
        data = require_json_object(value, field="task")
        status = _required_str(data, "status")
        if status not in {"queued", "processing", "succeeded", "invalid", "failed"}:
            raise ValueError("Unsupported task status")
        provider_value = data.get("provider_keys", [])
        if not isinstance(provider_value, list) or not all(
            isinstance(item, str) for item in provider_value
        ):
            raise ValueError("provider_keys must be an array of strings")
        lease_value = data.get("claim_lease_until")
        dispatch_value = data.get("dispatch_updated_at")
        result = cls(
            project_id=_required_str(data, "project_id"),
            run_id=_required_str(data, "run_id"),
            sample_id=_required_str(data, "sample_id"),
            solution_id=_required_str(data, "solution_id"),
            image_artifact_path=_required_str(data, "image_artifact_path"),
            media_type=_required_str(data, "media_type"),
            provider_keys=tuple(str(item) for item in provider_value),
            status=status,  # type: ignore[arg-type]
            stage=_required_str(data, "stage"),
            attempts=_required_int(data, "attempts"),
            dispatch_token=_optional_str(data, "dispatch_token"),
            dispatch_message_id=_optional_str(data, "dispatch_message_id"),
            dispatch_updated_at=(
                parse_datetime(dispatch_value, field_name="dispatch_updated_at")
                if dispatch_value is not None
                else None
            ),
            claim_owner=_optional_str(data, "claim_owner"),
            claim_lease_until=(
                parse_datetime(lease_value, field_name="claim_lease_until")
                if lease_value is not None
                else None
            ),
            raw_artifact_path=_optional_str(data, "raw_artifact_path"),
            parsed_artifact_path=_optional_str(data, "parsed_artifact_path"),
            comparison_artifact_path=_optional_str(data, "comparison_artifact_path"),
            telemetry_artifact_path=_optional_str(data, "telemetry_artifact_path"),
            error_code=_optional_str(data, "error_code"),
            error_message=_optional_str(data, "error_message"),
            poisoned=_required_bool(data, "poisoned"),
            poison_outbox_id=_optional_str(data, "poison_outbox_id"),
            poison_source_message_id=_optional_str(data, "poison_source_message_id"),
            poison_reason_code=_optional_str(data, "poison_reason_code"),
            poison_message_id=_optional_str(data, "poison_message_id"),
            poison_dispatched=_optional_bool(data, "poison_dispatched", default=False),
            terminal_intent_outcome=_optional_terminal_outcome(
                data,
                "terminal_intent_outcome",
            ),
            terminal_intent_error_code=_optional_str(data, "terminal_intent_error_code"),
            terminal_intent_error_message=_optional_str(data, "terminal_intent_error_message"),
            terminal_intent_poisoned=_optional_bool(
                data,
                "terminal_intent_poisoned",
                default=False,
            ),
            terminal_pending_tasks=_optional_int(data, "terminal_pending_tasks"),
            terminal_event_emitted=_optional_bool(
                data,
                "terminal_event_emitted",
                default=False,
            ),
            created_at=parse_datetime(data.get("created_at"), field_name="created_at"),
            updated_at=parse_datetime(data.get("updated_at"), field_name="updated_at"),
            metadata=_object(data.get("metadata", {}), "metadata"),
        )
        stored_pair = data.get("pair_id")
        if stored_pair is not None and stored_pair != result.pair_id:
            raise ValueError("Stored pair_id does not match task identifiers")
        return result


@dataclass(frozen=True)
class TaskMessage:
    project_id: str
    run_id: str
    sample_id: str
    solution_id: str
    dispatch_token: str | None = None

    def __post_init__(self) -> None:
        validate_id(self.project_id, field="project_id")
        validate_id(self.run_id, field="run_id")
        validate_id(self.sample_id, field="sample_id")
        validate_id(self.solution_id, field="solution_id")
        if self.dispatch_token is not None:
            validate_id(self.dispatch_token, field="dispatch_token")

    @property
    def pair_id(self) -> str:
        return pair_id(self.sample_id, self.solution_id)

    def to_dict(self) -> JsonObject:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "sample_id": self.sample_id,
            "solution_id": self.solution_id,
            "pair_id": self.pair_id,
            "dispatch_token": self.dispatch_token,
        }

    @classmethod
    def from_dict(cls, value: object) -> TaskMessage:
        data = require_json_object(value, field="queue message")
        result = cls(
            project_id=_required_str(data, "project_id"),
            run_id=_required_str(data, "run_id"),
            sample_id=_required_str(data, "sample_id"),
            solution_id=_required_str(data, "solution_id"),
            dispatch_token=_optional_str(data, "dispatch_token"),
        )
        stored_pair = data.get("pair_id")
        if stored_pair is not None and stored_pair != result.pair_id:
            raise ValueError("Stored pair_id does not match queue message identifiers")
        return result


@dataclass(frozen=True)
class QueueLease:
    message_id: str
    pop_receipt: str
    message: TaskMessage
    dequeue_count: int
    lease_until: datetime

    def __post_init__(self) -> None:
        validate_id(self.message_id, field="message_id")
        if not self.pop_receipt:
            raise ValueError("Queue pop receipt must be non-empty")
        if self.dequeue_count < 1:
            raise ValueError("Queue dequeue count must be positive")
        object.__setattr__(
            self,
            "lease_until",
            ensure_utc(self.lease_until, field_name="lease_until"),
        )


@dataclass(frozen=True)
class EventRecord:
    run_id: str
    sequence: int
    event: str
    data: JsonObject
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_id(self.run_id, field="run_id")
        if self.sequence < 1:
            raise ValueError("Event sequence must be positive")
        validate_id(self.event, field="event")
        object.__setattr__(self, "data", validate_metadata(self.data))
        object.__setattr__(self, "created_at", ensure_utc(self.created_at, field_name="created_at"))

    def to_dict(self) -> JsonObject:
        return {
            "run_id": self.run_id,
            "sequence": self.sequence,
            "event": self.event,
            "data": self.data,
            "created_at": datetime_text(self.created_at),
        }

    @classmethod
    def from_dict(cls, value: object) -> EventRecord:
        data = require_json_object(value, field="event")
        return cls(
            run_id=_required_str(data, "run_id"),
            sequence=_required_int(data, "sequence"),
            event=_required_str(data, "event"),
            data=_object(data.get("data", {}), "data"),
            created_at=parse_datetime(data.get("created_at"), field_name="created_at"),
        )


@dataclass(frozen=True)
class TerminalAccounting:
    applied: bool
    run: Versioned[RunRecord]
    became_zero: bool


@dataclass(frozen=True)
class TaskClaim:
    acquired: bool
    task: Versioned[TaskRecord]


@dataclass(frozen=True)
class TaskCompletion:
    applied: bool
    task: Versioned[TaskRecord]


@dataclass(frozen=True)
class FinalizerCompletion:
    applied: bool
    run: Versioned[RunRecord]

    @property
    def value(self) -> RunRecord:
        return self.run.value

    @property
    def etag(self) -> str:
        return self.run.etag


@dataclass(frozen=True)
class SeedLease:
    seed_id: str
    owner: str
    token: str
    lease_until: datetime
    lease_seconds: int
    handle: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        validate_id(self.seed_id, field="seed_id")
        validate_id(self.owner, field="owner")
        if not self.token:
            raise ValueError("Seed lease token must be non-empty")
        if self.lease_seconds < 1:
            raise ValueError("Seed lease duration must be positive")
        object.__setattr__(
            self,
            "lease_until",
            ensure_utc(self.lease_until, field_name="lease_until"),
        )


def _required_str(data: JsonObject, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _optional_str(data: JsonObject, key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value


def _optional_terminal_outcome(data: JsonObject, key: str) -> TerminalFailureOutcome | None:
    value = _optional_str(data, key)
    if value is None:
        return None
    if value not in {"invalid", "failed"}:
        raise ValueError(f"{key} must be invalid, failed, or null")
    return "invalid" if value == "invalid" else "failed"


def _required_int(data: JsonObject, key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _optional_int(data: JsonObject, key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer or null")
    return value


def _required_bool(data: JsonObject, key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _optional_bool(data: JsonObject, key: str, *, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _object(value: JsonValue, key: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


__all__ = [
    "ArtifactInfo",
    "EventRecord",
    "FinalizerCompletion",
    "ProjectRecord",
    "QueueLease",
    "RunRecord",
    "RunStatus",
    "SeedLease",
    "TaskClaim",
    "TaskCompletion",
    "TaskMessage",
    "TaskRecord",
    "TaskStatus",
    "TerminalAccounting",
    "TerminalFailureOutcome",
    "TerminalOutcome",
    "Versioned",
    "datetime_text",
    "ensure_utc",
    "parse_datetime",
    "utc_now",
]
