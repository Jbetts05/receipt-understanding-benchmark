from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from typing import Literal, cast

import jsonschema  # type: ignore[import-untyped]
from pydantic import Field, model_validator

from receipt_bench.projects.models import (
    CompatibilityLevel,
    ContractModel,
    JsonObject,
    JsonPrimitive,
    JsonValue,
    Project,
    ScoringRole,
)
from receipt_bench.projects.normalization import normalize_answer, normalize_scalar

ComparisonStatus = Literal["matched", "wrong", "missing", "extra"]
AnswerStatus = Literal["succeeded", "failed", "unparseable"]
_MISSING = object()


class FieldComparison(ContractModel):
    path: str = Field(min_length=1)
    status: ComparisonStatus
    expected_present: bool
    actual_present: bool
    expected: JsonValue = None
    actual: JsonValue = None
    normalized_expected: str | None = None
    normalized_actual: str | None = None


class ComparisonCounts(ContractModel):
    matched: int = Field(ge=0)
    wrong: int = Field(ge=0)
    missing: int = Field(ge=0)
    extra: int = Field(ge=0)

    @classmethod
    def from_fields(cls, fields: Sequence[FieldComparison]) -> ComparisonCounts:
        counts = {status: 0 for status in ("matched", "wrong", "missing", "extra")}
        for field in fields:
            counts[field.status] += 1
        return cls(**counts)

    @property
    def precision(self) -> float:
        denominator = self.matched + self.wrong + self.extra
        return self.matched / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.matched + self.wrong + self.missing
        return self.matched / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        denominator = self.precision + self.recall
        return 2 * self.precision * self.recall / denominator if denominator else 0.0


class ProjectEvaluation(ContractModel):
    project_id: str
    sample_id: str
    solution_id: str
    scoring_role: ScoringRole
    answer_status: AnswerStatus
    compatibility: CompatibilityLevel
    comparable_quality: bool
    schema_valid: bool
    fields: tuple[FieldComparison, ...]
    counts: ComparisonCounts
    field_precision: float = Field(ge=0.0, le=1.0)
    field_recall: float = Field(ge=0.0, le=1.0)
    field_f1: float = Field(ge=0.0, le=1.0)
    exact: bool

    @model_validator(mode="after")
    def validate_derived_metrics(self) -> ProjectEvaluation:
        calculated = ComparisonCounts.from_fields(self.fields)
        if self.counts != calculated:
            raise ValueError("Evaluation counts do not match field comparisons")
        if (
            self.field_precision != calculated.precision
            or self.field_recall != calculated.recall
            or self.field_f1 != calculated.f1
        ):
            raise ValueError("Evaluation metrics do not match comparison counts")
        expected_exact = (
            self.answer_status == "succeeded"
            and bool(self.fields)
            and all(field.status == "matched" for field in self.fields)
        )
        if self.exact != expected_exact:
            raise ValueError("Whole-answer exactness does not match field comparisons")
        return self


class ProjectAggregate(ContractModel):
    project_id: str
    solution_id: str
    compatibility: CompatibilityLevel
    comparable_quality: bool
    sample_count: int = Field(ge=1)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    unparseable: int = Field(ge=0)
    schema_valid: int = Field(ge=0)
    matched: int = Field(ge=0)
    wrong: int = Field(ge=0)
    missing: int = Field(ge=0)
    extra: int = Field(ge=0)
    field_precision: float = Field(ge=0.0, le=1.0)
    field_recall: float = Field(ge=0.0, le=1.0)
    field_f1: float = Field(ge=0.0, le=1.0)
    exact_answers: int = Field(ge=0)
    exact_rate: float = Field(ge=0.0, le=1.0)
    schema_valid_rate: float = Field(ge=0.0, le=1.0)


def _child_path(path: str, key: str) -> str:
    return f"{path}.{key}" if path != "$" else f"$.{key}"


def _index_path(path: str, index: int | str) -> str:
    return f"{path}[{index}]"


def _as_primitive(value: JsonValue) -> JsonPrimitive | None:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return None


