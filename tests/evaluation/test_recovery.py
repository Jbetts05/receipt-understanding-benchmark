from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from receipt_bench.evaluation.recovery import (
    RecoveryError,
    recover_format_invalid_outputs,
    remap_prebuilt_receipt_outputs,
)
from receipt_bench.models import CanonicalReceipt, ComponentMeasurement, SolutionOutput, Usage


def _invalid_output(
    *,
    solution_id: str,
    raw_artifact: Path,
    sample_id: str = "sample-1",
) -> SolutionOutput:
    now = datetime.now(UTC)
    return SolutionOutput(
        run_id="base-run",
        sample_id=sample_id,
        solution_id=solution_id,
        started_at=now,
        completed_at=now,
        status="invalid",
        raw_artifact=str(raw_artifact),
        error_type="InvalidSolutionOutput",
        error_message="currency RM does not match the strict schema",
        attempts=1,
        components=[
            ComponentMeasurement(
                component="normalize",
                latency_ms=123.0,
                usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
            )
        ],
    )


def _write_results(path: Path, outputs: list[SolutionOutput]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(output.model_dump_json() + "\n" for output in outputs),
        encoding="utf-8",
    )


def _canonical_payload(currency: str = "RM") -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "merchant": {"name": "Cafe", "address": None, "phone": None, "tax_id": None},
        "transaction": {
            "date": "2026-08-29",
            "time": None,
            "receipt_number": None,
            "currency": currency,
            "country": "MY",
        },
        "amounts": {
            "subtotal": None,
            "discount": None,
            "tax": None,
            "tip": None,
            "total": 12.0,
        },
        "payment_methods": None,
        "receipt_type": None,
        "line_items": None,
    }


