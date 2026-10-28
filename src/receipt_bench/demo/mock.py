from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from receipt_bench.demo.analysis import field_comparisons
from receipt_bench.demo.api_models import (
    ComponentView,
    DemoSolution,
    ErrorView,
    SolutionResult,
    TerminalStatus,
)
from receipt_bench.demo.domain import DemoInput
from receipt_bench.evaluation.pricing import (
    PricingSnapshot,
    estimate_component_cost,
    rate_for,
)
from receipt_bench.evaluation.scoring import check_arithmetic_consistency, validate_schema
from receipt_bench.models import (
    CanonicalReceipt,
    ComponentMeasurement,
    LineItem,
    SolutionOutput,
    Usage,
)

_REFERENCE_TIME = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
_EXACT_SOLUTIONS = {
    "gpt-5.6-sol",
    "content-understanding-custom",
    "document-intelligence-receipt",
}


def _seed(input_receipt: DemoInput, solution_id: str) -> int:
    digest = hashlib.sha256(f"{input_receipt.sha256}:{solution_id}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big")


def delay_seconds(input_receipt: DemoInput, solution_id: str, scale: float) -> float:
    return (0.04 + (_seed(input_receipt, solution_id) % 12) * 0.012) * scale


def _upload_receipt(input_receipt: DemoInput) -> CanonicalReceipt:
    seed = int(input_receipt.sha256[:8], 16)
    first = float(700 + seed % 900)
    second = float(300 + (seed // 17) % 500)
    subtotal = first + second
    tax = round(subtotal * 0.1, 2)
    return CanonicalReceipt.model_validate(
        {
            "schema_version": "1.0",
            "merchant": {
                "name": "Mock Corner Market",
                "address": "Synthetic result — not read from upload",
                "phone": None,
                "tax_id": None,
            },
            "transaction": {
                "date": "2026-08-29",
                "time": "12:00:00",
                "receipt_number": f"MOCK-{input_receipt.sha256[:6].upper()}",
                "currency": "USD",
                "country": "US",
            },
            "amounts": {
                "subtotal": subtotal,
                "discount": 0.0,
                "tax": tax,
                "tip": 0.0,
                "total": subtotal + tax,
            },
            "payment_methods": None,
            "receipt_type": "mock-upload",
            "line_items": [
                {
                    "description": "Synthetic item A",
                    "product_code": None,
                    "quantity": 1.0,
                    "unit": None,
                    "unit_price": first,
                    "discount": None,
                    "tax": None,
                    "total_price": first,
                },
                {
                    "description": "Synthetic item B",
                    "product_code": None,
                    "quantity": 1.0,
                    "unit": None,
                    "unit_price": second,
                    "discount": None,
                    "tax": None,
                    "total_price": second,
                },
            ],
        }
    )


def _base_receipt(input_receipt: DemoInput) -> CanonicalReceipt:
    if input_receipt.sample is not None:
        return input_receipt.sample.ground_truth.model_copy(deep=True)
    return _upload_receipt(input_receipt)


def _mutate_receipt(base: CanonicalReceipt, solution_id: str) -> CanonicalReceipt:
    if solution_id in _EXACT_SOLUTIONS:
        return base

    payload = base.model_dump(mode="json")
    items = payload.get("line_items")
    if not isinstance(items, list):
        items = []
        payload["line_items"] = items

    if solution_id == "gpt-5.6-terra" and items:
        items[0]["description"] = f"{items[0].get('description') or 'Item'} (unclear)"
    elif solution_id == "gpt-5.6-luna":
        payload["amounts"]["total"] = None
    elif solution_id == "gpt-5.4-mini":
        subtotal = payload["amounts"].get("subtotal")
        if isinstance(subtotal, int | float):
            payload["amounts"]["subtotal"] = float(subtotal) + 1000.0
    elif solution_id == "gpt-5.2":
        items.append(
            LineItem(description="MOCK EXTRA ITEM", quantity=1.0, total_price=500.0).model_dump(
                mode="json"
            )
        )
    elif solution_id == "grok-4.6":
        total = payload["amounts"].get("total")
        if isinstance(total, int | float):
            payload["amounts"]["total"] = float(total) + 500.0
    elif solution_id == "mistral-large-3" and items:
        items[0]["quantity"] = None
    elif solution_id == "content-understanding-prebuilt-receipt" and items:
        items[0]["description"] = "Partially recognized item"
    elif solution_id == "mistral-document-ai-2512" and items:
        items[-1]["total_price"] = None
    elif solution_id == "azure-vision-read" and items:
        items[0]["quantity"] = None
    elif solution_id == "content-understanding-read":
        payload["amounts"]["tax"] = None
    elif solution_id == "content-understanding-layout" and items:
        items[0]["description"] = str(items[0].get("description") or "").replace(" ", "")

    return CanonicalReceipt.model_validate(payload)


def _measurements(solution: DemoSolution, seed: int) -> list[ComponentMeasurement]:
    latency = float(240 + seed % 980)
    input_tokens = 780 + seed % 620
    output_tokens = 110 + seed % 190

    if solution.kind == "ocr_pipeline":
        return [
            ComponentMeasurement(
                component="ocr",
                latency_ms=round(latency * 0.46, 1),
                usage=Usage(pages=1),
            ),
            ComponentMeasurement(
                component="normalize",
                latency_ms=round(latency * 0.54, 1),
                usage=Usage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                ),
            ),
        ]
    if solution.api == "content-understanding":
        return [
            ComponentMeasurement(
                component="native_extraction",
                latency_ms=round(latency, 1),
                usage=Usage(pages=1, contextualization_tokens=80 + seed % 70),
            ),
            ComponentMeasurement(
                component="model:gpt-5.4-mini",
                latency_ms=0.0,
                usage=Usage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                ),
            ),
        ]
    if solution.kind == "native":
        return [
            ComponentMeasurement(
                component="native_extraction",
                latency_ms=round(latency, 1),
                usage=Usage(pages=1),
            )
        ]
    return [
        ComponentMeasurement(
            component="direct_extraction",
            latency_ms=round(latency, 1),
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
        )
    ]


