from __future__ import annotations

import pytest

from receipt_bench.projects.evaluation import (
    aggregate_evaluations,
    aggregate_project,
    comparable_quality_summary,
    evaluate_answer,
    evaluate_json_text,
)
from receipt_bench.projects.models import (
    JsonObject,
    Project,
    ProjectSchema,
    canonical_sha256,
)


def _sroie_gold() -> dict[str, object]:
    return {
        "company": "EXAMPLE STORE SDN BHD",
        "address": "1 MAIN  ROAD",
        "date": "25/12/2018",
        "total": "1,234.50",
    }


def _cord_gold() -> dict[str, object]:
    return {
        "menu": [
            {"nm": "COFFEE", "cnt": "1X", "price": "10.000"},
            {"nm": "COFFEE", "cnt": "2", "price": "20,000.00"},
        ],
        "sub_total": {"subtotal_price": "30.000"},
        "total": {"total_price": "30,000", "cashprice": "50.000"},
    }


def test_sroie_project_normalization_matches_case_date_and_numeric_formats(
    sroie_project: Project,
) -> None:
    result = evaluate_answer(
        project=sroie_project,
        sample_id="sroie-001",
        solution_id="gpt-5.4-mini",
        gold=_sroie_gold(),
        actual={
            "company": "example store sdn bhd",
            "address": " 1 main road ",
            "date": "2018-12-25",
            "total": "$1234.5",
        },
    )

    assert result.counts.matched == 4
    assert result.field_f1 == 1.0
    assert result.exact


def test_cord_menu_matching_is_order_insensitive_and_numeric_aware(
    cord_project: Project,
) -> None:
    result = evaluate_answer(
        project=cord_project,
        sample_id="cord-003",
        solution_id="gpt-5.4-mini",
        gold=_cord_gold(),
        actual={
            "menu": [
                {"nm": "coffee", "cnt": "2X", "price": 20000},
                {"nm": "COFFEE", "cnt": "1", "price": "Rp 10.000"},
            ],
            "sub_total": {"subtotal_price": "30,000.00"},
            "total": {"total_price": 30000, "cashprice": "50,000"},
        },
    )

    assert result.counts.matched == 9
    assert result.counts.wrong == 0
    assert result.counts.missing == 0
    assert result.counts.extra == 0
    assert result.exact
    assert not result.schema_valid


def test_cord_menu_matching_remains_optimal_with_sixteen_predictions(
    cord_project: Project,
) -> None:
    fields = ("nm", "cnt", "price", "unitprice", "num")

    def item(values: str) -> dict[str, object]:
        return dict(zip(fields, values, strict=True))

    gold_menu = [item("00000"), item("00111")]
    actual_menu = [item("00001"), item("11000")]
    for index in range(14):
        unique_values = chr(ord("a") + index) * len(fields)
        gold_menu.append(item(unique_values))
        actual_menu.append(item(unique_values))

    result = evaluate_answer(
        project=cord_project,
        sample_id="cord-003",
        solution_id="gpt-5.4-mini",
        gold={"menu": gold_menu},
        actual={"menu": actual_menu},
    )

    assert result.counts.matched == 76
    assert result.counts.wrong == 4
    assert result.counts.missing == 0
    assert result.counts.extra == 0


@pytest.mark.parametrize(
    ("field_name", "empty_value"),
    [("sub_total", {}), ("menu", [])],
)
def test_omitted_empty_expected_container_is_missing(
    cord_project: Project,
    field_name: str,
    empty_value: dict[str, object] | list[object],
) -> None:
    result = evaluate_answer(
        project=cord_project,
        sample_id="cord-003",
        solution_id="gpt-5.4-mini",
        gold={
            field_name: empty_value,
            "total": {"total_price": "5.000"},
        },
        actual={"total": {"total_price": "5,000"}},
    )

    missing = [field for field in result.fields if field.status == "missing"]
    assert result.counts.matched == 1
    assert result.counts.missing == 1
    assert [(field.path, field.expected) for field in missing] == [
        (f"$.{field_name}", empty_value)
    ]
    assert not result.exact


