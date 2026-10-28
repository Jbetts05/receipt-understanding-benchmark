from __future__ import annotations

import copy
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

from receipt_bench.datasets.dates import parse_day_first_date
from receipt_bench.datasets.errors import DatasetParsingError
from receipt_bench.datasets.money import parse_cord_money
from receipt_bench.projects.models import (
    JsonObject,
    JsonPrimitive,
    NormalizationProfile,
    ensure_json_object,
)

_WHITESPACE = re.compile(r"\s+")
_SROIE_TOTAL_PREFIX = re.compile(r"^(?:RM\s*|\$)", re.IGNORECASE)
_CORD_COUNT = re.compile(
    r"^[xX]?\s*([+-]?\d+(?:[.,]\d+)?)\s*(?:[xX]\s*(?:items?)?)?$",
    re.IGNORECASE,
)
_INDEX = re.compile(r"\[\d+\]")
_CORD_MONEY_FIELDS = frozenset(
    {
        "price",
        "unitprice",
        "subtotal_price",
        "discount_price",
        "tax_price",
        "service_price",
        "total_price",
        "cashprice",
        "changeprice",
        "creditcardprice",
    }
)
_CORD_COUNT_FIELDS = frozenset({"cnt", "menuqty_cnt", "menutype_cnt"})


def _text(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value))
    return _WHITESPACE.sub(" ", normalized.strip()).casefold()


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("Numeric comparison values must be finite")
    rendered = format(value.normalize(), "f")
    return "0" if rendered in {"-0", ""} else rendered


def _number(value: JsonPrimitive) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        try:
            result = Decimal(str(value))
        except InvalidOperation:
            return None
        return result if result.is_finite() else None
    return None


def _normalize_sroie_date(value: JsonPrimitive) -> str | None:
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    try:
        return parse_day_first_date(value, context="SROIE comparison date").isoformat()
    except DatasetParsingError:
        return None


def _normalize_sroie_total(value: JsonPrimitive) -> str | None:
    numeric = _number(value)
    if numeric is not None:
        return _decimal_text(numeric)
    if not isinstance(value, str):
        return None
    body = _SROIE_TOTAL_PREFIX.sub("", value.strip(), count=1)
    body = re.sub(r"[,\s]", "", body)
    try:
        return _decimal_text(Decimal(body))
    except InvalidOperation:
        return None


def _normalize_cord_money(value: JsonPrimitive) -> str | None:
    numeric = _number(value)
    if numeric is not None:
        return _decimal_text(numeric)
    if not isinstance(value, str):
        return None
    try:
        return str(parse_cord_money(value, context="CORD comparison amount"))
    except DatasetParsingError:
        return None


def _normalize_cord_count(value: JsonPrimitive) -> str | None:
    numeric = _number(value)
    if numeric is not None:
        return _decimal_text(numeric)
    if not isinstance(value, str):
        return None
    match = _CORD_COUNT.fullmatch(value.strip())
    if match is None:
        return None
    try:
        return _decimal_text(Decimal(match.group(1).replace(",", ".")))
    except InvalidOperation:
        return None


def _field_name(path: str) -> str:
    without_indexes = _INDEX.sub("", path)
    return without_indexes.rsplit(".", 1)[-1]


def normalize_scalar(
    profile: NormalizationProfile,
    path: str,
    value: JsonPrimitive,
) -> str:
    """Return a tagged comparison value under the project's versioned rules."""

    if profile == "sroie-v1":
        if path.endswith(".date") or path == "$.date":
            normalized_date = _normalize_sroie_date(value)
            if normalized_date is not None:
                return f"date:{normalized_date}"
        if path.endswith(".total") or path == "$.total":
            normalized_total = _normalize_sroie_total(value)
            if normalized_total is not None:
                return f"number:{normalized_total}"

    if profile == "cord-v2-v1":
        field = _field_name(path)
        if field in _CORD_MONEY_FIELDS or ".sub_total.etc[" in path:
            normalized_money = _normalize_cord_money(value)
            if normalized_money is not None:
                return f"number:{normalized_money}"
        if field in _CORD_COUNT_FIELDS:
            normalized_count = _normalize_cord_count(value)
            if normalized_count is not None:
                return f"number:{normalized_count}"

    if value is None:
        return "null:"
    if isinstance(value, bool):
        return f"bool:{str(value).lower()}"
    if isinstance(value, int | float):
        numeric = _number(value)
        if numeric is None:
            raise ValueError(f"Non-finite numeric value at {path}")
        return f"number:{_decimal_text(numeric)}"
    return f"text:{_text(value)}"


def normalize_cord_answer(answer: JsonObject) -> JsonObject:
    """Normalize CORD's singleton menu without erasing malformed evidence.

    Invalid structures remain in the answer so schema compliance can fail while
    unrelated valid fields still receive partial scoring.
    """

    normalized = copy.deepcopy(answer)
    menu = normalized.get("menu")
    if isinstance(menu, dict):
        normalized["menu"] = [menu]
    return normalized


def normalize_cord_content_understanding_answer(answer: JsonObject) -> JsonObject:
    """Restore CORD singleton unions from Content Understanding's array-only fields."""

    normalized = normalize_cord_answer(answer)
    menu = normalized.get("menu")
    if isinstance(menu, list):
        for item in menu:
            if not isinstance(item, dict):
                continue
            sub_items = item.get("sub")
            if isinstance(sub_items, list) and len(sub_items) == 1:
                item["sub"] = sub_items[0]
    total = normalized.get("total")
    if isinstance(total, dict):
        cash_prices = total.get("cashprice")
        if isinstance(cash_prices, list) and len(cash_prices) == 1:
            total["cashprice"] = cash_prices[0]
    return normalized


def normalize_answer(profile: NormalizationProfile, answer: object) -> JsonObject:
    payload = ensure_json_object(answer, context="answer")
    if profile == "cord-v2-v1":
        return normalize_cord_answer(payload)
    return copy.deepcopy(payload)


__all__ = [
    "normalize_answer",
    "normalize_cord_answer",
    "normalize_cord_content_understanding_answer",
    "normalize_scalar",
]
