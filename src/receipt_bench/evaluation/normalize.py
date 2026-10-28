from __future__ import annotations

import re
from datetime import date, time
from typing import Any

from rapidfuzz import fuzz

NUMERIC_TOLERANCE = 0.01

_WHITESPACE = re.compile(r"\s+")


def normalize_string(value: str) -> str:
    """Casefold and collapse whitespace for tolerant string comparison."""
    return _WHITESPACE.sub(" ", value.strip()).casefold()


def string_similarity(expected: str, actual: str) -> float:
    """Token-order-insensitive similarity ratio in ``[0.0, 1.0]``."""
    if not expected and not actual:
        return 1.0
    if not expected or not actual:
        return 0.0
    return fuzz.token_sort_ratio(expected, actual) / 100.0


def numbers_close(expected: float, actual: float, tolerance: float = NUMERIC_TOLERANCE) -> bool:
    """Whether two numbers are equal within an absolute tolerance (default ``0.01``)."""
    return abs(expected - actual) <= tolerance


def _number_similarity(expected: float, actual: float) -> float:
    if numbers_close(expected, actual):
        return 1.0
    denominator = max(abs(expected), abs(actual), 1.0)
    return max(0.0, 1.0 - abs(expected - actual) / denominator)


def _to_comparable_string(value: Any) -> str:
    if isinstance(value, date | time):
        return value.isoformat()
    return str(value)


def compare_values(expected: Any, actual: Any) -> tuple[bool, float]:
    """Compare a ground-truth leaf value against a predicted leaf value.

    Returns ``(exact_match, normalized_similarity)`` where similarity is in
    ``[0.0, 1.0]``. Dispatch is based on the runtime type of ``expected``:

    * ``None`` predictions never match (``actual`` presence is required).
    * ``bool`` values compare by equality.
    * Numeric values (``int``/``float``) use a ``0.01`` absolute tolerance for exact
      match and a distance-scaled similarity otherwise. A numeric/non-numeric type
      mismatch is always a non-match.
    * Everything else (strings, ISO dates/times, currency codes, enums) is normalized
      (casefolded, whitespace-collapsed) and compared as text, with a fuzzy similarity
      ratio when not exactly equal.
    """
    if actual is None:
        return False, 0.0
    if isinstance(expected, bool) or isinstance(actual, bool):
        exact = expected == actual
        return exact, 1.0 if exact else 0.0
    expected_is_numeric = isinstance(expected, int | float)
    actual_is_numeric = isinstance(actual, int | float)
    if expected_is_numeric or actual_is_numeric:
        if not (expected_is_numeric and actual_is_numeric):
            return False, 0.0
        exact = numbers_close(float(expected), float(actual))
        return exact, 1.0 if exact else _number_similarity(float(expected), float(actual))

    expected_str = normalize_string(_to_comparable_string(expected))
    actual_str = normalize_string(_to_comparable_string(actual))
    if expected_str == actual_str:
        return True, 1.0
    return False, string_similarity(expected_str, actual_str)
