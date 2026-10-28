from __future__ import annotations

import asyncio
import base64
import builtins
import inspect
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

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
    ensure_utc,
    parse_datetime,
    utc_now,
)
from receipt_bench.storage.paths import run_event, seed_lock, seed_marker
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
    StrictJsonError,
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

_AZURE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$")
_AZURE_TABLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]{2,62}$")
_MAX_BLOB_METADATA_BYTES = 4 * 1024
_MAX_TABLE_STRING_BYTES = 64 * 1024
_MAX_CAS_ATTEMPTS = 32
_EVENT_COUNTER_BYTES = MAX_EVENT_BYTES * 2
_EVENT_LEASE_SECONDS = 15
_TABLE_IDENTITY_LEASE_SECONDS = 60
_TABLE_IDENTITY_LOCK_ATTEMPTS = 100
_RECOVERY_PARTITION = "recovery"
_RUN_LOOKUP_PARTITION = "runlookup"
_PAYLOAD_DELETION_PARTITION = "payloaddeletions"
_RECOVERY_ORPHAN_GRACE = timedelta(minutes=5)
_PENDING_PAYLOAD_BYTES = "_PayloadBytes"


async def _maybe_await[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


async def _iterate(value: object) -> AsyncIterator[object]:
    resolved = await _maybe_await(value) if inspect.isawaitable(value) else value
    if hasattr(resolved, "__aiter__"):
        async for item in resolved:
            yield item
        return
    for item in resolved:  # type: ignore[union-attr]
        yield item


def _status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None)
    return value if isinstance(value, int) else None


def _is_not_found(exc: BaseException) -> bool:
    return _status_code(exc) == 404 or type(exc).__name__ in {
        "ResourceNotFoundError",
        "ServiceRequestError404",
    }


def _error_code(exc: BaseException) -> str | None:
    for value in (getattr(exc, "error_code", None), getattr(exc, "code", None)):
        if isinstance(value, str) and value:
            return value.casefold()
    error = getattr(exc, "error", None)
    value = getattr(error, "code", None)
    if isinstance(value, str) and value:
        return value.casefold()
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if isinstance(headers, Mapping):
        for name in ("x-ms-error-code", "X-Ms-Error-Code"):
            value = headers.get(name)
            if isinstance(value, str) and value:
                return value.casefold()
    return None


def _is_blob_exists(exc: BaseException) -> bool:
    return _error_code(exc) == "blobalreadyexists"


def _is_table_exists(exc: BaseException) -> bool:
    return _error_code(exc) == "entityalreadyexists" or type(exc).__name__ in {
        "ResourceExistsError",
        "TableEntityExistsError",
    }


def _is_precondition_failed(exc: BaseException) -> bool:
    return (
        _status_code(exc) == 412
        or _error_code(exc)
        in {
            "conditionnotmet",
            "etagmismatch",
            "resourcemodified",
            "updateconditionnotsatisfied",
        }
        or type(exc).__name__ == "ResourceModifiedError"
    )


def _is_blob_lease_conflict(exc: BaseException) -> bool:
    return _is_precondition_failed(exc) or _error_code(exc) in {
        "leasealreadypresent",
        "leaseidmissing",
        "leaseidmismatchwithbloboperation",
        "leaseidmismatchwithleaseoperation",
        "leaseisbrokenandcannotberenewed",
        "leaseisbreakingandcannotbeacquired",
        "leaselost",
        "leasenotpresentwithbloboperation",
        "leasenotpresentwithleaseoperation",
    }


def _is_queue_lease_conflict(exc: BaseException) -> bool:
    return _is_precondition_failed(exc) or _error_code(exc) == "popreceiptmismatch"


def _mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raise ValueError(f"Azure SDK returned {type(value).__name__}, expected a mapping")


