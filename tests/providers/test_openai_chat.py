"""Tests for the shared OpenAI-compatible chat-completion core."""

from __future__ import annotations

import httpx
import openai
import pytest

from receipt_bench.providers._openai_chat import (
    MAX_OUTPUT_TOKENS,
    chat_completion_text,
    json_object_response_format,
    strict_json_schema_response_format,
    validate_reasoning_effort,
)
from receipt_bench.providers.base import InvalidSolutionOutput, ProviderResponseError
from tests.providers.conftest import FakeOpenAIClient, make_chat_completion


@pytest.mark.parametrize(
    "value", ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
)
def test_validate_reasoning_effort_accepts_known_values(value: str) -> None:
    assert validate_reasoning_effort(value) == value


def test_validate_reasoning_effort_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="Unsupported reasoning_effort"):
        validate_reasoning_effort("ultra")


def test_strict_json_schema_response_format_wraps_schema() -> None:
    schema = {"type": "object", "properties": {}}
    response_format = strict_json_schema_response_format(schema)
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"] == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def test_json_object_response_format() -> None:
    assert json_object_response_format() == {"type": "json_object"}


def test_chat_completion_text_returns_text_raw_and_measurement() -> None:
    client = FakeOpenAIClient([make_chat_completion('{"a": 1}')])

    text, raw_response, measurement = chat_completion_text(
        client,  # type: ignore[arg-type]
        deployment="my-deployment",
        messages=[],
        response_format={"type": "json_object"},
        reasoning_effort="low",
        token_limit_param="max_completion_tokens",
        component="direct_extraction",
    )

    assert text == '{"a": 1}'
    assert raw_response["id"] == "chatcmpl-fake"
    assert measurement.component == "direct_extraction"
    assert measurement.latency_ms >= 0.0
    assert measurement.usage.input_tokens == 100
    assert measurement.usage.output_tokens == 50
    assert measurement.usage.total_tokens == 150
    assert client.completions.calls[0]["reasoning_effort"] == "low"
    assert client.completions.calls[0]["model"] == "my-deployment"


def test_chat_completion_text_omits_reasoning_effort_when_none() -> None:
    client = FakeOpenAIClient([make_chat_completion('{"a": 1}')])

    chat_completion_text(
        client,  # type: ignore[arg-type]
        deployment="my-deployment",
        messages=[],
        response_format={"type": "json_object"},
        reasoning_effort=None,
        token_limit_param="max_completion_tokens",
        component="direct_extraction",
    )

    assert "reasoning_effort" not in client.completions.calls[0]


def test_chat_completion_text_sends_max_completion_tokens_for_azure_openai() -> None:
    """Azure OpenAI GPT deployments (direct extraction and the normalizer) accept the
    current ``max_completion_tokens`` name; it must be bounded at 8192 and the legacy
    ``max_tokens`` name must never be sent alongside it."""
    client = FakeOpenAIClient([make_chat_completion('{"a": 1}')])

    chat_completion_text(
        client,  # type: ignore[arg-type]
        deployment="my-deployment",
        messages=[],
        response_format={"type": "json_object"},
        reasoning_effort=None,
        token_limit_param="max_completion_tokens",
        component="direct_extraction",
    )

    call = client.completions.calls[0]
    assert call["max_completion_tokens"] == MAX_OUTPUT_TOKENS == 8192
    assert "max_tokens" not in call


def test_chat_completion_text_sends_max_tokens_for_foundry_openai() -> None:
    """The Foundry OpenAI-compatible surface (Grok, Mistral Large) rejects
    ``max_completion_tokens`` today, so it must receive the legacy ``max_tokens`` name
    instead, bounded at the same 8192 value."""
    client = FakeOpenAIClient([make_chat_completion('{"a": 1}')])

    chat_completion_text(
        client,  # type: ignore[arg-type]
        deployment="my-deployment",
        messages=[],
        response_format={"type": "json_object"},
        reasoning_effort=None,
        token_limit_param="max_tokens",
        component="direct_extraction",
    )

    call = client.completions.calls[0]
    assert call["max_tokens"] == MAX_OUTPUT_TOKENS == 8192
    assert "max_completion_tokens" not in call


def test_chat_completion_text_sends_max_completion_tokens_with_reasoning_effort() -> None:
    """Both the token bound and reasoning effort must be present together when both are
    configured, using the ``max_completion_tokens`` name for the Azure OpenAI surface."""
    client = FakeOpenAIClient([make_chat_completion('{"a": 1}')])

    chat_completion_text(
        client,  # type: ignore[arg-type]
        deployment="my-deployment",
        messages=[],
        response_format={"type": "json_object"},
        reasoning_effort="low",
        token_limit_param="max_completion_tokens",
        component="direct_extraction",
    )

    call = client.completions.calls[0]
    assert call["max_completion_tokens"] == 8192
    assert call["reasoning_effort"] == "low"
    assert "max_tokens" not in call


def test_chat_completion_text_raises_invalid_solution_output_on_no_choices() -> None:
    """A successful call with no choices (e.g. a content-filtered response) is invalid
    semantic output, not a transport failure, so the decoded raw response/usage must be
    retained on the exception rather than discarded."""
    client = FakeOpenAIClient([make_chat_completion(None, has_choices=False)])

    with pytest.raises(InvalidSolutionOutput, match="no choices") as exc_info:
        chat_completion_text(
            client,  # type: ignore[arg-type]
            deployment="my-deployment",
            messages=[],
            response_format={"type": "json_object"},
            reasoning_effort=None,
            token_limit_param="max_completion_tokens",
            component="direct_extraction",
        )

    assert exc_info.value.raw_response["id"] == "chatcmpl-fake"
    assert len(exc_info.value.components) == 1
    assert exc_info.value.components[0].component == "direct_extraction"
    assert exc_info.value.components[0].usage.input_tokens == 100


def test_chat_completion_text_raises_invalid_solution_output_on_no_content() -> None:
    """A successful call whose message has no text content (e.g. a refusal) is invalid
    semantic output, not a transport failure, so raw response/usage must be retained."""
    client = FakeOpenAIClient([make_chat_completion(None)])

    with pytest.raises(InvalidSolutionOutput, match="no text content") as exc_info:
        chat_completion_text(
            client,  # type: ignore[arg-type]
            deployment="my-deployment",
            messages=[],
            response_format={"type": "json_object"},
            reasoning_effort=None,
            token_limit_param="max_completion_tokens",
            component="direct_extraction",
        )

    assert exc_info.value.raw_response["id"] == "chatcmpl-fake"
    assert len(exc_info.value.components) == 1


def test_chat_completion_text_wraps_sdk_errors_as_provider_response_error() -> None:
    sdk_error = openai.APIConnectionError(request=httpx.Request("POST", "https://example.com"))
    client = FakeOpenAIClient([sdk_error])

    with pytest.raises(ProviderResponseError, match="chat completion call failed") as exc_info:
        chat_completion_text(
            client,  # type: ignore[arg-type]
            deployment="my-deployment",
            messages=[],
            response_format={"type": "json_object"},
            reasoning_effort=None,
            token_limit_param="max_completion_tokens",
            component="direct_extraction",
        )
    assert exc_info.value.__cause__ is sdk_error
