"""Shared fixtures/fakes for provider adapter tests.

None of these fakes perform network I/O: OpenAI/Anthropic fakes are constructed from
real SDK response models (``ChatCompletion``, ``Message``) so adapter code is
exercised against genuine response shapes, while Azure SDK fakes duck-type the
methods the adapters call.
"""

from __future__ import annotations

from typing import Any

import pytest
from anthropic.types import Message
from openai.types.chat import ChatCompletion

from receipt_bench.config import AzureSettings


def _azure_settings(*, auth_mode: str, api_key: str | None) -> AzureSettings:
    return AzureSettings(
        azure_subscription_id="00000000-0000-0000-0000-000000000000",
        azure_resource_group="rg-receipt-bench-test",
        azure_location="swedencentral",
        azure_foundry_account="airbtest0001",
        azure_foundry_project="receipt-bench",
        azure_ai_auth_mode=auth_mode,
        azure_foundry_api_key=api_key,
        azure_foundry_openai_endpoint="https://fake-account.openai.azure.com/openai/v1/",
        azure_foundry_models_endpoint="https://fake-account.services.ai.azure.com/openai/v1",
        azure_anthropic_endpoint="https://fake-account.services.ai.azure.com/anthropic",
        azure_mistral_ocr_endpoint="https://fake-account.services.ai.azure.com",
        azure_content_understanding_endpoint="https://fake-account.cognitiveservices.azure.com",
        azure_document_intelligence_endpoint="https://fake-account.cognitiveservices.azure.com",
        azure_vision_endpoint="https://fake-account.cognitiveservices.azure.com",
        _env_file=None,
    )


@pytest.fixture
def azure_settings() -> AzureSettings:
    """Fully populated key-auth settings with no real ``.env`` read."""
    return _azure_settings(auth_mode="key", api_key="fake-foundry-api-key")


@pytest.fixture
def entra_azure_settings() -> AzureSettings:
    """Fully populated Entra-auth settings with no API key or real ``.env`` read."""
    return _azure_settings(auth_mode="entra", api_key=None)


def make_chat_completion(
    content: str | None,
    *,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    has_choices: bool = True,
) -> ChatCompletion:
    """Build a real ``openai`` ``ChatCompletion`` response object for fake clients."""
    choices: list[dict[str, Any]] = (
        [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ]
        if has_choices
        else []
    )
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 1_700_000_000,
            "model": "fake-deployment",
            "choices": choices,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    )


def make_anthropic_message(
    text_segments: list[str], *, input_tokens: int = 12, output_tokens: int = 7
) -> Message:
    """Build a real ``anthropic`` ``Message`` response object for fake clients."""
    return Message.model_validate(
        {
            "id": "msg-fake",
            "type": "message",
            "role": "assistant",
            "model": "fake-deployment",
            "content": [{"type": "text", "text": segment} for segment in text_segments],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    )


class FakeChatCompletions:
    """Fakes ``client.chat.completions`` returning a scripted sequence of responses."""

    def __init__(self, responses: list[ChatCompletion | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> ChatCompletion:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeChatCompletions: no more scripted responses")
        next_item = self._responses.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item


class FakeChat:
    def __init__(self, completions: FakeChatCompletions) -> None:
        self.completions = completions


class FakeOpenAIClient:
    """Duck-types ``openai.OpenAI`` for ``chat.completions.create`` calls only."""

    def __init__(self, responses: list[ChatCompletion | Exception]) -> None:
        self.completions = FakeChatCompletions(responses)
        self.chat = FakeChat(self.completions)


class FakeAnthropicMessages:
    def __init__(self, responses: list[Message | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeAnthropicMessages: no more scripted responses")
        next_item = self._responses.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item


class FakeAnthropicClient:
    """Duck-types ``anthropic.Anthropic`` for ``messages.create`` calls only."""

    def __init__(self, responses: list[Message | Exception]) -> None:
        self.messages = FakeAnthropicMessages(responses)
