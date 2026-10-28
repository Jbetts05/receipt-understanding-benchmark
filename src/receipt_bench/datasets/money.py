from __future__ import annotations

import re
from typing import Any

from receipt_bench.datasets.errors import DatasetParsingError

#: SROIE ``total`` strings use ``,`` as a thousands separator and ``.`` as the
#: decimal point (e.g. ``"1,234.50"``); only the separator/whitespace is stripped.
_SROIE_AMOUNT_NOISE = re.compile(r"[,\s]")

#: A subset of the real pinned SROIE key JSON files (audited across all 80
#: selected ids) render ``total`` with a leading currency symbol instead of a
#: bare number, e.g. an invented example shape like ``"$12.34"`` (the actual
#: SROIE annotation values are restricted, gated-license content and are
#: never reproduced here). Exactly this one symbol is narrowly recognized and
#: stripped as a prefix; no other leading character is ever treated as
#: currency noise.
_SROIE_AMOUNT_PREFIX = re.compile(r"^\$")

#: CORD money strings use ``.`` (and occasionally ``,``) purely as thousands
#: separators, with no fractional/minor-unit component -- except for the
#: occasional anglophone ``",XXX.NN"`` cents rendering (see
#: ``_CORD_TRAILING_GROUP`` below), which is truncated to its integral part.
_CORD_SEPARATOR_NOISE = re.compile(r"[.,\s]")

#: Real CORD ground truth also renders some monetary fields with a currency
#: prefix: ``"Rp 138.000"`` (subtotal/total) or ``"@11000"`` / ``"@20,000"``
#: (a unit-price "at rate of" marker). Both are narrowly and unambiguously
#: recognized and stripped; nothing else is treated as a prefix.
_CORD_MONEY_PREFIX = re.compile(r"^(?:Rp\s*|@)", re.IGNORECASE)

#: Matches the final ``.``/``,``-delimited digit group, used to distinguish a
#: genuine thousands separator (always a full 3-digit trailing group, e.g.
#: the ``.000`` in ``"25.000"``) from an anglophone decimal point (a 1- or
#: 2-digit trailing group, e.g. the ``.00`` in ``"45,000.00"``).
_CORD_TRAILING_GROUP = re.compile(r"[.,](\d+)$")


def parse_sroie_amount(raw: Any, *, context: str) -> float:
    """Parse a SROIE monetary string (e.g. ``"9.00"`` or ``"1,234.50"``) into a float.

    Real key JSON across the pinned 80 selected ids also includes a leading
    ``"$"`` currency-symbol prefix on some ``total`` values (an invented
    example shape: ``"$12.34"``; the actual SROIE annotation values are
    restricted, gated-license content and are never reproduced here) -- this
    exact prefix is narrowly stripped before parsing; every other character
    is left as-is and must resolve to a plain (optionally
    comma-thousands-separated) decimal number. Raises ``DatasetParsingError``
    for a non-string input or an empty/unparsable value; ``context`` is
    prefixed to the error message to identify the sample and field.
    """
    if not isinstance(raw, str):
        raise DatasetParsingError(f"{context}: expected a string amount, got {type(raw).__name__}")
    stripped = raw.strip()
    body = _SROIE_AMOUNT_PREFIX.sub("", stripped, count=1)
    cleaned = _SROIE_AMOUNT_NOISE.sub("", body)
    if not cleaned:
        raise DatasetParsingError(f"{context}: empty monetary value")
    try:
        return float(cleaned)
    except ValueError as exc:
        raise DatasetParsingError(f"{context}: cannot parse monetary value {raw!r}") from exc


