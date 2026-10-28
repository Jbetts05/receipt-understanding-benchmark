from __future__ import annotations

import pytest

from receipt_bench.projects.ocr_evaluation import evaluate_ocr_text


def test_sroie_ocr_metrics_include_error_rates_and_token_recall() -> None:
    result = evaluate_ocr_text(
        project_id="sroie",
        reference="Store Name\nTOTAL 12.00",
        hypothesis="store name total 12.0",
    )

    assert result.reference_kind == "full_page"
    assert result.reference_token_recall == pytest.approx(0.75)
    assert result.character_error_rate is not None
    assert result.word_error_rate == pytest.approx(0.25)
    assert result.reference_words == 4


def test_cord_uses_subset_recall_without_misleading_full_page_error_rates() -> None:
    result = evaluate_ocr_text(
        project_id="cord-v2",
        reference="NASI 10.000 TOTAL 10.000",
        hypothesis="TOKO HEADER NASI 10.000 TOTAL 10.000 FOOTER",
    )

    assert result.reference_kind == "labeled_subset"
    assert result.reference_token_recall == 1.0
    assert result.character_error_rate is None
    assert result.word_error_rate is None


def test_imported_custom_transcripts_are_full_page_ocr_references() -> None:
    result = evaluate_ocr_text(
        project_id="custom-receipts",
        reference="CONTOSO CAFE\nTOTAL 12.50",
        hypothesis="CONTOSO CAFE TOTAL 12.50",
    )

    assert result.reference_kind == "full_page"
    assert result.reference_token_recall == 1.0
    assert result.character_error_rate == 0.0
    assert result.word_error_rate == 0.0


def test_token_recall_respects_duplicate_reference_tokens() -> None:
    result = evaluate_ocr_text(
        project_id="cord-v2",
        reference="ITEM ITEM TOTAL",
        hypothesis="ITEM TOTAL",
    )

    assert result.matched_reference_tokens == 2
    assert result.reference_tokens == 3
    assert result.reference_token_recall == pytest.approx(2 / 3)


def test_empty_reference_is_rejected() -> None:
    with pytest.raises(ValueError, match="no comparable tokens"):
        evaluate_ocr_text(project_id="sroie", reference="---", hypothesis="text")
