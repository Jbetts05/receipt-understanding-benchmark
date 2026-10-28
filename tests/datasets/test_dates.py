from __future__ import annotations

from datetime import date

import pytest

from receipt_bench.datasets.dates import parse_day_first_date
from receipt_bench.datasets.errors import DatasetParsingError


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("25/12/2018", date(2018, 12, 25)),
        ("25-12-2018", date(2018, 12, 25)),
        ("2018-12-25", date(2018, 12, 25)),
        (" 03/04/2019 ", date(2019, 4, 3)),
        ("25 DEC 2018", date(2018, 12, 25)),
        # The real pinned SROIE key JSON files (audited across all 80
        # selected ids) also use these format shapes. The values below are
        # synthetic, invented representatives of each shape -- not actual
        # transcribed SROIE annotation dates, which are restricted,
        # gated-license content that must not be vendored into this
        # repository even in stripped-down form.
        ("12-06-19", date(2019, 6, 12)),  # DD-MM-YY
        ("7/9/2019", date(2019, 9, 7)),  # single-digit D/M/YYYY
        ("09 JUN 2019", date(2019, 6, 9)),  # DD MMM YYYY
        ("30 JUN 19", date(2019, 6, 30)),  # DD MMM YY
        ("2019-06-23", date(2019, 6, 23)),  # ISO YYYY-MM-DD
        # A month/day-ambiguous numeric date where the second slot cannot be
        # a month: dateutil already falls back to month-first for this value
        # on its own, so no special-casing is required.
        ("11/25/2020", date(2020, 11, 25)),
        # Bare unseparated YYYYMMDD: with dayfirst=True and no separators to
        # anchor grouping, dateutil misreads the trailing digits as
        # day-then-month ("2021-03-12"), which is wrong -- the only valid
        # positional reading of "20211203" is 2021-12-03. This compact form
        # is special-cased via strptime rather than routed through
        # dateutil's dayfirst heuristic; see parse_day_first_date.
        ("20211203", date(2021, 12, 3)),
    ],
)
def test_parse_day_first_date_valid(raw: str, expected: date) -> None:
    assert parse_day_first_date(raw, context="ctx") == expected


def test_parse_day_first_date_compact_iso_regression() -> None:
    """Regression test for a real production bug found in real SROIE data.

    A bare, unseparated 8-digit ``date`` string (``YYYYMMDD``, no
    separators) was previously misparsed via ``dateutil``'s ``dayfirst=True``
    heuristic, which reads the trailing four digits as day-then-month
    instead of the only valid positional reading. Before the fix,
    ``parse_day_first_date("20211203", ...)`` returned 2021-03-12 (wrong)
    instead of 2021-12-03 (correct). This uses an invented, synthetic date
    to demonstrate the shape of the bug -- not an actual transcribed SROIE
    annotation value.
    """
    assert parse_day_first_date("20211203", context="ctx") == date(2021, 12, 3)


@pytest.mark.parametrize("raw", ["", "   ", "not-a-date", None, 20181225, "32/13/2018"])
def test_parse_day_first_date_rejects_invalid(raw: object) -> None:
    with pytest.raises(DatasetParsingError):
        parse_day_first_date(raw, context="ctx")
