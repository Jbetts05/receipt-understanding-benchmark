"""Foundry OpenAI-compatible direct adapter (Grok, Mistral Large).

These models are served through the unified Azure AI Foundry Models endpoint's
OpenAI-compatible route. They do not support strict ``json_schema`` response
formatting, so this adapter uses ``json_object`` mode and embeds the schema as text
in the prompt, then validates the resulting JSON strictly (no repair) via
``parse_receipt_json``.
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
from receipt_bench.providers._openai_chat import chat_completion_text, json_object_response_format
from receipt_bench.providers._openai_messages import system_message, user_image_message
from receipt_bench.providers.base import AdapterResponse, InvalidSolutionOutput, SolutionAdapter


class FoundryOpenAIAdapter(SolutionAdapter):
    """Direct single-call multimodal extraction against a Foundry Models deployment."""

    def __init__(
        self,
        solution_id: str,
        *,
        client: OpenAI,
        deployment: str,
        schema: dict[str, Any],
    ) -> None:
        super().__init__(solution_id)
        self._client = client
        self._deployment = deployment
        self._schema = dict(schema)

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
            response_format=json_object_response_format(),
            reasoning_effort=None,
            token_limit_param="max_tokens",
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
