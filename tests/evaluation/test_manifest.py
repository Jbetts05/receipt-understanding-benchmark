from __future__ import annotations

from pathlib import Path

import pytest

from receipt_bench.evaluation.errors import ManifestError
from receipt_bench.evaluation.manifest import load_manifest
from receipt_bench.models import ReceiptSample


def test_loads_valid_jsonl_manifest(manifest_path: Path) -> None:
    samples = load_manifest(manifest_path)
    assert [sample.sample_id for sample in samples] == ["sample-001", "sample-002"]
    assert all(isinstance(sample, ReceiptSample) for sample in samples)


def test_missing_manifest_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_manifest(tmp_path / "does-not-exist.jsonl")


def test_skips_blank_lines(manifest_path: Path) -> None:
    with manifest_path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n   \n")
    samples = load_manifest(manifest_path)
    assert len(samples) == 2


def test_rejects_invalid_json_with_line_number(manifest_path: Path) -> None:
    first_line = manifest_path.read_text(encoding="utf-8").splitlines()[0]
    path = manifest_path.parent / "broken-manifest.jsonl"
    path.write_text(f"{first_line}\nnot json at all\n", encoding="utf-8")
    with pytest.raises(ManifestError, match=r":2: .*invalid JSON"):
        load_manifest(path)


def test_rejects_non_object_row(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    path.write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(ManifestError, match=r":1: .*expected a JSON object"):
        load_manifest(path)


def test_rejects_schema_violation(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    path.write_text('{"sample_id": "a"}\n', encoding="utf-8")
    with pytest.raises(ManifestError, match=r":1: .*schema violation"):
        load_manifest(path)


def test_rejects_duplicate_sample_id(manifest_path: Path) -> None:
    lines = manifest_path.read_text(encoding="utf-8").splitlines()
    with manifest_path.open("a", encoding="utf-8") as handle:
        handle.write(lines[0])
        handle.write("\n")
    with pytest.raises(ManifestError, match="duplicate sample_id"):
        load_manifest(manifest_path)


def test_rejects_empty_manifest(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ManifestError, match="no samples"):
        load_manifest(path)
