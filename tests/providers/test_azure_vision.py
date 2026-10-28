"""Tests for the Azure Vision Read OCR extractor."""

from __future__ import annotations

from typing import Any

import pytest
from azure.ai.vision.imageanalysis.models import (
    DetectedTextBlock,
    DetectedTextLine,
    ImageAnalysisResult,
    ReadResult,
)
from azure.core.exceptions import ServiceRequestError

from receipt_bench.providers.azure_vision import AzureVisionReadExtractor
from receipt_bench.providers.base import ProviderResponseError


class FakeImageAnalysisClient:
    def __init__(self, result: ImageAnalysisResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def analyze(self, **kwargs: Any) -> ImageAnalysisResult:
        self.calls.append(kwargs)
        return self._result


class FailingImageAnalysisClient:
    """Fakes a client whose call raises an azure-core SDK error immediately."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def analyze(self, **kwargs: Any) -> ImageAnalysisResult:
        raise self._error


def _read_result(lines: list[str]) -> ImageAnalysisResult:
    block = DetectedTextBlock(lines=[DetectedTextLine(text=line) for line in lines])
    return ImageAnalysisResult(
        model_version="2024-02-01", metadata={}, read=ReadResult(blocks=[block])
    )


def test_extract_text_joins_lines_in_order() -> None:
    result = _read_result(["Contoso Cafe", "Total $12.00"])
    client = FakeImageAnalysisClient(result)
    extractor = AzureVisionReadExtractor(client=client)  # type: ignore[arg-type]

    extraction = extractor.extract_text(b"fake-image-bytes", "image/png")

    assert extraction.text == "Contoso Cafe\nTotal $12.00"
    assert extraction.measurement.component == "ocr"
    assert extraction.measurement.usage.pages == 1
    assert isinstance(extraction.raw_response, dict)
    assert client.calls[0]["image_data"] == b"fake-image-bytes"


def test_extract_text_raises_when_no_read_result() -> None:
    result = ImageAnalysisResult(model_version="2024-02-01", metadata={}, read=None)
    client = FakeImageAnalysisClient(result)
    extractor = AzureVisionReadExtractor(client=client)  # type: ignore[arg-type]

    with pytest.raises(ProviderResponseError, match="no read result"):
        extractor.extract_text(b"fake-image-bytes", "image/png")


def test_extract_text_raises_when_no_text_detected() -> None:
    result = _read_result([])
    client = FakeImageAnalysisClient(result)
    extractor = AzureVisionReadExtractor(client=client)  # type: ignore[arg-type]

    with pytest.raises(ProviderResponseError, match="no text was detected"):
        extractor.extract_text(b"fake-image-bytes", "image/png")


def test_extract_text_wraps_sdk_errors_as_provider_response_error() -> None:
    sdk_error = ServiceRequestError("connection reset")
    client = FailingImageAnalysisClient(sdk_error)
    extractor = AzureVisionReadExtractor(client=client)  # type: ignore[arg-type]

    with pytest.raises(ProviderResponseError, match="analyze call failed") as exc_info:
        extractor.extract_text(b"fake-image-bytes", "image/png")
    assert exc_info.value.__cause__ is sdk_error
