"""Shared fixtures for receipt_bench evaluation/reporting/CLI tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from receipt_bench.models import CanonicalReceipt, ComponentMeasurement, ReceiptSample, Usage
from receipt_bench.providers.base import AdapterResponse, SolutionAdapter

ROOT = Path(__file__).resolve().parents[1]

# A single 1x1 PNG's worth of arbitrary bytes: fine for tests, no real image needed since
# nothing decodes it, only checksums and passes it around as opaque bytes.
_FAKE_IMAGE_BYTES = b"fake-receipt-image-bytes"


@pytest.fixture
def receipt_schema() -> dict[str, Any]:
    """The committed canonical receipt JSON Schema."""
    schema: dict[str, Any] = json.loads(
        (ROOT / "schemas" / "expense-receipt.schema.json").read_text("utf-8")
    )
    return schema


def build_ground_truth_receipt(**overrides: Any) -> CanonicalReceipt:
    """Build a fully-populated, schema-valid ``CanonicalReceipt`` for tests."""
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "merchant": {
            "name": "Contoso Cafe",
            "address": "1 Main St",
            "phone": "555-0100",
            "tax_id": "TX-123",
        },
        "transaction": {
            "date": "2026-08-28",
            "time": "12:15:00",
            "receipt_number": "R-42",
            "currency": "GBP",
            "country": "GB",
        },
        "amounts": {
            "subtotal": 10.00,
            "discount": 0.0,
            "tax": 2.00,
            "tip": 0.0,
            "total": 12.00,
        },
        "payment_methods": [{"method": "visa", "amount": 12.00, "card_last4": "1234"}],
        "receipt_type": "meal",
        "line_items": [
            {
                "description": "Coffee",
                "product_code": None,
                "quantity": 2.0,
                "unit": "each",
                "unit_price": 3.0,
                "discount": None,
                "tax": None,
                "total_price": 6.0,
            },
            {
                "description": "Bagel",
                "product_code": None,
                "quantity": 1.0,
                "unit": "each",
                "unit_price": 4.0,
                "discount": None,
                "tax": None,
                "total_price": 4.0,
            },
        ],
    }
    payload.update(overrides)
    return CanonicalReceipt.model_validate(payload)


@pytest.fixture
def ground_truth_receipt() -> CanonicalReceipt:
    return build_ground_truth_receipt()


@pytest.fixture
def make_sample(tmp_path: Path) -> Callable[..., ReceiptSample]:
    """Factory fixture that writes a fake image file and returns a matching ReceiptSample."""

    def _make(
        sample_id: str = "sample-001",
        dataset: str = "unit-test",
        split: str = "test",
        labeled_fields: list[str] | None = None,
        ground_truth: CanonicalReceipt | None = None,
        image_bytes: bytes = _FAKE_IMAGE_BYTES,
    ) -> ReceiptSample:
        image_path = tmp_path / f"{sample_id}.png"
        image_path.write_bytes(image_bytes)
        digest = hashlib.sha256(image_bytes).hexdigest()
        return ReceiptSample(
            sample_id=sample_id,
            dataset=dataset,
            split=split,
            image_path=image_path.name,
            image_sha256=digest,
            source_language="en",
            ground_truth=ground_truth if ground_truth is not None else build_ground_truth_receipt(),
            labeled_fields=(
                labeled_fields
                if labeled_fields is not None
                else [
                    "merchant.name",
                    "amounts.total",
                    "transaction.date",
                    "line_items",
                    "payment_methods",
                ]
            ),
        )

    return _make


@pytest.fixture
def manifest_path(tmp_path: Path, make_sample: Callable[..., ReceiptSample]) -> Path:
    """A manifest JSONL file (in ``tmp_path``) containing two samples."""
    sample_one = make_sample(sample_id="sample-001")
    sample_two = make_sample(sample_id="sample-002")
    path = tmp_path / "manifest.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        handle.write(sample_one.model_dump_json())
        handle.write("\n")
        handle.write(sample_two.model_dump_json())
        handle.write("\n")
    return path


class FakeAdapter(SolutionAdapter):
    """A deterministic, no-network ``SolutionAdapter`` for tests.

    Records every call it receives and returns a scripted sequence of responses (or
    raises a scripted exception) so tests never make real, potentially paid, provider
    calls.
    """

    def __init__(
        self,
        solution_id: str,
        responses: list[AdapterResponse | Exception] | None = None,
        default_receipt: CanonicalReceipt | None = None,
    ) -> None:
        super().__init__(solution_id)
        self.calls: list[tuple[bytes, str]] = []
        self._responses = list(responses) if responses is not None else None
        self._default_receipt = default_receipt

    def execute(self, image_bytes: bytes, media_type: str) -> AdapterResponse:
        self.calls.append((image_bytes, media_type))
        if self._responses is not None:
            if not self._responses:
                raise AssertionError(f"{self.solution_id}: no more scripted responses")
            next_item = self._responses.pop(0)
            if isinstance(next_item, Exception):
                raise next_item
            return next_item

        receipt = (
            self._default_receipt
            if self._default_receipt is not None
            else build_ground_truth_receipt()
        )
        return AdapterResponse(
            receipt=receipt,
            raw_response={"ok": True},
            components=[
                ComponentMeasurement(
                    component="extraction",
                    latency_ms=42.0,
                    usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
                    estimated_cost=0.01,
                )
            ],
        )


__all__ = [
    "FakeAdapter",
    "build_ground_truth_receipt",
]
