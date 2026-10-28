from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from receipt_bench.datasets.config import SroieSourceConfig
from receipt_bench.datasets.errors import DatasetFetchError, DatasetParsingError
from receipt_bench.datasets.sroie import (
    iter_sroie_samples,
    map_sroie_labels,
    parse_sroie_key,
    sroie_source_id,
    sroie_source_ids,
    sroie_urls,
)

_COMMIT = "27be4271b251c256f695acbade9a801bffe85994"


def _source(**overrides: object) -> SroieSourceConfig:
    kwargs: dict[str, object] = {
        "repo": "zzzDavid/ICDAR-2019-SROIE",
        "commit": _COMMIT,
        "count": 3,
        "id_width": 3,
        "license_note": "test",
    }
    kwargs.update(overrides)
    return SroieSourceConfig(**kwargs)  # type: ignore[arg-type]


def test_sroie_source_id_zero_pads() -> None:
    assert sroie_source_id(0, 3) == "000"
    assert sroie_source_id(7, 3) == "007"
    assert sroie_source_id(79, 3) == "079"


def test_sroie_source_id_rejects_overflow_and_negative() -> None:
    with pytest.raises(ValueError, match="fit"):
        sroie_source_id(1000, 3)
    with pytest.raises(ValueError, match="non-negative"):
        sroie_source_id(-1, 3)


def test_sroie_source_ids_generates_deterministic_range() -> None:
    source = _source(count=80, id_width=3)
    ids = sroie_source_ids(source)
    assert len(ids) == 80
    assert ids[0] == "000"
    assert ids[-1] == "079"
    assert ids == sorted(ids)


def test_sroie_urls_uses_template() -> None:
    source = _source()
    urls = sroie_urls(source, "000")
    assert urls == {
        "img": f"https://raw.githubusercontent.com/zzzDavid/ICDAR-2019-SROIE/{_COMMIT}/data/img/000.jpg",
        "key": f"https://raw.githubusercontent.com/zzzDavid/ICDAR-2019-SROIE/{_COMMIT}/data/key/000.json",
        "box": f"https://raw.githubusercontent.com/zzzDavid/ICDAR-2019-SROIE/{_COMMIT}/data/box/000.csv",
    }


def test_parse_sroie_key_valid() -> None:
    payload = parse_sroie_key(b'{"company": "ACME", "total": "9.00"}', source_id="000")
    assert payload == {"company": "ACME", "total": "9.00"}


def test_parse_sroie_key_rejects_invalid_json() -> None:
    with pytest.raises(DatasetParsingError):
        parse_sroie_key(b"not json", source_id="000")


def test_parse_sroie_key_rejects_non_object_root() -> None:
    with pytest.raises(DatasetParsingError):
        parse_sroie_key(b"[1, 2, 3]", source_id="000")


def test_parse_sroie_key_rejects_bad_utf8() -> None:
    with pytest.raises(DatasetParsingError):
        parse_sroie_key(b"\xff\xfe\x00", source_id="000")


def test_map_sroie_labels_maps_only_four_fields() -> None:
    payload = {
        "company": " ACME CORP ",
        "address": "1 Main St",
        "date": "25/12/2018",
        "total": "9.00",
        "some_other_field": "ignored",
    }
    receipt, pointers = map_sroie_labels(payload, source_id="000")

    assert receipt.merchant.name == "ACME CORP"
    assert receipt.merchant.address == "1 Main St"
    assert receipt.transaction.date is not None
    assert receipt.transaction.date.isoformat() == "2018-12-25"
    assert receipt.amounts.total == 9.00
    assert set(pointers) == {
        "/merchant/name",
        "/merchant/address",
        "/transaction/date",
        "/amounts/total",
    }
    # Nothing else on the receipt was inferred.
    assert receipt.merchant.phone is None
    assert receipt.merchant.tax_id is None
    assert receipt.transaction.receipt_number is None
    assert receipt.amounts.subtotal is None
    assert receipt.line_items is None


def test_map_sroie_labels_skips_absent_or_blank_fields() -> None:
    receipt, pointers = map_sroie_labels({"company": "  "}, source_id="000")
    assert pointers == []
    assert receipt.merchant.name is None
    assert receipt.amounts.total is None