def test_recovers_cached_normalizer_output_without_losing_measurements(
    tmp_path: Path, receipt_schema: dict[str, Any]
) -> None:
    raw_path = tmp_path / "raw.json"
    raw_path.write_text(
        json.dumps(
            {
                "ocr": {"text": "TOTAL 12.00"},
                "normalization": {
                    "choices": [
                        {"message": {"content": json.dumps(_canonical_payload())}}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    original = _invalid_output(
        solution_id="content-understanding-read", raw_artifact=raw_path
    )
    base_path = tmp_path / "base" / "results.jsonl"
    overlay_path = tmp_path / "overlay" / "results.jsonl"
    _write_results(base_path, [original])

    recovered = recover_format_invalid_outputs(
        base_results_path=base_path,
        overlay_results_path=overlay_path,
        overlay_run_id="recovered-run",
        schema=receipt_schema,
    )

    assert len(recovered) == 1
    output = recovered[0]
    assert output.run_id == "recovered-run"
    assert output.status == "succeeded"
    assert output.receipt is not None
    assert output.receipt.transaction.currency == "RM"
    assert output.components == original.components
    assert output.started_at == original.started_at
    assert output.raw_artifact == original.raw_artifact
    assert output.error_type is None
    assert output.error_message is None
    assert overlay_path.read_text(encoding="utf-8").count("\n") == 1


def test_recovers_content_understanding_custom_typed_fields(
    tmp_path: Path, receipt_schema: dict[str, Any]
) -> None:
    raw_path = tmp_path / "custom.json"
    raw_path.write_text(
        json.dumps(
            {
                "status": "Succeeded",
                "result": {
                    "contents": [
                        {
                            "fields": {
                                "Merchant": {
                                    "type": "object",
                                    "valueObject": {
                                        "Name": {
                                            "type": "string",
                                            "valueString": "Cafe",
                                        }
                                    },
                                },
                                "Transaction": {
                                    "type": "object",
                                    "valueObject": {
                                        "Currency": {
                                            "type": "string",
                                            "valueString": "SR",
                                        }
                                    },
                                },
                                "Amounts": {
                                    "type": "object",
                                    "valueObject": {
                                        "Total": {
                                            "type": "number",
                                            "valueNumber": 12.0,
                                        }
                                    },
                                },
                            }
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    base_path = tmp_path / "base" / "results.jsonl"
    overlay_path = tmp_path / "overlay" / "results.jsonl"
    _write_results(
        base_path,
        [_invalid_output(solution_id="content-understanding-custom", raw_artifact=raw_path)],
    )

    recovered = recover_format_invalid_outputs(
        base_results_path=base_path,
        overlay_results_path=overlay_path,
        overlay_run_id="recovered-run",
        schema=receipt_schema,
    )

    assert recovered[0].receipt is not None
    assert recovered[0].receipt.merchant.name == "Cafe"
    assert recovered[0].receipt.transaction.currency == "SR"
    assert recovered[0].receipt.amounts.total == 12.0


def test_refuses_unsupported_invalid_solution_and_leaves_no_partial_overlay(
    tmp_path: Path, receipt_schema: dict[str, Any]
) -> None:
    raw_path = tmp_path / "raw.json"
    raw_path.write_text("{}", encoding="utf-8")
    base_path = tmp_path / "base" / "results.jsonl"
    overlay_path = tmp_path / "overlay" / "results.jsonl"
    _write_results(
        base_path,
        [_invalid_output(solution_id="unknown-solution", raw_artifact=raw_path)],
    )

    with pytest.raises(RecoveryError, match="Unsupported invalid solution"):
        recover_format_invalid_outputs(
            base_results_path=base_path,
            overlay_results_path=overlay_path,
            overlay_run_id="recovered-run",
            schema=receipt_schema,
        )

    assert not overlay_path.exists()


def test_refuses_to_overwrite_existing_overlay(
    tmp_path: Path, receipt_schema: dict[str, Any]
) -> None:
    base_path = tmp_path / "base" / "results.jsonl"
    overlay_path = tmp_path / "overlay" / "results.jsonl"
    overlay_path.parent.mkdir(parents=True)
    overlay_path.write_text("existing\n", encoding="utf-8")

    with pytest.raises(RecoveryError, match="Refusing to overwrite"):
        recover_format_invalid_outputs(
            base_results_path=base_path,
            overlay_results_path=overlay_path,
            overlay_run_id="recovered-run",
            schema=receipt_schema,
        )


def test_remaps_cached_prebuilt_receipt_with_tax_detail_fallback(
    tmp_path: Path,
) -> None:
    raw_path = tmp_path / "prebuilt.json"

    def money(amount: float | None) -> dict[str, Any]:
        return {
            "type": "object",
            "valueObject": {
                "CurrencyCode": {"type": "string", "valueString": "USD"},
                "Amount": {"type": "number", "valueNumber": amount},
            },
        }

    raw_path.write_text(
        json.dumps(
            {
                "status": "Succeeded",
                "result": {
                    "contents": [
                        {
                            "category": "receipt.generic",
                            "fields": {
                                "TotalAmount": money(12.0),
                                "TotalTaxAmount": money(None),
                                "TaxDetails": {
                                    "type": "array",
                                    "valueArray": [
                                        {
                                            "type": "object",
                                            "valueObject": {"Amount": money(2.0)},
                                        }
                                    ],
                                },
                            },
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    now = datetime.now(UTC)
    original = SolutionOutput(
        run_id="correction-run",
        sample_id="sample-1",
        solution_id="content-understanding-prebuilt-receipt",
        started_at=now,
        completed_at=now,
        status="succeeded",
        receipt=CanonicalReceipt(),
        raw_artifact=str(raw_path),
    )
    source_path = tmp_path / "source" / "results.jsonl"
    overlay_path = tmp_path / "overlay" / "results.jsonl"
    _write_results(source_path, [original])

    remapped = remap_prebuilt_receipt_outputs(
        source_results_path=source_path,
        overlay_results_path=overlay_path,
        overlay_run_id="tax-remap",
    )

    assert len(remapped) == 1
    assert remapped[0].receipt is not None
    assert remapped[0].receipt.amounts.tax == 2.0
    assert remapped[0].receipt.amounts.total == 12.0
    assert remapped[0].run_id == "tax-remap"
