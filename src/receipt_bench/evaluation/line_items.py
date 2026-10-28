from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

from receipt_bench.models import LineItem, PaymentMethod

_AMOUNT_TOLERANCE = 0.01
_LINE_ITEM_DESCRIPTION_WEIGHT = 0.7
_LINE_ITEM_TOTAL_WEIGHT = 0.3
_PAYMENT_METHOD_WEIGHT = 0.5
_PAYMENT_AMOUNT_WEIGHT = 0.5


@dataclass(frozen=True)
class ItemMatch[T]:
    """One pairing produced by greedy list matching.

    Either side may be ``None``: ``actual is None`` means a ground-truth entry with no
    matching prediction (missing), and ``expected is None`` means a predicted entry with
    no ground-truth counterpart (an unlabeled extra, not scored).
    """

    expected_index: int | None
    actual_index: int | None
    expected: T | None
    actual: T | None


def _greedy_match[T](
    expected_items: Sequence[T],
    actual_items: Sequence[T],
    score_fn: Callable[[T, T], float],
) -> list[ItemMatch[T]]:
    """Pair items from two lists by consuming the highest-scoring candidates first.

    This is a simple greedy assignment, not an optimal one (no Hungarian algorithm or
    other external optimization package is used): all ``len(expected) * len(actual)``
    candidate pairs are scored, sorted by descending score, and consumed in order,
    skipping any pair where either side has already been claimed.
    """
    candidates: list[tuple[float, int, int]] = []
    for e_index, expected in enumerate(expected_items):
        for a_index, actual in enumerate(actual_items):
            candidates.append((score_fn(expected, actual), e_index, a_index))
    candidates.sort(key=lambda candidate: (-candidate[0], candidate[1], candidate[2]))

    matched_expected: set[int] = set()
    matched_actual: set[int] = set()
    matches: list[ItemMatch[T]] = []
    for score, e_index, a_index in candidates:
        if score <= 0.0 or e_index in matched_expected or a_index in matched_actual:
            continue
        matched_expected.add(e_index)
        matched_actual.add(a_index)
        matches.append(
            ItemMatch(
                expected_index=e_index,
                actual_index=a_index,
                expected=expected_items[e_index],
                actual=actual_items[a_index],
            )
        )

    for e_index, expected in enumerate(expected_items):
        if e_index not in matched_expected:
            matches.append(
                ItemMatch(expected_index=e_index, actual_index=None, expected=expected, actual=None)
            )
    for a_index, actual in enumerate(actual_items):
        if a_index not in matched_actual:
            matches.append(
                ItemMatch(expected_index=None, actual_index=a_index, expected=None, actual=actual)
            )

    matches.sort(
        key=lambda match: (
            match.expected_index if match.expected_index is not None else 10**9,
            match.actual_index if match.actual_index is not None else 10**9,
        )
    )
    return matches


def _description_similarity(expected: str | None, actual: str | None) -> float:
    if not expected and not actual:
        return 1.0
    if not expected or not actual:
        return 0.0
    return fuzz.token_sort_ratio(expected, actual) / 100.0


def _amount_similarity(expected: float | None, actual: float | None) -> float:
    if expected is None and actual is None:
        return 1.0
    if expected is None or actual is None:
        return 0.0
    if abs(expected - actual) <= _AMOUNT_TOLERANCE:
        return 1.0
    denominator = max(abs(expected), abs(actual), 1.0)
    return max(0.0, 1.0 - abs(expected - actual) / denominator)


def _line_item_score(expected: LineItem, actual: LineItem) -> float:
    return (
        _LINE_ITEM_DESCRIPTION_WEIGHT
        * _description_similarity(expected.description, actual.description)
        + _LINE_ITEM_TOTAL_WEIGHT * _amount_similarity(expected.total_price, actual.total_price)
    )


def match_line_items(
    expected_items: Sequence[LineItem], actual_items: Sequence[LineItem]
) -> list[ItemMatch[LineItem]]:
    """Greedily pair ground-truth/predicted line items by description similarity and totals."""
    return _greedy_match(list(expected_items), list(actual_items), _line_item_score)


def _payment_method_score(expected: PaymentMethod, actual: PaymentMethod) -> float:
    return _PAYMENT_METHOD_WEIGHT * _description_similarity(
        expected.method, actual.method
    ) + _PAYMENT_AMOUNT_WEIGHT * _amount_similarity(expected.amount, actual.amount)


def match_payment_methods(
    expected_items: Sequence[PaymentMethod], actual_items: Sequence[PaymentMethod]
) -> list[ItemMatch[PaymentMethod]]:
    """Greedily pair ground-truth/predicted payment methods by method name and amount."""
    return _greedy_match(list(expected_items), list(actual_items), _payment_method_score)
