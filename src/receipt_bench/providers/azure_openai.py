"""Azure OpenAI direct multimodal extraction adapter (GPT deployments).

Talks to the Azure OpenAI v1 API surface (``{account}.openai.azure.com/openai/v1``)
using the standard, unmodified ``openai`` SDK client, per Azure's "v1 preview" API
compatibility guarantee: no ``AzureOpenAI`` subclass, no ``api-version`` query
parameter, and either API-key or Entra bearer authentication supplied through the
SDK's standard ``api_key`` hook.
"""

from __future__ import annotations

from typing import Any

from openai import OpenAI

from receipt_bench.prompting import (
    DIRECT_SYSTEM_PROMPT,
    image_data_uri,
    parse_receipt_json,
    schema_prompt,
)
from receipt_bench.providers._openai_chat import (
    ReasoningEffort,
    chat_completion_text,
    strict_json_schema_response_format,
)
from receipt_bench.providers._openai_messages import system_message, user_image_message
from receipt_bench.providers.base import AdapterResponse, InvalidSolutionOutput, SolutionAdapter


class AzureOpenAIAdapter(SolutionAdapter):
    """Direct single-call multimodal extraction against an Azure OpenAI GPT deployment."""

    def __init__(
        self,
        solution_id: str,
        *,
        client: OpenAI,
        deployment: str,
        schema: dict[str, Any],
        reasoning_effort: ReasoningEffort | None,
    ) -> None:
        super().__init__(solution_id)
        self._client = client
        self._deployment = deployment
        self._schema = dict(schema)
        self._reasoning_effort = reasoning_effort

    def execute(self, image_bytes: bytes, media_type: str) -> AdapterResponse:
        messages = [
            system_message(DIRECT_SYSTEM_PROMPT),
            user_image_message(
                schema_prompt(self._schema),
                image_data_uri(image_bytes, media_type),
            ),
        ]
        text, raw_response, measurement = chat_completion_text(
            self._client,
            deployment=self._deployment,
            messages=messages,
            response_format=strict_json_schema_response_format(self._schema),
            reasoning_effort=self._reasoning_effort,
            token_limit_param="max_completion_tokens",
            component="direct_extraction",
        )
        try:
            receipt = parse_receipt_json(text, self._schema)
        except (ValueError, TypeError) as exc:
            raise InvalidSolutionOutput(
                f"direct_extraction: response could not be parsed into CanonicalReceipt: {exc}",
                raw_response=raw_response,
                components=[measurement],
            ) from exc
        return AdapterResponse(receipt=receipt, raw_response=raw_response, components=[measurement])
