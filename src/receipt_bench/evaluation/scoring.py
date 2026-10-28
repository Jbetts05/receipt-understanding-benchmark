from __future__ import annotations

from typing import Any

import jsonschema  # type: ignore[import-untyped]

from receipt_bench.evaluation.line_items import ItemMatch, match_line_items, match_payment_methods
from receipt_bench.evaluation.normalize import compare_values, numbers_close
from receipt_bench.evaluation.pointers import extra_field_name, flatten_receipt, pointer_to_field
from receipt_bench.models import (
    CanonicalReceipt,
    EvaluatedOutput,
    FieldScore,
    ReceiptSample,
    SolutionOutput,
)

_LIST_POINTER_PREFIXES = ("/line_items", "/payment_methods")
_IGNORED_POINTERS = frozenset({"/schema_version"})
_ARITHMETIC_TOLERANCE = 0.01


def _is_list_pointer(pointer: str) -> bool:
    return pointer.startswith(_LIST_POINTER_PREFIXES)


def validate_schema(receipt: CanonicalReceipt, schema: dict[str, Any]) -> bool:
    """Validate a parsed receipt's canonical JSON form against the JSON Schema."""
    payload = receipt.model_dump(mode="json")
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema, format_checker=jsonschema.FormatChecker())
    return bool(validator.is_valid(payload))


def _score_scalar_fields(
    expected: CanonicalReceipt, actual: CanonicalReceipt, labeled_fields: set[str]
) -> list[FieldScore]:
    expected_flat = flatten_receipt(expected)
    actual_flat = flatten_receipt(actual)
    scores: list[FieldScore] = []
    for pointer, expected_value in expected_flat.items():
        if pointer in _IGNORED_POINTERS or _is_list_pointer(pointer):
            continue
        if expected_value is None:
            continue
        field_name = pointer_to_field(pointer)
        actual_value = actual_flat.get(pointer)
        exact_match, similarity = compare_values(expected_value, actual_value)
        scores.append(
            FieldScore(
                field=field_name,
                labeled=field_name in labeled_fields,
                exact_match=exact_match,
                normalized_similarity=similarity,
                expected=expected_value,
                actual=actual_value,
            )
        )
    return scores


def _score_matches(
    matches: list[ItemMatch[Any]],
    field_prefix: str,
    list_field_name: str,
    labeled_fields: set[str],
) -> list[FieldScore]:
    scores: list[FieldScore] = []
    list_labeled = list_field_name in labeled_fields
    for match in matches:
        if match.expected is None:
            # An extra predicted entry with no ground-truth counterpart: there is no
            # expected value to score it against, so record it as a labeled false
            # positive rather than silently dropping it. This lets aggregation count
            # (and report the rate of) hallucinated repeated entities instead of
            # undercounting how often a solution over-predicts line items or payment
            # methods that were never in the receipt.
            actual_dump = match.actual.model_dump(mode="json") if match.actual is not None else None
            scores.append(
                FieldScore(
                    field=extra_field_name(list_field_name),
                    labeled=list_labeled,
                    exact_match=False,
                    normalized_similarity=0.0,
                    expected=None,
                    actual=actual_dump,
                )
            )
            continue
        expected_dump = match.expected.model_dump(mode="json")
        actual_dump = match.actual.model_dump(mode="json") if match.actual is not None else {}
        for key, expected_value in expected_dump.items():
            if expected_value is None:
                continue
            field_name = f"{field_prefix}.{key}"
            actual_value = actual_dump.get(key)
            exact_match, similarity = compare_values(expected_value, actual_value)
            labeled = field_name in labeled_fields or list_labeled
            scores.append(
                FieldScore(
                    field=field_name,
                    labeled=labeled,
                    exact_match=exact_match,
                    normalized_similarity=similarity,
                    expected=expected_value,
                    actual=actual_value,
                )
            )
    return scores


def _score_line_items(
    expected: CanonicalReceipt, actual: CanonicalReceipt, labeled_fields: set[str]
) -> list[FieldScore]:
    matches = match_line_items(expected.line_items or [], actual.line_items or [])
    return _score_matches(list(matches), "line_items", "line_items", labeled_fields)


def _score_payment_methods(
    expected: CanonicalReceipt, actual: CanonicalReceipt, labeled_fields: set[str]
) -> list[FieldScore]:
    matches = match_payment_methods(expected.payment_methods or [], actual.payment_methods or [])
    return _score_matches(list(matches), "payment_methods", "payment_methods", labeled_fields)


def check_arithmetic_consistency(receipt: CanonicalReceipt) -> bool | None:
    """Check whether a predicted receipt's own numbers are internally consistent.

    This never looks at ground truth: it only asks whether the extracted amounts and
    line items agree with each other, within a ``0.01`` absolute tolerance. Returns
    ``None`` when there is not enough data present to run any check.
    """
    amounts = receipt.amounts
    checks: list[bool] = []

    if (
        amounts.total is not None
        and amounts.subtotal is not None
        and amounts.tax is not None
        and amounts.tip is not None
        and amounts.discount is not None
    ):
        expected_total = amounts.subtotal - amounts.discount + amounts.tax + amounts.tip
        checks.append(numbers_close(expected_total, amounts.total, _ARITHMETIC_TOLERANCE))

    if receipt.line_items and amounts.subtotal is not None:
        totals = [item.total_price for item in receipt.line_items]
        if all(total is not None for total in totals):
            line_item_sum = sum(total for total in totals if total is not None)
            checks.append(numbers_close(line_item_sum, amounts.subtotal, _ARITHMETIC_TOLERANCE))

    if not checks:
        return None
    return all(checks)


def score_output(
    sample: ReceiptSample, output: SolutionOutput, schema: dict[str, Any]
) -> EvaluatedOutput:
    """Deterministically score one solution output against its ground-truth sample.

    No LLM or other judge is used: schema validity is checked with ``jsonschema``,
    fields are compared with rule-based normalization and tolerance, line items and
    payment methods are matched with a greedy (non-optimizing) assignment, and
    arithmetic consistency is checked against the prediction's own numbers.
    """
    if output.sample_id != sample.sample_id:
        raise ValueError(
            f"Output sample_id {output.sample_id!r} does not match sample {sample.sample_id!r}"
        )

    if output.status != "succeeded" or output.receipt is None:
        return EvaluatedOutput(
            output=output, schema_valid=False, fields=[], arithmetic_consistent=None
        )

    receipt = output.receipt
    labeled_fields = set(sample.labeled_fields)
    schema_valid = validate_schema(receipt, schema)
    fields = (
        _score_scalar_fields(sample.ground_truth, receipt, labeled_fields)
        + _score_line_items(sample.ground_truth, receipt, labeled_fields)
        + _score_payment_methods(sample.ground_truth, receipt, labeled_fields)
    )
    arithmetic_consistent = check_arithmetic_consistency(receipt)
    return EvaluatedOutput(
        output=output,
        schema_valid=schema_valid,
        fields=fields,
        arithmetic_consistent=arithmetic_consistent,
    )


__all__ = [
    "check_arithmetic_consistency",
    "score_output",
    "validate_schema",
]
