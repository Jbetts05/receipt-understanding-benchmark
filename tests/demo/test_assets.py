from __future__ import annotations

import hashlib
import json
import re
from importlib.resources import files
from pathlib import Path
from typing import Any

from receipt_bench.demo.assets import (
    image_path,
    load_asset_manifest,
    load_verified_image,
)
from receipt_bench.evaluation.scoring import validate_schema
from receipt_bench.prompting import load_json_schema

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "src" / "receipt_bench" / "demo" / "assets"
STATIC = ROOT / "src" / "receipt_bench" / "demo" / "static"


def test_guided_assets_are_exactly_three_verified_cord_samples() -> None:
    manifest_path = ASSETS / "manifest.json"
    manifest = load_asset_manifest(manifest_path)
    schema: dict[str, Any] = load_json_schema(ROOT / "schemas" / "expense-receipt.schema.json")

    assert [item.demo_id for item in manifest.receipts] == [
        "clean-simple",
        "complex-itemized",
        "challenging-noisy",
    ]
    assert {item.source_row_index for item in manifest.receipts} == {80, 28, 34}
    assert manifest.dataset.license == "CC BY 4.0"
    assert "complete receipt annotations" in manifest.dataset.annotation_scope
    assert {path.name for path in (ASSETS / "cord-v2").glob("*.jpg")} == {
        "cord-080.jpg",
        "cord-028.jpg",
        "cord-034.jpg",
    }

    for asset in manifest.receipts:
        data = load_verified_image(manifest_path, asset)
        assert hashlib.sha256(data).hexdigest() == asset.sample.image_sha256
        assert asset.sample.dataset == "cord-v2"
        assert "sroie" not in str(image_path(manifest_path, asset)).lower()
        assert validate_schema(asset.sample.ground_truth, schema)
        assert asset.sample.labeled_fields
        assert "line_items" in asset.sample.labeled_fields


def test_asset_attribution_records_license_provenance_and_masking() -> None:
    attribution = (ASSETS / "ATTRIBUTION.md").read_text(encoding="utf-8")
    assert "CC BY 4.0" in attribution
    assert "7f0115a4b758a71d6473b8d085751692da2fef98" in attribution
    assert "labeled_fields" in attribution
    assert "No SROIE" in attribution


def test_prebuilt_frontend_is_packaged_and_references_local_assets() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "Receipt Bench" in index
    assert index.index("/theme-init.js") < index.index('type="module"')
    theme_init = (STATIC / "theme-init.js").read_text(encoding="utf-8")
    assert "scoutTheme" in theme_init

    references = re.findall(r'(?:src|href)="(/assets/[^"]+)"', index)
    assert len(references) == 2
    for reference in references:
        assert (STATIC / reference.removeprefix("/")).is_file()


def test_runtime_assets_are_exact_package_data_copies() -> None:
    package = files("receipt_bench.demo.runtime_assets")
    pairs = [
        (ROOT / "configs" / "solutions.yaml", "solutions.yaml"),
        (
            ROOT / "schemas" / "expense-receipt.schema.json",
            "expense-receipt.schema.json",
        ),
        (ROOT / "configs" / "pricing.yaml", "pricing.yaml"),
    ]
    for source, packaged_name in pairs:
        assert package.joinpath(packaged_name).read_bytes() == source.read_bytes()


def test_react_ecosystem_notices_match_lockfile_and_are_packaged() -> None:
    notice = (
        files("receipt_bench.demo").joinpath("THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    )
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    expected = {
        "React": lock["packages"]["node_modules/react"]["version"],
        "React DOM": lock["packages"]["node_modules/react-dom"]["version"],
        "Scheduler": lock["packages"]["node_modules/scheduler"]["version"],
        "Vite": lock["packages"]["node_modules/vite"]["version"],
    }
    for name, version in expected.items():
        assert f"{name} {version}" in notice
    assert "MIT License" in notice
    assert "Meta Platforms, Inc. and affiliates" in notice
    assert "VoidZero Inc. and Vite contributors" in notice
