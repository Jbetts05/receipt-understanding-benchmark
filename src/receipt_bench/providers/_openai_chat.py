"""Shared OpenAI-compatible chat-completion core.

Used by the direct Azure OpenAI adapter, the Foundry OpenAI-compatible adapter (Grok,
Mistral Large), and the GPT-5.4 Mini text normalizer, since all three talk to an
OpenAI-compatible ``/chat/completions`` surface via the ``openai`` SDK's ``OpenAI``
client (``AzureOpenAI`` is a subclass of ``OpenAI``, so it is accepted as well).
"""

from __future__ import annotations

import time
from typing import Any, Literal

from openai import OpenAI, OpenAIError
from openai.types.chat import ChatCompletionMessageParam
from openai.types.chat.completion_create_params import ResponseFormat
from openai.types.shared_params.response_format_json_object import ResponseFormatJSONObject
from openai.types.shared_params.response_format_json_schema import (
    JSONSchema,
    ResponseFormatJSONSchema,
)

from receipt_bench.models import ComponentMeasurement, Usage
from receipt_bench.prompting import openai_response_format
from receipt_bench.providers._timing import elapsed_ms
from receipt_bench.providers.base import InvalidSolutionOutput, ProviderResponseError

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]

_KNOWN_REASONING_EFFORTS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

#: Which output-token-bound parameter name this OpenAI-compatible surface accepts.
#: Azure OpenAI GPT deployments accept the current ``max_completion_tokens``; the
#: Foundry OpenAI-compatible surface for Grok/Mistral Large only accepts the legacy
#: ``max_tokens`` name. Callers must pick one explicitly rather than sending both.
TokenLimitParam = Literal["max_completion_tokens", "max_tokens"]

#: Fixed output-token bound applied uniformly across direct/normalizer chat-completion
#: calls for cost/reliability fairness across solutions (Claude uses the same bound via
#: its own ``max_tokens`` argument in ``anthropic_foundry.py``).
MAX_OUTPUT_TOKENS = 8192


def validate_reasoning_effort(value: str) -> ReasoningEffort:
    """Narrow a configured ``reasoning_effort`` string to the SDK's literal type.

    Raises ``ValueError`` explicitly for any value outside the known set instead of
    silently forwarding an unsupported value to the API.
    """
    if value == "none":
        return "none"
    if value == "minimal":
        return "minimal"
    if value == "low":
        return "low"
    if value == "medium":
        return "medium"
    if value == "high":
        return "high"
    if value == "xhigh":
        return "xhigh"
    if value == "max":
        return "max"
    raise ValueError(
        f"Unsupported reasoning_effort {value!r}; expected one of {_KNOWN_REASONING_EFFORTS}"
    )


def strict_json_schema_response_format(schema: dict[str, Any]) -> ResponseFormatJSONSchema:
    """Build a strict ``json_schema`` response format from the canonical receipt schema."""
    payload = openai_response_format(schema)
    json_schema = payload["json_schema"]
    typed_schema: JSONSchema = {
        "name": json_schema["name"],
        "strict": json_schema["strict"],
        "schema": json_schema["schema"],
    }
    return {"type": "json_schema", "json_schema": typed_schema}


def json_object_response_format() -> ResponseFormatJSONObject:
    """The legacy ``json_object`` response format for APIs without strict schema mode."""
    return {"type": "json_object"}


def chat_completion_text(
    client: OpenAI,
    *,
    deployment: str,
    messages: list[ChatCompletionMessageParam],
    response_format: ResponseFormat,
    reasoning_effort: ReasoningEffort | None,
    token_limit_param: TokenLimitParam,
    component: str,
) -> tuple[str, dict[str, Any], ComponentMeasurement]:
    """Call chat completions once, measure latency, and return (text, raw, measurement).

    ``token_limit_param`` selects which output-token-bound keyword this surface accepts
    (``max_completion_tokens`` for Azure OpenAI, ``max_tokens`` for the Foundry
    OpenAI-compatible surface); exactly one is sent, fixed at ``MAX_OUTPUT_TOKENS``, never
    both, so every direct/normalizer call is bounded the same way for cost/reliability
    fairness across solutions.

    Raises ``ProviderResponseError`` explicitly if the underlying SDK call itself fails
    (network error, HTTP error status, auth failure, etc.) — a transport/provider-contract
    failure with no usable response to attach. If the call *succeeds* but the completion
    has no choices or no text content (for example a safety refusal), that is treated as
    invalid semantic output: ``InvalidSolutionOutput`` is raised instead, carrying the
    already-decoded raw response and component measurement so latency/usage are not lost.
    Invalid JSON or schema content in a *successful* response's text is left for the caller
    (``parse_receipt_json``) to raise on, so the orchestrator can distinguish a bad model
    output from a failed call.
    """
    start = time.monotonic()
    try:
        if token_limit_param == "max_completion_tokens":
            if reasoning_effort is not None:
                completion = client.chat.completions.create(
                    model=deployment,
                    messages=messages,
                    response_format=response_format,
                    reasoning_effort=reasoning_effort,
                    max_completion_tokens=MAX_OUTPUT_TOKENS,
                )
            else:
                completion = client.chat.completions.create(
                    model=deployment,
                    messages=messages,
                    response_format=response_format,
                    max_completion_tokens=MAX_OUTPUT_TOKENS,
                )
        else:
            if reasoning_effort is not None:
                completion = client.chat.completions.create(
                    model=deployment,
                    messages=messages,
                    response_format=response_format,
                    reasoning_effort=reasoning_effort,
                    max_tokens=MAX_OUTPUT_TOKENS,
                )
            else:
                completion = client.chat.completions.create(
                    model=deployment,
                    messages=messages,
                    response_format=response_format,
                    max_tokens=MAX_OUTPUT_TOKENS,
                )
    except OpenAIError as exc:
        raise ProviderResponseError(f"{component}: chat completion call failed: {exc}") from exc
    latency_ms = elapsed_ms(start)

    raw_response = completion.model_dump(mode="json")
    usage_obj = completion.usage
    usage = Usage(
        input_tokens=usage_obj.prompt_tokens if usage_obj is not None else None,
        output_tokens=usage_obj.completion_tokens if usage_obj is not None else None,
        total_tokens=usage_obj.total_tokens if usage_obj is not None else None,
        provider_metadata=usage_obj.model_dump(mode="json") if usage_obj is not None else {},
    )
    measurement = ComponentMeasurement(component=component, latency_ms=latency_ms, usage=usage)

    if not completion.choices:
        raise InvalidSolutionOutput(
            f"{component}: chat completion returned no choices",
            raw_response=raw_response,
            components=[measurement],
        )
    content = completion.choices[0].message.content
    if content is None:
        raise InvalidSolutionOutput(
            f"{component}: chat completion message had no text content (possible refusal)",
            raw_response=raw_response,
            components=[measurement],
        )

    return content, raw_response, measurement
