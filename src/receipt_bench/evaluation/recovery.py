from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from receipt_bench.evaluation.results_io import read_results
from receipt_bench.models import CanonicalReceipt, SolutionOutput
from receipt_bench.prompting import parse_receipt_json
from receipt_bench.providers.base import ProviderResponseError
from receipt_bench.providers.content_understanding import (
    _decode_content_field,
    _first_content,
    _map_prebuilt_receipt_fields,
    map_expense_receipt_fields,
)

_NORMALIZER_SOLUTIONS = frozenset(
    {
        "content-understanding-read",
        "mistral-document-ai-2512",
        "mistral-ocr-4.0",
    }
)


class RecoveryError(RuntimeError):
    """Raised when an immutable invalid result cannot be recovered safely."""


def _read_raw_artifact(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RecoveryError(f"Cannot read raw artifact {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RecoveryError(f"Raw artifact {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RecoveryError(f"Raw artifact {path} must contain a JSON object")
    return payload


def _recover_custom_receipt(raw: dict[str, Any]) -> CanonicalReceipt:
    content = _first_content(raw)
    fields = content.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise RecoveryError("Content Understanding artifact contains no fields")
    decoded = {name: _decode_content_field(value) for name, value in fields.items()}
    return CanonicalReceipt.model_validate(map_expense_receipt_fields(decoded))


def _recover_prebuilt_receipt(raw: dict[str, Any]) -> CanonicalReceipt:
    content = _first_content(raw)
    fields = content.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise RecoveryError("Content Understanding artifact contains no fields")
    decoded = {name: _decode_content_field(value) for name, value in fields.items()}
    return CanonicalReceipt.model_validate(_map_prebuilt_receipt_fields(decoded, content))


def _recover_normalized_receipt(
    raw: dict[str, Any], schema: dict[str, Any]
) -> CanonicalReceipt:
    try:
        content = raw["normalization"]["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RecoveryError("Normalizer artifact contains no completion text") from exc
    if not isinstance(content, str):
        raise RecoveryError("Normalizer completion content must be a string")
    return parse_receipt_json(content, schema)


def recover_format_invalid_outputs(
    *,
    base_results_path: Path,
    overlay_results_path: Path,
    overlay_run_id: str,
    schema: dict[str, Any],
) -> list[SolutionOutput]:
    """Recover format-only invalid answers into a new immutable overlay run.

    The base run and its raw artifacts are never modified. Original timestamps,
    component measurements, attempts, and raw-artifact references are preserved;
    only the run ID, status, parsed receipt, and error fields change.
    """
    if overlay_results_path.exists():
        raise RecoveryError(f"Refusing to overwrite existing overlay: {overlay_results_path}")

    recovered: list[SolutionOutput] = []
    for output in read_results(base_results_path):
        if output.status != "invalid":
            continue
        raw = _read_raw_artifact(Path(output.raw_artifact))
        try:
            if output.solution_id == "content-understanding-custom":
                receipt = _recover_custom_receipt(raw)
            elif output.solution_id in _NORMALIZER_SOLUTIONS:
                receipt = _recover_normalized_receipt(raw, schema)
            else:
                raise RecoveryError(
                    f"Unsupported invalid solution {output.solution_id!r} "
                    f"for sample {output.sample_id!r}"
                )
        except (
            RecoveryError,
            ProviderResponseError,
            ValidationError,
            ValueError,
            TypeError,
        ) as exc:
            if isinstance(exc, RecoveryError):
                raise
            raise RecoveryError(
                f"Cannot recover {output.solution_id}/{output.sample_id}: {exc}"
            ) from exc

        recovered.append(
            output.model_copy(
                update={
                    "run_id": overlay_run_id,
                    "status": "succeeded",
                    "receipt": receipt,
                    "error_type": None,
                    "error_message": None,
                }
            )
        )

    _write_overlay(overlay_results_path, recovered)
    return recovered


def _write_overlay(path: Path, outputs: list[SolutionOutput]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for output in sorted(
                outputs, key=lambda item: (item.sample_id, item.solution_id)
            ):
                handle.write(output.model_dump_json())
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    except FileExistsError as exc:
        raise RecoveryError(f"Refusing to overwrite existing overlay: {path}") from exc
    except OSError as exc:
        raise RecoveryError(f"Cannot write recovery overlay {path}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)

def remap_prebuilt_receipt_outputs(
    *,
    source_results_path: Path,
    overlay_results_path: Path,
    overlay_run_id: str,
) -> list[SolutionOutput]:
    """Reapply the current prebuilt-receipt mapper to cached provider responses."""
    if overlay_results_path.exists():
        raise RecoveryError(f"Refusing to overwrite existing overlay: {overlay_results_path}")

    remapped: list[SolutionOutput] = []
    for output in read_results(source_results_path):
        if output.solution_id != "content-understanding-prebuilt-receipt":
            continue
        raw = _read_raw_artifact(Path(output.raw_artifact))
        try:
            receipt = _recover_prebuilt_receipt(raw)
        except (
            RecoveryError,
            ProviderResponseError,
            ValidationError,
            ValueError,
            TypeError,
        ) as exc:
            if isinstance(exc, RecoveryError):
                raise
            raise RecoveryError(
                f"Cannot remap {output.solution_id}/{output.sample_id}: {exc}"
            ) from exc
        remapped.append(
            output.model_copy(
                update={
                    "run_id": overlay_run_id,
                    "status": "succeeded",
                    "receipt": receipt,
                    "error_type": None,
                    "error_message": None,
                }
            )
        )

    _write_overlay(overlay_results_path, remapped)
    return remapped


__all__ = [
    "RecoveryError",
    "recover_format_invalid_outputs",
    "remap_prebuilt_receipt_outputs",
]
