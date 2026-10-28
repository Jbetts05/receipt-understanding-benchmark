from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from dateutil import parser as date_parser  # type: ignore[import-untyped]

from receipt_bench.datasets.errors import DatasetParsingError

#: A small subset of the real pinned SROIE key JSON files (audited across all
#: 80 selected ids) render ``date`` as a bare 8-digit string with no
#: separators at all, e.g. ``"20211203"`` (invented example shape; SROIE
#: annotation values themselves are restricted, gated-license content and are
#: never reproduced here). This compact form has exactly one unambiguous
#: positional meaning (``YYYYMMDD``) and is parsed explicitly via
#: ``strptime`` rather than through ``dateutil``'s day-first heuristic: with
#: no separators to anchor the day/month grouping, ``dateutil`` with
#: ``dayfirst=True`` instead misreads the trailing four digits as ``DDMM``
#: (e.g. it would turn ``"20211203"`` -- 3 December 2021 -- into 12 March
#: 2021).
_COMPACT_ISO_DATE = re.compile(r"^\d{8}$")


def parse_day_first_date(raw: Any, *, context: str) -> date:
    """Parse a SROIE ``date`` string using a day-first convention.

    SROIE key JSON dates appear in a mix of formats, confirmed against the
    real pinned key JSON across all 80 selected ids (the format shapes below
    are invented examples; the actual SROIE annotation values are restricted,
    gated-license content and are never reproduced here): ``"25/12/2018"``,
    ``"12-06-19"``, ``"7/9/2019"`` (single-digit day/month), ``"09 JUN
    2019"`` / ``"30 JUN 19"`` (abbreviated month name), the ISO
    ``"2019-06-23"``, and the bare separator-less ``"20211203"``. Ambiguous
    separated numeric dates are interpreted day-first (``dateutil``'s
    ``dayfirst=True``), matching the SROIE receipts' predominantly DD/MM/YYYY
    convention; this also correctly resolves a same-batch outlier where the
    second numeric slot cannot be a month (e.g. ``"11/25/2020"``: 25 cannot
    be a month, so ``dateutil`` falls back to month-first for that one value
    on its own). The bare 8-digit form is special-cased and parsed explicitly
    as ``YYYYMMDD`` (see ``_COMPACT_ISO_DATE``) rather than through the
    day-first heuristic, since that heuristic misreads it (see the
    module-level note). Fuzzy parsing is disabled so garbage input fails
    explicitly rather than being silently guessed at. Raises
    ``DatasetParsingError`` for a non-string input or an empty/unparsable
    value.
    """
    if not isinstance(raw, str):
        raise DatasetParsingError(f"{context}: expected a string date, got {type(raw).__name__}")
    stripped = raw.strip()
    if not stripped:
        raise DatasetParsingError(f"{context}: empty date value")
    if _COMPACT_ISO_DATE.fullmatch(stripped):
        try:
            return datetime.strptime(stripped, "%Y%m%d").date()
        except ValueError as exc:
            raise DatasetParsingError(f"{context}: cannot parse date {raw!r}: {exc}") from exc
    try:
        parsed: datetime = date_parser.parse(stripped, dayfirst=True, fuzzy=False)
    except (ValueError, OverflowError) as exc:
        raise DatasetParsingError(f"{context}: cannot parse date {raw!r}: {exc}") from exc
    return parsed.date()


__all__ = ["parse_day_first_date"]
