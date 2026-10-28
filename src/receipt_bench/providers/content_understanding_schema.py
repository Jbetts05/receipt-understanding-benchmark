from __future__ import annotations

from typing import Any, cast

from receipt_bench.storage.validation import JsonObject, JsonValue

_SUPPORTED_TYPES = frozenset(
    {"string", "number", "integer", "boolean", "object", "array"}
)


def _resolve_reference(root: JsonObject, reference: str) -> JsonObject:
    if not reference.startswith("#/"):
        raise ValueError("Content Understanding schemas require local references")
    current: JsonValue = root
    for raw_segment in reference[2:].split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or segment not in current:
            raise ValueError(f"JSON Schema reference does not resolve: {reference}")
        current = current[segment]
    if not isinstance(current, dict):
        raise ValueError("JSON Schema reference must resolve to an object schema")
    return current


def _effective_schema(root: JsonObject, schema: JsonObject) -> JsonObject:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        resolved = dict(_resolve_reference(root, reference))
        if isinstance(schema.get("description"), str):
            resolved["description"] = schema["description"]
        return _effective_schema(root, resolved)

    alternatives = schema.get("oneOf") or schema.get("anyOf")
    if isinstance(alternatives, list):
        non_null = [
            item
            for item in alternatives
            if isinstance(item, dict) and item.get("type") != "null"
        ]
        if len(non_null) != 1:
            raise ValueError(
                "Content Understanding custom analyzers require one non-null "
                "shape for each field"
            )
        selected = dict(non_null[0])
        if isinstance(schema.get("description"), str):
            selected["description"] = schema["description"]
        return _effective_schema(root, selected)

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        non_null_types = [item for item in schema_type if item != "null"]
        if len(non_null_types) != 1 or not isinstance(non_null_types[0], str):
            raise ValueError(
                "Content Understanding custom analyzers require one non-null "
                "type for each field"
            )
        selected = dict(schema)
        selected["type"] = non_null_types[0]
        return selected
    return schema


def _field_definition(
    root: JsonObject,
    name: str,
    schema: JsonObject,
    *,
    include_method: bool,
) -> dict[str, Any]:
    effective = _effective_schema(root, schema)
    schema_type = effective.get("type")
    if not isinstance(schema_type, str) or schema_type not in _SUPPORTED_TYPES:
        raise ValueError(
            f"Content Understanding does not support field {name!r} type "
            f"{schema_type!r}"
        )
    description = effective.get("description")
    field: dict[str, Any] = {
        "type": schema_type,
        "description": (
            description.strip()
            if isinstance(description, str) and description.strip()
            else f"Extract the {name} value exactly as supported by the receipt."
        ),
    }
    if include_method:
        field["method"] = "extract"

    if schema_type == "object":
        properties = effective.get("properties")
        if not isinstance(properties, dict) or not properties:
            raise ValueError(
                f"Content Understanding object field {name!r} requires properties"
            )
        field["properties"] = {
            property_name: _field_definition(
                root,
                property_name,
                property_schema,
                include_method=True,
            )
            for property_name, property_schema in properties.items()
            if isinstance(property_name, str) and isinstance(property_schema, dict)
        }
        if len(field["properties"]) != len(properties):
            raise ValueError(f"Content Understanding field {name!r} is malformed")
    elif schema_type == "array":
        items = effective.get("items")
        if not isinstance(items, dict):
            raise ValueError(
                f"Content Understanding array field {name!r} requires one item schema"
            )
        field["items"] = _field_definition(
            root,
            f"{name} item",
            items,
            include_method=False,
        )
    return field


def content_understanding_analyzer_definition(
    schema: JsonObject,
    *,
    schema_hash: str,
) -> JsonObject:
    effective = _effective_schema(schema, schema)
    if effective.get("type") != "object":
        raise ValueError("Content Understanding custom schema root must be an object")
    properties = effective.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise ValueError("Content Understanding custom schema requires properties")
    fields = {
        name: _field_definition(
            schema,
            name,
            field_schema,
            include_method=True,
        )
        for name, field_schema in properties.items()
        if isinstance(name, str) and isinstance(field_schema, dict)
    }
    if len(fields) != len(properties):
        raise ValueError("Content Understanding custom schema properties are malformed")
    return cast(JsonObject, {
        "description": "Receipt Bench custom project extraction contract.",
        "baseAnalyzerId": "prebuilt-document",
        "models": {"completion": "gpt-5.4-mini"},
        "config": {
            "returnDetails": True,
            "enableFormula": False,
            "estimateFieldSourceAndConfidence": True,
            "tableFormat": "html",
        },
        "fieldSchema": {"fields": fields},
        "tags": {
            "managedBy": "receipt-bench",
            "schemaSha256": schema_hash,
        },
    })


__all__ = ["content_understanding_analyzer_definition"]