def _missing_fields(value: JsonValue, path: str) -> list[FieldComparison]:
    if isinstance(value, dict):
        fields = [
            field
            for key, child in value.items()
            for field in _missing_fields(child, _child_path(path, key))
        ]
        return fields or [
            FieldComparison(
                path=path,
                status="missing",
                expected_present=True,
                actual_present=False,
                expected=value,
            )
        ]
    if isinstance(value, list):
        fields = [
            field
            for index, child in enumerate(value)
            for field in _missing_fields(child, _index_path(path, index))
        ]
        return fields or [
            FieldComparison(
                path=path,
                status="missing",
                expected_present=True,
                actual_present=False,
                expected=value,
            )
        ]
    return [
        FieldComparison(
            path=path,
            status="missing",
            expected_present=True,
            actual_present=False,
            expected=value,
        )
    ]


def _extra_fields(value: JsonValue, path: str) -> list[FieldComparison]:
    if isinstance(value, dict):
        fields = [
            field
            for key, child in value.items()
            for field in _extra_fields(child, _child_path(path, key))
        ]
        return fields or [
            FieldComparison(
                path=path,
                status="extra",
                expected_present=False,
                actual_present=True,
                actual=value,
            )
        ]
    if isinstance(value, list):
        fields = [
            field
            for index, child in enumerate(value)
            for field in _extra_fields(child, _index_path(path, index))
        ]
        return fields or [
            FieldComparison(
                path=path,
                status="extra",
                expected_present=False,
                actual_present=True,
                actual=value,
            )
        ]
    return [
        FieldComparison(
            path=path,
            status="extra",
            expected_present=False,
            actual_present=True,
            actual=value,
        )
    ]


def _matched_empty_container(value: JsonValue, path: str) -> list[FieldComparison]:
    return [
        FieldComparison(
            path=path,
            status="matched",
            expected_present=True,
            actual_present=True,
            expected=value,
            actual=value,
        )
    ]


def _compare_scalar(
    project: Project,
    expected: JsonPrimitive,
    actual: object,
    path: str,
) -> FieldComparison:
    if actual is _MISSING:
        return FieldComparison(
            path=path,
            status="missing",
            expected_present=True,
            actual_present=False,
            expected=expected,
        )
    if isinstance(actual, dict | list):
        return FieldComparison(
            path=path,
            status="wrong",
            expected_present=True,
            actual_present=True,
            expected=expected,
            actual=cast(JsonValue, actual),
            normalized_expected=normalize_scalar(
                project.normalization_profile, path, expected
            ),
        )

    actual_primitive = cast(JsonPrimitive, actual)
    normalized_expected = normalize_scalar(project.normalization_profile, path, expected)
    normalized_actual = normalize_scalar(
        project.normalization_profile, path, actual_primitive
    )
    matched = normalized_expected == normalized_actual
    return FieldComparison(
        path=path,
        status="matched" if matched else "wrong",
        expected_present=True,
        actual_present=True,
        expected=expected,
        actual=actual_primitive,
        normalized_expected=normalized_expected,
        normalized_actual=normalized_actual,
    )


def _pair_weight(project: Project, expected: JsonValue, actual: JsonValue) -> int:
    fields = _compare_nodes(project, expected, actual, "$.menu[]", menu_matching=False)
    matched = sum(field.status == "matched" for field in fields)
    similarity = 0.0
    for field in fields:
        if field.normalized_expected is not None and field.normalized_actual is not None:
            similarity += SequenceMatcher(
                None, field.normalized_expected, field.normalized_actual
            ).ratio()
    return matched * 1_000_000_000 + int(similarity * 1_000_000) + 1


