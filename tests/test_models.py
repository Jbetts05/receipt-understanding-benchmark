from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from receipt_bench.models import CanonicalReceipt, Usage
from receipt_bench.prompting import openai_compatible_schema

ROOT = Path(__file__).resolve().parents[1]


def test_canonical_receipt_matches_committed_schema() -> None:
    receipt = CanonicalReceipt.model_validate(
        {
            "schema_version": "1.0",
            "merchant": {
                "name": "Contoso Cafe",
                "address": None,
                "phone": None,
                "tax_id": None,
            },
            "transaction": {
                "date": "2026-08-28",
                "time": "12:15:00",
                "receipt_number": "R-42",
                "currency": "GBP",
                "country": "GB",
            },
            "amounts": {
                "subtotal": 10.0,
                "discount": None,
                "tax": 2.0,
                "tip": None,
                "total": 12.0,
            },
            "payment_methods": None,
            "receipt_type": "meal",
            "line_items": None,
        }
    )
    schema = json.loads(
        (ROOT / "schemas" / "expense-receipt.schema.json").read_text(encoding="utf-8")
    )

    jsonschema.Draft202012Validator(schema).validate(receipt.model_dump(mode="json"))


def test_canonical_receipt_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CanonicalReceipt.model_validate({"schema_version": "1.0", "unexpected": True})


def test_answer_model_preserves_nonstandard_currency_and_date_strings() -> None:
    receipt = CanonicalReceipt.model_validate(
        {
            "transaction": {
                "currency": "RM",
                "date": "not-a-date",
            }
        }
    )

    assert receipt.transaction.currency == "RM"
    assert receipt.transaction.date == "not-a-date"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_models_reject_non_finite_typed_and_nested_numbers(value: float) -> None:
    with pytest.raises(ValidationError, match="finite"):
        CanonicalReceipt.model_validate({"amounts": {"total": value}})
    with pytest.raises(ValidationError, match="Non-finite"):
        Usage(provider_metadata={"nested": [{"value": value}]})


def test_openai_schema_uses_supported_subset() -> None:
    schema = json.loads(
        (ROOT / "schemas" / "expense-receipt.schema.json").read_text(encoding="utf-8")
    )

    compatible = openai_compatible_schema(schema)
    serialized = json.dumps(compatible)

    assert '"oneOf"' not in serialized
    assert '"const"' not in serialized
    assert '"pattern"' not in serialized
    assert '"format"' not in serialized
    assert '"anyOf"' in serialized
    assert compatible["properties"]["schema_version"]["enum"] == ["1.0"]
