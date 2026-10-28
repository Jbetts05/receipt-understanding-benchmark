from __future__ import annotations

import asyncio
import builtins
import os
import secrets
import shutil
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

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
    datetime_text,
    parse_datetime,
    utc_now,
)
from receipt_bench.storage.paths import run_event
from receipt_bench.storage.protocols import (
    AlreadyExistsError,
    ConcurrencyError,
    ImmutableArtifactError,
    LeaseLostError,
    NotFoundError,
)
from receipt_bench.storage.validation import (
    MAX_EVENT_BYTES,
    MAX_METADATA_BYTES,
    MAX_QUEUE_MESSAGE_BYTES,
    JsonObject,
    JsonValue,
    bounded_text,
    content_sha256,
    require_json_object,
    split_pair_id,
    strict_json_bytes,
    strict_json_loads,
    validate_artifact_path,
    validate_content_type,
    validate_deletion_prefix,
    validate_id,
    validate_metadata,
)

_LOCKS_GUARD = threading.Lock()
_ROOT_LOCKS: dict[str, threading.RLock] = {}
_EVENT_COUNTER_BYTES = MAX_EVENT_BYTES * 2


def _root_lock(root: Path) -> threading.RLock:
    key = os.path.normcase(str(root.resolve()))
    with _LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(key, threading.RLock())


def _checked_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None:
        raise ValueError("Storage clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _temporary_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.{secrets.token_hex(12)}.tmp")


def _write_temporary(target: Path, data: bytes) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(target)
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return temporary


def _atomic_replace(target: Path, data: bytes) -> None:
    temporary = _write_temporary(target, data)
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_create(target: Path, data: bytes) -> bool:
    temporary = _write_temporary(target, data)
    try:
        try:
            os.link(temporary, target)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _read_json_object(path: Path, *, max_bytes: int) -> JsonObject:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise NotFoundError(str(path)) from exc
    value = strict_json_loads(raw, max_bytes=max_bytes)
    return require_json_object(value)


def _record_filename(value: str) -> str:
    return content_sha256(value.encode("utf-8"))


def _remove_tree(path: Path) -> None:
    with suppress(FileNotFoundError):
        shutil.rmtree(path)