def _maximum_weight_assignment(weights: Sequence[Sequence[int]]) -> tuple[int, ...]:
    """Return a maximum-weight column for each row using the Hungarian algorithm."""

    row_count = len(weights)
    if not row_count:
        return ()
    column_count = len(weights[0])
    if column_count < row_count or any(
        len(row_weights) != column_count for row_weights in weights
    ):
        raise ValueError("Assignment weights must be rectangular with rows <= columns")

    row_potentials = [0] * (row_count + 1)
    column_potentials = [0] * (column_count + 1)
    matched_row = [0] * (column_count + 1)
    previous_column = [0] * (column_count + 1)

    for row in range(1, row_count + 1):
        matched_row[0] = row
        minimum_cost: list[int | None] = [None] * (column_count + 1)
        visited = [False] * (column_count + 1)
        column = 0

        while True:
            visited[column] = True
            active_row = matched_row[column]
            delta: int | None = None
            next_column = 0

            for candidate_column in range(1, column_count + 1):
                if visited[candidate_column]:
                    continue
                reduced_cost = (
                    -weights[active_row - 1][candidate_column - 1]
                    - row_potentials[active_row]
                    - column_potentials[candidate_column]
                )
                candidate_cost = minimum_cost[candidate_column]
                if candidate_cost is None or reduced_cost < candidate_cost:
                    candidate_cost = reduced_cost
                    minimum_cost[candidate_column] = candidate_cost
                    previous_column[candidate_column] = column
                if delta is None or candidate_cost < delta:
                    delta = candidate_cost
                    next_column = candidate_column

            if delta is None:
                raise RuntimeError("Assignment has no augmenting path")
            for candidate_column in range(column_count + 1):
                if visited[candidate_column]:
                    row_potentials[matched_row[candidate_column]] += delta
                    column_potentials[candidate_column] -= delta
                elif candidate_column:
                    candidate_cost = minimum_cost[candidate_column]
                    if candidate_cost is None:
                        raise RuntimeError("Assignment has no candidate cost")
                    minimum_cost[candidate_column] = candidate_cost - delta

            column = next_column
            if matched_row[column] == 0:
                break

        while column:
            predecessor = previous_column[column]
            matched_row[column] = matched_row[predecessor]
            column = predecessor

    assignment = [0] * row_count
    for column in range(1, column_count + 1):
        if matched_row[column]:
            assignment[matched_row[column] - 1] = column - 1
    return tuple(assignment)


def _optimal_menu_assignment(
    project: Project,
    expected: Sequence[JsonValue],
    actual: Sequence[JsonValue],
) -> tuple[int | None, ...]:
    if not expected:
        return ()
    if not actual:
        return tuple(None for _ in expected)

    weights = [
        [_pair_weight(project, expected_item, actual_item) for actual_item in actual]
        for expected_item in expected
    ]
    tie_base = len(actual) + 1
    primary_scale = tie_base ** len(expected)
    place_value = primary_scale // tie_base
    encoded_weights: list[list[int]] = []
    for row_weights in weights:
        encoded_weights.append(
            [
                weight * primary_scale - (actual_index + 1) * place_value
                for actual_index, weight in enumerate(row_weights)
            ]
            + [0] * len(expected)
        )
        place_value //= tie_base

    # The positional penalty retains the previous preference for None and then
    # lower actual indexes at the first expected item where optimal ties differ.
    assignment = _maximum_weight_assignment(encoded_weights)
    return tuple(
        actual_index if actual_index < len(actual) else None
        for actual_index in assignment
    )


def _compare_menu(
    project: Project,
    expected: Sequence[JsonValue],
    actual: Sequence[JsonValue],
    path: str,
) -> list[FieldComparison]:
    assignment = _optimal_menu_assignment(project, expected, actual)
    fields: list[FieldComparison] = []
    used: set[int] = set()
    for expected_index, actual_index in enumerate(assignment):
        expected_item = expected[expected_index]
        item_path = _index_path(path, expected_index)
        if actual_index is None:
            fields.extend(_missing_fields(expected_item, item_path))
            continue
        used.add(actual_index)
        fields.extend(
            _compare_nodes(
                project,
                expected_item,
                actual[actual_index],
                item_path,
                menu_matching=False,
            )
        )
    for actual_index, actual_item in enumerate(actual):
        if actual_index not in used:
            fields.extend(_extra_fields(actual_item, _index_path(path, f"+{actual_index}")))
    return fields


