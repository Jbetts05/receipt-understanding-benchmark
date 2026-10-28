from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from receipt_bench.models import ReceiptSample


def write_manifest(path: Path, samples: Iterable[ReceiptSample]) -> Path:
    """Atomically write ``ReceiptSample`` rows as a deterministic JSONL manifest.

    Samples are written in the exact order given by the caller (callers should
    already have sorted them for determinism); each row is one compact JSON
    object per line, in the format
    ``receipt_bench.evaluation.manifest.load_manifest`` reads back. Raises
    ``ValueError`` if given no samples (an empty manifest is rejected by
    ``load_manifest`` too, so refuse to write one).
    """
    materialized = list(samples)
    if not materialized:
        raise ValueError(f"Refusing to write an empty dataset manifest to {path}")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
        for sample in materialized:
            handle.write(sample.model_dump_json())
            handle.write("\n")
    os.replace(tmp_path, path)
    return path


__all__ = ["write_manifest"]
