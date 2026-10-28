from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

MAX_ID_LENGTH = 128
MAX_METADATA_BYTES = 64 * 1024
MAX_QUEUE_MESSAGE_BYTES = 48 * 1024
MAX_EVENT_BYTES = 64 * 1024
MAX_ERROR_LENGTH = 2_000
MAX_CONTENT_TYPE_LENGTH = 256

_SAFE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_SAFE_PATH_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MEDIA_TYPE_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class UnsafeIdentifierError(ValueError):
    """Raised when an identifier cannot safely be used in storage keys."""


class UnsafePathError(ValueError):
    """Raised when an artifact path is absolute, ambiguous, or traverses directories."""


class StrictJsonError(ValueError):
    """Raised when a value is not finite, bounded strict JSON."""


def _normalize_unicode(value: str, *, path: str) -> str:
    normalized: list[str] = []
    index = 0
    while index < len(value):
        codepoint = ord(value[index])
        if 0xD800 <= codepoint <= 0xDBFF:
            if index + 1 >= len(value):
                raise StrictJsonError(f"Unpaired Unicode surrogate at {path}")
            low = ord(value[index + 1])
            if not 0xDC00 <= low <= 0xDFFF:
                raise StrictJsonError(f"Unpaired Unicode surrogate at {path}")
            normalized.append(chr(0x10000 + ((codepoint - 0xD800) << 10) + low - 0xDC00))
            index += 2
            continue
        if 0xDC00 <= codepoint <= 0xDFFF:
            raise StrictJsonError(f"Unpaired Unicode surrogate at {path}")
        normalized.append(value[index])
        index += 1
    result = "".join(normalized)
    try:
        result.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StrictJsonError(f"Invalid Unicode string at {path}") from exc
    return result


def _is_windows_reserved(value: str) -> bool:
    return value.split(".", 1)[0].upper() in _WINDOWS_RESERVED


def validate_id(value: str, *, field: str = "identifier") -> str:
    if not isinstance(value, str) or not value:
        raise UnsafeIdentifierError(f"{field} must be a non-empty string")
    if len(value) > MAX_ID_LENGTH or _SAFE_ID.fullmatch(value) is None:
        raise UnsafeIdentifierError(
            f"{field} must be canonical lowercase, start and end with an ASCII letter "
            "or digit, and contain only ASCII lowercase letters, digits, '.', '_', or '-'"
        )
    if value in {".", ".."}:
        raise UnsafeIdentifierError(f"{field} cannot be a traversal segment")
    if value.endswith((".", " ")) or _is_windows_reserved(value):
        raise UnsafeIdentifierError(f"{field} is not portable across storage backends")
    return value