def _compare_nodes(
    project: Project,
    expected: JsonValue,
    actual: object,
    path: str,
    *,
    menu_matching: bool = True,
) -> list[FieldComparison]:
    if isinstance(expected, dict):
        if not isinstance(actual, Mapping):
            return _missing_fields(expected, path)
        if not expected and not actual:
            return _matched_empty_container(expected, path)
        object_fields = [
            field
            for key, expected_child in expected.items()
            for field in _compare_nodes(
                project,
                expected_child,
                actual.get(key, _MISSING),
                _child_path(path, key),
                menu_matching=menu_matching,
            )
        ]
        for key, actual_child in actual.items():
            if key not in expected:
                object_fields.extend(
                    _extra_fields(cast(JsonValue, actual_child), _child_path(path, key))
                )
        return object_fields

    if isinstance(expected, list):
        if not isinstance(actual, list):
            return _missing_fields(expected, path)
        actual_items = cast(list[JsonValue], actual)
        if not expected and not actual_items:
            return _matched_empty_container(expected, path)
        if menu_matching and project.normalization_profile == "cord-v2-v1" and path == "$.menu":
            return _compare_menu(project, expected, actual_items, path)
        fields: list[FieldComparison] = []
        for index, expected_child in enumerate(expected):
            if index >= len(actual_items):
                fields.extend(_missing_fields(expected_child, _index_path(path, index)))
            else:
                fields.extend(
                    _compare_nodes(
                        project,
                        expected_child,
                        actual_items[index],
                        _index_path(path, index),
                        menu_matching=menu_matching,
                    )
                )
        for index in range(len(expected), len(actual_items)):
            fields.extend(_extra_fields(actual_items[index], _index_path(path, f"+{index}")))
        return fields

    primitive = _as_primitive(expected)
    if primitive is None and expected is not None:
        raise TypeError(f"Expected value at {path} is not a JSON scalar")
    return [_compare_scalar(project, primitive, actual, path)]


def _schema_valid(project: Project, answer: JsonObject) -> bool:
    schema = project.project_schema.document
    validator_class = jsonschema.validators.validator_for(schema)
    validator_class.check_schema(schema)
    return bool(
        validator_class(
            schema,
            format_checker=jsonschema.FormatChecker(),
        ).is_valid(answer)
    )


def evaluate_answer(
    *,
    project: Project,
    sample_id: str,
    solution_id: str,
    gold: object,
    actual: object | None,
    answer_status: AnswerStatus = "succeeded",
) -> ProjectEvaluation:
    """Compare a project-native answer against the complete gold object."""

    sample = project.sample(sample_id)
    compatibility = project.solution_compatibility(solution_id)
    normalized_gold = normalize_answer(project.normalization_profile, gold)

    if answer_status == "succeeded":
        if actual is None:
            raise ValueError("A succeeded answer must include a JSON object")
        try:
            normalized_actual = normalize_answer(project.normalization_profile, actual)
        except (TypeError, ValueError):
            answer_status = "unparseable"
            normalized_actual = None
    else:
        normalized_actual = None

    if normalized_actual is None:
        fields = tuple(_missing_fields(normalized_gold, "$"))
        schema_valid = False
    else:
        fields = tuple(_compare_nodes(project, normalized_gold, normalized_actual, "$"))
        schema_valid = _schema_valid(project, normalized_actual)

    counts = ComparisonCounts.from_fields(fields)
    exact = (
        answer_status == "succeeded"
        and bool(fields)
        and all(field.status == "matched" for field in fields)
    )
    return ProjectEvaluation(
        project_id=project.project_id,
        sample_id=sample_id,
        solution_id=solution_id,
        scoring_role=sample.scoring_role,
        answer_status=answer_status,
        compatibility=compatibility.level,
        comparable_quality=compatibility.comparable_quality,
        schema_valid=schema_valid,
        fields=fields,
        counts=counts,
        field_precision=counts.precision,
        field_recall=counts.recall,
        field_f1=counts.f1,
        exact=exact,
    )


def evaluate_json_text(
    *,
    project: Project,
    sample_id: str,
    solution_id: str,
    gold: object,
    raw_answer: str,
) -> ProjectEvaluation:
    try:
        actual = json.loads(raw_answer)
    except (json.JSONDecodeError, TypeError):
        return evaluate_answer(
            project=project,
            sample_id=sample_id,
            solution_id=solution_id,
            gold=gold,
            actual=None,
            answer_status="unparseable",
        )
    return evaluate_answer(
        project=project,
        sample_id=sample_id,
        solution_id=solution_id,
        gold=gold,
        actual=actual,
    )


