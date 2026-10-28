from __future__ import annotations

import pytest

from receipt_bench.evaluation.normalize import (
    compare_values,
    normalize_string,
    numbers_close,
    string_similarity,
)


def test_normalize_string_collapses_whitespace_and_casefolds() -> None:
    assert normalize_string("  Contoso   Cafe  ") == "contoso cafe"
    assert normalize_string("CONTOSO") == normalize_string("contoso")


def test_string_similarity_identical_strings_is_one() -> None:
    assert string_similarity("coffee", "coffee") == 1.0


def test_string_similarity_handles_empty_strings() -> None:
    assert string_similarity("", "") == 1.0
    assert string_similarity("coffee", "") == 0.0
    assert string_similarity("", "coffee") == 0.0


def test_string_similarity_is_token_order_insensitive() -> None:
    assert string_similarity("cafe contoso", "contoso cafe") == 1.0


@pytest.mark.parametrize(
    ("expected", "actual", "tolerance", "close"),
    [
        (10.0, 10.0, 0.01, True),
        (10.00, 10.009, 0.01, True),
        (10.00, 10.02, 0.01, False),
        (10.00, 9.99, 0.01, True),
        (10.00, 11.00, 0.01, False),
    ],
)
def test_numbers_close_uses_absolute_tolerance(
    expected: float, actual: float, tolerance: float, close: bool
) -> None:
    assert numbers_close(expected, actual, tolerance) is close


def test_numbers_close_default_tolerance_is_one_cent() -> None:
    assert numbers_close(10.00, 10.005) is True
    assert numbers_close(10.00, 10.02) is False


def test_compare_values_actual_none_never_matches() -> None:
    exact, similarity = compare_values("hello", None)
    assert exact is False
    assert similarity == 0.0


def test_compare_values_bool_exact_only() -> None:
    exact, similarity = compare_values(True, True)
    assert exact is True
    assert similarity == 1.0

    exact, similarity = compare_values(True, False)
    assert exact is False
    assert similarity == 0.0


def test_compare_values_numeric_within_tolerance() -> None:
    exact, similarity = compare_values(12.00, 12.005)
    assert exact is True
    assert similarity == 1.0


def test_compare_values_numeric_type_mismatch_is_no_match() -> None:
    exact, similarity = compare_values(12.00, "twelve")
    assert exact is False
    assert similarity == 0.0


def test_compare_values_numeric_far_apart_has_partial_similarity() -> None:
    exact, similarity = compare_values(100.0, 50.0)
    assert exact is False
    assert 0.0 < similarity < 1.0


def test_compare_values_string_normalizes_case_and_whitespace() -> None:
    exact, similarity = compare_values("Contoso Cafe", "  contoso   cafe ")
    assert exact is True
    assert similarity == 1.0


def test_compare_values_string_partial_similarity_when_not_equal() -> None:
    exact, similarity = compare_values("Contoso Cafe", "Contoso Bakery")
    assert exact is False
    assert 0.0 < similarity < 1.0


def test_compare_values_date_as_iso_strings() -> None:
    exact, similarity = compare_values("2026-08-28", "2026-08-28")
    assert exact is True
    assert similarity == 1.0
