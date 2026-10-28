from __future__ import annotations

from pathlib import Path

import pytest

from receipt_bench.datasets.manifest import write_manifest
from receipt_bench.evaluation.manifest import load_manifest
from receipt_bench.models import Amounts, CanonicalReceipt, Merchant, ReceiptSample


def _sample(sample_id: str, tmp_path: Path) -> ReceiptSample:
    image_path = tmp_path / f"{sample_id}.jpg"
    image_path.write_bytes(b"fake-bytes-" + sample_id.encode())
    return ReceiptSample(
        sample_id=sample_id,
        dataset="unit-test",
        split="test",
        image_path=image_path.name,
        image_sha256="0" * 64,
        source_language="en",
        ground_truth=CanonicalReceipt(merchant=Merchant(name="ACME"), amounts=Amounts(total=1.0)),
        labeled_fields=["merchant.name", "amounts.total"],
    )


def test_write_manifest_round_trips_through_load_manifest(tmp_path: Path) -> None:
    samples = [_sample("a", tmp_path), _sample("b", tmp_path)]
    manifest_path = tmp_path / "manifests" / "benchmark.jsonl"

    result_path = write_manifest(manifest_path, samples)

    assert result_path == manifest_path
    loaded = load_manifest(manifest_path)
    assert [sample.sample_id for sample in loaded] == ["a", "b"]
    assert loaded[0].ground_truth.merchant.name == "ACME"


def test_write_manifest_preserves_given_order(tmp_path: Path) -> None:
    samples = [_sample("z", tmp_path), _sample("a", tmp_path)]
    manifest_path = tmp_path / "benchmark.jsonl"

    write_manifest(manifest_path, samples)

    loaded = load_manifest(manifest_path)
    assert [sample.sample_id for sample in loaded] == ["z", "a"]


def test_write_manifest_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    samples = [_sample("a", tmp_path)]
    manifest_path = tmp_path / "benchmark.jsonl"

    write_manifest(manifest_path, samples)

    leftovers = list(manifest_path.parent.glob("*.tmp-*"))
    assert leftovers == []


def test_write_manifest_rejects_empty_sample_list(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        write_manifest(tmp_path / "benchmark.jsonl", [])
