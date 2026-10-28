from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from receipt_bench.projects.normalization import normalize_cord_answer

ROOT = Path(__file__).resolve().parents[2]


def _schema(name: str) -> dict[str, Any]:
    value = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    jsonschema.Draft202012Validator.check_schema(value)
    return value


def test_sroie_schema_is_exactly_four_required_strings() -> None:
    schema = _schema("sroie.schema.json")
    validator = jsonschema.Draft202012Validator(schema)
    answer = {
        "company": "Example Store",
        "address": "1 Example Road",
        "date": "25/12/2018",
        "total": "9.00",
    }

    assert set(schema["properties"]) == {"company", "address", "date", "total"}
    assert set(schema["required"]) == set(schema["properties"])
    validator.validate(answer)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**answer, "currency": "MYR"})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**answer, "total": 9.0})
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({key: value for key, value in answer.items() if key != "address"})


def test_cord_schema_covers_only_observed_fixed_set_fields() -> None:
    schema = _schema("cord.schema.json")
    validator = jsonschema.Draft202012Validator(schema)
    answer = {
        "menu": [
            {
                "nm": "ITEM A",
                "cnt": "1X",
                "price": "10,000",
                "unitprice": "@10,000",
                "num": "001",
                "etc": "Food",
                "sub": {"nm": "Ice", "price": "0"},
            },
            {
                "nm": "ITEM B",
                "sub": [{"nm": "Large"}, {"nm": "Extra", "price": "2.000"}],
            },
        ],
        "sub_total": {
            "subtotal_price": "12,000",
            "discount_price": "0",
            "tax_price": "1,200",
            "service_price": "600",
            "etc": ["10,000", "2,000"],
        },
        "total": {
            "total_price": "13,800",
            "cashprice": ["20,000", "20,000"],
            "changeprice": "6,200",
            "creditcardprice": "13,800",
            "menuqty_cnt": "2xITEMs",
            "menutype_cnt": "2",
        },
    }

    assert set(schema["properties"]) == {"menu", "sub_total", "total"}
    assert set(schema["$defs"]["menuItem"]["properties"]) == {
        "nm",
        "cnt",
        "price",
        "unitprice",
        "num",
        "etc",
        "sub",
    }
    assert set(schema["$defs"]["subItem"]["properties"]) == {"nm", "price"}
    assert set(schema["$defs"]["subTotal"]["properties"]) == {
        "subtotal_price",
        "discount_price",
        "tax_price",
        "service_price",
        "etc",
    }
    assert set(schema["$defs"]["total"]["properties"]) == {
        "total_price",
        "cashprice",
        "changeprice",
        "creditcardprice",
        "menuqty_cnt",
        "menutype_cnt",
    }
    validator.validate(answer)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(
            {
                **answer,
                "menu": [{**answer["menu"][0], "itemsubtotal": "10,000"}],
            }
        )


def test_cord_schema_accepts_all_fixed_dataset_native_gold_shapes() -> None:
    rows = json.loads(
        (ROOT / "tests" / "datasets" / "cord_real_rows.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(rows) == {
        "3",
        "11",
        "15",
        "26",
        "28",
        "34",
        "35",
        "42",
        "46",
        "47",
        "49",
        "58",
        "63",
        "68",
        "72",
        "73",
        "74",
        "80",
        "85",
        "98",
    }

    validator = jsonschema.Draft202012Validator(_schema("cord.schema.json"))
    for native_gold in rows.values():
        validator.validate(normalize_cord_answer(native_gold))


def test_cord_menu_singleton_is_normalized_to_array_without_value_changes() -> None:
    native = {
        "menu": {"nm": "ONE ITEM", "cnt": "1", "price": "10.000"},
        "total": {"total_price": "10.000"},
    }
    normalized = normalize_cord_answer(native)

    assert normalized == {
        "menu": [{"nm": "ONE ITEM", "cnt": "1", "price": "10.000"}],
        "total": {"total_price": "10.000"},
    }
    assert native["menu"] == {"nm": "ONE ITEM", "cnt": "1", "price": "10.000"}
    jsonschema.Draft202012Validator(_schema("cord.schema.json")).validate(normalized)
