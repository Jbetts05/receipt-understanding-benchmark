"""Tests for the Mistral OCR/Document AI Foundry adapter."""

from __future__ import annotations

import httpx
import pytest
import respx

from receipt_bench.providers.base import ProviderResponseError
from receipt_bench.providers.mistral_ocr import MistralOcrExtractor

_BASE_URL = "https://fake-account.services.ai.azure.com"


def _client() -> httpx.Client:
    return httpx.Client(base_url=_BASE_URL, headers={"Authorization": "Bearer fake-key"})


@respx.mock
def test_extract_text_orders_pages_and_joins_markdown() -> None:
    route = respx.post(f"{_BASE_URL}/providers/mistral/azure/ocr").mock(
        return_value=httpx.Response(
            200,
            json={
                "pages": [
                    {"index": 1, "markdown": "second page"},
                    {"index": 0, "markdown": "first page"},
                ],
                "usage": {"total_tokens": 42},
            },
        )
    )
    extractor = MistralOcrExtractor(client=_client(), deployment="receipt-mistral-ocr-4")

    extraction = extractor.extract_text(b"fake-image-bytes", "image/png")

    assert extraction.text == "first page\n\nsecond page"
    assert extraction.measurement.component == "ocr"
    assert extraction.measurement.usage.total_tokens == 42
    assert extraction.measurement.usage.pages == 2
    assert route.called
    request_body = route.calls[0].request.content
    assert b"receipt-mistral-ocr-4" in request_body
    assert b"data:image/png;base64," in request_body


@respx.mock
def test_extract_text_raises_when_no_pages() -> None:
    respx.post(f"{_BASE_URL}/providers/mistral/azure/ocr").mock(
        return_value=httpx.Response(200, json={"pages": []})
    )
    extractor = MistralOcrExtractor(client=_client(), deployment="receipt-mistral-ocr-4")

    with pytest.raises(ProviderResponseError, match="no pages"):
        extractor.extract_text(b"fake-image-bytes", "image/png")


@respx.mock
def test_extract_text_raises_when_page_has_no_markdown() -> None:
    respx.post(f"{_BASE_URL}/providers/mistral/azure/ocr").mock(
        return_value=httpx.Response(200, json={"pages": [{"index": 0, "markdown": ""}]})
    )
    extractor = MistralOcrExtractor(client=_client(), deployment="receipt-mistral-ocr-4")

    with pytest.raises(ProviderResponseError, match="no markdown"):
        extractor.extract_text(b"fake-image-bytes", "image/png")


@respx.mock
def test_extract_text_raises_provider_response_error_on_http_error_status() -> None:
    respx.post(f"{_BASE_URL}/providers/mistral/azure/ocr").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    extractor = MistralOcrExtractor(client=_client(), deployment="receipt-mistral-ocr-4")

    with pytest.raises(ProviderResponseError, match="request failed") as exc_info:
        extractor.extract_text(b"fake-image-bytes", "image/png")
    assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)


@respx.mock
def test_extract_text_raises_provider_response_error_on_invalid_json_body() -> None:
    respx.post(f"{_BASE_URL}/providers/mistral/azure/ocr").mock(
        return_value=httpx.Response(
            200, content=b"not json", headers={"content-type": "text/plain"}
        )
    )
    extractor = MistralOcrExtractor(client=_client(), deployment="receipt-mistral-ocr-4")

    with pytest.raises(ProviderResponseError, match="not valid JSON"):
        extractor.extract_text(b"fake-image-bytes", "image/png")