def _price_components(
    solution: DemoSolution,
    measurements: list[ComponentMeasurement],
    pricing: PricingSnapshot | None,
) -> tuple[list[ComponentView], float | None]:
    if not measurements:
        return [], None
    views: list[ComponentView] = []
    costs: list[float | None] = []
    for measurement in measurements:
        rate = rate_for(
            pricing,
            solution.id,
            measurement.component,
            normalizer_model=solution.normalizer,
        )
        cost = estimate_component_cost(rate, measurement.usage)
        costs.append(cost)
        views.append(
            ComponentView(
                component=measurement.component,
                latency_ms=measurement.latency_ms,
                usage=measurement.usage,
                estimated_cost_usd=cost,
            )
        )
    total = None if any(cost is None for cost in costs) else sum(cost for cost in costs if cost)
    return views, total


def _terminal_mock_error(
    input_receipt: DemoInput, solution: DemoSolution
) -> tuple[TerminalStatus, ErrorView] | None:
    if input_receipt.receipt_id == "challenging-noisy" and solution.id == "grok-4.6":
        return (
            "failed",
            ErrorView(
                code="mock_provider_timeout",
                message="Mock provider timeout. No live request was made.",
            ),
        )
    if input_receipt.receipt_id == "challenging-noisy" and solution.id == "mistral-ocr-4.0":
        return (
            "invalid",
            ErrorView(
                code="mock_invalid_output",
                message="Mock normalizer returned non-canonical JSON after OCR.",
            ),
        )
    return None


def build_mock_result(
    *,
    run_id: str,
    input_receipt: DemoInput,
    solution: DemoSolution,
    schema: dict[str, Any],
    pricing: PricingSnapshot | None,
) -> SolutionResult:
    seed = _seed(input_receipt, solution.id)
    measurements = _measurements(solution, seed)
    terminal_error = _terminal_mock_error(input_receipt, solution)
    if terminal_error is not None:
        status, error = terminal_error
        if status == "failed":
            measurements = []
        elif measurements:
            measurements = measurements[:1]
        components, cost = _price_components(solution, measurements, pricing)
        latency = sum(component.latency_ms for component in components)
        return SolutionResult(
            receipt_id=input_receipt.receipt_id,
            sample_id=input_receipt.sample_id,
            solution_id=solution.id,
            solution_order=solution.order,
            status=status,
            mock=True,
            schema_valid=False,
            latency_ms=round(latency, 1),
            estimated_cost_usd=cost,
            components=components,
            error=error,
        )

    receipt = _mutate_receipt(_base_receipt(input_receipt), solution.id)
    components, cost = _price_components(solution, measurements, pricing)
    latency = round(sum(component.latency_ms for component in components) + 12.0, 1)
    output = SolutionOutput(
        run_id=run_id,
        sample_id=input_receipt.sample_id,
        solution_id=solution.id,
        started_at=_REFERENCE_TIME,
        completed_at=_REFERENCE_TIME,
        status="succeeded",
        receipt=receipt,
        raw_artifact="",
        components=measurements,
    )
    comparisons = (
        field_comparisons(input_receipt.sample, output, schema)
        if input_receipt.sample is not None
        else []
    )
    return SolutionResult(
        receipt_id=input_receipt.receipt_id,
        sample_id=input_receipt.sample_id,
        solution_id=solution.id,
        solution_order=solution.order,
        status="succeeded",
        mock=True,
        schema_valid=validate_schema(receipt, schema),
        arithmetic_consistent=check_arithmetic_consistency(receipt),
        latency_ms=latency,
        estimated_cost_usd=cost,
        canonical_result=receipt,
        components=components,
        field_comparisons=comparisons,
    )


__all__ = ["build_mock_result", "delay_seconds"]
