"""AnthropicFoundry adapter for Claude Opus 5.

Talks to the Azure AI Foundry Anthropic-compatible endpoint
(``{account}.services.ai.azure.com/anthropic``) via the official
``anthropic.AnthropicFoundry`` client (not the generic ``anthropic.Anthropic``
client), with API-key or Entra authentication, base64 image content, and an exact
JSON-only schema prompt (no tool use / structured output mode is assumed available
on this surface).
"""

from __future__ import annotations

import base64
import time
from typing import Any, Literal

import anthropic
from anthropic.types import ImageBlockParam, MessageParam, TextBlockParam

from receipt_bench.models import ComponentMeasurement, Usage
from receipt_bench.prompting import DIRECT_SYSTEM_PROMPT, parse_receipt_json, schema_prompt
from receipt_bench.providers._timing import elapsed_ms
from receipt_bench.providers.base import (
    AdapterResponse,
    InvalidSolutionOutput,
    ProviderResponseError,
    SolutionAdapter,
)

AnthropicImageMediaType = Literal["image/jpeg", "image/png", "image/gif", "image/webp"]

_KNOWN_IMAGE_MEDIA_TYPES: tuple[AnthropicImageMediaType, ...] = (
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
)

# Receipts can carry many line items; leave generous headroom for the JSON payload.
_MAX_OUTPUT_TOKENS = 8192


def _validate_image_media_type(value: str) -> AnthropicImageMediaType:
    for candidate in _KNOWN_IMAGE_MEDIA_TYPES:
        if value == candidate:
            return candidate
    raise ValueError(
        f"Unsupported image media type {value!r} for Anthropic; "
        f"expected one of {_KNOWN_IMAGE_MEDIA_TYPES}"
    )


class AnthropicFoundryAdapter(SolutionAdapter):
    """Direct single-call multimodal extraction against Claude Opus 5 on Foundry."""

    def __init__(
        self,
        solution_id: str,
        *,
        client: anthropic.AnthropicFoundry,
        deployment: str,
        schema: dict[str, Any],
    ) -> None:
        super().__init__(solution_id)
        self._client = client
        self._deployment = deployment
        self._schema = dict(schema)

    def execute(self, image_bytes: bytes, media_type: str) -> AdapterResponse:
        anthropic_media_type = _validate_image_media_type(media_type)
        image_block: ImageBlockParam = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": anthropic_media_type,
                "data": base64.b64encode(image_bytes).decode("ascii"),
            },
        }
        text_block: TextBlockParam = {"type": "text", "text": schema_prompt(self._schema)}
        message: MessageParam = {"role": "user", "content": [text_block, image_block]}

        start = time.monotonic()
        try:
            response = self._client.messages.create(
                model=self._deployment,
                max_tokens=_MAX_OUTPUT_TOKENS,
                system=DIRECT_SYSTEM_PROMPT,
                messages=[message],
            )
        except anthropic.AnthropicError as exc:
            raise ProviderResponseError(
                f"direct_extraction: Anthropic message call failed: {exc}"
            ) from exc
        latency_ms = elapsed_ms(start)

        raw_response = response.model_dump(mode="json")
        usage = Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            total_tokens=response.usage.input_tokens + response.usage.output_tokens,
            provider_metadata=response.usage.model_dump(mode="json"),
        )
        measurement = ComponentMeasurement(
            component="direct_extraction", latency_ms=latency_ms, usage=usage
        )

        text_segments = [block.text for block in response.content if block.type == "text"]
        if not text_segments:
            raise InvalidSolutionOutput(
                "direct_extraction: response contained no text blocks (possible refusal)",
                raw_response=raw_response,
                components=[measurement],
            )
        try:
            receipt = parse_receipt_json("".join(text_segments), self._schema)
        except (ValueError, TypeError) as exc:
            raise InvalidSolutionOutput(
                f"direct_extraction: response could not be parsed into CanonicalReceipt: {exc}",
                raw_response=raw_response,
                components=[measurement],
            ) from exc
        return AdapterResponse(receipt=receipt, raw_response=raw_response, components=[measurement])