def parse_cord_money(raw: Any, *, context: str) -> int:
    """Parse a CORD (Indonesian Rupiah) monetary string into an integral IDR amount.

    CORD ground truth renders money with ``.`` (and occasionally ``,``) as a
    thousands separator and no fractional/minor-unit component -- e.g.
    ``"60.000"`` means sixty thousand rupiah, and ``"-60.000"`` is a negative
    sixty thousand rupiah (used for per-line and subtotal discounts). Real
    ground truth also includes two narrowly-tolerated variants observed
    across the pinned test-split selection:

    - A currency/rate prefix: ``"Rp 138.000"`` (subtotal/total fields) or a
      leading ``@`` (a unit-price "at rate of" marker, e.g. ``"@11000"`` /
      ``"@20,000"``). Exactly these two prefixes are recognized and stripped;
      anything else at the start is treated as unparsable junk.
    - An anglophone ``",XXX.NN"`` cents rendering, e.g. ``"45,000.00"``. This
      is distinguished from a genuine thousands separator by trailing-group
      length: a real thousands group is always exactly 3 digits, so a final
      ``.``/``,``-delimited group of 1 or 2 digits is instead treated as a
      decimal remainder and truncated (money here is always integral IDR, so
      any fractional remainder carries no information).

    All remaining separators are stripped and the digits are parsed as an
    integer, preserving a leading ``-`` sign. Raises ``DatasetParsingError``
    for a non-string input or an empty/unparsable value.
    """
    if not isinstance(raw, str):
        raise DatasetParsingError(f"{context}: expected a string amount, got {type(raw).__name__}")
    stripped = raw.strip()
    if not stripped:
        raise DatasetParsingError(f"{context}: empty monetary value")
    body = _CORD_MONEY_PREFIX.sub("", stripped, count=1)
    negative = body.startswith("-")
    body = body[1:] if negative else body
    trailing = _CORD_TRAILING_GROUP.search(body)
    if trailing is not None and len(trailing.group(1)) != 3:
        body = body[: trailing.start()]
    digits = _CORD_SEPARATOR_NOISE.sub("", body)
    if not digits or not digits.isdigit():
        raise DatasetParsingError(f"{context}: cannot parse monetary value {raw!r}")
    value = int(digits)
    return -value if negative else value


#: Real CORD ``cnt`` values sometimes carry a bare multiplier marker instead
#: of a plain number: a leading ``x``/``X`` (e.g. ``"x1"``, ``"x2"``) or a
#: trailing one (e.g. ``"1X"``). Exactly these two shapes are recognized; any
#: other placement (e.g. ``"1xx"``, ``"xx1"``) is left unmodified and falls
#: through to the existing float parse, which correctly rejects it as junk.
_CORD_QUANTITY_LEADING_MARKER = re.compile(r"^[xX](\d+(?:[.,]\d+)?)$")
_CORD_QUANTITY_TRAILING_MARKER = re.compile(r"^(\d+(?:[.,]\d+)?)[xX]$")


def parse_cord_quantity(raw: Any, *, context: str) -> float:
    """Parse a CORD menu quantity (``cnt``), which may be a decimal, into a float.

    Quantities are not thousands-separated: the raw string (or a
    comma-as-decimal-point variant of it) is parsed directly as a float.
    Real ground truth also renders some quantities with a bare multiplier
    marker instead of a plain number -- a leading ``x``/``X`` (``"x1"``) or a
    trailing one (``"1X"``) -- which is stripped before the numeric parse;
    any other placement of ``x``/``X`` is left unmodified and rejected as
    junk. Raises ``DatasetParsingError`` for a non-string input or an
    empty/unparsable value.
    """
    if not isinstance(raw, str):
        raise DatasetParsingError(
            f"{context}: expected a string quantity, got {type(raw).__name__}"
        )
    stripped = raw.strip()
    if not stripped:
        raise DatasetParsingError(f"{context}: empty quantity value")
    leading = _CORD_QUANTITY_LEADING_MARKER.fullmatch(stripped)
    trailing = _CORD_QUANTITY_TRAILING_MARKER.fullmatch(stripped)
    if leading is not None:
        stripped = leading.group(1)
    elif trailing is not None:
        stripped = trailing.group(1)
    for candidate in (stripped, stripped.replace(",", ".")):
        try:
            return float(candidate)
        except ValueError:
            continue
    raise DatasetParsingError(f"{context}: cannot parse quantity value {raw!r}")


__all__ = ["parse_cord_money", "parse_cord_quantity", "parse_sroie_amount"]
