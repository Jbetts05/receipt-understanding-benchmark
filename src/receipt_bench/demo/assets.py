from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import Field

from receipt_bench.models import ReceiptSample, StrictModel


class DemoDataset(StrictModel):
    name: str
    id: str
    url: str
    revision: str
    split: str
    license: str
    license_url: str
    citation: str
    annotation_scope: str


class DemoReceiptAsset(StrictModel):
    demo_id: str
    title: str
    description: str
    selection_reason: str
    source_row_index: int
    source_row_url: str
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    sample: ReceiptSample


class DemoAssetManifest(StrictModel):
    manifest_version: str
    dataset: DemoDataset
    receipts: list[DemoReceiptAsset]


def load_asset_manifest(path: Path) -> DemoAssetManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    manifest = DemoAssetManifest.model_validate(payload)
    if len(manifest.receipts) != 3:
        raise ValueError("The guided demo must contain exactly three receipts")
    demo_ids = [receipt.demo_id for receipt in manifest.receipts]
    if len(set(demo_ids)) != len(demo_ids):
        raise ValueError("The guided demo contains duplicate demo receipt ids")
    for receipt in manifest.receipts:
        if receipt.sample.dataset != "cord-v2":
            raise ValueError(f"Demo asset {receipt.demo_id!r} is not a CORD v2 sample")
    return manifest


def image_path(manifest_path: Path, asset: DemoReceiptAsset) -> Path:
    root = manifest_path.parent.resolve()
    candidate = (root / asset.sample.image_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Demo image path escapes the asset directory: {asset.sample.image_path}")
    return candidate


def load_verified_image(manifest_path: Path, asset: DemoReceiptAsset) -> bytes:
    path = image_path(manifest_path, asset)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != asset.sample.image_sha256:
        raise ValueError(
            f"Demo image checksum mismatch for {asset.sample.sample_id}: "
            f"expected {asset.sample.image_sha256}, got {digest}"
        )
    return data


__all__ = [
    "DemoAssetManifest",
    "DemoDataset",
    "DemoReceiptAsset",
    "image_path",
    "load_asset_manifest",
    "load_verified_image",
]
