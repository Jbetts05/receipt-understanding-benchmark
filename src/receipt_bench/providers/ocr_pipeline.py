"""OCR-pipeline composition: an OCR text extractor feeding a shared normalizer.

Used by every ``ocr_pipeline`` solution (Mistral OCR/Document AI, Azure Vision Read,
Content Understanding read/layout). Each pipeline records two separate
``ComponentMeasurement`` entries: one for the OCR/ingestion call and one for the
configured normalization call, so latency and usage are attributed independently.

If the normalizer stage fails with a ``ProviderResponseError`` (a transport/provider
failure, not an invalid model output) after the OCR call already succeeded, that
already-incurred OCR raw response/measurement must not be discarded: a new
``ProviderResponseError`` is re-raised carrying both, chained from the original.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from receipt_bench.models import ComponentMeasurement
from receipt_bench.providers.base import (
    AdapterResponse,
    InvalidSolutionOutput,
    ProviderResponseError,
    SolutionAdapter,
)
from receipt_bench.providers.normalizer import StructuredOutputNormalizer


@dataclass(frozen=True)
class OcrExtraction:
    """The result of one OCR/ingestion call: ordered text plus its own measurement."""

    text: str
    raw_response: dict[str, Any]
    measurement: ComponentMeasurement


class OcrTextExtractor(Protocol):
    """Anything that can turn a receipt image into ordered OCR text."""

    def extract_text(self, image_bytes: bytes, media_type: str) -> OcrExtraction: ...


class OcrPipelineAdapter(SolutionAdapter):
    """Compose an OCR text extractor with the shared configured normalizer."""

    def __init__(
        self,
        solution_id: str,
        *,
        extractor: OcrTextExtractor,
        normalizer: StructuredOutputNormalizer,
        schema: dict[str, Any],
    ) -> None:
        super().__init__(solution_id)
        self._extractor = extractor
        self._normalizer = normalizer
        self._schema = dict(schema)

    def execute(self, image_bytes: bytes, media_type: str) -> AdapterResponse:
        extraction = self._extractor.extract_text(image_bytes, media_type)
        try:
            receipt, normalizer_raw, normalizer_measurement = self._normalizer.normalize(
                extraction.text, self._schema
            )
        except InvalidSolutionOutput as exc:
            raise InvalidSolutionOutput(
                str(exc),
                raw_response={
                    "ocr": extraction.raw_response,
                    "normalization": exc.raw_response,
                },
                components=[extraction.measurement, *exc.components],
            ) from exc
        except ProviderResponseError as exc:
            # The OCR call already succeeded and incurred real latency/usage; a
            # transport/provider failure in the normalizer stage must not discard
            # that already-completed measurement/raw response.
            raise ProviderResponseError(
                f"normalize: normalizer call failed after a successful OCR call: {exc}",
                raw_response={
                    "ocr": extraction.raw_response,
                    "normalization": {"error": str(exc), "error_type": type(exc).__name__},
                },
                components=[extraction.measurement],
            ) from exc
        raw_response: dict[str, Any] = {
            "ocr": extraction.raw_response,
            "normalization": normalizer_raw,
        }
        components = [extraction.measurement, normalizer_measurement]
        return AdapterResponse(receipt=receipt, raw_response=raw_response, components=components)
