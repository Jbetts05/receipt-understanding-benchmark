from __future__ import annotations

from receipt_bench.evaluation.line_items import match_line_items, match_payment_methods
from receipt_bench.models import LineItem, PaymentMethod


def _item(description: str, total_price: float) -> LineItem:
    return LineItem(description=description, total_price=total_price)


def test_match_line_items_pairs_by_description_and_total() -> None:
    expected = [_item("Coffee", 6.0), _item("Bagel", 4.0)]
    actual = [_item("Bagel", 4.0), _item("Coffee", 6.0)]

    matches = match_line_items(expected, actual)

    assert len(matches) == 2
    by_description = {match.expected.description: match for match in matches if match.expected}
    assert by_description["Coffee"].actual is not None
    assert by_description["Coffee"].actual.description == "Coffee"
    assert by_description["Bagel"].actual is not None
    assert by_description["Bagel"].actual.description == "Bagel"


def test_match_line_items_reports_missing_when_actual_is_shorter() -> None:
    expected = [_item("Coffee", 6.0), _item("Bagel", 4.0)]
    actual = [_item("Coffee", 6.0)]

    matches = match_line_items(expected, actual)

    missing = [match for match in matches if match.actual is None]
    assert len(missing) == 1
    assert missing[0].expected is not None
    assert missing[0].expected.description == "Bagel"


def test_match_line_items_reports_unmatched_extra_when_actual_is_longer() -> None:
    expected = [_item("Coffee", 6.0)]
    actual = [_item("Coffee", 6.0), _item("Muffin", 3.5)]

    matches = match_line_items(expected, actual)

    extras = [match for match in matches if match.expected is None]
    assert len(extras) == 1
    assert extras[0].actual is not None
    assert extras[0].actual.description == "Muffin"


def test_match_line_items_handles_empty_lists() -> None:
    assert match_line_items([], []) == []


def test_match_line_items_prefers_best_overall_pairing() -> None:
    # "Latte" is closer to "Late" than "Coffee" is, so the greedy match should not
    # accidentally pair "Coffee" with "Late" just because of list order.
    expected = [_item("Latte", 5.0), _item("Coffee", 6.0)]
    actual = [_item("Late", 5.0), _item("Coffee", 6.0)]

    matches = match_line_items(expected, actual)
    by_description = {match.expected.description: match for match in matches if match.expected}

    assert by_description["Latte"].actual is not None
    assert by_description["Latte"].actual.description == "Late"
    assert by_description["Coffee"].actual is not None
    assert by_description["Coffee"].actual.description == "Coffee"


def test_match_payment_methods_pairs_by_method_and_amount() -> None:
    expected = [PaymentMethod(method="visa", amount=12.0)]
    actual = [PaymentMethod(method="visa", amount=12.0)]

    matches = match_payment_methods(expected, actual)

    assert len(matches) == 1
    assert matches[0].expected is not None
    assert matches[0].actual is not None
    assert matches[0].actual.method == "visa"
