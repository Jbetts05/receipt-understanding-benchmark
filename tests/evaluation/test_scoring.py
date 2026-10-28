from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

import pytest

from receipt_bench.evaluation.scoring import (
    check_arithmetic_consistency,
    score_output,
    validate_schema,
)
from receipt_bench.models import CanonicalReceipt, ReceiptSample, SolutionOutput


def _output(
    sample: ReceiptSample,
    receipt: CanonicalReceipt | None,
    status: Literal["succeeded", "failed", "invalid"] = "succeeded",
) -> SolutionOutput:
    now = datetime.now(UTC)
    return SolutionOutput(
        run_id="run-1",
        sample_id=sample.sample_id,
        solution_id="solution-a",
        started_at=now,
        completed_at=now,
        status=status,
        receipt=receipt,
        raw_artifact="artifacts/run-1/raw/solution-a/sample.json",
    )


def test_validate_schema_accepts_ground_truth(
    ground_truth_receipt: CanonicalReceipt, receipt_schema: dict[str, Any]
) -> None:
    assert validate_schema(ground_truth_receipt, receipt_schema) is True


def test_validate_schema_enforces_date_and_time_formats(
    ground_truth_receipt: CanonicalReceipt, receipt_schema: dict[str, Any]
) -> None:
    invalid = ground_truth_receipt.model_copy(
        update={
            "transaction": ground_truth_receipt.transaction.model_copy(
                update={"date": "not-a-date", "time": "not-a-time"}
            )
        }
    )

    assert validate_schema(invalid, receipt_schema) is False


def test_score_output_raises_on_sample_id_mismatch(
    make_sample: Any, receipt_schema: dict[str, Any]
) -> None:
    sample = make_sample(sample_id="sample-001")
    other_sample = make_sample(sample_id="sample-002")
    output = _output(other_sample, other_sample.ground_truth)
    with pytest.raises(ValueError, match="does not match"):
        score_output(sample, output, receipt_schema)


def test_score_output_short_circuits_for_non_succeeded_status(
    make_sample: Any, receipt_schema: dict[str, Any]
) -> None:
    sample = make_sample(sample_id="sample-001")
    output = _output(sample, None, status="failed")
    evaluated = score_output(sample, output, receipt_schema)
    assert evaluated.schema_valid is False
    assert evaluated.fields == []
    assert evaluated.arithmetic_consistent is None


def test_score_output_perfect_prediction_is_fully_labeled_and_exact(
    make_sample: Any, receipt_schema: dict[str, Any]
) -> None:
    sample = make_sample(sample_id="sample-001")
    output = _output(sample, sample.ground_truth)
    evaluated = score_output(sample, output, receipt_schema)

    assert evaluated.schema_valid is True
    labeled_scores = [score for score in evaluated.fields if score.labeled]
    assert labeled_scores
    assert all(score.exact_match for score in labeled_scores)
    assert evaluated.arithmetic_consistent is True


def test_schema_format_violation_remains_scoreable(
    make_sample: Any, receipt_schema: dict[str, Any]
) -> None:
    sample = make_sample(sample_id="sample-001")
    prediction = sample.ground_truth.model_copy(
        update={
            "transaction": sample.ground_truth.transaction.model_copy(
                update={"currency": "RM"}
            )
        }
    )

    evaluated = score_output(sample, _output(sample, prediction), receipt_schema)

    assert evaluated.schema_valid is False
    labeled_scores = [score for score in evaluated.fields if score.labeled]
    assert labeled_scores
    assert all(score.exact_match for score in labeled_scores)


def test_score_output_uses_label_mask_for_labeled_flag(
    make_sample: Any, receipt_schema: dict[str, Any]
) -> None:
    sample = make_sample(sample_id="sample-001", labeled_fields=["merchant.name"])
    output = _output(sample, sample.ground_truth)
    evaluated = score_output(sample, output, receipt_schema)

    by_field = {score.field: score for score in evaluated.fields}
    assert by_field["merchant.name"].labeled is True
    assert by_field["transaction.date"].labeled is False


def test_score_output_detects_mismatched_field(
    make_sample: Any, receipt_schema: dict[str, Any]
) -> None:
    sample = make_sample(sample_id="sample-001", labeled_fields=["merchant.name"])
    wrong = sample.ground_truth.model_copy(
        update={"merchant": sample.ground_truth.merchant.model_copy(update={"name": "Wrong Name"})}
    )
    output = _output(sample, wrong)
    evaluated = score_output(sample, output, receipt_schema)

    by_field = {score.field: score for score in evaluated.fields}
    assert by_field["merchant.name"].exact_match is False
    assert 0.0 <= (by_field["merchant.name"].normalized_similarity or 0.0) < 1.0


