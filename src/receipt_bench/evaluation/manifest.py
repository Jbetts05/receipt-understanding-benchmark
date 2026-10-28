from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from receipt_bench.evaluation.errors import ManifestError
from receipt_bench.models import ReceiptSample


def load_manifest(path: Path) -> list[ReceiptSample]:
    """Load a JSONL dataset manifest into ``ReceiptSample`` records.

    Each non-blank line must be a single JSON object matching the ``ReceiptSample``
    schema. Raises ``FileNotFoundError`` if the manifest file is missing and
    ``ManifestError`` (with the file path and 1-based line number) for any malformed
    row: invalid JSON, a non-object row, a row that fails ``ReceiptSample`` validation,
    or a duplicate ``sample_id``. An empty manifest is also rejected.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Dataset manifest not found: {path}")

    samples: list[ReceiptSample] = []
    seen_at: dict[str, int] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ManifestError(
                    f"{path}:{line_number}: malformed manifest row (invalid JSON: {exc.msg})"
                ) from exc
            if not isinstance(payload, dict):
                raise ManifestError(
                    f"{path}:{line_number}: malformed manifest row "
                    f"(expected a JSON object, got {type(payload).__name__})"
                )
            try:
                sample = ReceiptSample.model_validate(payload)
            except ValidationError as exc:
                raise ManifestError(
                    f"{path}:{line_number}: malformed manifest row (schema violation: {exc})"
                ) from exc
            if sample.sample_id in seen_at:
                raise ManifestError(
                    f"{path}:{line_number}: duplicate sample_id {sample.sample_id!r} "
                    f"(first seen on line {seen_at[sample.sample_id]})"
                )
            seen_at[sample.sample_id] = line_number
            samples.append(sample)

    if not samples:
        raise ManifestError(f"{path}: manifest contains no samples")
    return samples
