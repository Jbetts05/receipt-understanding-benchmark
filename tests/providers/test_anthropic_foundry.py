"""Tests for the AnthropicFoundry adapter (Claude Opus 5)."""

from __future__ import annotations

from typing import Any

import anthropic
import httpx
import pytest

from receipt_bench.providers.anthropic_foundry import AnthropicFoundryAdapter
from receipt_bench.providers.base import InvalidSolutionOutput, ProviderResponseError
from tests.conftest import build_ground_truth_receipt
from tests.providers.conftest import FakeAnthropicClient, make_anthropic_message


def test_execute_extracts_text_and_usage(receipt_schema: dict[str, Any]) -> None:
    receipt_json = build_ground_truth_receipt().model_dump_json()
    client = FakeAnthropicClient([make_anthropic_message([receipt_json])])
    adapter = AnthropicFoundryAdapter(
        "claude-opus-5",
        client=client,  # type: ignore[arg-type]
        deployment="receipt-claude-opus-5",
        schema=receipt_schema,
    )

    response = adapter.execute(b"fake-image-bytes", "image/png")

    assert response.receipt == build_ground_truth_receipt()
    assert len(response.components) == 1
    measurement = response.components[0]
    assert measurement.component == "direct_extraction"
    assert measurement.usage.input_tokens == 12
    assert measurement.usage.output_tokens == 7
    assert measurement.usage.total_tokens == 19
    assert isinstance(response.raw_response, dict)

    call = client.messages.calls[0]
    assert call["model"] == "receipt-claude-opus-5"
    assert call["max_tokens"] == 8192
    image_block = call["messages"][0]["content"][1]
    assert image_block["type"] == "image"
    assert image_block["source"]["media_type"] == "image/png"


def test_execute_joins_multiple_text_blocks(receipt_schema: dict[str, Any]) -> None:
    receipt_json = build_ground_truth_receipt().model_dump_json()
    half = len(receipt_json) // 2
    client = FakeAnthropicClient(
        [make_anthropic_message([receipt_json[:half], receipt_json[half:]])]
    )
    adapter = AnthropicFoundryAdapter(
        "claude-opus-5",
        client=client,  # type: ignore[arg-type]
        deployment="receipt-claude-opus-5",
        schema=receipt_schema,
    )

    response = adapter.execute(b"fake-image-bytes", "image/png")

    assert response.receipt == build_ground_truth_receipt()


def test_execute_rejects_unsupported_media_type(receipt_schema: dict[str, Any]) -> None:
    client = FakeAnthropicClient([])
    adapter = AnthropicFoundryAdapter(
        "claude-opus-5",
        client=client,  # type: ignore[arg-type]
        deployment="receipt-claude-opus-5",
        schema=receipt_schema,
    )

    with pytest.raises(ValueError, match="Unsupported image media type"):
        adapter.execute(b"fake-image-bytes", "image/tiff")

    assert client.messages.calls == []


def test_execute_raises_invalid_solution_output_on_no_text_blocks(
    receipt_schema: dict[str, Any],
) -> None:
    """A successful call with no text blocks (e.g. a refusal) is invalid semantic output,
    not a transport failure, so raw response/usage must be retained on the exception."""
    client = FakeAnthropicClient([make_anthropic_message([])])
    adapter = AnthropicFoundryAdapter(
        "claude-opus-5",
        client=client,  # type: ignore[arg-type]
        deployment="receipt-claude-opus-5",
        schema=receipt_schema,
    )

    with pytest.raises(InvalidSolutionOutput, match="no text blocks") as exc_info:
        adapter.execute(b"fake-image-bytes", "image/png")

    assert isinstance(exc_info.value.raw_response, dict)
    assert len(exc_info.value.components) == 1
    assert exc_info.value.components[0].usage.input_tokens == 12


def test_execute_raises_invalid_solution_output_on_invalid_json(
    receipt_schema: dict[str, Any],
) -> None:
    client = FakeAnthropicClient([make_anthropic_message(["not valid json at all"])])
    adapter = AnthropicFoundryAdapter(
        "claude-opus-5",
        client=client,  # type: ignore[arg-type]
        deployment="receipt-claude-opus-5",
        schema=receipt_schema,
    )

    with pytest.raises(InvalidSolutionOutput) as exc_info:
        adapter.execute(b"fake-image-bytes", "image/png")

    assert isinstance(exc_info.value.raw_response, dict)
    assert len(exc_info.value.components) == 1
    measurement = exc_info.value.components[0]
    assert measurement.component == "direct_extraction"
    assert measurement.usage.input_tokens == 12


def test_execute_wraps_sdk_errors_as_provider_response_error(
    receipt_schema: dict[str, Any],
) -> None:
    sdk_error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://example.com"))
    client = FakeAnthropicClient([sdk_error])
    adapter = AnthropicFoundryAdapter(
        "claude-opus-5",
        client=client,  # type: ignore[arg-type]
        deployment="receipt-claude-opus-5",
        schema=receipt_schema,
    )

    with pytest.raises(ProviderResponseError, match="Anthropic message call failed") as exc_info:
        adapter.execute(b"fake-image-bytes", "image/png")
    assert exc_info.value.__cause__ is sdk_error