def aggregate_evaluations(
    evaluations: Sequence[ProjectEvaluation],
    *,
    expected_sample_ids: Sequence[str] | None = None,
) -> ProjectAggregate:
    if not evaluations:
        raise ValueError("Cannot aggregate an empty evaluation set")
    projects = {evaluation.project_id for evaluation in evaluations}
    solutions = {evaluation.solution_id for evaluation in evaluations}
    if len(projects) != 1:
        raise ValueError("SROIE, CORD, and custom project quality must never be combined")
    if len(solutions) != 1:
        raise ValueError("Aggregate one solution at a time")
    if any(evaluation.scoring_role != "scored" for evaluation in evaluations):
        raise ValueError("Non-scored samples are excluded from scored aggregates")

    sample_ids = [evaluation.sample_id for evaluation in evaluations]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("Duplicate sample result in aggregate")
    if expected_sample_ids is not None:
        expected = tuple(expected_sample_ids)
        if not expected or len(expected) != len(set(expected)):
            raise ValueError("Expected sample IDs must be non-empty and unique")
        if set(sample_ids) != set(expected):
            raise ValueError("Terminal results must exactly cover the aggregate denominator")

    compatibility = {evaluation.compatibility for evaluation in evaluations}
    comparable = {evaluation.comparable_quality for evaluation in evaluations}
    if len(compatibility) != 1 or len(comparable) != 1:
        raise ValueError("Compatibility must be stable within a solution aggregate")

    counts = ComparisonCounts.from_fields(
        [field for evaluation in evaluations for field in evaluation.fields]
    )
    total = len(evaluations)
    status_counts: defaultdict[str, int] = defaultdict(int)
    for evaluation in evaluations:
        status_counts[evaluation.answer_status] += 1
    exact_answers = sum(evaluation.exact for evaluation in evaluations)
    schema_valid = sum(evaluation.schema_valid for evaluation in evaluations)
    return ProjectAggregate(
        project_id=evaluations[0].project_id,
        solution_id=evaluations[0].solution_id,
        compatibility=next(iter(compatibility)),
        comparable_quality=next(iter(comparable)),
        sample_count=total,
        succeeded=status_counts["succeeded"],
        failed=status_counts["failed"],
        unparseable=status_counts["unparseable"],
        schema_valid=schema_valid,
        matched=counts.matched,
        wrong=counts.wrong,
        missing=counts.missing,
        extra=counts.extra,
        field_precision=counts.precision,
        field_recall=counts.recall,
        field_f1=counts.f1,
        exact_answers=exact_answers,
        exact_rate=exact_answers / total,
        schema_valid_rate=schema_valid / total,
    )


def aggregate_project(
    evaluations: Sequence[ProjectEvaluation],
    *,
    expected_sample_ids: Sequence[str] | None = None,
) -> tuple[ProjectAggregate, ...]:
    if not evaluations:
        raise ValueError("Cannot aggregate an empty evaluation set")
    if len({evaluation.project_id for evaluation in evaluations}) != 1:
        raise ValueError("SROIE, CORD, and custom project quality must never be combined")
    grouped: dict[str, list[ProjectEvaluation]] = defaultdict(list)
    for evaluation in evaluations:
        grouped[evaluation.solution_id].append(evaluation)
    return tuple(
        aggregate_evaluations(grouped[solution_id], expected_sample_ids=expected_sample_ids)
        for solution_id in sorted(grouped)
    )


def comparable_quality_summary(
    aggregates: Sequence[ProjectAggregate],
) -> tuple[ProjectAggregate, ...]:
    project_ids = {aggregate.project_id for aggregate in aggregates}
    if len(project_ids) > 1:
        raise ValueError("Comparable summaries are project-specific")
    return tuple(aggregate for aggregate in aggregates if aggregate.comparable_quality)


__all__ = [
    "AnswerStatus",
    "ComparisonCounts",
    "FieldComparison",
    "ProjectAggregate",
    "ProjectEvaluation",
    "aggregate_evaluations",
    "aggregate_project",
    "comparable_quality_summary",
    "evaluate_answer",
    "evaluate_json_text",
]
