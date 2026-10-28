"""Tests for the Foundry OpenAI-compatible direct adapter (Grok, Mistral Large)."""

from __future__ import annotations

from typing import Any

import pytest

from receipt_bench.providers.base import InvalidSolutionOutput
from receipt_bench.providers.foundry_openai import FoundryOpenAIAdapter
from tests.conftest import build_ground_truth_receipt
from tests.providers.conftest import FakeOpenAIClient, make_chat_completion


def test_execute_uses_json_object_mode_and_schema_in_prompt(receipt_schema: dict[str, Any]) -> None:
    receipt_json = build_ground_truth_receipt().model_dump_json()
    client = FakeOpenAIClient([make_chat_completion(receipt_json)])
    adapter = FoundryOpenAIAdapter(
        "grok-4.6",
        client=client,  # type: ignore[arg-type]
        deployment="receipt-grok-46",
        schema=receipt_schema,
    )

    response = adapter.execute(b"fake-image-bytes", "image/jpeg")

    assert response.receipt == build_ground_truth_receipt()
    call = client.completions.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert "reasoning_effort" not in call
    assert call["max_tokens"] == 8192
    assert "max_completion_tokens" not in call
    user_text = call["messages"][1]["content"][0]["text"]
    assert "JSON Schema" in user_text


def test_execute_raises_invalid_solution_output_on_invalid_json(
    receipt_schema: dict[str, Any],
) -> None:
    client = FakeOpenAIClient([make_chat_completion("not valid json at all")])
    adapter = FoundryOpenAIAdapter(
        "mistral-large-3",
        client=client,  # type: ignore[arg-type]
        deployment="receipt-mistral-large-3",
        schema=receipt_schema,
    )

    with pytest.raises(InvalidSolutionOutput) as exc_info:
        adapter.execute(b"fake-image-bytes", "image/jpeg")

    assert isinstance(exc_info.value.raw_response, dict)
    assert len(exc_info.value.components) == 1
    assert exc_info.value.components[0].component == "direct_extraction"


def test_execute_raises_invalid_solution_output_on_no_choices(
    receipt_schema: dict[str, Any],
) -> None:
    """A successful call with no choices is invalid semantic output, not a transport
    failure, so raw response/usage must be retained on the exception."""
    client = FakeOpenAIClient([make_chat_completion(None, has_choices=False)])
    adapter = FoundryOpenAIAdapter(
        "grok-4.6",
        client=client,  # type: ignore[arg-type]
        deployment="receipt-grok-46",
        schema=receipt_schema,
    )

    with pytest.raises(InvalidSolutionOutput) as exc_info:
        adapter.execute(b"fake-image-bytes", "image/jpeg")

    assert isinstance(exc_info.value.raw_response, dict)
    assert len(exc_info.value.components) == 1
