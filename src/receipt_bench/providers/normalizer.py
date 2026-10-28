"""Shared text-to-schema normalizer.

Used by every OCR/ingestion pipeline adapter (Mistral OCR, Azure Vision Read,
Content Understanding read/layout) to turn ordered OCR text into a
``CanonicalReceipt``, via the same Azure OpenAI chat-completion core used by the
direct GPT adapter.
"""

from __future__ import annotations

from typing import Any

from openai import OpenAI

from receipt_bench.models import CanonicalReceipt, ComponentMeasurement
from receipt_bench.prompting import (
    NORMALIZER_SYSTEM_PROMPT,
    normalization_prompt,
    parse_receipt_json,
)
from receipt_bench.providers._openai_chat import (
    ReasoningEffort,
    chat_completion_text,
    strict_json_schema_response_format,
)
from receipt_bench.providers._openai_messages import system_message, user_text_message
from receipt_bench.providers.base import InvalidSolutionOutput


class StructuredOutputNormalizer:
    """Text-only OCR-to-schema normalizer backed by a configured Azure OpenAI deployment."""

    def __init__(
        self,
        *,
        client: OpenAI,
        deployment: str,
        reasoning_effort: ReasoningEffort | None,
    ) -> None:
        self._client = client
        self._deployment = deployment
        self._reasoning_effort = reasoning_effort

    def normalize(
        self, ocr_text: str, schema: dict[str, Any]
    ) -> tuple[CanonicalReceipt, dict[str, Any], ComponentMeasurement]:
        messages = [
            system_message(NORMALIZER_SYSTEM_PROMPT),
            user_text_message(normalization_prompt(ocr_text, schema)),
        ]
        text, raw_response, measurement = chat_completion_text(
            self._client,
            deployment=self._deployment,
            messages=messages,
            response_format=strict_json_schema_response_format(schema),
            reasoning_effort=self._reasoning_effort,
            token_limit_param="max_completion_tokens",
            component="normalize",
        )
        try:
            receipt = parse_receipt_json(text, schema)
        except (ValueError, TypeError) as exc:
            raise InvalidSolutionOutput(
                f"normalize: response could not be parsed into CanonicalReceipt: {exc}",
                raw_response=raw_response,
                components=[measurement],
            ) from exc
        return receipt, raw_response, measurement
