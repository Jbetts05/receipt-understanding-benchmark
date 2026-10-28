"""Tests for the shared adapter exception contracts in ``providers/base.py``."""

from __future__ import annotations

from receipt_bench.models import ComponentMeasurement, Usage
from receipt_bench.providers.base import ProviderResponseError


def test_provider_response_error_defaults_to_no_raw_response_or_components() -> None:
    """The common case: a hard failure before any usable response exists."""
    exc = ProviderResponseError("chat completion call failed: boom")

    assert exc.raw_response is None
    assert exc.components == []
    assert str(exc) == "chat completion call failed: boom"


def test_provider_response_error_carries_explicit_raw_response_and_components() -> None:
    """The multi-stage case: an earlier stage succeeded before a later one failed."""
    measurement = ComponentMeasurement(component="ocr", latency_ms=11.0, usage=Usage(pages=1))

    exc = ProviderResponseError(
        "normalize: normalizer call failed after a successful OCR call",
        raw_response={"ocr": {"markdown": "..."}, "normalization": {"error": "boom"}},
        components=[measurement],
    )

    assert exc.raw_response == {"ocr": {"markdown": "..."}, "normalization": {"error": "boom"}}
    assert exc.components == [measurement]