def test_equal_empty_root_object_is_exact_and_aggregates(
    cord_project: Project,
) -> None:
    schema_document: JsonObject = {
        "type": "object",
        "additionalProperties": False,
    }
    project_schema = ProjectSchema(
        schema_id=cord_project.project_schema.schema_id,
        version=cord_project.project_schema.version,
        content_hash=canonical_sha256(schema_document),
        source=cord_project.project_schema.source,
        document=schema_document,
    )
    project = cord_project.model_copy(update={"project_schema": project_schema})

    result = evaluate_answer(
        project=project,
        sample_id="cord-003",
        solution_id="gpt-5.4-mini",
        gold={},
        actual={},
    )

    assert result.schema_valid
    comparisons = [
        (field.path, field.status, field.expected, field.actual)
        for field in result.fields
    ]
    assert comparisons == [("$", "matched", {}, {})]
    assert result.counts.matched == 1
    assert result.field_precision == 1.0
    assert result.field_recall == 1.0
    assert result.field_f1 == 1.0
    assert result.exact

    aggregate = aggregate_evaluations([result])
    assert aggregate.matched == 1
    assert aggregate.field_precision == 1.0
    assert aggregate.field_recall == 1.0
    assert aggregate.field_f1 == 1.0
    assert aggregate.exact_answers == 1
    assert aggregate.exact_rate == 1.0
    assert aggregate.schema_valid == 1
    assert aggregate.schema_valid_rate == 1.0


def test_equal_nested_empty_containers_are_matched_and_schema_valid(
    cord_project: Project,
) -> None:
    answer = {
        "menu": [],
        "sub_total": {},
        "total": {"total_price": "5.000"},
    }

    result = evaluate_answer(
        project=cord_project,
        sample_id="cord-003",
        solution_id="gpt-5.4-mini",
        gold=answer,
        actual=answer,
    )

    assert result.schema_valid
    assert result.counts.matched == 3
    assert {
        (field.path, field.status)
        for field in result.fields
    } == {
        ("$.menu", "matched"),
        ("$.sub_total", "matched"),
        ("$.total.total_price", "matched"),
    }
    assert result.field_f1 == 1.0
    assert result.exact


def test_gold_complete_objects_count_all_actual_only_fields(
    cord_project: Project,
) -> None:
    gold = {
        "menu": [{"nm": "TEA", "price": "5.000"}],
        "total": {"total_price": "5.000"},
    }
    result = evaluate_answer(
        project=cord_project,
        sample_id="cord-003",
        solution_id="gpt-5.4-mini",
        gold=gold,
        actual={
            "menu": [
                {"nm": "TEA", "price": "5,000"},
                {"nm": "CAKE", "price": "7.000"},
            ],
            "sub_total": {"tax_price": "0"},
            "total": {"total_price": "5,000", "cashprice": "5.000"},
        },
    )

    assert result.counts.matched == 3
    assert result.counts.extra == 4
    assert result.counts.wrong == 0
    assert result.counts.missing == 0
    assert not result.exact
    assert {field.path for field in result.fields if field.status == "extra"} == {
        "$.menu[+1].nm",
        "$.menu[+1].price",
        "$.sub_total.tax_price",
        "$.total.cashprice",
    }
    assert result.field_precision == pytest.approx(3 / 7)
    assert result.field_recall == 1.0


def test_extra_fields_are_recursively_counted_at_every_cord_object_level(
    cord_project: Project,
) -> None:
    result = evaluate_answer(
        project=cord_project,
        sample_id="cord-003",
        solution_id="gpt-5.4-mini",
        gold={
            "menu": [{"nm": "TEA", "sub": {"nm": "ICE"}}],
            "total": {"total_price": "5.000"},
        },
        actual={
            "menu": [
                {
                    "nm": "TEA",
                    "cnt": "1",
                    "sub": {"nm": "ICE", "price": "0"},
                }
            ],
            "sub_total": {
                "subtotal_price": "5.000",
                "etc": ["5.000", "0"],
            },
            "total": {
                "total_price": "5.000",
                "cashprice": ["10.000", "10.000"],
                "changeprice": "5.000",
            },
        },
    )

    assert result.counts.matched == 3
    assert result.counts.extra == 8
    assert {field.path for field in result.fields if field.status == "extra"} == {
        "$.menu[0].cnt",
        "$.menu[0].sub.price",
        "$.sub_total.subtotal_price",
        "$.sub_total.etc[0]",
        "$.sub_total.etc[1]",
        "$.total.cashprice[0]",
        "$.total.cashprice[1]",
        "$.total.changeprice",
    }
    assert not result.exact
    assert result.schema_valid


def test_unknown_nested_root_fields_are_recursively_counted(
    sroie_project: Project,
) -> None:
    result = evaluate_answer(
        project=sroie_project,
        sample_id="sroie-001",
        solution_id="gpt-5.4-mini",
        gold=_sroie_gold(),
        actual={
            **_sroie_gold(),
            "debug": {
                "confidence": 0.9,
                "spans": [],
            },
        },
    )

    assert result.counts.matched == 4
    assert result.counts.extra == 2
    assert {field.path for field in result.fields if field.status == "extra"} == {
        "$.debug.confidence",
        "$.debug.spans",
    }
    assert not result.exact
    assert not result.schema_valid


