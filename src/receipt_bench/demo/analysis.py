from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Literal

from receipt_bench.demo.api_models import FieldComparison, GoldField
from receipt_bench.evaluation.scoring import score_output
from receipt_bench.models import ReceiptSample, SolutionOutput

_REFERENCE_TIME = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _field_label(field: str, occurrence: int) -> str:
    if field == "line_items.__extra__":
        return f"Extra line item {occurrence}"
    if field.startswith("line_items."):
        leaf = field.removeprefix("line_items.").replace("_", " ")
        return f"Line item {occurrence} · {leaf}"
    if field.startswith("payment_methods."):
        leaf = field.removeprefix("payment_methods.").replace("_", " ")
        return f"Payment {occurrence} · {leaf}"
    return field.replace(".", " · ").replace("_", " ")


def field_comparisons(
    sample: ReceiptSample,
    output: SolutionOutput,
    schema: dict[str, Any],
) -> list[FieldComparison]:
    evaluated = score_output(sample, output, schema)
    occurrences: defaultdict[str, int] = defaultdict(int)
    comparisons: list[FieldComparison] = []
    for score in evaluated.fields:
        if not score.labeled:
            continue
        occurrences[score.field] += 1
        occurrence = occurrences[score.field]
        key = (
            score.field
            if occurrence == 1 and "." not in score.field
            else f"{score.field}:{occurrence}"
        )
        state: Literal["matched", "missing", "mismatched", "extra"]
        if score.expected is None and score.actual is not None:
            state = "extra"
        elif score.actual is None:
            state = "missing"
        elif score.exact_match:
            state = "matched"
        else:
            state = "mismatched"
        comparisons.append(
            FieldComparison(
                key=key,
                field=score.field,
                label=_field_label(score.field, occurrence),
                state=state,
                expected=score.expected,
                actual=score.actual,
                similarity=score.normalized_similarity,
            )
        )
    return comparisons


def gold_fields(sample: ReceiptSample, schema: dict[str, Any]) -> list[GoldField]:
    output = SolutionOutput(
        run_id="gold-reference",
        sample_id=sample.sample_id,
        solution_id="gold-reference",
        started_at=_REFERENCE_TIME,
        completed_at=_REFERENCE_TIME,
        status="succeeded",
        receipt=sample.ground_truth,
        raw_artifact="",
    )
    return [
        GoldField(key=item.key, field=item.field, label=item.label, expected=item.expected)
        for item in field_comparisons(sample, output, schema)
        if item.state != "extra"
    ]


__all__ = ["field_comparisons", "gold_fields"]