def validate_artifact_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise UnsafePathError("Artifact path must be a non-empty string")
    if value != value.lower():
        raise UnsafePathError("Artifact path must use canonical lowercase segments")
    if "\\" in value or "\x00" in value or value.startswith("/"):
        raise UnsafePathError("Artifact path must be a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value:
        raise UnsafePathError("Artifact path must be normalized")
    segments = path.parts
    if not segments or any(
        segment in {"", ".", ".."}
        or segment.endswith((".", " "))
        or _is_windows_reserved(segment)
        or _SAFE_PATH_SEGMENT.fullmatch(segment) is None
        for segment in segments
    ):
        raise UnsafePathError("Artifact path contains an unsafe segment")
    return value


def validate_deletion_prefix(value: str) -> str:
    """Validate a narrowly scoped project or run artifact deletion prefix."""
    if not isinstance(value, str) or not value.endswith("/"):
        raise UnsafePathError("Artifact deletion prefix must end with '/'")
    path = validate_artifact_path(value[:-1])
    segments = path.split("/")
    if len(segments) != 2 or segments[0] not in {"projects", "runs"}:
        raise UnsafePathError("Artifact deletion prefix must identify one project or run")
    validate_id(segments[1], field=f"{segments[0][:-1]}_id")
    return value


def pair_id(sample_id: str, solution_id: str) -> str:
    return (
        f"{validate_id(sample_id, field='sample_id')}|"
        f"{validate_id(solution_id, field='solution_id')}"
    )


def split_pair_id(value: str) -> tuple[str, str]:
    if value.count("|") != 1:
        raise UnsafeIdentifierError("Pair ID must be '<sample_id>|<solution_id>'")
    sample_id, solution_id = value.split("|", 1)
    return (
        validate_id(sample_id, field="sample_id"),
        validate_id(solution_id, field="solution_id"),
    )


def _normalize_json(value: object, *, path: str, seen: set[int]) -> JsonValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _normalize_unicode(value, path=path)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StrictJsonError(f"Non-finite number at {path}")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise StrictJsonError(f"Circular mapping at {path}")
        seen.add(identity)
        try:
            normalized: JsonObject = {}
            for key, child in value.items():
                if not isinstance(key, str):
                    raise StrictJsonError(f"JSON object key at {path} is not a string")
                normalized_key = _normalize_unicode(key, path=f"{path}.<key>")
                if normalized_key in normalized:
                    raise StrictJsonError(f"Duplicate normalized JSON object key at {path}")
                normalized[normalized_key] = _normalize_json(
                    child,
                    path=f"{path}.{normalized_key}",
                    seen=seen,
                )
            return normalized
        finally:
            seen.remove(identity)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        identity = id(value)
        if identity in seen:
            raise StrictJsonError(f"Circular sequence at {path}")
        seen.add(identity)
        try:
            return [
                _normalize_json(child, path=f"{path}[{index}]", seen=seen)
                for index, child in enumerate(value)
            ]
        finally:
            seen.remove(identity)
    raise StrictJsonError(f"Unsupported JSON value {type(value).__name__} at {path}")


def normalize_json(value: object) -> JsonValue:
    return _normalize_json(value, path="$", seen=set())


def strict_json_bytes(
    value: object,
    *,
    max_bytes: int | None = None,
    newline: bool = False,
) -> bytes:
    normalized = normalize_json(value)
    text = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if newline:
        text += "\n"
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StrictJsonError("JSON contains invalid Unicode") from exc
    if max_bytes is not None and len(encoded) > max_bytes:
        raise StrictJsonError(f"JSON value exceeds the {max_bytes}-byte limit")
    return encoded


def strict_json_loads(data: bytes | str, *, max_bytes: int | None = None) -> JsonValue:
    if isinstance(data, str):
        text = data
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise StrictJsonError("Invalid UTF-8 JSON") from exc
    else:
        encoded = bytes(data)
        try:
            text = encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StrictJsonError("Invalid UTF-8 JSON") from exc
    if max_bytes is not None and len(encoded) > max_bytes:
        raise StrictJsonError(f"JSON value exceeds the {max_bytes}-byte limit")
    if text.startswith("\ufeff"):
        raise StrictJsonError("UTF-8 JSON must not contain a byte-order mark")
    try:
        value = json.loads(
            text,
            parse_constant=lambda token: (_ for _ in ()).throw(
                StrictJsonError(f"Non-finite JSON constant: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StrictJsonError("Invalid UTF-8 JSON") from exc
    return normalize_json(value)


def require_json_object(value: object, *, field: str = "value") -> JsonObject:
    normalized = normalize_json(value)
    if not isinstance(normalized, dict):
        raise StrictJsonError(f"{field} must be a JSON object")
    return normalized


def validate_metadata(value: object) -> JsonObject:
    normalized = require_json_object(value, field="metadata")
    strict_json_bytes(normalized, max_bytes=MAX_METADATA_BYTES)
    return normalized


def bounded_text(value: str | None, *, field: str, limit: int = MAX_ERROR_LENGTH) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string or None")
    normalized = _normalize_unicode(value, path=field)
    if len(normalized.encode("utf-8")) > limit:
        raise ValueError(f"{field} exceeds the {limit}-byte limit")
    return normalized


def validate_content_type(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("content_type must be a string")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("content_type must contain only printable ASCII") from exc
    if (
        not value
        or value != value.strip()
        or len(encoded) > MAX_CONTENT_TYPE_LENGTH
        or any(byte < 0x20 or byte > 0x7E for byte in encoded)
    ):
        raise ValueError("content_type must be a bounded printable ASCII media type")
    media_type, *parameters = value.split(";")
    type_name, separator, subtype = media_type.strip().partition("/")
    if (
        separator != "/"
        or not _MEDIA_TYPE_TOKEN.fullmatch(type_name)
        or not _MEDIA_TYPE_TOKEN.fullmatch(subtype)
    ):
        raise ValueError("content_type must contain a valid type and subtype")
    for parameter in parameters:
        name, separator, parameter_value = parameter.strip().partition("=")
        parameter_value = parameter_value.strip()
        if (
            separator != "="
            or not _MEDIA_TYPE_TOKEN.fullmatch(name.strip())
            or not parameter_value
            or (
                not _MEDIA_TYPE_TOKEN.fullmatch(parameter_value)
                and not (
                    len(parameter_value) >= 2
                    and parameter_value.startswith('"')
                    and parameter_value.endswith('"')
                )
            )
        ):
            raise ValueError("content_type contains an invalid parameter")
    return value


def content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


__all__ = [
    "MAX_CONTENT_TYPE_LENGTH",
    "MAX_ERROR_LENGTH",
    "MAX_EVENT_BYTES",
    "MAX_ID_LENGTH",
    "MAX_METADATA_BYTES",
    "MAX_QUEUE_MESSAGE_BYTES",
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "StrictJsonError",
    "UnsafeIdentifierError",
    "UnsafePathError",
    "bounded_text",
    "content_sha256",
    "normalize_json",
    "pair_id",
    "require_json_object",
    "split_pair_id",
    "strict_json_bytes",
    "strict_json_loads",
    "validate_artifact_path",
    "validate_content_type",
    "validate_id",
    "validate_metadata",
]