@pytest.mark.parametrize("answer_status", ["failed", "unparseable"])
def test_failed_or_unparseable_answer_is_zero(
    sroie_project: Project, answer_status: str
) -> None:
    result = evaluate_answer(
        project=sroie_project,
        sample_id="sroie-001",
        solution_id="gpt-5.4-mini",
        gold=_sroie_gold(),
        actual=None,
        answer_status=answer_status,  # type: ignore[arg-type]
    )

    assert result.counts.missing == 4
    assert result.field_precision == 0.0
    assert result.field_recall == 0.0
    assert result.field_f1 == 0.0
    assert not result.exact


def test_invalid_json_is_unparseable_and_zero(sroie_project: Project) -> None:
    result = evaluate_json_text(
        project=sroie_project,
        sample_id="sroie-001",
        solution_id="gpt-5.4-mini",
        gold=_sroie_gold(),
        raw_answer="not json",
    )
    assert result.answer_status == "unparseable"
    assert result.field_f1 == 0.0


def test_malformed_cord_menu_does_not_erase_correct_total(
    cord_project: Project,
) -> None:
    result = evaluate_answer(
        project=cord_project,
        sample_id="cord-003",
        solution_id="gpt-5.4-mini",
        gold={
            "menu": [{"nm": "TEA"}],
            "total": {"total_price": "5.000"},
        },
        actual={
            "menu": "not-an-array",
            "total": {"total_price": "5,000"},
        },
    )

    assert result.answer_status == "succeeded"
    assert not result.schema_valid
    assert result.counts.matched == 1
    assert result.counts.missing == 1
    assert result.field_f1 > 0.0
    assert not result.exact


def test_aggregate_denominator_includes_terminal_failures(
    sroie_project: Project,
) -> None:
    exact = evaluate_answer(
        project=sroie_project,
        sample_id="sroie-001",
        solution_id="gpt-5.4-mini",
        gold=_sroie_gold(),
        actual=_sroie_gold(),
    )
    failed = evaluate_answer(
        project=sroie_project,
        sample_id="sroie-003",
        solution_id="gpt-5.4-mini",
        gold=_sroie_gold(),
        actual=None,
        answer_status="failed",
    )

    aggregate = aggregate_evaluations(
        [exact, failed], expected_sample_ids=["sroie-001", "sroie-003"]
    )
    assert aggregate.sample_count == 2
    assert aggregate.exact_rate == 0.5
    assert aggregate.field_precision == 1.0
    assert aggregate.field_recall == 0.5
    assert aggregate.field_f1 == pytest.approx(2 / 3)
    with pytest.raises(ValueError, match="denominator"):
        aggregate_evaluations(
            [exact, failed], expected_sample_ids=["sroie-001", "sroie-004"]
        )


def test_aggregate_rejects_non_scored_samples_and_cross_project_mixing(
    sroie_project: Project, cord_project: Project
) -> None:
    for sample_id in ("sroie-012", "sroie-015"):
        non_scored = evaluate_answer(
            project=sroie_project,
            sample_id=sample_id,
            solution_id="gpt-5.4-mini",
            gold=_sroie_gold(),
            actual=_sroie_gold(),
        )
        with pytest.raises(ValueError, match="Non-scored"):
            aggregate_evaluations([non_scored])

    sroie = evaluate_answer(
        project=sroie_project,
        sample_id="sroie-001",
        solution_id="gpt-5.4-mini",
        gold=_sroie_gold(),
        actual=_sroie_gold(),
    )
    cord = evaluate_answer(
        project=cord_project,
        sample_id="cord-003",
        solution_id="gpt-5.4-mini",
        gold=_cord_gold(),
        actual=_cord_gold(),
    )
    with pytest.raises(ValueError, match="never be combined"):
        aggregate_project([sroie, cord])


def test_cord_native_prebuilt_remains_in_comparable_summary(
    cord_project: Project,
) -> None:
    result = evaluate_answer(
        project=cord_project,
        sample_id="cord-003",
        solution_id="document-intelligence-receipt",
        gold=_cord_gold(),
        actual=_cord_gold(),
    )
    aggregate = aggregate_evaluations([result])

    assert aggregate.compatibility == "full"
    assert aggregate.comparable_quality
    assert comparable_quality_summary([aggregate]) == (aggregate,)
