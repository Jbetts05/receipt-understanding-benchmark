"""Tests for permissive answer capture in ``receipt_bench.prompting``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from receipt_bench.evaluation.scoring import validate_schema
from receipt_bench.prompting import (
    openai_compatible_schema,
    parse_receipt_json,
    strip_provider_nulls,
)
from tests.conftest import build_ground_truth_receipt


def test_parse_receipt_json_accepts_a_fully_valid_receipt(receipt_schema: dict[str, Any]) -> None:
    receipt_json = build_ground_truth_receipt().model_dump_json()

    receipt = parse_receipt_json(receipt_json, receipt_schema)

    assert receipt == build_ground_truth_receipt()


def test_parse_receipt_json_preserves_empty_answer_for_missing_field_scoring(
    receipt_schema: dict[str, Any],
) -> None:
    receipt = parse_receipt_json("{}", receipt_schema)

    assert receipt.merchant.name is None
    assert validate_schema(receipt, receipt_schema)


def test_parse_receipt_json_accepts_missing_schema_fields_for_answer_scoring(
    receipt_schema: dict[str, Any],
) -> None:
    payload = json.loads(build_ground_truth_receipt().model_dump_json())
    del payload["merchant"]

    receipt = parse_receipt_json(json.dumps(payload), receipt_schema)

    assert receipt.merchant.name is None
    assert validate_schema(receipt, receipt_schema)


def test_parse_receipt_json_preserves_format_violations_for_answer_scoring(
    receipt_schema: dict[str, Any],
) -> None:
    payload = json.loads(build_ground_truth_receipt().model_dump_json())
    payload["transaction"]["currency"] = "RM"
    payload["transaction"]["date"] = "not-a-date"

    receipt = parse_receipt_json(json.dumps(payload), receipt_schema)

    assert receipt.transaction.currency == "RM"
    assert receipt.transaction.date == "not-a-date"
    assert not validate_schema(receipt, receipt_schema)


def test_parse_receipt_json_rejects_additional_properties(receipt_schema: dict[str, Any]) -> None:
    payload = json.loads(build_ground_truth_receipt().model_dump_json())
    payload["unexpected_field"] = "not part of the contract"

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        parse_receipt_json(json.dumps(payload), receipt_schema)


def test_parse_receipt_json_rejects_empty_string(receipt_schema: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="empty response"):
        parse_receipt_json("   ", receipt_schema)


def test_parse_receipt_json_rejects_non_object_json(receipt_schema: dict[str, Any]) -> None:
    with pytest.raises(TypeError, match="must be a JSON object"):
        parse_receipt_json("[]", receipt_schema)


def test_parse_receipt_json_rejects_invalid_json_syntax(receipt_schema: dict[str, Any]) -> None:
    with pytest.raises(json.JSONDecodeError):
        parse_receipt_json("not json", receipt_schema)


def test_openai_schema_makes_optional_project_fields_required_nullable() -> None:
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / "cord.schema.json").read_text(
            encoding="utf-8"
        )
    )

    compatible = openai_compatible_schema(schema)

    assert set(compatible["required"]) == set(compatible["properties"])
    menu_item = compatible["$defs"]["menuItem"]
    assert set(menu_item["required"]) == set(menu_item["properties"])
    assert menu_item["properties"]["cnt"]["anyOf"][-1] == {"type": "null"}
    assert compatible["properties"]["sub_total"]["anyOf"][-1] == {"type": "null"}


def test_strip_provider_nulls_preserves_real_nullable_values_only() -> None:
    schema = {
        "type": "object",
        "properties": {
            "optional_text": {"type": "string"},
            "required_text": {"type": "string"},
            "real_nullable": {"type": ["string", "null"]},
            "nested": {
                "type": "object",
                "properties": {"optional_number": {"type": "number"}},
            },
        },
        "required": ["required_text"],
    }
    provider_value = {
        "optional_text": None,
        "required_text": None,
        "real_nullable": None,
        "unknown": None,
        "nested": {"optional_number": None},
    }

    cleaned = strip_provider_nulls(provider_value, schema)

    assert cleaned == {
        "required_text": None,
        "real_nullable": None,
        "unknown": None,
        "nested": {},
    }


def test_openai_schema_closes_custom_objects_recursively() -> None:
    schema = {
        "type": "object",
        "properties": {
            "nested": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            }
        },
    }

    compatible = openai_compatible_schema(schema)

    assert compatible["additionalProperties"] is False
    nested = compatible["properties"]["nested"]["anyOf"][0]
    assert nested["additionalProperties"] is False
    assert nested["properties"] == {"value": {"anyOf": [{"type": "string"}, {"type": "null"}]}}


def test_openai_schema_closes_explicit_nullable_object_types() -> None:
    compatible = openai_compatible_schema(
        {
            "type": "object",
            "properties": {
                "nested": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                }
            },
            "additionalProperties": False,
        }
    )

    nested = compatible["properties"]["nested"]
    assert nested["type"] == ["object", "null"]
    assert nested["properties"] == {}
    assert nested["required"] == []
    assert nested["additionalProperties"] is False


def test_strip_provider_nulls_selects_array_union_branch_and_preserves_enum_null() -> None:
    schema = {
        "type": "object",
        "properties": {
            "sub": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {"nm": {"type": "string"}},
                        "required": ["nm"],
                    },
                    {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "nm": {"type": "string"},
                                "price": {"type": "string"},
                            },
                            "required": ["nm"],
                        },
                    },
                ]
            },
            "status": {"enum": [None, "ok"]},
        },
    }

    cleaned = strip_provider_nulls(
        {
            "sub": [{"nm": "ICE", "price": None}],
            "status": None,
        },
        schema,
    )

    assert cleaned == {"sub": [{"nm": "ICE"}], "status": None}


def test_openai_schema_rejects_boolean_property_schema() -> None:
    with pytest.raises(TypeError, match="Boolean JSON Schemas"):
        openai_compatible_schema(
            {"type": "object", "properties": {"anything": True}}
        )
