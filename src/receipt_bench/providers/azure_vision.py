"""Azure Vision Read OCR extractor for the OCR-pipeline solutions.

Uses ``ImageAnalysisClient`` with bytes input and the ``READ`` visual feature.
Produces ordered OCR text (no per-token usage is available from this API, since it
is a flat per-transaction analysis call rather than a token-metered model call).
"""

from __future__ import annotations

import time

from azure.ai.vision.imageanalysis import ImageAnalysisClient
from azure.ai.vision.imageanalysis.models import VisualFeatures
from azure.core.exceptions import AzureError

from receipt_bench.models import ComponentMeasurement, Usage
from receipt_bench.providers._timing import elapsed_ms
from receipt_bench.providers.base import ProviderResponseError
from receipt_bench.providers.ocr_pipeline import OcrExtraction


class AzureVisionReadExtractor:
    """Native OCR extraction via Azure AI Vision's Read feature."""

    def __init__(self, *, client: ImageAnalysisClient) -> None:
        self._client = client

    def extract_text(self, image_bytes: bytes, media_type: str) -> OcrExtraction:
        start = time.monotonic()
        try:
            result = self._client.analyze(
                image_data=image_bytes, visual_features=[VisualFeatures.READ]
            )
        except AzureError as exc:
            raise ProviderResponseError(f"azure-vision: analyze call failed: {exc}") from exc
        latency_ms = elapsed_ms(start)

        if result.read is None:
            raise ProviderResponseError("azure-vision: response contained no read result")
        lines = [line.text for block in result.read.blocks for line in block.lines]
        text = "\n".join(lines)
        if not text.strip():
            raise ProviderResponseError("azure-vision: no text was detected in the image")

        raw_response = result.as_dict()
        usage = Usage(pages=1)
        measurement = ComponentMeasurement(component="ocr", latency_ms=latency_ms, usage=usage)
        return OcrExtraction(text=text, raw_response=raw_response, measurement=measurement)
