from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]

from receipt_bench.models import CanonicalReceipt

DIRECT_SYSTEM_PROMPT = """You extract employee expense receipt data.
Return exactly one JSON object matching the supplied schema.
Use null when a value is not visibly present. Do not infer missing values.
Preserve item descriptions. Return monetary values as JSON numbers without
currency symbols. Use ISO 4217 currency codes, ISO 8601 dates, and 24-hour
times when those values are visible. Do not add commentary or markdown."""

NORMALIZER_SYSTEM_PROMPT = """You normalize OCR output from an employee expense receipt.
Return exactly one JSON object matching the supplied schema.
Use only evidence present in the OCR text. Use null when evidence is absent or
ambiguous. Do not guess. Return monetary values as JSON numbers without
currency symbols. Use ISO 4217 currency codes, ISO 8601 dates, and 24-hour
times when visible. Do not add commentary or markdown."""


def load_json_schema(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Schema not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"Schema root must be an object: {path}")
    return payload


def image_data_uri(image_bytes: bytes, media_type: str) -> str:
    if not image_bytes:
        raise ValueError("Image bytes cannot be empty")
    if not media_type.startswith("image/"):
        raise ValueError(f"Unsupported media type for image input: {media_type}")
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def openai_response_format(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "expense_receipt",
            "strict": True,
            "schema": openai_compatible_schema(schema),
        },
    }


