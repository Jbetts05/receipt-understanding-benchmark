from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from receipt_bench.projects.models import ContractModel

_WHITESPACE = re.compile(r"\s+")
_TOKEN = re.compile(r"\w+(?:[.,:/-]\w+)*", re.UNICODE)
OCR_NORMALIZATION_ID = "receipt-ocr-text-v1"


class OcrTextEvaluation(ContractModel):
    metric_family: Literal["ocr_text"] = "ocr_text"
    normalization_id: str = OCR_NORMALIZATION_ID
    reference_kind: Literal["full_page", "labeled_subset"]
    reference_token_recall: float = Field(ge=0.0, le=1.0)
    matched_reference_tokens: int = Field(ge=0)
    reference_tokens: int = Field(ge=1)
    hypothesis_tokens: int = Field(ge=0)
    character_error_rate: float | None = Field(default=None, ge=0.0)
    character_edits: int | None = Field(default=None, ge=0)
    reference_characters: int | None = Field(default=None, ge=1)
    word_error_rate: float | None = Field(default=None, ge=0.0)
    word_edits: int | None = Field(default=None, ge=0)
    reference_words: int | None = Field(default=None, ge=1)


def normalize_ocr_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _WHITESPACE.sub(" ", normalized).strip()


def _tokens(value: str) -> list[str]:
    return _TOKEN.findall(normalize_ocr_text(value))


def _edit_distance[T](reference: Sequence[T], hypothesis: Sequence[T]) -> int:
    if not reference:
        return len(hypothesis)
    previous = list(range(len(hypothesis) + 1))
    for reference_index, expected in enumerate(reference, start=1):
        current = [reference_index]
        for hypothesis_index, actual in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[hypothesis_index] + 1,
                    previous[hypothesis_index - 1] + (expected != actual),
                )
            )
        previous = current
    return previous[-1]


def evaluate_ocr_text(
    *,
    project_id: str,
    reference: str,
    hypothesis: str,
) -> OcrTextEvaluation:
    reference_text = normalize_ocr_text(reference)
    hypothesis_text = normalize_ocr_text(hypothesis)
    reference_tokens = _tokens(reference_text)
    hypothesis_tokens = _tokens(hypothesis_text)
    if not reference_tokens:
        raise ValueError("OCR reference contains no comparable tokens")

    reference_counts = Counter(reference_tokens)
    hypothesis_counts = Counter(hypothesis_tokens)
    matched_tokens = sum(
        min(count, hypothesis_counts[token])
        for token, count in reference_counts.items()
    )
    reference_kind: Literal["full_page", "labeled_subset"] = (
        "labeled_subset" if project_id == "cord-v2" else "full_page"
    )
    if reference_kind == "labeled_subset":
        return OcrTextEvaluation(
            reference_kind=reference_kind,
            reference_token_recall=matched_tokens / len(reference_tokens),
            matched_reference_tokens=matched_tokens,
            reference_tokens=len(reference_tokens),
            hypothesis_tokens=len(hypothesis_tokens),
        )

    reference_characters = len(reference_text)
    if reference_characters == 0:
        raise ValueError("OCR reference contains no comparable characters")
    character_edits = _edit_distance(reference_text, hypothesis_text)
    word_edits = _edit_distance(reference_tokens, hypothesis_tokens)
    return OcrTextEvaluation(
        reference_kind=reference_kind,
        reference_token_recall=matched_tokens / len(reference_tokens),
        matched_reference_tokens=matched_tokens,
        reference_tokens=len(reference_tokens),
        hypothesis_tokens=len(hypothesis_tokens),
        character_error_rate=character_edits / reference_characters,
        character_edits=character_edits,
        reference_characters=reference_characters,
        word_error_rate=word_edits / len(reference_tokens),
        word_edits=word_edits,
        reference_words=len(reference_tokens),
    )


__all__ = [
    "OCR_NORMALIZATION_ID",
    "OcrTextEvaluation",
    "evaluate_ocr_text",
    "normalize_ocr_text",
]
