from __future__ import annotations

import pytest

from receipt_bench.datasets.errors import DatasetParsingError
from receipt_bench.datasets.money import parse_cord_money, parse_cord_quantity, parse_sroie_amount


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9.00", 9.0),
        ("1,234.50", 1234.50),
        (" 12.00 ", 12.0),
        ("0.00", 0.0),
        # A subset of the real pinned SROIE key JSON files render `total` with
        # a leading "$" currency symbol instead of a bare number. These are
        # synthetic representative values chosen only to exercise that
        # format shape -- not actual transcribed SROIE annotation amounts,
        # which are restricted, gated-license content that must not be
        # vendored into this repository even in stripped-down form.
        ("$12.34", 12.34),
        ("$0.99", 0.99),
        ("$100.00", 100.0),
    ],
)
def test_parse_sroie_amount_valid(raw: str, expected: float) -> None:
    assert parse_sroie_amount(raw, context="ctx") == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "abc",
        None,
        12.0,
        "12,00,00.x",
        "$",  # prefix with nothing left to parse
        "$$1.00",  # only one leading "$" is ever stripped
        "1.00$",  # "$" is only tolerated as a leading prefix, never trailing
        "RM1.00",  # only "$" is a narrowly-tolerated prefix, not other symbols
    ],
)
def test_parse_sroie_amount_rejects_invalid(raw: object) -> None:
    with pytest.raises(DatasetParsingError):
        parse_sroie_amount(raw, context="ctx")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("60.000", 60000),
        ("-60.000", -60000),
        ("1,234", 1234),
        ("500", 500),
        (" 12.500 ", 12500),
        # Real pinned CORD test-split rows (audited from the live dataset):
        ("Rp 138.000", 138000),  # row 26 subtotal_price/total_price
        ("Rp138.000", 138000),  # narrowly tolerate no-space variant too
        ("@11000", 11000),  # row 3 menu.unitprice ("at rate of" marker)
        ("@20,000", 20000),  # row 46 menu.unitprice
        ("45,000.00", 45000),  # row 47: anglophone ",XXX.NN" cents rendering
        ("0.00", 0),  # row 47 discount_price
        ("18,000.00", 18000),  # row 47 unitprice
        ("1,591", 1591),  # row 72: genuine non-round tax value, not a bug
        ("25.000", 25000),  # row 15: dot-thousands
        ("17.950", 17950),  # row 85: dot-thousands, 3-digit trailing group
    ],
)
def test_parse_cord_money_valid(raw: str, expected: int) -> None:
    value = parse_cord_money(raw, context="ctx")
    assert value == expected
    assert isinstance(value, int)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "abc",
        None,
        12000,
        "-",
        "12.5a",
        "Rp",  # prefix with nothing left to parse
        "@",  # prefix with nothing left to parse
        "USD 100",  # only Rp/@ are tolerated prefixes
        "Rp Rp 100",  # only one prefix occurrence is ever stripped
    ],
)
def test_parse_cord_money_rejects_invalid(raw: object) -> None:
    with pytest.raises(DatasetParsingError):
        parse_cord_money(raw, context="ctx")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", 1.0),
        ("2.5", 2.5),
        ("2,5", 2.5),
        (" 3 ", 3.0),
        # Real pinned CORD test-split rows (audited from the live dataset):
        ("1X", 1.0),  # rows 3, 46: trailing multiplier marker
        ("x1", 1.0),  # row 63, 73: leading multiplier marker
        ("x2", 2.0),  # row 63
    ],
)
def test_parse_cord_quantity_valid(raw: str, expected: float) -> None:
    assert parse_cord_quantity(raw, context="ctx") == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", None, 1.0, "1,2,3", "1xx", "xx1", "x", "1X2"])
def test_parse_cord_quantity_rejects_invalid(raw: object) -> None:
    with pytest.raises(DatasetParsingError):
        parse_cord_quantity(raw, context="ctx")
