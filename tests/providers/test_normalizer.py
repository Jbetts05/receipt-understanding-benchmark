"""Tests for the shared text-to-schema normalizer."""

from __future__ import annotations

from typing import Any

import pytest

from receipt_bench.providers.base import InvalidSolutionOutput
from receipt_bench.providers.normalizer import StructuredOutputNormalizer
from tests.conftest import build_ground_truth_receipt
from tests.providers.conftest import FakeOpenAIClient, make_chat_completion


def test_normalize_returns_receipt_raw_and_measurement(receipt_schema: dict[str, Any]) -> None:
    receipt_json = build_ground_truth_receipt().model_dump_json()
    client = FakeOpenAIClient([make_chat_completion(receipt_json)])
    normalizer = StructuredOutputNormalizer(
        client=client,  # type: ignore[arg-type]
        deployment="receipt-gpt-54-mini",
        reasoning_effort="low",
    )

    receipt, raw_response, measurement = normalizer.normalize(
        "Contoso Cafe\nTotal $12.00", receipt_schema
    )

    assert receipt == build_ground_truth_receipt()
    assert measurement.component == "normalize"
    assert isinstance(raw_response, dict)

    call = client.completions.calls[0]
    assert call["model"] == "receipt-gpt-54-mini"
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["max_completion_tokens"] == 8192
    assert "max_tokens" not in call
    assert "OCR text begins" in call["messages"][1]["content"]


def test_normalize_rejects_empty_ocr_text(receipt_schema: dict[str, Any]) -> None:
    client = FakeOpenAIClient([])
    normalizer = StructuredOutputNormalizer(
        client=client,  # type: ignore[arg-type]
        deployment="receipt-gpt-54-mini",
        reasoning_effort=None,
    )

    with pytest.raises(ValueError, match="OCR text cannot be empty"):
        normalizer.normalize("   ", receipt_schema)
    assert client.completions.calls == []


def test_normalize_raises_invalid_solution_output_on_no_choices(
    receipt_schema: dict[str, Any],
) -> None:
    """A successful call with no choices is invalid semantic output, not a transport
    failure, so raw response/usage must be retained on the exception."""
    client = FakeOpenAIClient([make_chat_completion(None, has_choices=False)])
    normalizer = StructuredOutputNormalizer(
        client=client,  # type: ignore[arg-type]
        deployment="receipt-gpt-54-mini",
        reasoning_effort=None,
    )

    with pytest.raises(InvalidSolutionOutput) as exc_info:
        normalizer.normalize("Contoso Cafe\nTotal $12.00", receipt_schema)

    assert isinstance(exc_info.value.raw_response, dict)
    assert len(exc_info.value.components) == 1


def test_normalize_raises_invalid_solution_output_on_invalid_json(
    receipt_schema: dict[str, Any],
) -> None:
    client = FakeOpenAIClient([make_chat_completion("not valid json")])
    normalizer = StructuredOutputNormalizer(
        client=client,  # type: ignore[arg-type]
        deployment="receipt-gpt-54-mini",
        reasoning_effort=None,
    )

    with pytest.raises(InvalidSolutionOutput) as exc_info:
        normalizer.normalize("Contoso Cafe\nTotal $12.00", receipt_schema)

    assert isinstance(exc_info.value.raw_response, dict)
    assert len(exc_info.value.components) == 1
    assert exc_info.value.components[0].component == "normalize"