def openai_compatible_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a project schema to Azure OpenAI's strict structured-output subset.

    Azure strict outputs require every object property to appear in ``required``.
    Dataset schemas intentionally use optional properties, so optional properties
    become required-but-nullable only in this provider-facing copy. Callers remove
    those synthetic null placeholders against the original schema after parsing.
    """
    unsupported = {
        "$id",
        "$schema",
        "format",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "multipleOf",
        "pattern",
        "uniqueItems",
    }

    def nullable(value: dict[str, Any]) -> dict[str, Any]:
        value_type = value.get("type")
        if value_type == "null" or (
            isinstance(value_type, list) and "null" in value_type
        ):
            return value
        alternatives = value.get("anyOf")
        if isinstance(alternatives, list) and any(
            isinstance(item, dict) and item.get("type") == "null"
            for item in alternatives
        ):
            return value
        return {"anyOf": [value, {"type": "null"}]}

    def describes_object(value: dict[str, Any]) -> bool:
        value_type = value.get("type")
        return value_type == "object" or (
            isinstance(value_type, list) and "object" in value_type
        )

    def convert(value: Any) -> Any:
        if isinstance(value, list):
            return [convert(item) for item in value]
        if not isinstance(value, dict):
            return copy.deepcopy(value)

        converted: dict[str, Any] = {}
        for key, item in value.items():
            if key in unsupported:
                continue
            if key == "oneOf":
                converted["anyOf"] = convert(item)
            elif key == "const":
                converted["enum"] = [convert(item)]
            else:
                converted[key] = convert(item)
        properties = converted.get("properties")
        if describes_object(converted) and properties is None:
            properties = {}
            converted["properties"] = properties
        if isinstance(properties, dict):
            originally_required = value.get("required")
            required = (
                set(originally_required)
                if isinstance(originally_required, list)
                else set()
            )
            converted_properties: dict[str, Any] = {}
            for name, property_schema in properties.items():
                if not isinstance(property_schema, dict):
                    raise TypeError(
                        "Boolean JSON Schemas are unsupported for structured-output "
                        f"property {name!r}"
                    )
                converted_properties[name] = (
                    property_schema
                    if name in required
                    else nullable(property_schema)
                )
            converted["properties"] = converted_properties
            converted["required"] = list(properties)
            converted["additionalProperties"] = False
        for keyword in ("$defs",):
            definitions = converted.get(keyword)
            if isinstance(definitions, dict) and any(
                not isinstance(item, dict) for item in definitions.values()
            ):
                raise TypeError("Boolean JSON Schemas are unsupported in $defs")
        items = converted.get("items")
        if isinstance(items, bool):
            raise TypeError("Boolean JSON Schemas are unsupported for array items")
        for keyword in ("anyOf", "allOf"):
            alternatives = converted.get(keyword)
            if isinstance(alternatives, list) and any(
                not isinstance(item, dict) for item in alternatives
            ):
                raise TypeError(
                    f"Boolean JSON Schemas are unsupported in {keyword}"
                )
        return converted

    converted_schema = convert(schema)
    if not isinstance(converted_schema, dict):
        raise TypeError("OpenAI-compatible schema root must be an object")
    return converted_schema


def strip_provider_nulls(
    value: Any,
    schema: dict[str, Any],
) -> Any:
    """Remove nulls introduced solely to satisfy provider strict-schema rules."""

    root = schema
    root_validator = jsonschema.Draft202012Validator(root)

    def resolve(current: dict[str, Any]) -> dict[str, Any]:
        reference = current.get("$ref")
        if not isinstance(reference, str) or not reference.startswith("#/"):
            return current
        resolved: Any = root
        for segment in reference[2:].split("/"):
            key = segment.replace("~1", "/").replace("~0", "~")
            if not isinstance(resolved, dict) or key not in resolved:
                return current
            resolved = resolved[key]
        return resolved if isinstance(resolved, dict) else current

    def allows_null(current: dict[str, Any]) -> bool:
        try:
            return bool(root_validator.evolve(schema=current).is_valid(None))
        except (jsonschema.exceptions.SchemaError, jsonschema.exceptions.RefResolutionError):
            return False

    def child_schema(current: dict[str, Any], item: Any) -> dict[str, Any]:
        resolved = resolve(current)
        for keyword in ("anyOf", "oneOf"):
            alternatives = resolved.get(keyword)
            if isinstance(alternatives, list):
                typed_fallback: dict[str, Any] | None = None
                for alternative in alternatives:
                    if not isinstance(alternative, dict):
                        continue
                    candidate = resolve(alternative)
                    try:
                        if root_validator.evolve(schema=alternative).is_valid(item):
                            return candidate
                    except (
                        jsonschema.exceptions.SchemaError,
                        jsonschema.exceptions.RefResolutionError,
                    ):
                        continue
                    candidate_type = candidate.get("type")
                    if (
                        (candidate_type == "object" and isinstance(item, dict))
                        or (candidate_type == "array" and isinstance(item, list))
                        or (
                            candidate_type in {"string", "number", "integer", "boolean"}
                            and not isinstance(item, dict | list)
                        )
                    ):
                        typed_fallback = typed_fallback or candidate
                if typed_fallback is not None:
                    return typed_fallback
        return resolved

    def clean(item: Any, current: dict[str, Any]) -> Any:
        resolved = child_schema(current, item)
        if isinstance(item, dict):
            properties = resolved.get("properties")
            property_schemas = properties if isinstance(properties, dict) else {}
            required_value = resolved.get("required")
            required = (
                set(required_value) if isinstance(required_value, list) else set()
            )
            cleaned: dict[str, Any] = {}
            for key, child in item.items():
                candidate = property_schemas.get(key, {})
                candidate_schema = candidate if isinstance(candidate, dict) else {}
                synthetic_optional_null = (
                    key in property_schemas
                    and key not in required
                    and child is None
                    and not allows_null(candidate_schema)
                )
                if synthetic_optional_null:
                    continue
                cleaned[key] = clean(child, candidate_schema)
            return cleaned
        if isinstance(item, list):
            items = resolved.get("items")
            item_schema = items if isinstance(items, dict) else {}
            return [clean(child, item_schema) for child in item]
        return item

    return clean(value, schema)


def schema_prompt(schema: dict[str, Any]) -> str:
    return (
        "Extract this receipt using the following JSON Schema:\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )


def normalization_prompt(ocr_text: str, schema: dict[str, Any]) -> str:
    if not ocr_text.strip():
        raise ValueError("OCR text cannot be empty")
    return (
        schema_prompt(schema)
        + "\n\nOCR text begins:\n"
        + ocr_text
        + "\nOCR text ends."
    )


def parse_receipt_json(raw_text: str, schema: dict[str, Any]) -> CanonicalReceipt:
    """Capture scoreable answers without making strict schema compliance a gate.

    The schema remains the prompt contract and is checked later by
    :func:`receipt_bench.evaluation.scoring.validate_schema` as a separate diagnostic.
    Here, Pydantic only establishes the minimal comparable receipt structure. This
    preserves fields such as a correct total when an unrelated formatting detail
    (for example ``"RM"`` rather than ``"MYR"``) violates the strict schema.
    """
    del schema
    if not raw_text.strip():
        raise ValueError("Model returned an empty response")
    payload = json.loads(raw_text)
    if not isinstance(payload, dict):
        raise TypeError("Model response must be a JSON object")
    return CanonicalReceipt.model_validate(payload)
