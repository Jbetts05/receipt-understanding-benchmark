"""Mistral OCR / Document AI adapter for the OCR-pipeline solutions.

Calls Foundry's Mistral provider route (``/providers/mistral/azure/ocr``) via
``httpx`` with base64 image data and the Azure deployment name as the ``model``.
Preserves the full JSON response and extracts ordered markdown across pages for
the shared normalizer. Authentication is configured on the shared client as either
the legacy API-key bearer header or a refreshing Entra bearer token.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from receipt_bench.models import ComponentMeasurement, Usage
from receipt_bench.prompting import image_data_uri
from receipt_bench.providers._timing import elapsed_ms
from receipt_bench.providers.base import ProviderResponseError
from receipt_bench.providers.ocr_pipeline import OcrExtraction

_OCR_PATH = "/providers/mistral/azure/ocr"


class MistralOcrExtractor:
    """Native Mistral OCR/Document AI extraction via direct REST."""

    def __init__(self, *, client: httpx.Client, deployment: str) -> None:
        self._client = client
        self._deployment = deployment

    def extract_text(self, image_bytes: bytes, media_type: str) -> OcrExtraction:
        request_body = {
            "model": self._deployment,
            "document": {
                "type": "image_url",
                "image_url": image_data_uri(image_bytes, media_type),
            },
        }
        start = time.monotonic()
        try:
            response = self._client.post(_OCR_PATH, json=request_body)
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        except httpx.HTTPError as exc:
            raise ProviderResponseError(f"mistral-ocr: request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderResponseError(
                f"mistral-ocr: response body was not valid JSON: {exc}"
            ) from exc
        latency_ms = elapsed_ms(start)

        pages = payload.get("pages")
        if not pages:
            raise ProviderResponseError("mistral-ocr: response contained no pages")
        ordered_pages = sorted(pages, key=lambda page: page.get("index", 0))
        markdown_segments = []
        for page in ordered_pages:
            markdown = page.get("markdown")
            if not markdown:
                raise ProviderResponseError("mistral-ocr: a page response had no markdown content")
            markdown_segments.append(markdown)
        text = "\n\n".join(markdown_segments)

        usage_payload = payload.get("usage") or {}
        usage = Usage(
            total_tokens=usage_payload.get("total_tokens"),
            pages=len(pages),
            provider_metadata=usage_payload,
        )
        measurement = ComponentMeasurement(component="ocr", latency_ms=latency_ms, usage=usage)
        return OcrExtraction(text=text, raw_response=payload, measurement=measurement)