def test_score_output_scores_line_items_by_matching(
    make_sample: Any, receipt_schema: dict[str, Any]
) -> None:
    sample = make_sample(sample_id="sample-001", labeled_fields=["line_items"])
    reordered = sample.ground_truth.model_copy(
        update={"line_items": list(reversed(sample.ground_truth.line_items or []))}
    )
    output = _output(sample, reordered)
    evaluated = score_output(sample, output, receipt_schema)

    line_item_scores = [
        score for score in evaluated.fields if score.field.startswith("line_items.")
    ]
    assert line_item_scores
    assert all(score.exact_match for score in line_item_scores)


def test_score_output_labels_extra_line_item_as_false_positive(
    make_sample: Any, receipt_schema: dict[str, Any]
) -> None:
    sample = make_sample(sample_id="sample-001", labeled_fields=["line_items"])
    extra_item = sample.ground_truth.line_items[0].model_copy(
        update={"description": "Muffin", "total_price": 3.5}
    )
    with_extra = sample.ground_truth.model_copy(
        update={"line_items": [*sample.ground_truth.line_items, extra_item]}
    )
    output = _output(sample, with_extra)
    evaluated = score_output(sample, output, receipt_schema)

    extra_scores = [score for score in evaluated.fields if score.field == "line_items.__extra__"]
    assert len(extra_scores) == 1
    extra_score = extra_scores[0]
    assert extra_score.labeled is True
    assert extra_score.exact_match is False
    assert extra_score.expected is None
    assert extra_score.actual is not None
    assert extra_score.actual["description"] == "Muffin"
    # The matched original line items are still scored as exact, unaffected by the extra.
    matched_scores = [
        score
        for score in evaluated.fields
        if score.field.startswith("line_items.") and score.field != "line_items.__extra__"
    ]
    assert matched_scores
    assert all(score.exact_match for score in matched_scores)


def test_score_output_extra_row_unlabeled_when_list_field_not_in_label_mask(
    make_sample: Any, receipt_schema: dict[str, Any]
) -> None:
    sample = make_sample(sample_id="sample-001", labeled_fields=["merchant.name"])
    extra_item = sample.ground_truth.line_items[0].model_copy(
        update={"description": "Muffin", "total_price": 3.5}
    )
    with_extra = sample.ground_truth.model_copy(
        update={"line_items": [*sample.ground_truth.line_items, extra_item]}
    )
    output = _output(sample, with_extra)
    evaluated = score_output(sample, output, receipt_schema)

    extra_scores = [score for score in evaluated.fields if score.field == "line_items.__extra__"]
    assert len(extra_scores) == 1
    assert extra_scores[0].labeled is False


def test_score_output_labels_extra_payment_method_as_false_positive(
    make_sample: Any, receipt_schema: dict[str, Any]
) -> None:
    sample = make_sample(sample_id="sample-001", labeled_fields=["payment_methods"])
    extra_payment = sample.ground_truth.payment_methods[0].model_copy(
        update={"method": "cash", "amount": 2.0}
    )
    with_extra = sample.ground_truth.model_copy(
        update={"payment_methods": [*sample.ground_truth.payment_methods, extra_payment]}
    )
    output = _output(sample, with_extra)
    evaluated = score_output(sample, output, receipt_schema)

    extra_scores = [
        score for score in evaluated.fields if score.field == "payment_methods.__extra__"
    ]
    assert len(extra_scores) == 1
    assert extra_scores[0].labeled is True
    assert extra_scores[0].exact_match is False
    assert extra_scores[0].actual["method"] == "cash"


def test_check_arithmetic_consistency_detects_inconsistent_totals(
    ground_truth_receipt: CanonicalReceipt,
) -> None:
    broken = ground_truth_receipt.model_copy(
        update={"amounts": ground_truth_receipt.amounts.model_copy(update={"total": 999.0})}
    )
    assert check_arithmetic_consistency(broken) is False


def test_check_arithmetic_consistency_true_for_consistent_receipt(
    ground_truth_receipt: CanonicalReceipt,
) -> None:
    assert check_arithmetic_consistency(ground_truth_receipt) is True


def test_check_arithmetic_consistency_none_when_insufficient_data() -> None:
    receipt = CanonicalReceipt()
    assert check_arithmetic_consistency(receipt) is None