def _property(value: object, *names: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _payload_ref(value: object | None) -> str | None:
    ref = _property(value, "PayloadRef") if value is not None else None
    return ref if isinstance(ref, str) else None


def _entity_etag(entity: object) -> str:
    metadata = getattr(entity, "metadata", None)
    if isinstance(metadata, Mapping):
        for key in ("etag", "odata.etag", "@odata.etag"):
            value = metadata.get(key)
            if value:
                return str(value)
    value = _property(entity, "etag", "odata.etag", "@odata.etag")
    if value:
        return str(value)
    raise ValueError("Azure Table entity did not include an ETag")


def _blob_etag(properties: object, fallback: str) -> str:
    value = _property(properties, "etag")
    return str(value) if value else fallback


def _as_utc_datetime(value: object, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value, field_name="Azure timestamp")
    return fallback


def _content_settings(content_type: str) -> object | None:
    try:
        from azure.storage.blob import ContentSettings
    except ImportError:
        return None
    return ContentSettings(content_type=content_type)


def _table_condition_arguments(expected_etag: str) -> dict[str, object]:
    try:
        from azure.core import MatchConditions
        from azure.data.tables import UpdateMode
    except ImportError:
        return {
            "mode": "replace",
            "etag": expected_etag,
            "match_condition": "IfNotModified",
        }
    return {
        "mode": UpdateMode.REPLACE,
        "etag": expected_etag,
        "match_condition": MatchConditions.IfNotModified,
    }


def _blob_condition_arguments(expected_etag: str) -> dict[str, object]:
    try:
        from azure.core import MatchConditions
    except ImportError:
        return {
            "etag": expected_etag,
            "match_condition": "IfNotModified",
        }
    return {
        "etag": expected_etag,
        "match_condition": MatchConditions.IfNotModified,
    }


def _encode_blob_metadata(
    metadata: JsonObject,
    *,
    content_type: str,
    digest: str,
) -> dict[str, str]:
    encoded = base64.urlsafe_b64encode(
        strict_json_bytes(metadata, max_bytes=_MAX_BLOB_METADATA_BYTES)
    ).decode("ascii")
    return {
        "sha256": digest,
        "content_type": content_type,
        "app_metadata": encoded,
    }


def _decode_blob_metadata(value: object) -> tuple[str, JsonObject]:
    if not isinstance(value, Mapping):
        return "application/octet-stream", {}
    content_type = value.get("content_type")
    encoded = value.get("app_metadata")
    metadata: JsonObject = {}
    if isinstance(encoded, str):
        try:
            decoded = base64.urlsafe_b64decode(encoded.encode("ascii"))
            parsed = strict_json_loads(decoded, max_bytes=_MAX_BLOB_METADATA_BYTES)
            metadata = require_json_object(parsed, field="blob metadata")
        except (ValueError, UnicodeEncodeError):
            metadata = {}
    return (
        content_type if isinstance(content_type, str) else "application/octet-stream",
        metadata,
    )


class AzureArtifactRepository:
    """Immutable Blob-backed artifacts using conditional creation only."""

    def __init__(self, container_client: object) -> None:
        self._container = container_client

    def _blob(self, path: str) -> object:
        validated = validate_artifact_path(path)
        return self._container.get_blob_client(validated)  # type: ignore[attr-defined]

    async def create_bytes(
        self,
        path: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: JsonObject | None = None,
    ) -> ArtifactInfo:
        validated_path = validate_artifact_path(path)
        payload = bytes(data)
        content_type = validate_content_type(content_type)
        normalized_metadata = validate_metadata(metadata or {})
        digest = content_sha256(payload)
        azure_metadata = _encode_blob_metadata(
            normalized_metadata,
            content_type=content_type,
            digest=digest,
        )
        blob = self._blob(validated_path)
        kwargs: dict[str, object] = {
            "overwrite": False,
            "metadata": azure_metadata,
        }
        settings = _content_settings(content_type)
        if settings is not None:
            kwargs["content_settings"] = settings
        try:
            await _maybe_await(blob.upload_blob(payload, **kwargs))  # type: ignore[attr-defined]
        except Exception as exc:
            if not _is_blob_exists(exc):
                raise
            existing = await self.get_bytes(validated_path)
            if existing != payload:
                raise ImmutableArtifactError(
                    f"Immutable artifact already exists with different bytes: {validated_path}"
                ) from exc
            info = await self.get_info(validated_path)
            if info.content_type != content_type or info.metadata != normalized_metadata:
                raise ImmutableArtifactError(
                    f"Immutable artifact metadata collision: {validated_path}"
                ) from exc
            return info
        properties = await _maybe_await(
            blob.get_blob_properties()  # type: ignore[attr-defined]
        )
        return ArtifactInfo(
            path=validated_path,
            etag=_blob_etag(properties, digest),
            sha256=digest,
            size=len(payload),
            content_type=content_type,
            metadata=normalized_metadata,
        )

    async def create_json(
        self,
        path: str,
        value: JsonValue,
        *,
        metadata: JsonObject | None = None,
    ) -> ArtifactInfo:
        return await self.create_bytes(
            path,
            strict_json_bytes(value, newline=True),
            content_type="application/json",
            metadata=metadata,
        )

    async def get_bytes(self, path: str) -> bytes:
        blob = self._blob(path)
        try:
            downloader = await _maybe_await(blob.download_blob())  # type: ignore[attr-defined]
            return bytes(await _maybe_await(downloader.readall()))
        except Exception as exc:
            if _is_not_found(exc):
                raise NotFoundError(path) from exc
            raise

    async def get_json(self, path: str) -> JsonValue:
        return strict_json_loads(await self.get_bytes(path))

    async def get_info(self, path: str) -> ArtifactInfo:
        validated_path = validate_artifact_path(path)
        blob = self._blob(validated_path)
        try:
            properties = await _maybe_await(blob.get_blob_properties())  # type: ignore[attr-defined]
        except Exception as exc:
            if _is_not_found(exc):
                raise NotFoundError(validated_path) from exc
            raise
        data = await self.get_bytes(validated_path)
        metadata_value = _property(properties, "metadata", default={})
        content_type, metadata = _decode_blob_metadata(metadata_value)
        settings = _property(properties, "content_settings")
        settings_type = _property(settings, "content_type") if settings is not None else None
        if isinstance(settings_type, str):
            content_type = settings_type
        digest = content_sha256(data)
        stored_digest = (
            metadata_value.get("sha256") if isinstance(metadata_value, Mapping) else None
        )
        if stored_digest is not None and stored_digest != digest:
            raise ValueError(f"Blob checksum metadata mismatch: {validated_path}")
        return ArtifactInfo(
            path=validated_path,
            etag=_blob_etag(properties, digest),
            sha256=digest,
            size=len(data),
            content_type=content_type,
            metadata=metadata,
        )

    async def exists(self, path: str) -> bool:
        blob = self._blob(path)
        try:
            return bool(await _maybe_await(blob.exists()))  # type: ignore[attr-defined]
        except Exception as exc:
            if _is_not_found(exc):
                return False
            raise

    async def delete(self, path: str) -> bool:
        validated = validate_artifact_path(path)
        blob = self._blob(validated)
        try:
            await _maybe_await(
                blob.delete_blob(delete_snapshots="include")  # type: ignore[attr-defined]
            )
        except Exception as exc:
            if _is_not_found(exc):
                return False
            try:
                exists = bool(await _maybe_await(blob.exists()))  # type: ignore[attr-defined]
            except Exception as readback_exc:
                raise exc from readback_exc
            if exists:
                raise
            return True
        return True

    async def list_prefix(self, prefix: str) -> list[str]:
        validated = validate_deletion_prefix(prefix)
        blobs = self._container.list_blobs(  # type: ignore[attr-defined]
            name_starts_with=validated
        )
        names: list[str] = []
        async for item in _iterate(blobs):
            name = _property(item, "name")
            if isinstance(name, str) and name.startswith(validated):
                names.append(validate_artifact_path(name))
        return sorted(names)

    async def delete_prefix(self, prefix: str) -> int:
        names = await self.list_prefix(prefix)
        for name in names:
            await self.delete(name)
        return len(names)

    async def delete_table_payload(self, path: str) -> None:
        """Delete one internally managed Table overflow payload."""

        validated = validate_artifact_path(path)
        parts = validated.split("/")
        digest = parts[-1].removesuffix(".json")
        if (
            len(parts) not in {4, 5}
            or parts[:2] != ["state", "table-payloads"]
            or not parts[-1].endswith(".json")
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("Invalid Azure Table payload path")
        await self.delete(validated)

    async def delete_table_payload_prefix(self, prefix: str) -> int:
        """Delete one entity's isolated Table overflow-payload namespace."""

        self._validate_table_payload_prefix(prefix)
        names: list[str] = []
        blobs = self._container.list_blobs(  # type: ignore[attr-defined]
            name_starts_with=prefix
        )
        async for item in _iterate(blobs):
            name = _property(item, "name")
            if isinstance(name, str) and name.startswith(prefix):
                names.append(validate_artifact_path(name))
        for name in names:
            await self.delete(name)
        return len(names)

    async def acquire_table_identity_lock(self, lock_id: str) -> object:
        if (
            len(lock_id) != 2
            or any(character not in "0123456789abcdef" for character in lock_id)
        ):
            raise ValueError("Azure Table identity lock ID is invalid")
        path = validate_artifact_path(
            f"state/table-identity-locks/{lock_id}.lock"
        )
        blob = self._blob(path)
        try:
            await _maybe_await(
                blob.upload_blob(b"", overwrite=False)  # type: ignore[attr-defined]
            )
        except Exception as exc:
            if not (_is_blob_exists(exc) or _is_blob_lease_conflict(exc)):
                raise
        for attempt in range(_TABLE_IDENTITY_LOCK_ATTEMPTS):
            try:
                return await _maybe_await(
                    blob.acquire_lease(  # type: ignore[attr-defined]
                        lease_duration=_TABLE_IDENTITY_LEASE_SECONDS
                    )
                )
            except Exception as exc:
                if not _is_blob_lease_conflict(exc):
                    raise
                await asyncio.sleep(min(0.01 * (attempt + 1), 0.1))
        raise ConcurrencyError("Could not acquire the Azure Table identity lock")

    @staticmethod
    async def release_table_identity_lock(lease: object) -> None:
        try:
            await _maybe_await(lease.release())  # type: ignore[attr-defined]
        except Exception as exc:
            if not (_is_blob_lease_conflict(exc) or _is_not_found(exc)):
                raise

    @staticmethod
    def _validate_table_payload_prefix(prefix: str) -> None:
        if not prefix.endswith("/"):
            raise ValueError("Azure Table payload prefix must end with '/'")
        validated = validate_artifact_path(prefix[:-1])
        parts = validated.split("/")
        owner = parts[-1]
        if (
            len(parts) != 4
            or parts[:2] != ["state", "table-payloads"]
            or len(owner) != 32
            or any(character not in "0123456789abcdef" for character in owner)
        ):
            raise ValueError("Invalid Azure Table payload prefix")

class _AzureTableRepository:
    def __init__(
        self,
        table_client: object,
        *,
        artifacts: AzureArtifactRepository | None = None,
    ) -> None:
        self._table = table_client
        self._artifacts = artifacts
        self._local_identity_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _entity_identity(entity: object) -> tuple[str, str, str]:
        partition_key = _property(entity, "PartitionKey")
        row_key = _property(entity, "RowKey")
        kind = _property(entity, "Kind")
        if (
            not isinstance(partition_key, str)
            or not isinstance(row_key, str)
            or not isinstance(kind, str)
        ):
            raise ValueError("Azure Table entity identity is invalid")
        return partition_key, row_key, kind

    @classmethod
    def _identity_lock_id(cls, entity: object) -> str:
        partition_key, row_key, kind = cls._entity_identity(entity)
        return content_sha256(
            f"{partition_key}\0{row_key}\0{kind}".encode()
        )[:2]

    @asynccontextmanager
    async def _identity_locks(
        self,
        entities: Iterable[object],
    ) -> AsyncIterator[None]:
        lock_ids = sorted({self._identity_lock_id(entity) for entity in entities})
        if self._artifacts is None:
            locks = [
                self._local_identity_locks.setdefault(lock_id, asyncio.Lock())
                for lock_id in lock_ids
            ]
            for lock in locks:
                await lock.acquire()
            try:
                yield
            finally:
                for lock in reversed(locks):
                    lock.release()
            return

        leases: list[object] = []
        try:
            for lock_id in lock_ids:
                leases.append(
                    await self._artifacts.acquire_table_identity_lock(lock_id)
                )
            yield
        finally:
            for lease in reversed(leases):
                await self._artifacts.release_table_identity_lock(lease)

    async def _create_entity(self, entity: dict[str, object]) -> None:
        async with self._identity_locks([entity]):
            await self._assert_no_payload_deletion(entity)
            stored_entity = await self._materialize_entity_payload(entity)
            try:
                await _maybe_await(
                    self._table.create_entity(entity=stored_entity)  # type: ignore[attr-defined]
                )
            except Exception as exc:
                if _is_table_exists(exc):
                    raise AlreadyExistsError(
                        f"{entity['PartitionKey']}/{entity['RowKey']}"
                    ) from exc
                raise

    async def _get_entity(self, partition_key: str, row_key: str) -> object:
        try:
            return await _maybe_await(
                self._table.get_entity(  # type: ignore[attr-defined]
                    partition_key=partition_key,
                    row_key=row_key,
                )
            )
        except Exception as exc:
            if _is_not_found(exc):
                raise NotFoundError(f"{partition_key}/{row_key}") from exc
            raise

    async def _replace_entity(
        self,
        entity: dict[str, object],
        *,
        expected_etag: str,
    ) -> None:
        async with self._identity_locks([entity]):
            await self._assert_no_payload_deletion(entity)
            stored_entity = await self._materialize_entity_payload(entity)
            try:
                await _maybe_await(
                    self._table.update_entity(  # type: ignore[attr-defined]
                        entity=stored_entity,
                        **_table_condition_arguments(expected_etag),
                    )
                )
            except Exception as exc:
                if _is_not_found(exc):
                    raise NotFoundError(
                        f"{entity['PartitionKey']}/{entity['RowKey']}"
                    ) from exc
                if _is_precondition_failed(exc):
                    raise ConcurrencyError("Azure Table ETag condition failed") from exc
                raise

    async def _delete_entity(
        self,
        partition_key: str,
        row_key: str,
        *,
        kind: str,
    ) -> None:
        identity = {
            "PartitionKey": partition_key,
            "RowKey": row_key,
            "Kind": kind,
        }
        async with self._identity_locks([identity]):
            try:
                entity = await self._get_entity(partition_key, row_key)
            except NotFoundError:
                entity = None
            payload_ref: str | None = None
            if entity is not None:
                actual_kind = _property(entity, "Kind")
                if actual_kind != kind:
                    raise ValueError(
                        "Azure Table entity kind mismatch: "
                        f"expected {kind!r}, got {actual_kind!r}"
                    )
                payload_ref = _payload_ref(entity)
            marker = await self._ensure_payload_deletion_marker(
                partition_key=partition_key,
                row_key=row_key,
                kind=kind,
                legacy_payload_ref=payload_ref,
            )
            await self._complete_payload_deletion_locked(marker)

    async def _submit_transaction(
        self,
        operations: list[tuple[object, ...]],
    ) -> None:
        entities = [
            dict(_mapping(operation[1]))
            for operation in operations
        ]
        async with self._identity_locks(entities):
            for entity in entities:
                await self._assert_no_payload_deletion(entity)
            materialized_operations: list[tuple[object, ...]] = []
            for operation, entity in zip(operations, entities, strict=True):
                materialized_operations.append(
                    (
                        operation[0],
                        await self._materialize_entity_payload(entity),
                        *operation[2:],
                    )
                )
            await _maybe_await(
                self._table.submit_transaction(materialized_operations)  # type: ignore[attr-defined]
            )

    async def _query(
        self,
        query_filter: str,
        *,
        parameters: dict[str, object],
    ) -> list[object]:
        values = self._table.query_entities(  # type: ignore[attr-defined]
            query_filter=query_filter,
            parameters=parameters,
        )
        return [item async for item in _iterate(values)]

    async def _assert_no_payload_deletion(self, entity: object) -> None:
        partition_key = _property(entity, "PartitionKey")
        row_key = _property(entity, "RowKey")
        kind = _property(entity, "Kind")
        if not all(
            isinstance(value, str)
            for value in (partition_key, row_key, kind)
        ):
            raise ValueError("Azure Table entity identity is invalid")
        if partition_key == _PAYLOAD_DELETION_PARTITION or kind == "PayloadDeletion":
            return
        marker_row_key = self._payload_deletion_row_key(
            str(partition_key),
            str(row_key),
            str(kind),
        )
        try:
            await self._get_entity(
                _PAYLOAD_DELETION_PARTITION,
                marker_row_key,
            )
        except NotFoundError:
            return
        raise ConcurrencyError("Azure Table entity deletion is still in progress")

    async def _materialize_entity_payload(
        self,
        entity: dict[str, object],
    ) -> dict[str, object]:
        stored = dict(entity)
        pending = stored.pop(_PENDING_PAYLOAD_BYTES, None)
        if pending is None:
            return stored
        ref = _payload_ref(stored)
        if (
            not isinstance(pending, bytes)
            or ref is None
            or self._artifacts is None
        ):
            raise ValueError("Azure Table overflow payload is invalid")
        await self._artifacts.create_bytes(
            ref,
            pending,
            content_type="application/json",
        )
        return stored

    @staticmethod
    def _payload_owner_prefix(
        *,
        partition_key: str,
        row_key: str,
        kind: str,
    ) -> str:
        owner = content_sha256(f"{partition_key}\0{row_key}".encode())[:32]
        return f"state/table-payloads/{kind.lower()}/{owner}/"

    async def _payload_reference_count(self, ref: str) -> int:
        entities = await self._query(
            "PayloadRef eq @payload_ref",
            parameters={"payload_ref": ref},
        )
        return len(entities)

    @staticmethod
    def _payload_deletion_row_key(
        partition_key: str,
        row_key: str,
        kind: str,
    ) -> str:
        identity = content_sha256(
            f"{partition_key}\0{row_key}\0{kind}".encode()
        )[:48]
        return f"{kind.lower()}-{identity}"

    async def _ensure_payload_deletion_marker(
        self,
        *,
        partition_key: str,
        row_key: str,
        kind: str,
        legacy_payload_ref: str | None,
    ) -> object:
        marker = {
            "PartitionKey": _PAYLOAD_DELETION_PARTITION,
            "RowKey": self._payload_deletion_row_key(partition_key, row_key, kind),
            "Kind": "PayloadDeletion",
            "TargetPartitionKey": partition_key,
            "TargetRowKey": row_key,
            "TargetKind": kind,
            "LegacyPayloadRef": legacy_payload_ref,
        }
        try:
            await _maybe_await(
                self._table.create_entity(entity=marker)  # type: ignore[attr-defined]
            )
        except Exception as exc:
            if not _is_table_exists(exc):
                raise
            existing = await self._get_entity(
                _PAYLOAD_DELETION_PARTITION,
                str(marker["RowKey"]),
            )
            for field in (
                "TargetPartitionKey",
                "TargetRowKey",
                "TargetKind",
            ):
                if _property(existing, field) != marker[field]:
                    raise ConcurrencyError(
                        "Azure Table payload-deletion marker collision"
                    ) from exc
            existing_ref = _payload_ref(
                {"PayloadRef": _property(existing, "LegacyPayloadRef")}
            )
            if (
                legacy_payload_ref is not None
                and existing_ref != legacy_payload_ref
            ):
                raise ConcurrencyError(
                    "Azure Table payload-deletion marker changed"
                ) from exc
            return existing
        return marker

    @staticmethod
    def _payload_deletion_identity(marker: object) -> dict[str, object]:
        return {
            "PartitionKey": _property(marker, "TargetPartitionKey"),
            "RowKey": _property(marker, "TargetRowKey"),
            "Kind": _property(marker, "TargetKind"),
        }

    async def _complete_payload_deletion(self, marker: object) -> None:
        async with self._identity_locks(
            [self._payload_deletion_identity(marker)]
        ):
            marker_row_key = _property(marker, "RowKey")
            if not isinstance(marker_row_key, str):
                raise ValueError("Azure Table payload-deletion marker is invalid")
            try:
                current = await self._get_entity(
                    _PAYLOAD_DELETION_PARTITION,
                    marker_row_key,
                )
            except NotFoundError:
                return
            if (
                self._payload_deletion_identity(current)
                != self._payload_deletion_identity(marker)
            ):
                raise ConcurrencyError(
                    "Azure Table payload-deletion marker identity changed"
                )
            await self._complete_payload_deletion_locked(current)

    async def _complete_payload_deletion_locked(self, marker: object) -> None:
        partition_key = _property(marker, "TargetPartitionKey")
        row_key = _property(marker, "TargetRowKey")
        kind = _property(marker, "TargetKind")
        legacy_ref = _property(marker, "LegacyPayloadRef")
        marker_row_key = _property(marker, "RowKey")
        if (
            not isinstance(partition_key, str)
            or not isinstance(row_key, str)
            or not isinstance(kind, str)
            or not isinstance(marker_row_key, str)
            or not (legacy_ref is None or isinstance(legacy_ref, str))
        ):
            raise ValueError("Azure Table payload-deletion marker is invalid")
        try:
            await _maybe_await(
                self._table.delete_entity(  # type: ignore[attr-defined]
                    partition_key=partition_key,
                    row_key=row_key,
                )
            )
        except Exception as exc:
            if not _is_not_found(exc):
                try:
                    await self._get_entity(partition_key, row_key)
                except NotFoundError:
                    pass
                except Exception as readback_exc:
                    raise exc from readback_exc
                else:
                    raise
        if self._artifacts is not None:
            prefix = self._payload_owner_prefix(
                partition_key=partition_key,
                row_key=row_key,
                kind=kind,
            )
            await self._artifacts.delete_table_payload_prefix(prefix)
            if (
                isinstance(legacy_ref, str)
                and await self._payload_reference_count(legacy_ref) == 0
            ):
                await self._artifacts.delete_table_payload(legacy_ref)
        try:
            await _maybe_await(
                self._table.delete_entity(  # type: ignore[attr-defined]
                    partition_key=_PAYLOAD_DELETION_PARTITION,
                    row_key=marker_row_key,
                )
            )
        except Exception as exc:
            if not _is_not_found(exc):
                try:
                    await self._get_entity(
                        _PAYLOAD_DELETION_PARTITION,
                        marker_row_key,
                    )
                except NotFoundError:
                    pass
                except Exception as readback_exc:
                    raise exc from readback_exc
                else:
                    raise

    async def _sweep_payload_deletions(self) -> None:
        markers = await self._query(
            "PartitionKey eq @partition and Kind eq @kind",
            parameters={
                "partition": _PAYLOAD_DELETION_PARTITION,
                "kind": "PayloadDeletion",
            },
        )
        for marker in markers:
            await self._complete_payload_deletion(marker)

    async def _resume_payload_deletion(
        self,
        *,
        partition_key: str,
        row_key: str,
        kind: str,
    ) -> bool:
        marker_row_key = self._payload_deletion_row_key(
            partition_key,
            row_key,
            kind,
        )
        try:
            marker = await self._get_entity(
                _PAYLOAD_DELETION_PARTITION,
                marker_row_key,
            )
        except NotFoundError:
            return False
        await self._complete_payload_deletion(marker)
        return True

    async def _record_entity(
        self,
        *,
        partition_key: str,
        row_key: str,
        kind: str,
        payload: JsonObject,
        indexes: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        encoded = strict_json_bytes(payload, max_bytes=MAX_METADATA_BYTES)
        text = encoded.decode("utf-8")
        entity: dict[str, object] = {
            "PartitionKey": partition_key,
            "RowKey": row_key,
            "Kind": kind,
        }
        if len(text.encode("utf-16-le")) <= _MAX_TABLE_STRING_BYTES:
            entity["Payload"] = text
        else:
            if self._artifacts is None:
                raise StrictJsonError(
                    "Azure Table payload exceeds the 64-KiB UTF-16 string limit; "
                    "configure Blob payload storage"
                )
            digest = content_sha256(encoded)
            ref = validate_artifact_path(
                self._payload_owner_prefix(
                    partition_key=partition_key,
                    row_key=row_key,
                    kind=kind,
                )
                + f"{digest}.json"
            )
            entity["PayloadRef"] = ref
            entity["PayloadSha256"] = digest
            entity[_PENDING_PAYLOAD_BYTES] = encoded
        if indexes:
            entity.update(indexes)
        return entity

    async def _entity_payload(self, entity: object) -> JsonObject:
        payload = _property(entity, "Payload")
        if isinstance(payload, str):
            if len(payload.encode("utf-16-le")) > _MAX_TABLE_STRING_BYTES:
                raise StrictJsonError("Azure Table payload exceeds its UTF-16 string limit")
            return require_json_object(
                strict_json_loads(payload, max_bytes=MAX_METADATA_BYTES),
                field="entity payload",
            )
        ref = _property(entity, "PayloadRef")
        digest = _property(entity, "PayloadSha256")
        if not isinstance(ref, str) or not isinstance(digest, str) or self._artifacts is None:
            raise ValueError("Azure Table entity is missing its JSON payload")
        encoded = await self._artifacts.get_bytes(validate_artifact_path(ref))
        if content_sha256(encoded) != digest:
            raise ValueError("Azure Table Blob payload checksum mismatch")
        return require_json_object(
            strict_json_loads(encoded, max_bytes=MAX_METADATA_BYTES),
            field="entity payload",
        )


class AzureProjectRepository(_AzureTableRepository):
    async def create(self, project: ProjectRecord) -> Versioned[ProjectRecord]:
        await self._create_entity(
            await self._record_entity(
                partition_key=project.owner_scope,
                row_key=project.project_id,
                kind="Project",
                payload=project.to_dict(),
                indexes={"UpdatedAt": datetime_text(project.updated_at)},
            )
        )
        return await self.get(project.owner_scope, project.project_id)

    async def get(self, owner_scope: str, project_id: str) -> Versioned[ProjectRecord]:
        entity = await self._get_entity(
            validate_id(owner_scope, field="owner_scope"),
            validate_id(project_id, field="project_id"),
        )
        return Versioned(
            ProjectRecord.from_dict(await self._entity_payload(entity)),
            _entity_etag(entity),
        )

    async def replace(
        self,
        project: ProjectRecord,
        *,
        expected_etag: str,
    ) -> Versioned[ProjectRecord]:
        await self._replace_entity(
            await self._record_entity(
                partition_key=project.owner_scope,
                row_key=project.project_id,
                kind="Project",
                payload=project.to_dict(),
                indexes={"UpdatedAt": datetime_text(project.updated_at)},
            ),
            expected_etag=expected_etag,
        )
        return await self.get(project.owner_scope, project.project_id)

    async def list(self, owner_scope: str) -> list[Versioned[ProjectRecord]]:
        owner = validate_id(owner_scope, field="owner_scope")
        entities = await self._query(
            "PartitionKey eq @partition and Kind eq @kind",
            parameters={"partition": owner, "kind": "Project"},
        )
        records = [
            Versioned(
                ProjectRecord.from_dict(await self._entity_payload(entity)),
                _entity_etag(entity),
            )
            for entity in entities
        ]
        return sorted(records, key=lambda stored: stored.value.project_id)

    async def resume_delete(self, owner_scope: str, project_id: str) -> bool:
        return await self._resume_payload_deletion(
            partition_key=validate_id(owner_scope, field="owner_scope"),
            row_key=validate_id(project_id, field="project_id"),
            kind="Project",
        )

    async def delete(self, owner_scope: str, project_id: str) -> None:
        await self._sweep_payload_deletions()
        await self._delete_entity(
            validate_id(owner_scope, field="owner_scope"),
            validate_id(project_id, field="project_id"),
            kind="Project",
        )


class AzureRunRepository(_AzureTableRepository):
    def __init__(
        self,
        table_client: object,
        *,
        clock: Callable[[], datetime] = utc_now,
        artifacts: AzureArtifactRepository | None = None,
    ) -> None:
        super().__init__(table_client, artifacts=artifacts)
        self._clock = clock

    @staticmethod
    def _recovery_pending(run: RunRecord) -> bool:
        return (
            not run.finalizer_completed
            and run.status not in {"completed", "failed"}
        ) or (
            run.finalizer_completed
            and not run.completion_event_emitted
        )

    @staticmethod
    def _recovery_row_key(run_id: str) -> str:
        return validate_id(run_id, field="run_id")

    async def _ensure_recovery_index(self, run: RunRecord) -> None:
        marker = await self._record_entity(
            partition_key=_RECOVERY_PARTITION,
            row_key=self._recovery_row_key(run.run_id),
            kind="RunRecovery",
            payload={
                "project_id": run.project_id,
                "run_id": run.run_id,
                "created_at": datetime_text(
                    ensure_utc(self._clock(), field_name="clock")
                ),
            },
        )
        try:
            await self._create_entity(marker)
        except AlreadyExistsError:
            existing = await self._get_entity(
                _RECOVERY_PARTITION,
                self._recovery_row_key(run.run_id),
            )
            payload = await self._entity_payload(existing)
            if (
                payload.get("project_id") != run.project_id
                or payload.get("run_id") != run.run_id
            ):
                raise ConcurrencyError("Run recovery index collision") from None

    async def _ensure_run_lookup(self, run: RunRecord) -> None:
        marker = await self._record_entity(
            partition_key=_RUN_LOOKUP_PARTITION,
            row_key=run.run_id,
            kind="RunLookup",
            payload={
                "project_id": run.project_id,
                "run_id": run.run_id,
            },
        )
        try:
            await self._create_entity(marker)
        except AlreadyExistsError:
            existing = await self._get_entity(
                _RUN_LOOKUP_PARTITION,
                run.run_id,
            )
            payload = await self._entity_payload(existing)
            if (
                payload.get("project_id") != run.project_id
                or payload.get("run_id") != run.run_id
            ):
                raise ConcurrencyError("Run lookup collision") from None

    async def _remove_recovery_index(self, run_id: str) -> None:
        await self._delete_entity(
            _RECOVERY_PARTITION,
            self._recovery_row_key(run_id),
            kind="RunRecovery",
        )

    async def _entity(self, run: RunRecord) -> dict[str, object]:
        return await self._record_entity(
            partition_key=run.project_id,
            row_key=run.run_id,
            kind="Run",
            payload=run.to_dict(),
            indexes={
                "RunId": run.run_id,
                "Status": run.status,
                "PendingTasks": run.pending_tasks,
                "FinalizerCompleted": run.finalizer_completed,
                "RecoveryPending": self._recovery_pending(run),
                "UpdatedAt": datetime_text(run.updated_at),
            },
        )

    async def create(self, run: RunRecord) -> Versioned[RunRecord]:
        await self._ensure_run_lookup(run)
        if self._recovery_pending(run):
            await self._ensure_recovery_index(run)
        await self._create_entity(await self._entity(run))
        if self._recovery_pending(run):
            await self._ensure_recovery_index(run)
        return await self.get(run.project_id, run.run_id)

    async def get(self, project_id: str, run_id: str) -> Versioned[RunRecord]:
        entity = await self._get_entity(
            validate_id(project_id, field="project_id"),
            validate_id(run_id, field="run_id"),
        )
        return Versioned(
            RunRecord.from_dict(await self._entity_payload(entity)),
            _entity_etag(entity),
        )

    async def find(self, run_id: str) -> Versioned[RunRecord]:
        run = validate_id(run_id, field="run_id")
        try:
            lookup = await self._get_entity(_RUN_LOOKUP_PARTITION, run)
        except NotFoundError:
            entities = await self._query(
                "Kind eq @kind and RunId eq @run",
                parameters={"kind": "Run", "run": run},
            )
            if not entities:
                raise
            if len(entities) != 1:
                raise ConcurrencyError("Run ID is not globally unique") from None
            entity = entities[0]
            return Versioned(
                RunRecord.from_dict(await self._entity_payload(entity)),
                _entity_etag(entity),
            )
        payload = await self._entity_payload(lookup)
        project_id = payload.get("project_id")
        if not isinstance(project_id, str) or payload.get("run_id") != run:
            raise ValueError("Run lookup is malformed")
        try:
            return await self.get(project_id, run)
        except NotFoundError:
            await self._delete_entity(
                _RUN_LOOKUP_PARTITION,
                run,
                kind="RunLookup",
            )
            raise

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
        entities = await self._query(
            "PartitionKey eq @partition and Kind eq @kind",
            parameters={"partition": project, "kind": "Run"},
        )
        records = [
            Versioned(
                RunRecord.from_dict(await self._entity_payload(entity)),
                _entity_etag(entity),
            )
            for entity in entities
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
        await self._replace_entity(
            await self._entity(run),
            expected_etag=expected_etag,
        )
        if not self._recovery_pending(run):
            await self._remove_recovery_index(run.run_id)
        return await self.get(run.project_id, run.run_id)

    async def list_unfinished(self) -> builtins.list[Versioned[RunRecord]]:
        entities = await self._query(
            "PartitionKey eq @partition and Kind eq @kind",
            parameters={
                "partition": _RECOVERY_PARTITION,
                "kind": "RunRecovery",
            },
        )
        records: list[Versioned[RunRecord]] = []
        for entity in entities:
            marker = await self._entity_payload(entity)
            project_id = marker.get("project_id")
            run_id = marker.get("run_id")
            if not isinstance(project_id, str) or not isinstance(run_id, str):
                raise ValueError("Run recovery index is malformed")
            try:
                stored = await self.get(project_id, run_id)
            except NotFoundError:
                created_at = parse_datetime(
                    marker.get("created_at"),
                    field_name="run recovery created_at",
                )
                if (
                    created_at + _RECOVERY_ORPHAN_GRACE
                    <= ensure_utc(self._clock(), field_name="clock")
                ):
                    await self._remove_recovery_index(run_id)
                continue
            if not self._recovery_pending(stored.value):
                await self._remove_recovery_index(run_id)
                continue
            records.append(stored)
        return sorted(
            records,
            key=lambda stored: (stored.value.created_at, stored.value.run_id),
        )

    async def delete_auxiliary(self, project_id: str, run_id: str) -> None:
        await self._sweep_payload_deletions()
        project = validate_id(project_id, field="project_id")
        run = validate_id(run_id, field="run_id")
        terminal = await self._query(
            "PartitionKey eq @partition and Kind eq @kind and RunId eq @run",
            parameters={"partition": project, "kind": "TerminalReceipt", "run": run},
        )
        for entity in terminal:
            row_key = _property(entity, "RowKey")
            if isinstance(row_key, str):
                await self._delete_entity(
                    project,
                    row_key,
                    kind="TerminalReceipt",
                )
        await self._delete_entity(
            _RECOVERY_PARTITION,
            self._recovery_row_key(run),
            kind="RunRecovery",
        )

    async def resume_delete(self, run_id: str) -> bool:
        run = validate_id(run_id, field="run_id")
        markers = await self._query(
            "PartitionKey eq @partition and Kind eq @kind",
            parameters={
                "partition": _PAYLOAD_DELETION_PARTITION,
                "kind": "PayloadDeletion",
            },
        )
        resumed = False
        for marker in markers:
            if (
                _property(marker, "TargetRowKey") == run
                and _property(marker, "TargetKind") in {"Run", "RunLookup"}
            ):
                await self._complete_payload_deletion(marker)
                resumed = True
        if resumed:
            await self._delete_entity(
                _RUN_LOOKUP_PARTITION,
                run,
                kind="RunLookup",
            )
        return resumed

    async def delete(self, project_id: str, run_id: str) -> None:
        await self._sweep_payload_deletions()
        project = validate_id(project_id, field="project_id")
        run = validate_id(run_id, field="run_id")
        await self._delete_entity(project, run, kind="Run")
        await self._delete_entity(
            _RUN_LOOKUP_PARTITION,
            run,
            kind="RunLookup",
        )

    def _terminal_row_key(self, run_id: str, task_pair_id: str) -> str:
        split_pair_id(task_pair_id)
        return f"{run_id}~terminal~{content_sha256(task_pair_id.encode())}"

    async def record_terminal(
        self,
        project_id: str,
        run_id: str,
        task_pair_id: str,
        outcome: TerminalOutcome,
        *,
        poisoned: bool = False,
    ) -> TerminalAccounting:
        validate_id(project_id, field="project_id")
        validate_id(run_id, field="run_id")
        split_pair_id(task_pair_id)
        if outcome not in {"succeeded", "invalid", "failed"}:
            raise ValueError("Unsupported terminal outcome")
        if poisoned and outcome != "failed":
            raise ValueError("Only a failed outcome can be poison-terminal")
        now = ensure_utc(self._clock(), field_name="clock")
        marker: JsonObject = {
            "run_id": run_id,
            "pair_id": task_pair_id,
            "outcome": outcome,
            "poisoned": poisoned,
            "recorded_at": datetime_text(now),
        }
        marker_entity = await self._record_entity(
            partition_key=project_id,
            row_key=self._terminal_row_key(run_id, task_pair_id),
            kind="TerminalReceipt",
            payload=marker,
            indexes={"RunId": run_id, "Outcome": outcome, "Poisoned": poisoned},
        )
        async def existing_marker() -> JsonObject | None:
            try:
                existing = await self._get_entity(
                    project_id,
                    str(marker_entity["RowKey"]),
                )
            except NotFoundError:
                return None
            return await self._entity_payload(existing)

        def validate_existing(existing_payload: JsonObject) -> None:
            if (
                existing_payload.get("pair_id") != task_pair_id
                or existing_payload.get("outcome") != outcome
                or existing_payload.get("poisoned") != poisoned
            ):
                raise ConcurrencyError(
                    f"Terminal outcome already recorded differently for {task_pair_id}"
                ) from None

        for _ in range(_MAX_CAS_ATTEMPTS):
            marker_payload = await existing_marker()
            if marker_payload is not None:
                validate_existing(marker_payload)
                return TerminalAccounting(
                    applied=False,
                    run=await self.get(project_id, run_id),
                    became_zero=False,
                )
            current = await self.get(project_id, run_id)
            if current.value.pending_tasks <= 0:
                raise ConcurrencyError("Run terminal counter is already complete")
            pending = current.value.pending_tasks - 1
            status = current.value.status
            if status == "queued":
                status = "running"
            updated = replace(
                current.value,
                pending_tasks=pending,
                succeeded_tasks=(
                    current.value.succeeded_tasks + (outcome == "succeeded")
                ),
                invalid_tasks=current.value.invalid_tasks + (outcome == "invalid"),
                failed_tasks=current.value.failed_tasks + (outcome == "failed"),
                poisoned_tasks=current.value.poisoned_tasks + poisoned,
                status=status,
                updated_at=now,
            )
            try:
                await self._submit_transaction(
                    [
                        ("create", marker_entity),
                        (
                            "update",
                            await self._entity(updated),
                            _table_condition_arguments(current.etag),
                        ),
                    ]
                )
            except Exception as exc:
                if _is_table_exists(exc):
                    marker_payload = await existing_marker()
                    if marker_payload is None:
                        raise ConcurrencyError(
                            "Terminal receipt transaction reported a collision "
                            "without a durable marker"
                        ) from exc
                    validate_existing(marker_payload)
                    return TerminalAccounting(
                        applied=False,
                        run=await self.get(project_id, run_id),
                        became_zero=False,
                    )
                if _is_precondition_failed(exc):
                    continue
                raise
            try:
                stored = await self.get(project_id, run_id)
            except NotFoundError:
                continue
            return TerminalAccounting(
                applied=True,
                run=stored,
                became_zero=current.value.pending_tasks > 0 and pending == 0,
            )
        raise ConcurrencyError("Could not update the run terminal counter")

    async def claim_finalizer(
        self,
        project_id: str,
        run_id: str,
        *,
        owner: str,
        lease_until: datetime,
    ) -> Versioned[RunRecord] | None:
        validate_id(owner, field="owner")
        for _ in range(_MAX_CAS_ATTEMPTS):
            current = await self.get(project_id, run_id)
            now = ensure_utc(self._clock(), field_name="clock")
            if lease_until.tzinfo is None or lease_until <= now:
                raise ValueError("Finalizer lease must expire in the future")
            active = (
                current.value.finalizer_lease_until is not None
                and current.value.finalizer_lease_until > now
            )
            if (
                current.value.pending_tasks != 0
                or current.value.metadata.get("dispatch_complete") is not True
                or current.value.finalizer_completed
                or (active and current.value.finalizer_owner != owner)
            ):
                return None
            updated = replace(
                current.value,
                status="finalizing",
                finalizer_owner=owner,
                finalizer_lease_until=lease_until,
                updated_at=now,
            )
            try:
                return await self.replace(updated, expected_etag=current.etag)
            except ConcurrencyError:
                continue
        raise ConcurrencyError("Could not claim the run finalizer")

    async def renew_finalizer(
        self,
        project_id: str,
        run_id: str,
        *,
        owner: str,
        lease_until: datetime,
    ) -> Versioned[RunRecord]:
        for _ in range(_MAX_CAS_ATTEMPTS):
            current = await self.get(project_id, run_id)
            now = ensure_utc(self._clock(), field_name="clock")
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
            try:
                return await self.replace(updated, expected_etag=current.etag)
            except ConcurrencyError:
                continue
        raise ConcurrencyError("Could not renew the run finalizer")

    async def complete_finalizer(
        self,
        project_id: str,
        run_id: str,
        *,
        owner: str,
        aggregate_artifact_path: str,
        download_artifact_path: str,
    ) -> FinalizerCompletion:
        for _ in range(_MAX_CAS_ATTEMPTS):
            current = await self.get(project_id, run_id)
            if current.value.finalizer_completed:
                return FinalizerCompletion(applied=False, run=current)
            now = ensure_utc(self._clock(), field_name="clock")
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
            try:
                stored = await self.replace(updated, expected_etag=current.etag)
            except ConcurrencyError:
                continue
            return FinalizerCompletion(applied=True, run=stored)
        raise ConcurrencyError("Could not complete the run finalizer")

    async def release_finalizer(
        self,
        project_id: str,
        run_id: str,
        *,
        owner: str,
    ) -> Versioned[RunRecord]:
        for _ in range(_MAX_CAS_ATTEMPTS):
            current = await self.get(project_id, run_id)
            if current.value.finalizer_completed:
                return current
            now = ensure_utc(self._clock(), field_name="clock")
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
            try:
                return await self.replace(updated, expected_etag=current.etag)
            except ConcurrencyError:
                continue
        raise ConcurrencyError("Could not release the run finalizer")


class AzureTaskRepository(_AzureTableRepository):
    def __init__(
        self,
        table_client: object,
        *,
        clock: Callable[[], datetime] = utc_now,
        artifacts: AzureArtifactRepository | None = None,
    ) -> None:
        super().__init__(table_client, artifacts=artifacts)
        self._clock = clock

    @staticmethod
    def _recovery_pending(task: TaskRecord) -> bool:
        return not task.terminal or not task.terminal_event_emitted

    @staticmethod
    def _recovery_row_key(run_id: str, task_pair_id: str) -> str:
        split_pair_id(task_pair_id)
        digest = content_sha256(f"{run_id}\n{task_pair_id}".encode())
        return validate_id(f"t{digest[:31]}", field="task_recovery_id")

    async def _ensure_recovery_index(self, task: TaskRecord) -> None:
        row_key = self._recovery_row_key(task.run_id, task.pair_id)
        marker = await self._record_entity(
            partition_key=_RECOVERY_PARTITION,
            row_key=row_key,
            kind="TaskRecovery",
            payload={
                "run_id": task.run_id,
                "pair_id": task.pair_id,
                "created_at": datetime_text(
                    ensure_utc(self._clock(), field_name="clock")
                ),
            },
        )
        try:
            await self._create_entity(marker)
        except AlreadyExistsError:
            existing = await self._get_entity(_RECOVERY_PARTITION, row_key)
            payload = await self._entity_payload(existing)
            if (
                payload.get("run_id") != task.run_id
                or payload.get("pair_id") != task.pair_id
            ):
                raise ConcurrencyError("Task recovery index collision") from None

    async def _remove_recovery_index(
        self,
        run_id: str,
        task_pair_id: str,
    ) -> None:
        await self._delete_entity(
            _RECOVERY_PARTITION,
            self._recovery_row_key(run_id, task_pair_id),
            kind="TaskRecovery",
        )

    async def _entity(self, task: TaskRecord) -> dict[str, object]:
        return await self._record_entity(
            partition_key=task.run_id,
            row_key=task.pair_id,
            kind="Task",
            payload=task.to_dict(),
            indexes={
                "ProjectId": task.project_id,
                "Status": task.status,
                "Stage": task.stage,
                "RecoveryPending": self._recovery_pending(task),
                "UpdatedAt": datetime_text(task.updated_at),
            },
        )

    async def create(self, task: TaskRecord) -> Versioned[TaskRecord]:
        if self._recovery_pending(task):
            await self._ensure_recovery_index(task)
        await self._create_entity(await self._entity(task))
        if self._recovery_pending(task):
            await self._ensure_recovery_index(task)
        return await self.get(task.run_id, task.pair_id)

    async def get(self, run_id: str, task_pair_id: str) -> Versioned[TaskRecord]:
        split_pair_id(task_pair_id)
        entity = await self._get_entity(validate_id(run_id, field="run_id"), task_pair_id)
        task = TaskRecord.from_dict(await self._entity_payload(entity))
        if task.pair_id != task_pair_id:
            raise ValueError("Task row key does not match task payload")
        return Versioned(task, _entity_etag(entity))

    async def replace(
        self,
        task: TaskRecord,
        *,
        expected_etag: str,
    ) -> Versioned[TaskRecord]:
        await self._replace_entity(
            await self._entity(task),
            expected_etag=expected_etag,
        )
        if not self._recovery_pending(task):
            await self._remove_recovery_index(task.run_id, task.pair_id)
        return await self.get(task.run_id, task.pair_id)

    async def list_for_run(self, run_id: str) -> list[Versioned[TaskRecord]]:
        run = validate_id(run_id, field="run_id")
        entities = await self._query(
            "PartitionKey eq @partition and Kind eq @kind",
            parameters={"partition": run, "kind": "Task"},
        )
        records = [
            Versioned(
                TaskRecord.from_dict(await self._entity_payload(entity)),
                _entity_etag(entity),
            )
            for entity in entities
        ]
        return sorted(records, key=lambda stored: stored.value.pair_id)

    async def list_resumable(self) -> list[Versioned[TaskRecord]]:
        entities = await self._query(
            "PartitionKey eq @partition and Kind eq @kind",
            parameters={
                "partition": _RECOVERY_PARTITION,
                "kind": "TaskRecovery",
            },
        )
        records: list[Versioned[TaskRecord]] = []
        for entity in entities:
            marker = await self._entity_payload(entity)
            run_id = marker.get("run_id")
            task_pair_id = marker.get("pair_id")
            if not isinstance(run_id, str) or not isinstance(task_pair_id, str):
                raise ValueError("Task recovery index is malformed")
            try:
                stored = await self.get(run_id, task_pair_id)
            except NotFoundError:
                created_at = parse_datetime(
                    marker.get("created_at"),
                    field_name="task recovery created_at",
                )
                if (
                    created_at + _RECOVERY_ORPHAN_GRACE
                    <= ensure_utc(self._clock(), field_name="clock")
                ):
                    await self._remove_recovery_index(run_id, task_pair_id)
                continue
            if not self._recovery_pending(stored.value):
                await self._remove_recovery_index(run_id, task_pair_id)
                continue
            records.append(stored)
        return sorted(
            records,
            key=lambda stored: (stored.value.run_id, stored.value.pair_id),
        )

    async def delete_for_run(self, run_id: str) -> None:
        await self._sweep_payload_deletions()
        run = validate_id(run_id, field="run_id")
        recovery = await self._query(
            "PartitionKey eq @partition and Kind eq @kind",
            parameters={"partition": _RECOVERY_PARTITION, "kind": "TaskRecovery"},
        )
        for entity in recovery:
            payload = await self._entity_payload(entity)
            if payload.get("run_id") != run:
                continue
            row_key = _property(entity, "RowKey")
            if isinstance(row_key, str):
                await self._delete_entity(
                    _RECOVERY_PARTITION,
                    row_key,
                    kind="TaskRecovery",
                )
        tasks = await self._query(
            "PartitionKey eq @partition and Kind eq @kind",
            parameters={"partition": run, "kind": "Task"},
        )
        for entity in tasks:
            row_key = _property(entity, "RowKey")
            if isinstance(row_key, str):
                await self._delete_entity(run, row_key, kind="Task")

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
        for _ in range(_MAX_CAS_ATTEMPTS):
            current = await self.get(run_id, task_pair_id)
            task = current.value
            now = ensure_utc(self._clock(), field_name="clock")
            if lease_until.tzinfo is None or lease_until <= now:
                raise LeaseLostError("Task claim lease has already expired")
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
            try:
                stored = await self.replace(updated, expected_etag=current.etag)
            except ConcurrencyError:
                continue
            return TaskClaim(acquired=True, task=stored)
        raise ConcurrencyError("Could not claim task")

    async def renew_claim(
        self,
        run_id: str,
        task_pair_id: str,
        *,
        owner: str,
        lease_until: datetime,
    ) -> Versioned[TaskRecord]:
        for _ in range(_MAX_CAS_ATTEMPTS):
            current = await self.get(run_id, task_pair_id)
            now = ensure_utc(self._clock(), field_name="clock")
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
            try:
                return await self.replace(updated, expected_etag=current.etag)
            except ConcurrencyError:
                continue
        raise ConcurrencyError("Could not renew task claim")

    async def release_claim(
        self,
        run_id: str,
        task_pair_id: str,
        *,
        owner: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> Versioned[TaskRecord]:
        for _ in range(_MAX_CAS_ATTEMPTS):
            current = await self.get(run_id, task_pair_id)
            if current.value.terminal:
                return current
            now = ensure_utc(self._clock(), field_name="clock")
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
            try:
                return await self.replace(updated, expected_etag=current.etag)
            except ConcurrencyError:
                continue
        raise ConcurrencyError("Could not release task claim")

    async def record_raw(
        self,
        run_id: str,
        task_pair_id: str,
        *,
        owner: str,
        raw_artifact_path: str,
        telemetry_artifact_path: str,
    ) -> Versioned[TaskRecord]:
        for _ in range(_MAX_CAS_ATTEMPTS):
            current = await self.get(run_id, task_pair_id)
            if current.value.terminal:
                return current
            now = ensure_utc(self._clock(), field_name="clock")
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
            try:
                return await self.replace(updated, expected_etag=current.etag)
            except ConcurrencyError:
                continue
        raise ConcurrencyError("Could not record raw artifact")

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
        for _ in range(_MAX_CAS_ATTEMPTS):
            current = await self.get(run_id, task_pair_id)
            task = current.value
            if task.terminal:
                return current
            now = ensure_utc(self._clock(), field_name="clock")
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
            try:
                return await self.replace(updated, expected_etag=current.etag)
            except ConcurrencyError:
                continue
        raise ConcurrencyError("Could not record terminal task intent")

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
        for _ in range(_MAX_CAS_ATTEMPTS):
            current = await self.get(run_id, task_pair_id)
            task = current.value
            if task.terminal:
                if task.status != outcome or task.poisoned != poisoned:
                    raise ConcurrencyError("Task already has a different terminal outcome")
                return TaskCompletion(applied=False, task=current)
            now = ensure_utc(self._clock(), field_name="clock")
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
            try:
                stored = await self.replace(updated, expected_etag=current.etag)
            except ConcurrencyError:
                continue
            return TaskCompletion(applied=True, task=stored)
        raise ConcurrencyError("Could not finish task")


class AzureEventRepository:
    """Ordered immutable Blob events suitable for polling SSE clients."""

    def __init__(
        self,
        artifacts: AzureArtifactRepository,
        container_client: object,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._artifacts = artifacts
        self._container = container_client
        self._clock = clock
        self._run_locks: dict[str, asyncio.Lock] = {}

    def _counter_path(self, run_id: str) -> str:
        return validate_artifact_path(
            f"runs/{validate_id(run_id, field='run_id')}/event-counter.json"
        )

    def _idempotency_path(self, run_id: str, key: str) -> str:
        if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 512:
            raise ValueError("Event idempotency key must be a non-empty bounded string")
        return validate_artifact_path(
            f"runs/{validate_id(run_id, field='run_id')}/event-keys/"
            f"{content_sha256(key.encode('utf-8'))}.json"
        )

    async def _read_blob(self, path: str) -> tuple[bytes, str]:
        blob = self._container.get_blob_client(path)  # type: ignore[attr-defined]
        try:
            downloader = await _maybe_await(blob.download_blob())
            data = bytes(await _maybe_await(downloader.readall()))
        except Exception as exc:
            if _is_not_found(exc):
                raise NotFoundError(path) from exc
            raise
        properties = _property(downloader, "properties")
        etag = _property(properties, "etag") if properties is not None else None
        if etag is None:
            etag = _property(downloader, "etag")
        if not isinstance(etag, str) or not etag:
            raise ValueError("Azure Blob download did not include an ETag")
        return data, etag

    async def _create_blob(self, path: str, data: bytes) -> bool:
        blob = self._container.get_blob_client(path)  # type: ignore[attr-defined]
        try:
            await _maybe_await(blob.upload_blob(data, overwrite=False))
        except Exception as exc:
            if _is_blob_exists(exc):
                return False
            raise
        return True

    async def _replace_blob(
        self,
        path: str,
        data: bytes,
        *,
        etag: str,
        lease: object | None = None,
    ) -> bool:
        blob = self._container.get_blob_client(path)  # type: ignore[attr-defined]
        arguments = _blob_condition_arguments(etag)
        if lease is not None:
            arguments["lease"] = lease
        try:
            await _maybe_await(
                blob.upload_blob(
                    data,
                    overwrite=True,
                    **arguments,
                )
            )
        except Exception as exc:
            if _is_precondition_failed(exc) or _is_not_found(exc):
                return False
            raise
        return True

    async def _acquire_counter_lease(self, run_id: str) -> object:
        path = self._counter_path(run_id)
        initial = strict_json_bytes(
            {"sequence": 0},
            max_bytes=_EVENT_COUNTER_BYTES,
            newline=True,
        )
        try:
            await self._read_blob(path)
        except NotFoundError:
            await self._create_blob(path, initial)
        blob = self._container.get_blob_client(path)  # type: ignore[attr-defined]
        for attempt in range(_MAX_CAS_ATTEMPTS):
            try:
                return await _maybe_await(
                    blob.acquire_lease(lease_duration=_EVENT_LEASE_SECONDS)
                )
            except Exception as exc:
                if not _is_blob_lease_conflict(exc):
                    raise
                await asyncio.sleep(min(0.01 * (attempt + 1), 0.1))
        raise ConcurrencyError("Could not acquire the durable event publication lease")

    async def _release_counter_lease(self, lease: object) -> None:
        try:
            await _maybe_await(lease.release())  # type: ignore[attr-defined]
        except Exception as exc:
            if not (_is_blob_lease_conflict(exc) or _is_not_found(exc)):
                raise

    async def _read_counter(self, run_id: str) -> tuple[JsonObject, str]:
        encoded, etag = await self._read_blob(self._counter_path(run_id))
        counter = require_json_object(
            strict_json_loads(encoded, max_bytes=_EVENT_COUNTER_BYTES),
            field="event counter",
        )
        sequence = counter.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValueError("Durable event counter is invalid")
        pending = counter.get("pending")
        if pending is not None:
            pending_data = require_json_object(pending, field="pending event")
            record = EventRecord.from_dict(pending_data.get("record"))
            if record.run_id != run_id or record.sequence != sequence + 1:
                raise ValueError("Pending durable event does not follow the committed watermark")
            marker_path = pending_data.get("idempotency_path")
            if marker_path is not None:
                if not isinstance(marker_path, str):
                    raise ValueError("Pending event idempotency path must be a string")
                validate_artifact_path(marker_path)
        return counter, etag

    async def _write_counter(
        self,
        run_id: str,
        counter: JsonObject,
        *,
        etag: str,
        lease: object,
    ) -> str:
        encoded = strict_json_bytes(
            counter,
            max_bytes=_EVENT_COUNTER_BYTES,
            newline=True,
        )
        if not await self._replace_blob(
            self._counter_path(run_id),
            encoded,
            etag=etag,
            lease=lease,
        ):
            raise ConcurrencyError("Durable event counter changed while leased")
        _, updated_etag = await self._read_blob(self._counter_path(run_id))
        return updated_etag

    async def _read_idempotency_marker(
        self,
        path: str,
        *,
        idempotency_key: str,
        event: str,
        data: JsonObject,
    ) -> tuple[JsonObject, str]:
        pending: JsonObject = {
            "idempotency_key": idempotency_key,
            "event": event,
            "data": data,
            "record": None,
        }
        try:
            encoded, etag = await self._read_blob(path)
        except NotFoundError:
            await self._create_blob(path, strict_json_bytes(pending, newline=True))
            encoded, etag = await self._read_blob(path)
        marker = require_json_object(
            strict_json_loads(encoded),
            field="event idempotency marker",
        )
        if (
            marker.get("idempotency_key") != idempotency_key
            or marker.get("event") != event
            or marker.get("data") != data
        ):
            raise ConcurrencyError("Event idempotency key was reused with different content")
        return marker, etag

    async def _confirm_idempotency_record(
        self,
        path: str,
        record: EventRecord,
    ) -> None:
        for _ in range(_MAX_CAS_ATTEMPTS):
            encoded, etag = await self._read_blob(path)
            marker = require_json_object(
                strict_json_loads(encoded),
                field="event idempotency marker",
            )
            if marker.get("event") != record.event or marker.get("data") != record.data:
                raise ConcurrencyError("Event idempotency marker content changed")
            stored = marker.get("record")
            if stored is not None:
                if EventRecord.from_dict(stored) != record:
                    raise ConcurrencyError("Event idempotency marker has a different record")
                return
            marker["record"] = record.to_dict()
            if await self._replace_blob(
                path,
                strict_json_bytes(marker, newline=True),
                etag=etag,
            ):
                return
        raise ConcurrencyError("Could not confirm the durable event idempotency record")

    async def _recover_pending(
        self,
        run_id: str,
        counter: JsonObject,
        *,
        etag: str,
        lease: object,
    ) -> tuple[JsonObject, str, EventRecord | None]:
        pending_value = counter.get("pending")
        if pending_value is None:
            return counter, etag, None
        pending = require_json_object(pending_value, field="pending event")
        record = EventRecord.from_dict(pending.get("record"))
        await self._artifacts.create_json(
            run_event(run_id, record.sequence),
            record.to_dict(),
        )
        marker_path = pending.get("idempotency_path")
        if marker_path is not None:
            if not isinstance(marker_path, str):
                raise ValueError("Pending event idempotency path must be a string")
            await self._confirm_idempotency_record(
                validate_artifact_path(marker_path),
                record,
            )
        committed: JsonObject = {"sequence": record.sequence}
        updated_etag = await self._write_counter(
            run_id,
            committed,
            etag=etag,
            lease=lease,
        )
        return committed, updated_etag, record

    async def _append_locked(
        self,
        run_id: str,
        event: str,
        data: JsonObject,
        *,
        idempotency_key: str | None,
        lease: object,
    ) -> EventRecord:
        counter, etag = await self._read_counter(run_id)
        marker_path = (
            self._idempotency_path(run_id, idempotency_key)
            if idempotency_key is not None
            else None
        )
        pending_value = counter.get("pending")
        pending_marker_path: str | None = None
        if pending_value is not None:
            existing_pending = require_json_object(pending_value, field="pending event")
            value = existing_pending.get("idempotency_path")
            if isinstance(value, str):
                pending_marker_path = value
        counter, etag, recovered = await self._recover_pending(
            run_id,
            counter,
            etag=etag,
            lease=lease,
        )
        if recovered is not None and pending_marker_path == marker_path:
            if recovered.event != event or recovered.data != data:
                raise ConcurrencyError(
                    "Recovered event operation has different content"
                )
            return recovered
        if idempotency_key is not None:
            if marker_path is None:
                raise RuntimeError("Event operation marker path is missing")
            marker, _ = await self._read_idempotency_marker(
                marker_path,
                idempotency_key=idempotency_key,
                event=event,
                data=data,
            )
            stored_record = marker.get("record")
            if stored_record is not None:
                record = EventRecord.from_dict(stored_record)
                await self._artifacts.create_json(
                    run_event(run_id, record.sequence),
                    record.to_dict(),
                )
                return record
        sequence_value = counter.get("sequence")
        if not isinstance(sequence_value, int) or isinstance(sequence_value, bool):
            raise ValueError("Durable event counter is invalid")
        sequence = sequence_value + 1
        record = EventRecord(
            run_id=run_id,
            sequence=sequence,
            event=event,
            data=data,
            created_at=ensure_utc(self._clock(), field_name="clock"),
        )
        pending: JsonObject = {
            "sequence": sequence - 1,
            "pending": {
                "record": record.to_dict(),
                "idempotency_path": marker_path,
            },
        }
        pending_etag = await self._write_counter(
            run_id,
            pending,
            etag=etag,
            lease=lease,
        )
        await self._recover_pending(
            run_id,
            pending,
            etag=pending_etag,
            lease=lease,
        )
        return record

    async def append(
        self,
        run_id: str,
        event: str,
        data: JsonObject,
        *,
        idempotency_key: str | None = None,
    ) -> EventRecord:
        validate_id(event, field="event")
        validate_metadata(data)
        validate_id(run_id, field="run_id")
        if idempotency_key is not None:
            normalized_key = bounded_text(
                idempotency_key,
                field="event idempotency key",
                limit=512,
            )
            if normalized_key is None:
                raise ValueError("Event idempotency key cannot be null")
            idempotency_key = normalized_key
            self._idempotency_path(run_id, idempotency_key)
        operation_key = idempotency_key or f"operation:{secrets.token_hex(16)}"
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            for _ in range(_MAX_CAS_ATTEMPTS):
                lease = await self._acquire_counter_lease(run_id)
                try:
                    return await self._append_locked(
                        run_id,
                        event,
                        data,
                        idempotency_key=operation_key,
                        lease=lease,
                    )
                except ConcurrencyError:
                    continue
                finally:
                    await self._release_counter_lease(lease)
        raise ConcurrencyError("Could not publish a durable event")

    async def latest_sequence(self, run_id: str) -> int:
        try:
            counter, _ = await self._read_counter(run_id)
        except NotFoundError:
            return 0
        sequence = counter.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise ValueError("Durable event counter is invalid")
        return sequence

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
        try:
            counter, _ = await self._read_counter(run_id)
        except NotFoundError:
            return []
        committed_value = counter.get("sequence")
        if not isinstance(committed_value, int) or isinstance(committed_value, bool):
            raise ValueError("Durable event counter is invalid")
        committed = committed_value
        records: list[EventRecord] = []
        upper_bound = min(committed, after_sequence + limit)
        for sequence in range(after_sequence + 1, upper_bound + 1):
            try:
                record = EventRecord.from_dict(
                    await self._artifacts.get_json(run_event(run_id, sequence))
                )
            except NotFoundError:
                break
            if record.sequence != sequence:
                raise ValueError("Durable event path does not match its sequence")
            records.append(record)
        return records


class AzureTaskQueue:
    def __init__(
        self,
        queue_client: object,
        poison_queue_client: object,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._queue = queue_client
        self._poison = poison_queue_client
        self._clock = clock

    def _assert_current(self, lease: QueueLease) -> None:
        if lease.lease_until <= ensure_utc(self._clock(), field_name="clock"):
            raise LeaseLostError("Azure Queue visibility lease expired")

    async def send(self, message: TaskMessage) -> str:
        content = strict_json_bytes(
            message.to_dict(),
            max_bytes=MAX_QUEUE_MESSAGE_BYTES,
        ).decode("utf-8")
        response = await _maybe_await(
            self._queue.send_message(content, time_to_live=-1)  # type: ignore[attr-defined]
        )
        message_id = _property(response, "id", "message_id")
        if not isinstance(message_id, str):
            raise ValueError("Azure Queue send did not return a message ID")
        validate_id(message_id, field="message_id")
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
        values = self._queue.receive_messages(  # type: ignore[attr-defined]
            messages_per_page=max_messages,
            visibility_timeout=visibility_timeout,
        )
        fallback = ensure_utc(self._clock(), field_name="clock") + timedelta(
            seconds=visibility_timeout
        )
        leases: list[QueueLease] = []
        async for item in _iterate(values):
            content = _property(item, "content")
            message_id = _property(item, "id", "message_id")
            receipt = _property(item, "pop_receipt")
            dequeue_count = _property(item, "dequeue_count")
            try:
                if not isinstance(content, (str, bytes)):
                    raise ValueError("Azure Queue message has no JSON content")
                if not isinstance(message_id, str) or not isinstance(receipt, str):
                    raise ValueError("Azure Queue message is missing lease identifiers")
                if not isinstance(dequeue_count, int) or isinstance(dequeue_count, bool):
                    raise ValueError("Azure Queue message is missing dequeue_count")
                parsed = strict_json_loads(content, max_bytes=MAX_QUEUE_MESSAGE_BYTES)
                lease = QueueLease(
                    message_id=message_id,
                    pop_receipt=receipt,
                    message=TaskMessage.from_dict(parsed),
                    dequeue_count=dequeue_count,
                    lease_until=_as_utc_datetime(
                        _property(item, "next_visible_on", "time_next_visible"),
                        fallback=fallback,
                    ),
                )
            except (StrictJsonError, TypeError, ValueError):
                if isinstance(message_id, str) and isinstance(receipt, str):
                    with suppress(Exception):
                        await _maybe_await(
                            self._queue.delete_message(  # type: ignore[attr-defined]
                                message_id,
                                receipt,
                            )
                        )
                continue
            leases.append(lease)
            if len(leases) >= max_messages:
                break
        return leases

    async def renew(self, lease: QueueLease, *, visibility_timeout: int) -> QueueLease:
        if visibility_timeout < 1:
            raise ValueError("visibility_timeout must be positive")
        self._assert_current(lease)
        content = strict_json_bytes(
            lease.message.to_dict(),
            max_bytes=MAX_QUEUE_MESSAGE_BYTES,
        ).decode("utf-8")
        try:
            response = await _maybe_await(
                self._queue.update_message(  # type: ignore[attr-defined]
                    lease.message_id,
                    lease.pop_receipt,
                    content=content,
                    visibility_timeout=visibility_timeout,
                )
            )
        except Exception as exc:
            if _is_queue_lease_conflict(exc) or _is_not_found(exc):
                raise LeaseLostError("Azure Queue visibility lease was lost") from exc
            raise
        receipt = _property(response, "pop_receipt")
        if not isinstance(receipt, str):
            raise ValueError("Azure Queue renewal did not return a pop receipt")
        fallback = ensure_utc(self._clock(), field_name="clock") + timedelta(
            seconds=visibility_timeout
        )
        return QueueLease(
            message_id=lease.message_id,
            pop_receipt=receipt,
            message=lease.message,
            dequeue_count=lease.dequeue_count,
            lease_until=_as_utc_datetime(
                _property(response, "next_visible_on", "time_next_visible"),
                fallback=fallback,
            ),
        )

    async def complete(self, lease: QueueLease) -> None:
        self._assert_current(lease)
        try:
            await _maybe_await(
                self._queue.delete_message(  # type: ignore[attr-defined]
                    lease.message_id,
                    lease.pop_receipt,
                )
            )
        except Exception as exc:
            if _is_queue_lease_conflict(exc) or _is_not_found(exc):
                raise LeaseLostError("Azure Queue visibility lease was lost") from exc
            raise

    async def release(self, lease: QueueLease, *, delay: int = 0) -> None:
        if delay < 0:
            raise ValueError("Queue release delay cannot be negative")
        self._assert_current(lease)
        content = strict_json_bytes(
            lease.message.to_dict(),
            max_bytes=MAX_QUEUE_MESSAGE_BYTES,
        ).decode("utf-8")
        try:
            await _maybe_await(
                self._queue.update_message(  # type: ignore[attr-defined]
                    lease.message_id,
                    lease.pop_receipt,
                    content=content,
                    visibility_timeout=delay,
                )
            )
        except Exception as exc:
            if _is_queue_lease_conflict(exc) or _is_not_found(exc):
                raise LeaseLostError("Azure Queue visibility lease was lost") from exc
            raise

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
        self._assert_current(lease)
        payload: JsonObject = {
            "outbox_id": outbox_id,
            "source_message_id": lease.message_id,
            "message": lease.message.to_dict(),
            "reason_code": reason_code,
        }
        content = strict_json_bytes(
            payload,
            max_bytes=MAX_QUEUE_MESSAGE_BYTES,
        ).decode("utf-8")
        response = await _maybe_await(
            self._poison.send_message(content, time_to_live=-1)  # type: ignore[attr-defined]
        )
        message_id = _property(response, "id", "message_id")
        if not isinstance(message_id, str):
            raise ValueError("Azure poison Queue send did not return a message ID")
        validate_id(message_id, field="message_id")
        return message_id


class AzureBlobSeedCoordinator:
    """Distributed startup coordination with completion fenced by the Blob lease."""

    def __init__(
        self,
        container_client: object,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._container = container_client
        self._clock = clock

    def _blob(self, path: str) -> object:
        return self._container.get_blob_client(path)  # type: ignore[attr-defined]

    async def _read_state(self, seed_id: str) -> JsonObject | None:
        lock = self._blob(seed_lock(seed_id))
        try:
            downloader = await _maybe_await(lock.download_blob())  # type: ignore[attr-defined]
            raw = bytes(await _maybe_await(downloader.readall()))
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise
        if not raw:
            return None
        return require_json_object(
            strict_json_loads(raw, max_bytes=MAX_METADATA_BYTES),
            field="seed state",
        )

    async def is_complete(self, seed_id: str) -> bool:
        state = await self._read_state(seed_id)
        if state is not None and state.get("status") == "completed":
            return True
        # Preserve compatibility with markers written by the pre-fencing implementation.
        marker = self._blob(seed_marker(seed_id))
        try:
            return bool(await _maybe_await(marker.exists()))  # type: ignore[attr-defined]
        except Exception as exc:
            if _is_not_found(exc):
                return False
            raise

    async def acquire(
        self,
        seed_id: str,
        *,
        owner: str,
        lease_seconds: int,
    ) -> SeedLease | None:
        validate_id(owner, field="owner")
        if lease_seconds < 15 or lease_seconds > 60:
            raise ValueError("Azure Blob lease duration must be between 15 and 60 seconds")
        if await self.is_complete(seed_id):
            return None
        lock_blob = self._blob(seed_lock(seed_id))
        initial = strict_json_bytes(
            {"seed_id": seed_id, "status": "pending"},
            max_bytes=MAX_METADATA_BYTES,
            newline=True,
        )
        try:
            await _maybe_await(
                lock_blob.upload_blob(initial, overwrite=False)  # type: ignore[attr-defined]
            )
        except Exception as exc:
            if not _is_blob_exists(exc):
                raise
        try:
            acquired_at = ensure_utc(self._clock(), field_name="clock")
            handle = await _maybe_await(
                lock_blob.acquire_lease(lease_duration=lease_seconds)  # type: ignore[attr-defined]
            )
        except Exception as exc:
            if _is_blob_lease_conflict(exc):
                return None
            raise
        if await self.is_complete(seed_id):
            await _maybe_await(handle.release())
            return None
        token = _property(handle, "id", "lease_id")
        if not isinstance(token, str) or not token:
            await _maybe_await(handle.release())
            raise ValueError("Azure Blob lease did not return a lease ID")
        lease_until = acquired_at + timedelta(seconds=lease_seconds)
        leased_state = strict_json_bytes(
            {
                "seed_id": seed_id,
                "status": "leased",
                "owner": owner,
                "token": token,
                "lease_until": datetime_text(lease_until),
            },
            max_bytes=MAX_METADATA_BYTES,
            newline=True,
        )
        try:
            await _maybe_await(
                lock_blob.upload_blob(  # type: ignore[attr-defined]
                    leased_state,
                    overwrite=True,
                    lease=handle,
                )
            )
        except Exception as exc:
            await _maybe_await(handle.release())
            if _is_blob_lease_conflict(exc) or _is_not_found(exc):
                raise LeaseLostError("Azure Blob seed lease was lost") from exc
            raise
        return SeedLease(
            seed_id=seed_id,
            owner=owner,
            token=token,
            lease_until=lease_until,
            lease_seconds=lease_seconds,
            handle=handle,
        )

    async def renew(self, lease: SeedLease, *, lease_seconds: int) -> SeedLease:
        if lease.handle is None:
            raise LeaseLostError("Azure Blob lease handle is unavailable")
        if lease_seconds < 15 or lease_seconds > 60:
            raise ValueError("Azure Blob lease duration must be between 15 and 60 seconds")
        if lease_seconds != lease.lease_seconds:
            raise ValueError("Azure Blob lease duration cannot change during renewal")
        now = ensure_utc(self._clock(), field_name="clock")
        if lease.lease_until <= now:
            raise LeaseLostError("Azure Blob seed lease expired")
        renewed_at = now
        try:
            await _maybe_await(lease.handle.renew())  # type: ignore[attr-defined]
        except Exception as exc:
            if _is_blob_lease_conflict(exc) or _is_not_found(exc):
                raise LeaseLostError("Azure Blob seed lease was lost") from exc
            raise
        return SeedLease(
            seed_id=lease.seed_id,
            owner=lease.owner,
            token=lease.token,
            lease_until=renewed_at + timedelta(seconds=lease.lease_seconds),
            lease_seconds=lease.lease_seconds,
            handle=lease.handle,
        )

    async def complete(self, lease: SeedLease, marker: JsonObject) -> None:
        if lease.handle is None:
            raise LeaseLostError("Azure Blob lease handle is unavailable")
        now = ensure_utc(self._clock(), field_name="clock")
        if lease.lease_until <= now:
            raise LeaseLostError("Azure Blob seed lease expired")
        payload: JsonObject = {
            "seed_id": lease.seed_id,
            "status": "completed",
            "owner": lease.owner,
            "completed_at": datetime_text(now),
            "marker": validate_metadata(marker),
        }
        lock_blob = self._blob(seed_lock(lease.seed_id))
        encoded = strict_json_bytes(payload, max_bytes=MAX_METADATA_BYTES, newline=True)
        try:
            await _maybe_await(
                lock_blob.upload_blob(  # type: ignore[attr-defined]
                    encoded,
                    overwrite=True,
                    lease=lease.handle,
                )
            )
        except Exception as exc:
            if _is_blob_lease_conflict(exc) or _is_not_found(exc):
                raise LeaseLostError("Azure Blob seed lease was lost") from exc
            raise
        await self.release(lease)

    async def release(self, lease: SeedLease) -> None:
        if lease.handle is None:
            return
        try:
            await _maybe_await(lease.handle.release())  # type: ignore[attr-defined]
        except Exception as exc:
            if not (_is_blob_lease_conflict(exc) or _is_not_found(exc)):
                raise


def create_azure_credential(
    *,
    hosted: bool,
    managed_identity_client_id: str | None = None,
) -> Any:
    """Create passwordless auth; DefaultAzureCredential is local-development only."""

    try:
        from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
    except ImportError as exc:
        raise RuntimeError(
            "Azure storage support requires azure-identity, azure-storage-blob, "
            "azure-storage-queue, and azure-data-tables"
        ) from exc
    if hosted:
        if managed_identity_client_id:
            return ManagedIdentityCredential(client_id=managed_identity_client_id)
        return ManagedIdentityCredential()
    return DefaultAzureCredential()


def _validate_account_endpoint(endpoint: str, *, service: str) -> str:
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(f"{service} endpoint must be an HTTPS account endpoint")
    return endpoint.rstrip("/")


def _validate_queue_or_container_name(value: str, *, field_name: str) -> str:
    if _AZURE_NAME.fullmatch(value) is None:
        raise ValueError(f"{field_name} is not a valid Azure Storage name")
    return value


def _validate_table_name(value: str) -> str:
    if _AZURE_TABLE_NAME.fullmatch(value) is None:
        raise ValueError("Azure Table name must be 3-63 alphanumeric characters")
    return value


class AzureStorage:
    """Repository bundle built from account endpoints and passwordless credentials."""

    def __init__(
        self,
        *,
        artifact_container_client: object,
        task_queue_client: object,
        poison_queue_client: object,
        projects_table_client: object,
        runs_table_client: object,
        tasks_table_client: object,
        credential: object | None = None,
        service_clients: Iterable[object] = (),
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.artifacts = AzureArtifactRepository(artifact_container_client)
        self.projects = AzureProjectRepository(
            projects_table_client,
            artifacts=self.artifacts,
        )
        self.runs = AzureRunRepository(
            runs_table_client,
            clock=clock,
            artifacts=self.artifacts,
        )
        self.tasks = AzureTaskRepository(
            tasks_table_client,
            clock=clock,
            artifacts=self.artifacts,
        )
        self.events = AzureEventRepository(
            self.artifacts,
            artifact_container_client,
            clock=clock,
        )
        self.queue = AzureTaskQueue(task_queue_client, poison_queue_client, clock=clock)
        self.seeds = AzureBlobSeedCoordinator(artifact_container_client, clock=clock)
        self._credential = credential
        self._service_clients = tuple(service_clients)

    @classmethod
    def from_endpoints(
        cls,
        *,
        blob_endpoint: str,
        queue_endpoint: str,
        table_endpoint: str,
        hosted: bool,
        artifact_container: str = "artifacts",
        task_queue: str = "extraction-tasks",
        poison_queue: str = "extraction-tasks-poison",
        projects_table: str = "Projects",
        runs_table: str = "Runs",
        tasks_table: str = "Tasks",
        managed_identity_client_id: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> AzureStorage:
        try:
            from azure.data.tables.aio import TableServiceClient
            from azure.storage.blob.aio import BlobServiceClient
            from azure.storage.queue.aio import QueueServiceClient
        except ImportError as exc:
            raise RuntimeError(
                "Azure storage support requires azure-identity, azure-storage-blob, "
                "azure-storage-queue, and azure-data-tables"
            ) from exc
        credential = create_azure_credential(
            hosted=hosted,
            managed_identity_client_id=managed_identity_client_id,
        )
        blob_service = BlobServiceClient(
            account_url=_validate_account_endpoint(blob_endpoint, service="Blob"),
            credential=credential,
        )
        queue_service = QueueServiceClient(
            account_url=_validate_account_endpoint(queue_endpoint, service="Queue"),
            credential=credential,
        )
        table_service = TableServiceClient(
            endpoint=_validate_account_endpoint(table_endpoint, service="Table"),
            credential=credential,
        )
        container_name = _validate_queue_or_container_name(
            artifact_container,
            field_name="artifact_container",
        )
        task_queue_name = _validate_queue_or_container_name(
            task_queue,
            field_name="task_queue",
        )
        poison_queue_name = _validate_queue_or_container_name(
            poison_queue,
            field_name="poison_queue",
        )
        return cls(
            artifact_container_client=blob_service.get_container_client(container_name),
            task_queue_client=queue_service.get_queue_client(task_queue_name),
            poison_queue_client=queue_service.get_queue_client(poison_queue_name),
            projects_table_client=table_service.get_table_client(
                _validate_table_name(projects_table)
            ),
            runs_table_client=table_service.get_table_client(_validate_table_name(runs_table)),
            tasks_table_client=table_service.get_table_client(_validate_table_name(tasks_table)),
            credential=credential,
            service_clients=(blob_service, queue_service, table_service),
            clock=clock,
        )

    async def close(self) -> None:
        for client in self._service_clients:
            close = getattr(client, "close", None)
            if close is not None:
                await _maybe_await(close())
        if self._credential is not None:
            close = getattr(self._credential, "close", None)
            if close is not None:
                await _maybe_await(close())


__all__ = [
    "AzureArtifactRepository",
    "AzureBlobSeedCoordinator",
    "AzureEventRepository",
    "AzureProjectRepository",
    "AzureRunRepository",
    "AzureStorage",
    "AzureTaskQueue",
    "AzureTaskRepository",
    "create_azure_credential",
]
