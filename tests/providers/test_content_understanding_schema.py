from __future__ import annotations

import pytest

from receipt_bench.providers.content_understanding_schema import (
    content_understanding_analyzer_definition,
)


def test_converts_nested_json_schema_to_content_understanding_fields() -> None:
    definition = content_understanding_analyzer_definition(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "vendor": {"type": "string", "description": "Printed vendor."},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "amount": {"type": ["number", "null"]},
                        },
                    },
                },
            },
        },
        schema_hash="a" * 64,
    )

    fields = definition["fieldSchema"]["fields"]  # type: ignore[index]
    assert fields["vendor"] == {  # type: ignore[index]
        "type": "string",
        "description": "Printed vendor.",
        "method": "extract",
    }
    item = fields["items"]["items"]  # type: ignore[index]
    assert item["type"] == "object"
    assert item["properties"]["amount"]["type"] == "number"
    assert definition["tags"]["schemaSha256"] == "a" * 64  # type: ignore[index]


def test_rejects_union_with_multiple_non_null_shapes() -> None:
    with pytest.raises(ValueError, match="one non-null shape"):
        content_understanding_analyzer_definition(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "value": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ]
                    }
                },
            },
            schema_hash="b" * 64,
        )
