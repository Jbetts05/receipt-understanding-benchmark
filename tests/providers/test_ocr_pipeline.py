"""Tests for OCR-pipeline composition (extractor + shared normalizer)."""

from __future__ import annotations

from typing import Any

import httpx
import openai
import pytest

from receipt_bench.models import ComponentMeasurement, Usage
from receipt_bench.providers.base import InvalidSolutionOutput, ProviderResponseError
from receipt_bench.providers.normalizer import StructuredOutputNormalizer
from receipt_bench.providers.ocr_pipeline import OcrExtraction, OcrPipelineAdapter
from tests.conftest import build_ground_truth_receipt
from tests.providers.conftest import FakeOpenAIClient, make_chat_completion


class FakeOcrExtractor:
    """A scripted ``OcrTextExtractor`` recording the calls it receives."""

    def __init__(self, extraction: OcrExtraction) -> None:
        self._extraction = extraction
        self.calls: list[tuple[bytes, str]] = []

    def extract_text(self, image_bytes: bytes, media_type: str) -> OcrExtraction:
        self.calls.append((image_bytes, media_type))
        return self._extraction


def test_execute_records_separate_ocr_and_normalize_measurements(
    receipt_schema: dict[str, Any],
) -> None:
    ocr_extraction = OcrExtraction(
        text="Contoso Cafe\nTotal $12.00",
        raw_response={"ocr": "payload"},
        measurement=ComponentMeasurement(
            component="ocr", latency_ms=11.0, usage=Usage(pages=1)
        ),
    )
    extractor = FakeOcrExtractor(ocr_extraction)

    receipt_json = build_ground_truth_receipt().model_dump_json()
    openai_client = FakeOpenAIClient([make_chat_completion(receipt_json)])
    normalizer = StructuredOutputNormalizer(
        client=openai_client,  # type: ignore[arg-type]
        deployment="receipt-gpt-54-mini",
        reasoning_effort="low",
    )

    adapter = OcrPipelineAdapter(
        "mistral-document-ai-2512",
        extractor=extractor,
        normalizer=normalizer,
        schema=receipt_schema,
    )

    response = adapter.execute(b"fake-image-bytes", "image/png")

    assert response.receipt == build_ground_truth_receipt()
    assert extractor.calls == [(b"fake-image-bytes", "image/png")]
    assert [component.component for component in response.components] == ["ocr", "normalize"]
    assert response.components[0].latency_ms == 11.0
    assert response.raw_response["ocr"] == {"ocr": "payload"}
    assert isinstance(response.raw_response["normalization"], dict)


def test_execute_raises_invalid_solution_output_with_both_ocr_and_normalizer_context(
    receipt_schema: dict[str, Any],
) -> None:
    ocr_extraction = OcrExtraction(
        text="Contoso Cafe\nTotal $12.00",
        raw_response={"ocr": "payload"},
        measurement=ComponentMeasurement(
            component="ocr", latency_ms=11.0, usage=Usage(pages=1)
        ),
    )
    extractor = FakeOcrExtractor(ocr_extraction)

    openai_client = FakeOpenAIClient([make_chat_completion("not valid json")])
    normalizer = StructuredOutputNormalizer(
        client=openai_client,  # type: ignore[arg-type]
        deployment="receipt-gpt-54-mini",
        reasoning_effort="low",
    )

    adapter = OcrPipelineAdapter(
        "mistral-document-ai-2512",
        extractor=extractor,
        normalizer=normalizer,
        schema=receipt_schema,
    )

    with pytest.raises(InvalidSolutionOutput) as exc_info:
        adapter.execute(b"fake-image-bytes", "image/png")

    exc = exc_info.value
    assert exc.raw_response["ocr"] == {"ocr": "payload"}
    assert isinstance(exc.raw_response["normalization"], dict)
    assert [component.component for component in exc.components] == ["ocr", "normalize"]
    assert exc.components[0].latency_ms == 11.0
    assert isinstance(exc.__cause__, InvalidSolutionOutput)


def test_execute_reraises_normalizer_transport_failure_with_ocr_context_preserved(
    receipt_schema: dict[str, Any],
) -> None:
    ocr_extraction = OcrExtraction(
        text="Contoso Cafe\nTotal $12.00",
        raw_response={"ocr": "payload"},
        measurement=ComponentMeasurement(
            component="ocr", latency_ms=11.0, usage=Usage(pages=1)
        ),
    )
    extractor = FakeOcrExtractor(ocr_extraction)

    sdk_error = openai.APIConnectionError(request=httpx.Request("POST", "https://example.com"))
    openai_client = FakeOpenAIClient([sdk_error])
    normalizer = StructuredOutputNormalizer(
        client=openai_client,  # type: ignore[arg-type]
        deployment="receipt-gpt-54-mini",
        reasoning_effort="low",
    )

    adapter = OcrPipelineAdapter(
        "mistral-document-ai-2512",
        extractor=extractor,
        normalizer=normalizer,
        schema=receipt_schema,
    )

    with pytest.raises(ProviderResponseError, match="normalizer call failed") as exc_info:
        adapter.execute(b"fake-image-bytes", "image/png")

    exc = exc_info.value
    # The OCR call succeeded and must not be discarded even though normalization
    # never produced a usable response.
    assert exc.raw_response is not None
    assert exc.raw_response["ocr"] == {"ocr": "payload"}
    assert exc.raw_response["normalization"]["error_type"] == "ProviderResponseError"
    assert exc.components == [ocr_extraction.measurement]
    assert isinstance(exc.__cause__, ProviderResponseError)
    assert exc.__cause__.__cause__ is sdk_error