class LocalArtifactRepository:
    """Immutable artifact storage rooted at a local directory."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._lock = _root_lock(self.root)

    def _target(self, path: str) -> Path:
        validated = validate_artifact_path(path)
        target = self.root.joinpath(*validated.split("/"))
        try:
            target.resolve().relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Artifact path escapes repository root") from exc
        return target

    def _metadata_target(self, path: str) -> Path:
        validated = validate_artifact_path(path)
        return (
            self.root
            / ".metadata"
            / Path(*validated.split("/")).with_suffix(Path(validated).suffix + ".json")
        )

    def _create_bytes(
        self,
        path: str,
        data: bytes,
        *,
        content_type: str,
        metadata: JsonObject | None,
    ) -> ArtifactInfo:
        target = self._target(path)
        content_type = validate_content_type(content_type)
        normalized_metadata = validate_metadata(metadata or {})
        digest = content_sha256(data)
        metadata_payload: JsonObject = {
            "path": path,
            "sha256": digest,
            "size": len(data),
            "content_type": content_type,
            "metadata": normalized_metadata,
        }
        metadata_bytes = strict_json_bytes(
            metadata_payload,
            max_bytes=MAX_METADATA_BYTES,
            newline=True,
        )
        with self._lock:
            created = _atomic_create(target, data)
            if not created:
                existing = target.read_bytes()
                if existing != data:
                    raise ImmutableArtifactError(
                        f"Immutable artifact already exists with different bytes: {path}"
                    )
            metadata_target = self._metadata_target(path)
            if not metadata_target.exists():
                _atomic_create(metadata_target, metadata_bytes)
            else:
                existing_metadata = _read_json_object(
                    metadata_target,
                    max_bytes=MAX_METADATA_BYTES,
                )
                if (
                    existing_metadata.get("sha256") != digest
                    or existing_metadata.get("content_type") != content_type
                    or existing_metadata.get("metadata") != normalized_metadata
                ):
                    raise ImmutableArtifactError(f"Immutable artifact metadata collision: {path}")
            return ArtifactInfo(
                path=path,
                etag=digest,
                sha256=digest,
                size=len(data),
                content_type=content_type,
                metadata=normalized_metadata,
            )

    async def create_bytes(
        self,
        path: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: JsonObject | None = None,
    ) -> ArtifactInfo:
        return await asyncio.to_thread(
            self._create_bytes,
            path,
            bytes(data),
            content_type=content_type,
            metadata=metadata,
        )

    async def create_json(
        self,
        path: str,
        value: JsonValue,
        *,
        metadata: JsonObject | None = None,
    ) -> ArtifactInfo:
        return await asyncio.to_thread(
            self._create_bytes,
            path,
            strict_json_bytes(value, newline=True),
            content_type="application/json",
            metadata=metadata,
        )

    def _get_bytes(self, path: str) -> bytes:
        target = self._target(path)
        try:
            return target.read_bytes()
        except FileNotFoundError as exc:
            raise NotFoundError(path) from exc

    async def get_bytes(self, path: str) -> bytes:
        return await asyncio.to_thread(self._get_bytes, path)

    async def get_json(self, path: str) -> JsonValue:
        return strict_json_loads(await self.get_bytes(path))

    def _get_info(self, path: str) -> ArtifactInfo:
        target = self._target(path)
        try:
            data = target.read_bytes()
        except FileNotFoundError as exc:
            raise NotFoundError(path) from exc
        metadata_target = self._metadata_target(path)
        content_type = "application/octet-stream"
        metadata: JsonObject = {}
        if metadata_target.exists():
            stored = _read_json_object(metadata_target, max_bytes=MAX_METADATA_BYTES)
            stored_type = stored.get("content_type")
            stored_metadata = stored.get("metadata")
            if isinstance(stored_type, str):
                content_type = stored_type
            if isinstance(stored_metadata, dict):
                metadata = stored_metadata
        digest = content_sha256(data)
        return ArtifactInfo(
            path=path,
            etag=digest,
            sha256=digest,
            size=len(data),
            content_type=content_type,
            metadata=metadata,
        )

    async def get_info(self, path: str) -> ArtifactInfo:
        return await asyncio.to_thread(self._get_info, path)

    async def exists(self, path: str) -> bool:
        return await asyncio.to_thread(self._target(path).is_file)

    def _delete(self, path: str) -> bool:
        target = self._target(path)
        metadata_target = self._metadata_target(path)
        with self._lock:
            existed = target.is_file()
            target.unlink(missing_ok=True)
            metadata_target.unlink(missing_ok=True)
        return existed

    async def delete(self, path: str) -> bool:
        return await asyncio.to_thread(self._delete, path)

    def _prefix_target(self, prefix: str) -> tuple[str, Path]:
        validated = validate_deletion_prefix(prefix)
        return validated, self._target(validated[:-1])

    async def list_prefix(self, prefix: str) -> list[str]:
        validated, target = self._prefix_target(prefix)
        if not target.is_dir():
            return []
        return sorted(
            f"{validated}{path.relative_to(target).as_posix()}"
            for path in target.rglob("*")
            if path.is_file()
        )

    async def delete_prefix(self, prefix: str) -> int:
        validated, target = self._prefix_target(prefix)
        metadata_target = self.root / ".metadata" / Path(*validated[:-1].split("/"))
        with self._lock:
            count = sum(1 for path in target.rglob("*") if path.is_file()) if target.is_dir() else 0
            _remove_tree(target)
            _remove_tree(metadata_target)
        return count


class _LocalRecordStore[RecordT: (ProjectRecord, RunRecord, TaskRecord)]:
    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], datetime],
        loader: Callable[[object], RecordT],
    ) -> None:
        self.root = root.resolve()
        self._clock = clock
        self._loader: Callable[[object], RecordT] = loader
        self._lock = _root_lock(self.root)

    def _read(self, path: Path) -> Versioned[RecordT]:
        envelope = _read_json_object(path, max_bytes=MAX_METADATA_BYTES)
        version = envelope.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError(f"Invalid durable record version: {path}")
        return Versioned(self._loader(envelope.get("payload")), str(version))

    def _create(self, path: Path, record: RecordT) -> Versioned[RecordT]:
        payload: JsonObject = {"version": 1, "payload": record.to_dict()}
        encoded = strict_json_bytes(payload, max_bytes=MAX_METADATA_BYTES, newline=True)
        with self._lock:
            if not _atomic_create(path, encoded):
                raise AlreadyExistsError(str(path))
            return Versioned(record, "1")

    def _replace(
        self,
        path: Path,
        record: RecordT,
        *,
        expected_etag: str,
    ) -> Versioned[RecordT]:
        with self._lock:
            current = self._read(path)
            if current.etag != expected_etag:
                raise ConcurrencyError(
                    f"ETag mismatch: expected {expected_etag}, found {current.etag}"
                )
            version = int(current.etag) + 1
            payload: JsonObject = {"version": version, "payload": record.to_dict()}
            encoded = strict_json_bytes(payload, max_bytes=MAX_METADATA_BYTES, newline=True)
            _atomic_replace(path, encoded)
            return Versioned(record, str(version))


class LocalProjectRepository(_LocalRecordStore[ProjectRecord]):
    def __init__(self, root: Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        super().__init__(root, clock=clock, loader=ProjectRecord.from_dict)

    def _path(self, owner_scope: str, project_id: str) -> Path:
        return (
            self.root
            / validate_id(owner_scope, field="owner_scope")
            / f"{validate_id(project_id, field='project_id')}.json"
        )

    async def create(self, project: ProjectRecord) -> Versioned[ProjectRecord]:
        return self._create(self._path(project.owner_scope, project.project_id), project)

    async def get(self, owner_scope: str, project_id: str) -> Versioned[ProjectRecord]:
        return self._read(self._path(owner_scope, project_id))

    async def replace(
        self,
        project: ProjectRecord,
        *,
        expected_etag: str,
    ) -> Versioned[ProjectRecord]:
        return self._replace(
            self._path(project.owner_scope, project.project_id),
            project,
            expected_etag=expected_etag,
        )

    async def list(self, owner_scope: str) -> list[Versioned[ProjectRecord]]:
        owner = validate_id(owner_scope, field="owner_scope")
        directory = self.root / owner
        if not directory.is_dir():
            return []
        return sorted(
            (self._read(path) for path in directory.glob("*.json")),
            key=lambda stored: stored.value.project_id,
        )

    async def resume_delete(self, owner_scope: str, project_id: str) -> bool:
        del owner_scope, project_id
        return False

    async def delete(self, owner_scope: str, project_id: str) -> None:
        path = self._path(owner_scope, project_id)
        with self._lock:
            path.unlink(missing_ok=True)
            with suppress(OSError):
                path.parent.rmdir()


class LocalRunRepository(_LocalRecordStore[RunRecord]):
    def __init__(self, root: Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        super().__init__(root, clock=clock, loader=RunRecord.from_dict)

    def _path(self, project_id: str, run_id: str) -> Path:
        return (
            self.root
            / validate_id(project_id, field="project_id")
            / validate_id(run_id, field="run_id")
            / "record.json"
        )

    def _terminal_path(self, project_id: str, run_id: str, task_pair_id: str) -> Path:
        split_pair_id(task_pair_id)
        return (
            self._path(project_id, run_id).parent
            / "terminal"
            / f"{_record_filename(task_pair_id)}.json"
        )

    async def create(self, run: RunRecord) -> Versioned[RunRecord]:
        return self._create(self._path(run.project_id, run.run_id), run)

    async def get(self, project_id: str, run_id: str) -> Versioned[RunRecord]:
        return self._read(self._path(project_id, run_id))

    async def find(self, run_id: str) -> Versioned[RunRecord]:
        run = validate_id(run_id, field="run_id")
        matches = [
            path
            for path in self.root.glob(f"*/{run}/record.json")
            if path.is_file()
        ]
        if not matches:
            raise NotFoundError(run_id)
        if len(matches) != 1:
            raise ConcurrencyError("Run ID is not globally unique")
        return self._read(matches[0])

    async def list(
        self,
        project_id: str,
        *,
        limit: int = 100,
    ) -> builtins.list[Versioned[RunRecord]]:
        project = validate_id(project_id, field="project_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Run list limit must be between 1 and 100")
        return (await self.list_all(project))[:limit]

    async def list_all(
        self,
        project_id: str,
    ) -> builtins.list[Versioned[RunRecord]]:
        project = validate_id(project_id, field="project_id")
        directory = self.root / project
        if not directory.is_dir():
            return []
        records = [
            self._read(path)
            for path in directory.glob("*/record.json")
            if path.is_file()
        ]
        return sorted(
            records,
            key=lambda stored: (stored.value.created_at, stored.value.run_id),
            reverse=True,
        )

    async def replace(
        self,
        run: RunRecord,
        *,
        expected_etag: str,
    ) -> Versioned[RunRecord]:
        return self._replace(
            self._path(run.project_id, run.run_id),
            run,
            expected_etag=expected_etag,
        )

    async def list_unfinished(self) -> builtins.list[Versioned[RunRecord]]:
        if not self.root.is_dir():
            return []
        records = [self._read(path) for path in self.root.glob("*/*/record.json") if path.is_file()]
        return sorted(
            (
                stored
                for stored in records
                if (
                    not stored.value.finalizer_completed
                    and stored.value.status not in {"completed", "failed"}
                )
                or (stored.value.finalizer_completed and not stored.value.completion_event_emitted)
            ),
            key=lambda stored: (stored.value.created_at, stored.value.run_id),
        )

    async def delete_auxiliary(self, project_id: str, run_id: str) -> None:
        record_path = self._path(project_id, run_id)
        with self._lock:
            _remove_tree(record_path.parent / "terminal")

    async def resume_delete(self, run_id: str) -> bool:
        del run_id
        return False

    async def delete(self, project_id: str, run_id: str) -> None:
        record_path = self._path(project_id, run_id)
        directory = record_path.parent
        with self._lock:
            record_path.unlink(missing_ok=True)
            with suppress(OSError):
                directory.rmdir()
                directory.parent.rmdir()

    async def record_terminal(
        self,
        project_id: str,
        run_id: str,
        task_pair_id: str,
        outcome: TerminalOutcome,
        *,
        poisoned: bool = False,
    ) -> TerminalAccounting:
        if outcome not in {"succeeded", "invalid", "failed"}:
            raise ValueError("Unsupported terminal outcome")
        if poisoned and outcome != "failed":
            raise ValueError("Only a failed outcome can be poison-terminal")
        marker_path = self._terminal_path(project_id, run_id, task_pair_id)
        marker: JsonObject = {
            "pair_id": task_pair_id,
            "outcome": outcome,
            "poisoned": poisoned,
            "recorded_at": datetime_text(_checked_now(self._clock)),
        }
        marker_bytes = strict_json_bytes(marker, max_bytes=MAX_METADATA_BYTES, newline=True)
        record_path = self._path(project_id, run_id)
        with self._lock:
            before = self._read(record_path)
            applied = _atomic_create(marker_path, marker_bytes)
            if applied:
                succeeded = before.value.succeeded_tasks + (outcome == "succeeded")
                invalid = before.value.invalid_tasks + (outcome == "invalid")
                failed = before.value.failed_tasks + (outcome == "failed")
                poison_count = before.value.poisoned_tasks + poisoned
                pending = before.value.pending_tasks - 1
                if pending < 0:
                    raise ConcurrencyError("Terminal ledger exceeds the run task count")
            else:
                existing = _read_json_object(marker_path, max_bytes=MAX_METADATA_BYTES)
                if (
                    existing.get("pair_id") != task_pair_id
                    or existing.get("outcome") != outcome
                    or existing.get("poisoned") != poisoned
                ):
                    raise ConcurrencyError(
                        f"Terminal outcome already recorded differently for {task_pair_id}"
                    )
                # A pre-existing marker can be left ahead of the run record by a
                # process crash, so reconcile the complete ledger on that rare path.
                markers = [
                    _read_json_object(path, max_bytes=MAX_METADATA_BYTES)
                    for path in marker_path.parent.glob("*.json")
                ]
                if len(markers) > before.value.total_tasks:
                    raise ConcurrencyError("Terminal ledger exceeds the run task count")
                succeeded = sum(item.get("outcome") == "succeeded" for item in markers)
                invalid = sum(item.get("outcome") == "invalid" for item in markers)
                failed = sum(item.get("outcome") == "failed" for item in markers)
                poison_count = sum(item.get("poisoned") is True for item in markers)
                pending = before.value.total_tasks - len(markers)
            status = before.value.status
            if status == "queued" and pending < before.value.total_tasks:
                status = "running"
            updated = replace(
                before.value,
                pending_tasks=pending,
                succeeded_tasks=succeeded,
                invalid_tasks=invalid,
                failed_tasks=failed,
                poisoned_tasks=poison_count,
                status=status,
                updated_at=_checked_now(self._clock),
            )
            after = self._replace(record_path, updated, expected_etag=before.etag)
            return TerminalAccounting(
                applied=applied,
                run=after,
                became_zero=before.value.pending_tasks > 0 and pending == 0,
            )

    async def claim_finalizer(
        self,
        project_id: str,
        run_id: str,
        *,
        owner: str,
        lease_until: datetime,
    ) -> Versioned[RunRecord] | None:
        validate_id(owner, field="owner")
        now = _checked_now(self._clock)
        if lease_until.tzinfo is None or lease_until <= now:
            raise ValueError("Finalizer lease must expire in the future")
        path = self._path(project_id, run_id)
        with self._lock:
            current = self._read(path)
            run = current.value
            active = run.finalizer_lease_until is not None and run.finalizer_lease_until > now
            if (
                run.pending_tasks != 0
                or run.metadata.get("dispatch_complete") is not True
                or run.finalizer_completed
                or (active and run.finalizer_owner != owner)
            ):
                return None
            updated = replace(
                run,
                status="finalizing",
                finalizer_owner=owner,
                finalizer_lease_until=lease_until,
                updated_at=now,
            )
            return self._replace(path, updated, expected_etag=current.etag)

    async def renew_finalizer(
        self,
        project_id: str,
        run_id: str,
        *,
        owner: str,
        lease_until: datetime,
    ) -> Versioned[RunRecord]:
        path = self._path(project_id, run_id)
        with self._lock:
            current = self._read(path)
            now = _checked_now(self._clock)
            if (
                current.value.finalizer_owner != owner
                or current.value.finalizer_completed
                or current.value.finalizer_lease_until is None
                or current.value.finalizer_lease_until <= now
            ):
                raise LeaseLostError("Finalizer lease is not owned")
            if lease_until.tzinfo is None or lease_until <= now:
                raise ValueError("Finalizer lease must expire in the future")
            updated = replace(
                current.value,
                finalizer_lease_until=lease_until,
                updated_at=now,
            )
            return self._replace(path, updated, expected_etag=current.etag)

    async def complete_finalizer(
        self,
        project_id: str,
        run_id: str,
        *,
        owner: str,
        aggregate_artifact_path: str,
        download_artifact_path: str,
    ) -> FinalizerCompletion:
        path = self._path(project_id, run_id)
        with self._lock:
            current = self._read(path)
            if current.value.finalizer_completed:
                return FinalizerCompletion(applied=False, run=current)
            now = _checked_now(self._clock)
            if (
                current.value.finalizer_owner != owner
                or current.value.finalizer_lease_until is None
                or current.value.finalizer_lease_until <= now
            ):
                raise LeaseLostError("Finalizer lease is not owned")
            updated = replace(
                current.value,
                status="completed",
                finalizer_owner=None,
                finalizer_lease_until=None,
                finalizer_completed=True,
                aggregate_artifact_path=aggregate_artifact_path,
                download_artifact_path=download_artifact_path,
                updated_at=now,
            )
            stored = self._replace(path, updated, expected_etag=current.etag)
            return FinalizerCompletion(applied=True, run=stored)

    async def release_finalizer(
        self,
        project_id: str,
        run_id: str,
        *,
        owner: str,
    ) -> Versioned[RunRecord]:
        path = self._path(project_id, run_id)
        with self._lock:
            current = self._read(path)
            if current.value.finalizer_completed:
                return current
            now = _checked_now(self._clock)
            if (
                current.value.finalizer_owner != owner
                or current.value.finalizer_lease_until is None
                or current.value.finalizer_lease_until <= now
            ):
                raise LeaseLostError("Finalizer lease is not owned")
            updated = replace(
                current.value,
                status="running",
                finalizer_owner=None,
                finalizer_lease_until=None,
                updated_at=now,
            )
            return self._replace(path, updated, expected_etag=current.etag)


class LocalTaskRepository(_LocalRecordStore[TaskRecord]):
    def __init__(self, root: Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        super().__init__(root, clock=clock, loader=TaskRecord.from_dict)

    def _path(self, run_id: str, task_pair_id: str) -> Path:
        split_pair_id(task_pair_id)
        return (
            self.root
            / validate_id(run_id, field="run_id")
            / f"{_record_filename(task_pair_id)}.json"
        )

    async def create(self, task: TaskRecord) -> Versioned[TaskRecord]:
        return self._create(self._path(task.run_id, task.pair_id), task)

    async def get(self, run_id: str, task_pair_id: str) -> Versioned[TaskRecord]:
        stored = await asyncio.to_thread(
            self._read,
            self._path(run_id, task_pair_id),
        )
        if stored.value.pair_id != task_pair_id:
            raise ValueError("Task record key does not match its payload")
        return stored

    async def replace(
        self,
        task: TaskRecord,
        *,
        expected_etag: str,
    ) -> Versioned[TaskRecord]:
        return self._replace(
            self._path(task.run_id, task.pair_id),
            task,
            expected_etag=expected_etag,
        )

    async def list_for_run(self, run_id: str) -> list[Versioned[TaskRecord]]:
        directory = self.root / validate_id(run_id, field="run_id")
        if not directory.is_dir():
            return []
        records = await asyncio.gather(
            *(asyncio.to_thread(self._read, path) for path in directory.glob("*.json"))
        )
        return sorted(
            records,
            key=lambda stored: stored.value.pair_id,
        )

    async def list_resumable(self) -> list[Versioned[TaskRecord]]:
        if not self.root.is_dir():
            return []
        records = await asyncio.gather(
            *(
                asyncio.to_thread(self._read, path)
                for path in self.root.glob("*/*.json")
            )
        )
        return sorted(
            (
                stored
                for stored in records
                if not stored.value.terminal
                or not stored.value.terminal_event_emitted
            ),
            key=lambda stored: (stored.value.run_id, stored.value.pair_id),
        )

    async def delete_for_run(self, run_id: str) -> None:
        directory = self.root / validate_id(run_id, field="run_id")
        with self._lock:
            _remove_tree(directory)

    async def claim(
        self,
        run_id: str,
        task_pair_id: str,
        *,
        owner: str,
        lease_until: datetime,
        dequeue_count: int | None = None,
        dispatch_token: str | None = None,
        message_id: str | None = None,
    ) -> TaskClaim:
        validate_id(owner, field="owner")
        now = _checked_now(self._clock)
        if lease_until.tzinfo is None or lease_until <= now:
            raise LeaseLostError("Task claim lease has already expired")
        path = self._path(run_id, task_pair_id)
        with self._lock:
            current = self._read(path)
            task = current.value
            if task.terminal:
                return TaskClaim(acquired=False, task=current)
            if task.dispatch_token != dispatch_token:
                return TaskClaim(acquired=False, task=current)
            if task.dispatch_message_id is not None and task.dispatch_message_id != message_id:
                return TaskClaim(acquired=False, task=current)
            active = task.claim_lease_until is not None and task.claim_lease_until > now
            if active and task.claim_owner != owner:
                return TaskClaim(acquired=False, task=current)
            if active and task.claim_owner == owner:
                return TaskClaim(acquired=True, task=current)
            updated = replace(
                task,
                status="processing",
                stage=(
                    "terminalizing"
                    if task.terminal_intent_outcome is not None
                    else ("parsing" if task.raw_artifact_path else "executing")
                ),
                attempts=task.attempts + 1,
                dispatch_message_id=(
                    task.dispatch_message_id
                    if task.dispatch_message_id is not None
                    else (message_id if dispatch_token is not None else None)
                ),
                claim_owner=owner,
                claim_lease_until=lease_until,
                error_code=None,
                error_message=None,
                updated_at=now,
            )
            stored = self._replace(path, updated, expected_etag=current.etag)
            return TaskClaim(acquired=True, task=stored)

    async def renew_claim(
        self,
        run_id: str,
        task_pair_id: str,
        *,
        owner: str,
        lease_until: datetime,
    ) -> Versioned[TaskRecord]:
        path = self._path(run_id, task_pair_id)
        with self._lock:
            current = self._read(path)
            now = _checked_now(self._clock)
            if (
                current.value.terminal
                or current.value.claim_owner != owner
                or current.value.claim_lease_until is None
                or current.value.claim_lease_until <= now
            ):
                raise LeaseLostError("Task claim is not owned")
            if lease_until.tzinfo is None or lease_until <= now:
                raise ValueError("Task claim lease must expire in the future")
            updated = replace(
                current.value,
                claim_lease_until=lease_until,
                updated_at=now,
            )
            return self._replace(path, updated, expected_etag=current.etag)

    async def release_claim(
        self,
        run_id: str,
        task_pair_id: str,
        *,
        owner: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> Versioned[TaskRecord]:
        path = self._path(run_id, task_pair_id)
        with self._lock:
            current = self._read(path)
            if current.value.terminal:
                return current
            now = _checked_now(self._clock)
            if (
                current.value.claim_owner != owner
                or current.value.claim_lease_until is None
                or current.value.claim_lease_until <= now
            ):
                raise LeaseLostError("Task claim is not owned")
            updated = replace(
                current.value,
                status="queued",
                stage="queued",
                claim_owner=None,
                claim_lease_until=None,
                error_code=error_code,
                error_message=error_message,
                updated_at=now,
            )
            return self._replace(path, updated, expected_etag=current.etag)

    async def record_raw(
        self,
        run_id: str,
        task_pair_id: str,
        *,
        owner: str,
        raw_artifact_path: str,
        telemetry_artifact_path: str,
    ) -> Versioned[TaskRecord]:
        path = self._path(run_id, task_pair_id)
        with self._lock:
            current = self._read(path)
            if current.value.terminal:
                return current
            now = _checked_now(self._clock)
            if (
                current.value.claim_owner != owner
                or current.value.claim_lease_until is None
                or current.value.claim_lease_until <= now
            ):
                raise LeaseLostError("Task claim is not owned")
            updated = replace(
                current.value,
                stage=(
                    "terminalizing"
                    if current.value.terminal_intent_outcome is not None
                    else "parsing"
                ),
                raw_artifact_path=raw_artifact_path,
                telemetry_artifact_path=telemetry_artifact_path,
                updated_at=now,
            )
            return self._replace(path, updated, expected_etag=current.etag)

    async def record_terminal_intent(
        self,
        run_id: str,
        task_pair_id: str,
        *,
        owner: str,
        outcome: TerminalFailureOutcome,
        error_code: str,
        error_message: str,
        poisoned: bool = False,
    ) -> Versioned[TaskRecord]:
        if outcome not in {"invalid", "failed"}:
            raise ValueError("Terminal failure intent must be invalid or failed")
        if poisoned and outcome != "failed":
            raise ValueError("Only failed terminal intent can be poisoned")
        path = self._path(run_id, task_pair_id)
        with self._lock:
            current = self._read(path)
            task = current.value
            if task.terminal:
                return current
            now = _checked_now(self._clock)
            if (
                task.claim_owner != owner
                or task.claim_lease_until is None
                or task.claim_lease_until <= now
            ):
                raise LeaseLostError("Task claim is not owned")
            intended = (
                task.terminal_intent_outcome,
                task.terminal_intent_error_code,
                task.terminal_intent_error_message,
                task.terminal_intent_poisoned,
            )
            requested = (outcome, error_code, error_message, poisoned)
            if task.terminal_intent_outcome is not None:
                if intended != requested:
                    raise ConcurrencyError("Task already has a different terminal intent")
                return current
            if poisoned and not task.poison_dispatched:
                raise ConcurrencyError(
                    "Poison-terminal intent requires a durable poison dispatch"
                )
            updated = replace(
                task,
                stage="terminalizing",
                terminal_intent_outcome=outcome,
                terminal_intent_error_code=error_code,
                terminal_intent_error_message=error_message,
                terminal_intent_poisoned=poisoned,
                updated_at=now,
            )
            return self._replace(path, updated, expected_etag=current.etag)

    async def finish(
        self,
        run_id: str,
        task_pair_id: str,
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
        if outcome not in {"succeeded", "invalid", "failed"}:
            raise ValueError("Unsupported terminal outcome")
        path = self._path(run_id, task_pair_id)
        with self._lock:
            current = self._read(path)
            task = current.value
            if task.terminal:
                if task.status != outcome or task.poisoned != poisoned:
                    raise ConcurrencyError("Task already has a different terminal outcome")
                return TaskCompletion(applied=False, task=current)
            now = _checked_now(self._clock)
            if owner is None:
                if task.claim_owner is not None:
                    raise LeaseLostError("Task has an active or stale claim")
            elif (
                task.claim_owner != owner
                or task.claim_lease_until is None
                or task.claim_lease_until <= now
            ):
                raise LeaseLostError("Task claim is not owned")
            if task.terminal_intent_outcome is not None and (
                task.terminal_intent_outcome != outcome
                or task.terminal_intent_error_code != error_code
                or task.terminal_intent_error_message != error_message
                or task.terminal_intent_poisoned != poisoned
            ):
                raise ConcurrencyError("Terminal completion does not match durable intent")
            if poisoned and not task.poison_dispatched:
                raise ConcurrencyError(
                    "Poison-terminal completion requires a durable poison dispatch"
                )
            updated = replace(
                task,
                status=outcome,
                stage="terminal",
                claim_owner=None,
                claim_lease_until=None,
                raw_artifact_path=raw_artifact_path,
                parsed_artifact_path=parsed_artifact_path,
                comparison_artifact_path=comparison_artifact_path,
                telemetry_artifact_path=telemetry_artifact_path,
                error_code=error_code,
                error_message=error_message,
                poisoned=poisoned,
                updated_at=now,
            )
            stored = self._replace(path, updated, expected_etag=current.etag)
            return TaskCompletion(applied=True, task=stored)


class LocalEventRepository:
    def __init__(
        self,
        artifacts: LocalArtifactRepository,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._artifacts = artifacts
        self._clock = clock
        self._lock = _root_lock(artifacts.root)

    def _event_directory(self, run_id: str) -> Path:
        return self._artifacts._target(f"runs/{validate_id(run_id, field='run_id')}/events")

    def _counter_path(self, run_id: str) -> Path:
        return self._artifacts._target(
            f"runs/{validate_id(run_id, field='run_id')}/event-counter.json"
        )

    def _idempotency_path(self, run_id: str, key: str) -> Path:
        if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 512:
            raise ValueError("Event idempotency key must be a non-empty bounded string")
        digest = content_sha256(key.encode("utf-8"))
        return self._artifacts._target(
            f"runs/{validate_id(run_id, field='run_id')}/event-keys/{digest}.json"
        )

    def _read_counter(self, run_id: str) -> JsonObject:
        counter_path = self._counter_path(run_id)
        if counter_path.exists():
            counter = _read_json_object(counter_path, max_bytes=_EVENT_COUNTER_BYTES)
            current = _integer(counter, "sequence")
        else:
            directory = self._event_directory(run_id)
            current = max(
                (int(path.stem) for path in directory.glob("*.json") if path.stem.isdigit()),
                default=0,
            )
            counter = {"sequence": current}
        pending_value = counter.get("pending")
        if pending_value is not None:
            pending = require_json_object(pending_value, field="pending event")
            record = EventRecord.from_dict(pending.get("record"))
            if record.run_id != run_id or record.sequence != current + 1:
                raise ValueError("Pending durable event does not follow the committed watermark")
            idempotency_key = pending.get("idempotency_key")
            if idempotency_key is not None:
                if not isinstance(idempotency_key, str):
                    raise ValueError("Pending event idempotency key must be a string")
                self._idempotency_path(run_id, idempotency_key)
        return counter

    def _write_counter(self, run_id: str, counter: JsonObject) -> None:
        encoded = strict_json_bytes(
            counter,
            max_bytes=_EVENT_COUNTER_BYTES,
            newline=True,
        )
        _atomic_replace(self._counter_path(run_id), encoded)

    async def latest_sequence(self, run_id: str) -> int:
        with self._lock:
            return _integer(self._read_counter(run_id), "sequence")

    def _recover_pending(
        self,
        run_id: str,
        counter: JsonObject,
    ) -> tuple[JsonObject, EventRecord | None]:
        pending_value = counter.get("pending")
        if pending_value is None:
            return counter, None
        pending = require_json_object(pending_value, field="pending event")
        record = EventRecord.from_dict(pending.get("record"))
        encoded = strict_json_bytes(
            record.to_dict(),
            max_bytes=MAX_EVENT_BYTES,
            newline=True,
        )
        self._artifacts._create_bytes(
            run_event(run_id, record.sequence),
            encoded,
            content_type="application/json",
            metadata={},
        )
        idempotency_key = pending.get("idempotency_key")
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str):
                raise ValueError("Pending event idempotency key must be a string")
            marker_path = self._idempotency_path(run_id, idempotency_key)
            if not _atomic_create(marker_path, encoded):
                existing = EventRecord.from_dict(
                    strict_json_loads(
                        marker_path.read_bytes(),
                        max_bytes=MAX_EVENT_BYTES,
                    )
                )
                if existing != record:
                    raise ConcurrencyError(
                        "Event idempotency marker has a different record"
                    )
        committed: JsonObject = {"sequence": record.sequence}
        self._write_counter(run_id, committed)
        return committed, record

    async def append(
        self,
        run_id: str,
        event: str,
        data: JsonObject,
        *,
        idempotency_key: str | None = None,
    ) -> EventRecord:
        validate_id(run_id, field="run_id")
        validate_id(event, field="event")
        validate_metadata(data)
        if idempotency_key is not None:
            normalized_key = bounded_text(
                idempotency_key,
                field="event idempotency key",
                limit=512,
            )
            if normalized_key is None:
                raise ValueError("Event idempotency key cannot be null")
            idempotency_key = normalized_key
        with self._lock:
            counter, _ = self._recover_pending(run_id, self._read_counter(run_id))
            marker_path = (
                self._idempotency_path(run_id, idempotency_key)
                if idempotency_key is not None
                else None
            )
            if marker_path is not None and marker_path.exists():
                existing = EventRecord.from_dict(
                    strict_json_loads(
                        marker_path.read_bytes(),
                        max_bytes=MAX_EVENT_BYTES,
                    )
                )
                if existing.event != event or existing.data != data:
                    raise ConcurrencyError(
                        "Event idempotency key was reused with different content"
                    )
                self._artifacts._create_bytes(
                    run_event(run_id, existing.sequence),
                    strict_json_bytes(
                        existing.to_dict(),
                        max_bytes=MAX_EVENT_BYTES,
                        newline=True,
                    ),
                    content_type="application/json",
                    metadata={},
                )
                return existing
            sequence = _integer(counter, "sequence") + 1
            record = EventRecord(
                run_id=run_id,
                sequence=sequence,
                event=event,
                data=data,
                created_at=_checked_now(self._clock),
            )
            strict_json_bytes(
                record.to_dict(),
                max_bytes=MAX_EVENT_BYTES,
                newline=True,
            )
            pending: JsonObject = {
                "sequence": sequence - 1,
                "pending": {
                    "record": record.to_dict(),
                    "idempotency_key": idempotency_key,
                },
            }
            strict_json_bytes(
                pending,
                max_bytes=_EVENT_COUNTER_BYTES,
                newline=True,
            )
            self._write_counter(run_id, pending)
            self._recover_pending(run_id, pending)
            return record

    async def read(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[EventRecord]:
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if limit < 1 or limit > 1_000:
            raise ValueError("Event limit must be between 1 and 1000")
        with self._lock:
            counter = self._read_counter(run_id)
            committed = _integer(counter, "sequence")
            records: list[EventRecord] = []
            upper_bound = min(committed, after_sequence + limit)
            for sequence in range(after_sequence + 1, upper_bound + 1):
                path = self._artifacts._target(run_event(run_id, sequence))
                try:
                    encoded = path.read_bytes()
                except FileNotFoundError:
                    break
                record = EventRecord.from_dict(
                    strict_json_loads(encoded, max_bytes=MAX_EVENT_BYTES)
                )
                if record.sequence != sequence:
                    raise ValueError("Durable event path does not match its sequence")
                records.append(record)
        for previous, current in pairwise(records):
            if current.sequence <= previous.sequence:
                raise ValueError("Durable events are not strictly ordered")
        return records


class LocalTaskQueue:
    def __init__(self, root: Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.root = root.resolve()
        self._messages = self.root / "messages"
        self._poison = self.root / "poison"
        self._clock = clock
        self._lock = _root_lock(self.root)

    def _message_id(self, message: TaskMessage) -> str:
        digest = content_sha256(
            (
                f"{message.project_id}\n{message.run_id}\n{message.pair_id}\n"
                f"{message.dispatch_token or ''}"
            ).encode()
        )
        return f"m{digest[:31]}"

    def _message_path(self, message_id: str) -> Path:
        return self._messages / f"{validate_id(message_id, field='message_id')}.json"

    def _load(self, path: Path) -> JsonObject:
        return _read_json_object(path, max_bytes=MAX_QUEUE_MESSAGE_BYTES)

    async def send(self, message: TaskMessage) -> str:
        message_id = self._message_id(message)
        now = _checked_now(self._clock)
        payload: JsonObject = {
            "message_id": message_id,
            "message": message.to_dict(),
            "enqueued_at": datetime_text(now),
            "visible_at": datetime_text(now),
            "dequeue_count": 0,
            "pop_receipt": None,
        }
        encoded = strict_json_bytes(
            payload,
            max_bytes=MAX_QUEUE_MESSAGE_BYTES,
            newline=True,
        )
        path = self._message_path(message_id)
        with self._lock:
            if _atomic_create(path, encoded):
                return message_id
            existing = self._load(path)
            if existing.get("message") != message.to_dict():
                raise AlreadyExistsError("Queue message ID collision")
            return message_id

    async def receive(
        self,
        *,
        max_messages: int,
        visibility_timeout: int,
    ) -> list[QueueLease]:
        if max_messages < 1 or max_messages > 32:
            raise ValueError("max_messages must be between 1 and 32")
        if visibility_timeout < 1:
            raise ValueError("visibility_timeout must be positive")
        now = _checked_now(self._clock)
        leases: list[QueueLease] = []
        with self._lock:
            candidates: list[tuple[datetime, str, Path, JsonObject, TaskMessage]] = []
            if self._messages.is_dir():
                for path in self._messages.glob("*.json"):
                    try:
                        data = self._load(path)
                        visible_at = parse_datetime(
                            data.get("visible_at"),
                            field_name="visible_at",
                        )
                        enqueued_at = parse_datetime(
                            data.get("enqueued_at"),
                            field_name="enqueued_at",
                        )
                        message_id = _string(data, "message_id")
                        validate_id(message_id, field="message_id")
                        if self._message_path(message_id) != path:
                            raise ValueError("Queue message path does not match its ID")
                        message = TaskMessage.from_dict(data.get("message"))
                        if _integer(data, "dequeue_count") < 0:
                            raise ValueError("Queue dequeue_count cannot be negative")
                    except (TypeError, ValueError):
                        path.unlink(missing_ok=True)
                        continue
                    if visible_at <= now:
                        candidates.append((enqueued_at, message_id, path, data, message))
            for _, message_id, path, data, message in sorted(candidates)[:max_messages]:
                count = _integer(data, "dequeue_count") + 1
                receipt = secrets.token_hex(24)
                lease_until = now + timedelta(seconds=visibility_timeout)
                data["dequeue_count"] = count
                data["pop_receipt"] = receipt
                data["visible_at"] = datetime_text(lease_until)
                _atomic_replace(
                    path,
                    strict_json_bytes(
                        data,
                        max_bytes=MAX_QUEUE_MESSAGE_BYTES,
                        newline=True,
                    ),
                )
                leases.append(
                    QueueLease(
                        message_id=message_id,
                        pop_receipt=receipt,
                        message=message,
                        dequeue_count=count,
                        lease_until=lease_until,
                    )
                )
        return leases

    def _owned_message(self, lease: QueueLease) -> tuple[Path, JsonObject, datetime]:
        now = _checked_now(self._clock)
        path = self._message_path(lease.message_id)
        data = self._load(path)
        if data.get("pop_receipt") != lease.pop_receipt:
            raise LeaseLostError("Queue lease pop receipt no longer matches")
        visible_at = parse_datetime(data.get("visible_at"), field_name="visible_at")
        if visible_at <= now:
            raise LeaseLostError("Queue visibility lease expired")
        if data.get("message") != lease.message.to_dict():
            raise LeaseLostError("Queue message payload changed")
        return path, data, now

    async def renew(self, lease: QueueLease, *, visibility_timeout: int) -> QueueLease:
        if visibility_timeout < 1:
            raise ValueError("visibility_timeout must be positive")
        with self._lock:
            path, data, now = self._owned_message(lease)
            new_receipt = secrets.token_hex(24)
            lease_until = now + timedelta(seconds=visibility_timeout)
            data["pop_receipt"] = new_receipt
            data["visible_at"] = datetime_text(lease_until)
            _atomic_replace(
                path,
                strict_json_bytes(data, max_bytes=MAX_QUEUE_MESSAGE_BYTES, newline=True),
            )
            return QueueLease(
                message_id=lease.message_id,
                pop_receipt=new_receipt,
                message=lease.message,
                dequeue_count=lease.dequeue_count,
                lease_until=lease_until,
            )

    async def complete(self, lease: QueueLease) -> None:
        with self._lock:
            path, _, _ = self._owned_message(lease)
            path.unlink()

    async def release(self, lease: QueueLease, *, delay: int = 0) -> None:
        if delay < 0:
            raise ValueError("Queue release delay cannot be negative")
        with self._lock:
            path, data, now = self._owned_message(lease)
            data["pop_receipt"] = None
            data["visible_at"] = datetime_text(now + timedelta(seconds=delay))
            _atomic_replace(
                path,
                strict_json_bytes(data, max_bytes=MAX_QUEUE_MESSAGE_BYTES, newline=True),
            )

    async def poison(self, lease: QueueLease, *, reason_code: str) -> None:
        source_key = f"{lease.message.run_id}|{lease.message.pair_id}|{reason_code}"
        outbox_id = f"p{content_sha256(source_key.encode())[:31]}"
        await self.send_poison(
            lease,
            reason_code=reason_code,
            outbox_id=outbox_id,
        )
        await self.complete(lease)

    async def send_poison(
        self,
        lease: QueueLease,
        *,
        reason_code: str,
        outbox_id: str,
    ) -> str:
        validate_id(reason_code, field="reason_code")
        validate_id(outbox_id, field="outbox_id")
        with self._lock:
            _, _, _ = self._owned_message(lease)
            poison_payload: JsonObject = {
                "outbox_id": outbox_id,
                "source_message_id": lease.message_id,
                "message": lease.message.to_dict(),
                "reason_code": reason_code,
            }
            target = self._poison / f"{outbox_id}.json"
            encoded = strict_json_bytes(
                poison_payload,
                max_bytes=MAX_QUEUE_MESSAGE_BYTES,
                newline=True,
            )
            if not _atomic_create(target, encoded):
                existing = self._load(target)
                if any(
                    existing.get(key) != poison_payload[key]
                    for key in ("outbox_id", "message", "reason_code")
                ):
                    raise AlreadyExistsError("Poison queue message collision")
            return outbox_id

    async def poison_messages(self) -> list[JsonObject]:
        if not self._poison.is_dir():
            return []
        return [self._load(path) for path in sorted(self._poison.glob("*.json"))]


class LocalSeedCoordinator:
    def __init__(self, root: Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.root = root.resolve()
        self._clock = clock
        self._lock = _root_lock(self.root)

    def _path(self, seed_id: str) -> Path:
        return self.root / f"{validate_id(seed_id, field='seed_id')}.json"

    def _load_optional(self, seed_id: str) -> JsonObject | None:
        path = self._path(seed_id)
        if not path.exists():
            return None
        return _read_json_object(path, max_bytes=MAX_METADATA_BYTES)

    async def is_complete(self, seed_id: str) -> bool:
        with self._lock:
            record = self._load_optional(seed_id)
            return record is not None and record.get("status") == "completed"

    async def acquire(
        self,
        seed_id: str,
        *,
        owner: str,
        lease_seconds: int,
    ) -> SeedLease | None:
        validate_id(seed_id, field="seed_id")
        validate_id(owner, field="owner")
        if lease_seconds < 1:
            raise ValueError("Seed lease duration must be positive")
        now = _checked_now(self._clock)
        path = self._path(seed_id)
        with self._lock:
            current = self._load_optional(seed_id)
            if current is not None and current.get("status") == "completed":
                return None
            if current is not None and current.get("status") == "leased":
                lease_until = parse_datetime(
                    current.get("lease_until"),
                    field_name="lease_until",
                )
                if lease_until > now:
                    return None
            token = secrets.token_hex(24)
            lease_until = now + timedelta(seconds=lease_seconds)
            payload: JsonObject = {
                "seed_id": seed_id,
                "status": "leased",
                "owner": owner,
                "token": token,
                "lease_until": datetime_text(lease_until),
                "marker": None,
            }
            _atomic_replace(
                path,
                strict_json_bytes(payload, max_bytes=MAX_METADATA_BYTES, newline=True),
            )
            return SeedLease(
                seed_id=seed_id,
                owner=owner,
                token=token,
                lease_until=lease_until,
                lease_seconds=lease_seconds,
            )

    def _assert_owned(self, lease: SeedLease) -> tuple[Path, JsonObject, datetime]:
        path = self._path(lease.seed_id)
        record = _read_json_object(path, max_bytes=MAX_METADATA_BYTES)
        now = _checked_now(self._clock)
        if (
            record.get("status") != "leased"
            or record.get("owner") != lease.owner
            or record.get("token") != lease.token
        ):
            raise LeaseLostError("Seed lease is not owned")
        lease_until = parse_datetime(record.get("lease_until"), field_name="lease_until")
        if lease_until <= now:
            raise LeaseLostError("Seed lease expired")
        return path, record, now

    async def renew(self, lease: SeedLease, *, lease_seconds: int) -> SeedLease:
        if lease_seconds < 1:
            raise ValueError("Seed lease duration must be positive")
        with self._lock:
            path, record, now = self._assert_owned(lease)
            lease_until = now + timedelta(seconds=lease_seconds)
            record["lease_until"] = datetime_text(lease_until)
            _atomic_replace(
                path,
                strict_json_bytes(record, max_bytes=MAX_METADATA_BYTES, newline=True),
            )
            return SeedLease(
                seed_id=lease.seed_id,
                owner=lease.owner,
                token=lease.token,
                lease_until=lease_until,
                lease_seconds=lease_seconds,
            )

    async def complete(self, lease: SeedLease, marker: JsonObject) -> None:
        normalized = validate_metadata(marker)
        with self._lock:
            path, _, now = self._assert_owned(lease)
            completed: JsonObject = {
                "seed_id": lease.seed_id,
                "status": "completed",
                "owner": lease.owner,
                "token": None,
                "lease_until": None,
                "completed_at": datetime_text(now),
                "marker": normalized,
            }
            _atomic_replace(
                path,
                strict_json_bytes(completed, max_bytes=MAX_METADATA_BYTES, newline=True),
            )

    async def release(self, lease: SeedLease) -> None:
        with self._lock:
            path = self._path(lease.seed_id)
            if not path.exists():
                return
            record = _read_json_object(path, max_bytes=MAX_METADATA_BYTES)
            if record.get("status") == "completed":
                return
            self._assert_owned(lease)
            path.unlink()


class LocalStorage:
    """Convenience bundle sharing one durable filesystem root."""

    def __init__(self, root: Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        resolved = root.resolve()
        self.artifacts = LocalArtifactRepository(resolved / "artifacts")
        self.projects = LocalProjectRepository(resolved / "state" / "projects", clock=clock)
        self.runs = LocalRunRepository(resolved / "state" / "runs", clock=clock)
        self.tasks = LocalTaskRepository(resolved / "state" / "tasks", clock=clock)
        self.events = LocalEventRepository(self.artifacts, clock=clock)
        self.queue = LocalTaskQueue(resolved / "queue", clock=clock)
        self.seeds = LocalSeedCoordinator(resolved / "coordination" / "seeds", clock=clock)


def _string(data: JsonObject, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _integer(data: JsonObject, key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


__all__ = [
    "LocalArtifactRepository",
    "LocalEventRepository",
    "LocalProjectRepository",
    "LocalRunRepository",
    "LocalSeedCoordinator",
    "LocalStorage",
    "LocalTaskQueue",
    "LocalTaskRepository",
]