@pytest.mark.parametrize(
    ("raw_date", "raw_total", "expected_date", "expected_total"),
    [
        # These format shapes (DD-MM-YY dates, `$`-prefixed totals, bare
        # compact YYYYMMDD dates) mirror what auditing the real pinned
        # SROIE key JSON files (commit 27be4271b251c256f695acbade9a801bffe85994)
        # found in production. The values themselves are invented synthetic
        # examples, not real transcribed annotation content -- SROIE totals
        # and dates are restricted, gated-license content that must not be
        # vendored into this repository even in isolation.
        ("12-06-19", "$45.67", date(2019, 6, 12), 45.67),
        ("09 JUN 2019", "12.34", date(2019, 6, 9), 12.34),
        ("20211203", "$100.00", date(2021, 12, 3), 100.00),
    ],
)
def test_map_sroie_labels_maps_representative_real_world_formats(
    raw_date: str, raw_total: str, expected_date: date, expected_total: float
) -> None:
    """End-to-end mapping check against representative real-world format shapes.

    Uses invented synthetic date/total strings shaped like the formats found
    while auditing the real pinned SROIE key JSON files, exercised through the
    actual production mapping path (`map_sroie_labels`), not just the
    lower-level parsers directly.
    """
    payload = {
        "company": "SYNTHETIC MERCHANT",
        "address": "SYNTHETIC ADDRESS",
        "date": raw_date,
        "total": raw_total,
    }
    receipt, pointers = map_sroie_labels(payload, source_id="000")
    assert "/merchant/name" in pointers
    assert "/merchant/address" in pointers
    assert "/transaction/date" in pointers
    assert "/amounts/total" in pointers
    assert receipt.transaction.date == expected_date
    assert receipt.amounts.total == expected_total


def _sroie_handler(
    images: dict[str, bytes], keys: dict[str, bytes], boxes: dict[str, bytes]
) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for source_id, content in images.items():
            if url.endswith(f"/img/{source_id}.jpg"):
                return httpx.Response(200, content=content)
        for source_id, content in keys.items():
            if url.endswith(f"/key/{source_id}.json"):
                return httpx.Response(200, content=content)
        for source_id, content in boxes.items():
            if url.endswith(f"/box/{source_id}.csv"):
                return httpx.Response(200, content=content)
        raise AssertionError(f"unexpected request: {url}")

    return handler


def test_iter_sroie_samples_end_to_end(tmp_path: Path) -> None:
    source = _source(count=2, id_width=3)
    ids = sroie_source_ids(source)
    images = {source_id: f"image-bytes-{source_id}".encode() for source_id in ids}
    keys = {
        source_id: json.dumps(
            {
                "company": f"Store {source_id}",
                "address": "1 Main St",
                "date": "01/02/2020",
                "total": "10.00",
            }
        ).encode("utf-8")
        for source_id in ids
    }
    boxes = {source_id: b"1,2,3,4,5,6,7,8,text" for source_id in ids}

    images_root = tmp_path / "images"
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    with httpx.Client(transport=httpx.MockTransport(_sroie_handler(images, keys, boxes))) as client:  # type: ignore[arg-type]
        samples = list(
            iter_sroie_samples(
                client, source, images_root=images_root, manifest_dir=manifest_dir
            )
        )

    assert [sample.sample_id for sample in samples] == ["sroie-000", "sroie-001"]
    for sample, source_id in zip(samples, ids, strict=True):
        assert sample.dataset == "sroie"
        assert sample.split == "trainval"
        assert sample.source_language == "en"
        assert sample.ground_truth.merchant.name == f"Store {source_id}"
        assert sample.labeled_fields == sorted(
            ["merchant.name", "merchant.address", "transaction.date", "amounts.total"]
        )
        resolved = (manifest_dir / sample.image_path).resolve()
        assert resolved == (images_root / "sroie" / source.commit / f"{source_id}.jpg").resolve()
        assert resolved.read_bytes() == images[source_id]
        # image_path must be portable: forward slashes, never OS-native backslashes.
        assert "\\" not in sample.image_path
        # box file was downloaded (integrity-verified) even though never parsed
        assert (images_root / "sroie" / source.commit / f"{source_id}.box.csv").is_file()


def test_iter_sroie_samples_is_idempotent_without_network(tmp_path: Path) -> None:
    source = _source(count=1, id_width=3)
    ids = sroie_source_ids(source)
    source_id = ids[0]
    images = {source_id: b"image-bytes"}
    keys = {
        source_id: json.dumps({"company": "Store", "total": "5.00"}).encode("utf-8"),
    }
    boxes = {source_id: b"box-bytes"}

    call_count = 0
    base_handler = _sroie_handler(images, keys, boxes)

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return base_handler(request)  # type: ignore[operator]

    images_root = tmp_path / "images"
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    with httpx.Client(transport=httpx.MockTransport(counting_handler)) as client:
        list(iter_sroie_samples(client, source, images_root=images_root, manifest_dir=manifest_dir))
        first_call_count = call_count
        list(iter_sroie_samples(client, source, images_root=images_root, manifest_dir=manifest_dir))

    assert first_call_count == 3  # img + key + box
    assert call_count == first_call_count  # second run made no additional network calls


