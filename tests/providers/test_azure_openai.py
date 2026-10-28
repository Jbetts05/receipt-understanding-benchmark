"""Tests for the direct Azure OpenAI multimodal extraction adapter."""

from __future__ import annotations

from typing import Any

import pytest

from receipt_bench.providers.azure_openai import AzureOpenAIAdapter
from receipt_bench.providers.base import InvalidSolutionOutput
from tests.conftest import build_ground_truth_receipt
from tests.providers.conftest import FakeOpenAIClient, make_chat_completion


def test_execute_parses_strict_json_schema_response(receipt_schema: dict[str, Any]) -> None:
    receipt_json = build_ground_truth_receipt().model_dump_json()
    client = FakeOpenAIClient([make_chat_completion(receipt_json)])
    adapter = AzureOpenAIAdapter(
        "gpt-5.6-sol",
        client=client,  # type: ignore[arg-type]
        deployment="receipt-gpt-56-sol",
        schema=receipt_schema,
        reasoning_effort="low",
    )

    response = adapter.execute(b"fake-image-bytes", "image/png")

    assert response.receipt == build_ground_truth_receipt()
    assert len(response.components) == 1
    measurement = response.components[0]
    assert measurement.component == "direct_extraction"
    assert measurement.usage.input_tokens == 100
    assert measurement.usage.output_tokens == 50
    assert isinstance(response.raw_response, dict)

    call = client.completions.calls[0]
    assert call["model"] == "receipt-gpt-56-sol"
    assert call["reasoning_effort"] == "low"
    assert call["max_completion_tokens"] == 8192
    assert "max_tokens" not in call
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True
    # One system message plus one user message containing text + image parts.
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][1]["role"] == "user"
    user_content = call["messages"][1]["content"]
    assert user_content[0]["type"] == "text"
    assert user_content[1]["type"] == "image_url"
    assert user_content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_execute_raises_invalid_solution_output_on_empty_response(
    receipt_schema: dict[str, Any],
) -> None:
    """A successful call with no choices is invalid semantic output (e.g. content-filtered),
    not a transport failure, so raw response/usage must be retained on the exception."""
    client = FakeOpenAIClient([make_chat_completion(None, has_choices=False)])
    adapter = AzureOpenAIAdapter(
        "gpt-5.6-sol",
        client=client,  # type: ignore[arg-type]
        deployment="receipt-gpt-56-sol",
        schema=receipt_schema,
        reasoning_effort=None,
    )

    with pytest.raises(InvalidSolutionOutput) as exc_info:
        adapter.execute(b"fake-image-bytes", "image/png")

    assert isinstance(exc_info.value.raw_response, dict)
    assert len(exc_info.value.components) == 1


def test_execute_raises_invalid_solution_output_on_invalid_json(
    receipt_schema: dict[str, Any],
) -> None:
    client = FakeOpenAIClient([make_chat_completion("not json")])
    adapter = AzureOpenAIAdapter(
        "gpt-5.6-sol",
        client=client,  # type: ignore[arg-type]
        deployment="receipt-gpt-56-sol",
        schema=receipt_schema,
        reasoning_effort=None,
    )

    with pytest.raises(InvalidSolutionOutput) as exc_info:
        adapter.execute(b"fake-image-bytes", "image/png")

    assert isinstance(exc_info.value.__cause__, Exception)
    assert isinstance(exc_info.value.raw_response, dict)
    assert len(exc_info.value.components) == 1
    assert exc_info.value.components[0].component == "direct_extraction"
    assert exc_info.value.components[0].usage.input_tokens == 100