def test_iter_sroie_samples_detects_tampered_cached_image(tmp_path: Path) -> None:
    source = _source(count=1, id_width=3)
    ids = sroie_source_ids(source)
    source_id = ids[0]
    images = {source_id: b"image-bytes"}
    keys = {source_id: json.dumps({"company": "Store", "total": "5.00"}).encode("utf-8")}
    boxes = {source_id: b"box-bytes"}

    images_root = tmp_path / "images"
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    with httpx.Client(
        transport=httpx.MockTransport(_sroie_handler(images, keys, boxes))
    ) as client:  # type: ignore[arg-type]
        list(iter_sroie_samples(client, source, images_root=images_root, manifest_dir=manifest_dir))

        image_dest = images_root / "sroie" / source.commit / f"{source_id}.jpg"
        image_dest.write_bytes(b"tampered-image-bytes")

        with pytest.raises(DatasetFetchError, match="failed integrity verification"):
            list(
                iter_sroie_samples(
                    client, source, images_root=images_root, manifest_dir=manifest_dir
                )
            )


def test_iter_sroie_samples_rejects_cache_missing_sidecar(tmp_path: Path) -> None:
    source = _source(count=1, id_width=3)
    ids = sroie_source_ids(source)
    source_id = ids[0]
    images = {source_id: b"image-bytes"}
    keys = {source_id: json.dumps({"company": "Store", "total": "5.00"}).encode("utf-8")}
    boxes = {source_id: b"box-bytes"}

    images_root = tmp_path / "images"
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    with httpx.Client(
        transport=httpx.MockTransport(_sroie_handler(images, keys, boxes))
    ) as client:  # type: ignore[arg-type]
        list(iter_sroie_samples(client, source, images_root=images_root, manifest_dir=manifest_dir))

        sidecar = images_root / "sroie" / source.commit / f"{source_id}.key.json.sha256"
        sidecar.unlink()

        with pytest.raises(DatasetFetchError, match=r"[Mm]issing SHA256 integrity sidecar"):
            list(
                iter_sroie_samples(
                    client, source, images_root=images_root, manifest_dir=manifest_dir
                )
            )


_COMMIT_B = "1111111111111111111111111111111111111111"


def test_iter_sroie_samples_cache_root_is_keyed_by_commit(tmp_path: Path) -> None:
    """A config pin change (different commit) must never reuse old cached bytes.

    Cache roots are keyed by ``source.commit`` (``sroie/<commit>/...``), so
    switching a pinned commit resolves to an entirely distinct cache path --
    even though the old commit's cached files (with still-internally-valid
    SHA256 sidecars) remain on disk -- guaranteeing the new pin's bytes are
    always freshly fetched and verified rather than silently reused from an
    older pin.
    """
    source_a = _source(count=1, id_width=3, commit=_COMMIT)
    source_b = _source(count=1, id_width=3, commit=_COMMIT_B)
    ids = sroie_source_ids(source_a)
    source_id = ids[0]

    images_a = {source_id: b"image-bytes-a"}
    images_b = {source_id: b"image-bytes-b"}
    keys = {source_id: json.dumps({"company": "Store", "total": "5.00"}).encode("utf-8")}
    boxes = {source_id: b"box-bytes"}

    images_root = tmp_path / "images"
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    with httpx.Client(
        transport=httpx.MockTransport(_sroie_handler(images_a, keys, boxes))
    ) as client:  # type: ignore[arg-type]
        list(
            iter_sroie_samples(
                client, source_a, images_root=images_root, manifest_dir=manifest_dir
            )
        )

    with httpx.Client(
        transport=httpx.MockTransport(_sroie_handler(images_b, keys, boxes))
    ) as client:  # type: ignore[arg-type]
        samples_b = list(
            iter_sroie_samples(
                client, source_b, images_root=images_root, manifest_dir=manifest_dir
            )
        )

    path_a = images_root / "sroie" / source_a.commit / f"{source_id}.jpg"
    path_b = images_root / "sroie" / source_b.commit / f"{source_id}.jpg"
    assert path_a != path_b
    assert path_a.is_file()
    assert path_b.is_file()
    assert path_a.read_bytes() == b"image-bytes-a"
    assert path_b.read_bytes() == b"image-bytes-b"
    # The commit-B run's resulting sample must reflect commit-B's own bytes,
    # not commit-A's stale cache, even though commit-A's cache (with a valid
    # sidecar) is still present on disk.
    assert (manifest_dir / samples_b[0].image_path).resolve() == path_b.resolve()
