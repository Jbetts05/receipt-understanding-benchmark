from __future__ import annotations

import json
import random
from pathlib import Path

import httpx
import pytest

from receipt_bench.datasets.config import CordSourceConfig
from receipt_bench.datasets.cord import (
    DATASET_INFO_URL_TEMPLATE,
    extract_ground_truth_text,
    extract_image_url,
    fetch_cord_row,
    iter_cord_samples,
    map_cord_labels,
    parse_ground_truth_text,
    verify_cord_revision,
)
from receipt_bench.datasets.errors import DatasetFetchError, DatasetParsingError

_REVISION = "7f0115a4b758a71d6473b8d085751692da2fef98"
_ROWS_TEMPLATE = (
    "https://datasets-server.huggingface.co/rows"
    "?dataset=naver-clova-ix%2Fcord-v2&config=default&split=test&offset={offset}&length=1"
)
_SEED = 20260828
_TOTAL_ROWS = 100


def _deterministic_indices(count: int) -> list[int]:
    """The same selection formula ``CordSourceConfig`` itself validates against."""
    return sorted(random.Random(_SEED).sample(range(_TOTAL_ROWS), count))


def _source(**overrides: object) -> CordSourceConfig:
    default_indices = [
        3, 11, 15, 26, 28, 34, 35, 42, 46, 47,
        49, 58, 63, 68, 72, 73, 74, 80, 85, 98,
    ]
    kwargs: dict[str, object] = {
        "dataset": "naver-clova-ix/cord-v2",
        "revision": _REVISION,
        "split": "test",
        "license": "CC-BY-4.0",
        "total_rows": _TOTAL_ROWS,
        "sample_count": 20,
        "selection_seed": _SEED,
        "selected_indices": default_indices,
        "rows_api_template": _ROWS_TEMPLATE,
    }
    if "selected_indices" in overrides and "sample_count" not in overrides:
        overrides = dict(overrides)
        overrides["sample_count"] = len(overrides["selected_indices"])  # type: ignore[arg-type]
    kwargs.update(overrides)
    return CordSourceConfig(**kwargs)  # type: ignore[arg-type]



def _row_response(index: int, *, image_src: str, ground_truth: dict[str, object]) -> httpx.Response:
    body = {
        "rows": [
            {
                "row_idx": index,
                "row": {
                    "image": {"src": image_src},
                    "ground_truth": json.dumps(ground_truth),
                },
            }
        ]
    }
    return httpx.Response(200, json=body)


def test_fetch_cord_row_valid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == _ROWS_TEMPLATE.format(offset=3)
        return _row_response(
            3, image_src="https://img.example/3.jpg", ground_truth={"gt_parse": {}}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        row = fetch_cord_row(client, _source(), 3)
    assert row["image"]["src"] == "https://img.example/3.jpg"


def test_fetch_cord_row_rejects_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(DatasetFetchError, match="500"),
    ):
        fetch_cord_row(client, _source(), 3)


def test_fetch_cord_row_rejects_mismatched_row_idx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _row_response(
            4, image_src="https://img.example/4.jpg", ground_truth={"gt_parse": {}}
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(DatasetFetchError, match="row_idx"),
    ):
        fetch_cord_row(client, _source(), 3)


def test_fetch_cord_row_rejects_wrong_row_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rows": []})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(DatasetFetchError, match="exactly one row"),
    ):
        fetch_cord_row(client, _source(), 3)


def test_fetch_cord_row_rejects_non_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(DatasetFetchError, match="not valid JSON"),
    ):
        fetch_cord_row(client, _source(), 3)


def test_extract_image_url_valid() -> None:
    url = extract_image_url({"image": {"src": "https://img.example/3.jpg"}}, index=3)
    assert url == "https://img.example/3.jpg"


@pytest.mark.parametrize(
    "row", [{}, {"image": {}}, {"image": {"src": ""}}, {"image": "not-a-dict"}]
)
def test_extract_image_url_rejects_malformed(row: dict[str, object]) -> None:
    with pytest.raises(DatasetFetchError):
        extract_image_url(row, index=3)


def test_extract_ground_truth_text_valid() -> None:
    assert extract_ground_truth_text({"ground_truth": "{}"}, index=3) == "{}"


@pytest.mark.parametrize("row", [{}, {"ground_truth": ""}, {"ground_truth": 123}])
def test_extract_ground_truth_text_rejects_malformed(row: dict[str, object]) -> None:
    with pytest.raises(DatasetFetchError):
        extract_ground_truth_text(row, index=3)


def test_parse_ground_truth_text_valid() -> None:
    gt_parse = parse_ground_truth_text(json.dumps({"gt_parse": {"menu": {}}}), index=3)
    assert gt_parse == {"menu": {}}


def test_parse_ground_truth_text_rejects_invalid_json() -> None:
    with pytest.raises(DatasetParsingError):
        parse_ground_truth_text("not json", index=3)


def test_parse_ground_truth_text_rejects_missing_gt_parse() -> None:
    with pytest.raises(DatasetParsingError):
        parse_ground_truth_text(json.dumps({"other": {}}), index=3)


def test_map_cord_labels_maps_amounts_and_menu_dict() -> None:
    gt_parse = {
        "sub_total": {
            "subtotal_price": "60.000",
            "discount_price": "-10.000",
            "tax_price": "5.000",
        },
        "total": {"total_price": "55.000", "creditcardprice": "55.000", "menuqty_cnt": "1"},
        "menu": {
            "nm": "Nasi Goreng",
            "cnt": "2",
            "unitprice": "25.000",
            "price": "50.000",
        },
    }
    receipt, pointers = map_cord_labels(gt_parse, index=3)

    assert receipt.amounts.subtotal == 60000.0
    assert receipt.amounts.discount == 10000.0
    assert receipt.amounts.tax == 5000.0
    assert receipt.amounts.total == 55000.0
    assert receipt.line_items is not None
    assert len(receipt.line_items) == 1
    item = receipt.line_items[0]
    assert item.description == "Nasi Goreng"
    assert item.quantity == 2.0
    assert item.unit_price == 25000.0
    assert item.total_price == 50000.0

    assert set(pointers) == {
        "/amounts/subtotal",
        "/amounts/discount",
        "/amounts/tax",
        "/amounts/total",
        "/line_items",
        "/line_items/0/description",
        "/line_items/0/quantity",
        "/line_items/0/unit_price",
        "/line_items/0/total_price",
    }
    # Nothing beyond the approved native fields was inferred.
    assert receipt.merchant.name is None
    assert receipt.transaction.date is None
    assert receipt.transaction.receipt_number is None


def test_map_cord_labels_menu_list_with_multiple_items() -> None:
    gt_parse = {
        "menu": [
            {"nm": "Item A", "cnt": "1", "price": "10.000"},
            {"nm": "Item B", "cnt": "2", "itemsubtotal": "20.000"},
        ]
    }
    receipt, pointers = map_cord_labels(gt_parse, index=7)

    assert receipt.line_items is not None
    assert [item.description for item in receipt.line_items] == ["Item A", "Item B"]
    assert receipt.line_items[0].total_price == 10000.0
    assert receipt.line_items[1].total_price == 20000.0  # itemsubtotal fallback used
    assert "/line_items" in pointers
    assert "/line_items/0/description" in pointers
    assert "/line_items/1/description" in pointers


def test_map_cord_labels_menu_entry_prefers_price_over_itemsubtotal() -> None:
    gt_parse = {"menu": {"nm": "Item", "price": "10.000", "itemsubtotal": "999.000"}}
    receipt, _ = map_cord_labels(gt_parse, index=1)
    assert receipt.line_items is not None
    assert receipt.line_items[0].total_price == 10000.0


def test_map_cord_labels_rejects_non_object_non_array_menu() -> None:
    with pytest.raises(DatasetParsingError):
        map_cord_labels({"menu": "not valid"}, index=1)


def test_map_cord_labels_absent_menu_yields_no_line_items() -> None:
    receipt, pointers = map_cord_labels({}, index=1)
    assert receipt.line_items is None
    assert pointers == []


def test_map_cord_labels_empty_menu_marks_complete_empty_collection() -> None:
    receipt, pointers = map_cord_labels({"menu": []}, index=1)

    assert receipt.line_items is None
    assert pointers == ["/line_items"]


# --- Real pinned CORD test-split rows -------------------------------------
#
# Vendored, verbatim ``gt_parse`` objects fetched from the live pinned
# ``datasets-server.huggingface.co`` rows API for all 20 selected indices
# (revision ``7f0115a4b758a71d6473b8d085751692da2fef98``), stored as
# ``tests/datasets/cord_real_rows.json``. This is research data only (no
# network call happens in the test suite itself) that exercises the parsers
# against actual observed noisy formats -- currency/rate prefixes, decimal
# cents, quantity multiplier markers, and mixed separators within a single
# receipt -- rather than only synthetic examples.
_REAL_ROWS_PATH = Path(__file__).parent / "cord_real_rows.json"
_REAL_ROWS: dict[str, dict[str, object]] = json.loads(_REAL_ROWS_PATH.read_text(encoding="utf-8"))


def test_real_rows_fixture_covers_all_selected_indices() -> None:
    assert sorted(int(key) for key in _REAL_ROWS) == _deterministic_indices(20)


@pytest.mark.parametrize("index", sorted(int(key) for key in _REAL_ROWS))
def test_map_cord_labels_real_pinned_rows_smoke(index: int) -> None:
    """Every one of the 20 real selected rows maps without raising."""
    receipt, pointers = map_cord_labels(_REAL_ROWS[str(index)], index=index)
    assert receipt.amounts.total is not None
    assert receipt.amounts.total >= 0
    assert len(pointers) == len(set(pointers))  # no duplicate pointers


def test_map_cord_labels_real_row_3_at_prefix_and_trailing_x_quantity() -> None:
    # menu.unitprice="@11000", menu.cnt="1X" -- cited by dataset review as blocking.
    receipt, pointers = map_cord_labels(_REAL_ROWS["3"], index=3)
    assert receipt.amounts.subtotal == 11000.0
    assert receipt.amounts.total == 11000.0
    assert receipt.line_items is not None
    item = receipt.line_items[0]
    assert item.unit_price == 11000.0
    assert item.quantity == 1.0
    assert item.total_price == 11000.0
    assert "/amounts/subtotal" in pointers
    assert "/amounts/total" in pointers


def test_map_cord_labels_real_row_26_rp_prefix_and_no_price_fallback() -> None:
    # sub_total/total="Rp 138.000" -- cited by dataset review as blocking.
    # Also: the second menu item has neither price nor itemsubtotal, so it
    # correctly yields no total_price (existing fallback-only design).
    receipt, pointers = map_cord_labels(_REAL_ROWS["26"], index=26)
    assert receipt.amounts.subtotal == 138000.0
    assert receipt.amounts.total == 138000.0
    assert receipt.line_items is not None
    assert receipt.line_items[1].description == "Teh Tawar"
    assert receipt.line_items[1].total_price is None
    assert receipt.line_items[1].unit_price == 4000.0
    assert "/line_items/1/total_price" not in pointers
    assert "/line_items/1/unit_price" in pointers


def test_map_cord_labels_real_row_46_at_prefix_with_comma_thousands() -> None:
    # menu.unitprice="@20,000", menu.cnt="1X" (both items) -- combined @ and comma.
    receipt, _ = map_cord_labels(_REAL_ROWS["46"], index=46)
    assert receipt.amounts.total == 40000.0
    assert receipt.line_items is not None
    for item in receipt.line_items:
        assert item.unit_price == 20000.0
        assert item.quantity == 1.0
        assert item.total_price == 20000.0


def test_map_cord_labels_real_row_47_decimal_cents_notation() -> None:
    # "45,000.00", "0.00", "18,000.00", "63,000.00" -- decimal-cents rendering
    # discovered during the audit (not explicitly named by the review).
    receipt, _ = map_cord_labels(_REAL_ROWS["47"], index=47)
    assert receipt.amounts.discount == 0.0
    assert receipt.amounts.total == 63000.0
    assert receipt.line_items is not None
    assert [item.unit_price for item in receipt.line_items] == [45000.0, 0.0, 18000.0]
    assert [item.total_price for item in receipt.line_items] == [45000.0, 0.0, 18000.0]


def test_map_cord_labels_real_row_63_leading_x_quantity_marker() -> None:
    # menu.cnt="x1" / "x2" -- leading multiplier marker.
    receipt, _ = map_cord_labels(_REAL_ROWS["63"], index=63)
    assert receipt.amounts.subtotal == 57000.0
    assert receipt.amounts.total == 57000.0
    assert receipt.line_items is not None
    assert [item.quantity for item in receipt.line_items] == [1.0, 2.0]


def test_map_cord_labels_real_row_72_non_round_genuine_amounts() -> None:
    # subtotal="15,909", tax="1,591" -- genuine non-multiple-of-100 receipt
    # values discovered during the audit; not a formatting bug.
    receipt, _ = map_cord_labels(_REAL_ROWS["72"], index=72)
    assert receipt.amounts.subtotal == 15909.0
    assert receipt.amounts.tax == 1591.0
    assert receipt.amounts.total == 17500.0


def test_map_cord_labels_real_row_80_mixed_separators_within_one_receipt() -> None:
    # menu.price uses "," (comma-thousands) but sub_total/total use "."
    # (dot-thousands) within the very same receipt -- confirms parsing must
    # be fully per-value, never cached/inferred at the row or receipt level.
    receipt, _ = map_cord_labels(_REAL_ROWS["80"], index=80)
    assert receipt.amounts.subtotal == 25000.0
    assert receipt.amounts.total == 25000.0
    assert receipt.line_items is not None
    assert receipt.line_items[0].total_price == 25000.0


_DATASET_INFO_URL = DATASET_INFO_URL_TEMPLATE.format(dataset="naver-clova-ix/cord-v2")


def test_verify_cord_revision_accepts_matching_sha() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == _DATASET_INFO_URL
        return httpx.Response(200, json={"id": "naver-clova-ix/cord-v2", "sha": _REVISION})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        verify_cord_revision(client, _source())  # must not raise


def test_verify_cord_revision_rejects_mismatched_sha() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sha": "a" * 40})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(DatasetFetchError, match="no longer matches pinned revision"),
    ):
        verify_cord_revision(client, _source())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"sha": ""}),
        httpx.Response(200, json={"sha": 123}),
    ],
)
def test_verify_cord_revision_rejects_malformed_metadata(response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(DatasetFetchError),
    ):
        verify_cord_revision(client, _source())


def test_verify_cord_revision_rejects_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(DatasetFetchError, match="boom"),
    ):
        verify_cord_revision(client, _source())


def _cord_handler(
    rows: dict[int, dict[str, object]],
    images: dict[str, bytes],
    *,
    revision_sha: str = _REVISION,
) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == _DATASET_INFO_URL:
            return httpx.Response(200, json={"sha": revision_sha})
        for index, gt_parse in rows.items():
            if url == _ROWS_TEMPLATE.format(offset=index):
                image_src = f"https://img.example/signed/{index}.jpg"
                return _row_response(
                    index, image_src=image_src, ground_truth={"gt_parse": gt_parse}
                )
        for image_src, content in images.items():
            if url == image_src:
                return httpx.Response(200, content=content)
        raise AssertionError(f"unexpected request: {url}")

    return handler


def test_iter_cord_samples_end_to_end(tmp_path: Path) -> None:
    idx_a, idx_b = _deterministic_indices(2)
    source = _source(selected_indices=[idx_a, idx_b])
    rows = {
        idx_a: {"total": {"total_price": "10.000"}, "menu": {"nm": "A", "price": "10.000"}},
        idx_b: {"total": {"total_price": "20.000"}, "menu": {"nm": "B", "price": "20.000"}},
    }
    images = {
        f"https://img.example/signed/{idx_a}.jpg": b"image-bytes-a",
        f"https://img.example/signed/{idx_b}.jpg": b"image-bytes-b",
    }

    images_root = tmp_path / "images"
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    with httpx.Client(transport=httpx.MockTransport(_cord_handler(rows, images))) as client:  # type: ignore[arg-type]
        samples = list(
            iter_cord_samples(client, source, images_root=images_root, manifest_dir=manifest_dir)
        )

    assert [sample.sample_id for sample in samples] == [f"cord-{idx_a:03d}", f"cord-{idx_b:03d}"]
    for sample, index, total in zip(samples, [idx_a, idx_b], [10000.0, 20000.0], strict=True):
        assert sample.dataset == "cord-v2"
        assert sample.split == "test"
        assert sample.source_language == "id"
        assert sample.ground_truth.amounts.total == total
        resolved = (manifest_dir / sample.image_path).resolve()
        assert resolved == (images_root / "cord" / source.revision / f"{index:03d}.jpg").resolve()
        assert resolved.read_bytes() == images[f"https://img.example/signed/{index}.jpg"]
        # image_path must be portable: forward slashes, never OS-native backslashes.
        assert "\\" not in sample.image_path


def test_iter_cord_samples_is_idempotent_without_network(tmp_path: Path) -> None:
    (index,) = _deterministic_indices(1)
    source = _source(selected_indices=[index])
    rows = {index: {"total": {"total_price": "10.000"}}}
    images = {f"https://img.example/signed/{index}.jpg": b"image-bytes"}

    call_count = 0
    base_handler = _cord_handler(rows, images)

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return base_handler(request)  # type: ignore[operator]

    images_root = tmp_path / "images"
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    with httpx.Client(transport=httpx.MockTransport(counting_handler)) as client:
        list(iter_cord_samples(client, source, images_root=images_root, manifest_dir=manifest_dir))
        first_call_count = call_count
        list(iter_cord_samples(client, source, images_root=images_root, manifest_dir=manifest_dir))

    assert first_call_count == 3  # revision preflight + rows API + image
    assert call_count == first_call_count + 1  # second run only repeats the revision preflight


def test_iter_cord_samples_force_redownloads(tmp_path: Path) -> None:
    (index,) = _deterministic_indices(1)
    source = _source(selected_indices=[index])
    rows = {index: {"total": {"total_price": "10.000"}}}
    images = {f"https://img.example/signed/{index}.jpg": b"image-bytes"}

    call_count = 0
    base_handler = _cord_handler(rows, images)

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return base_handler(request)  # type: ignore[operator]

    images_root = tmp_path / "images"
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    with httpx.Client(transport=httpx.MockTransport(counting_handler)) as client:
        list(iter_cord_samples(client, source, images_root=images_root, manifest_dir=manifest_dir))
        list(
            iter_cord_samples(
                client, source, images_root=images_root, manifest_dir=manifest_dir, force=True
            )
        )

    assert call_count == 6  # two full fetches (revision preflight + rows API + image) x2


def test_iter_cord_samples_detects_tampered_cached_image(tmp_path: Path) -> None:
    (index,) = _deterministic_indices(1)
    source = _source(selected_indices=[index])
    rows = {index: {"total": {"total_price": "10.000"}}}
    images = {f"https://img.example/signed/{index}.jpg": b"image-bytes"}

    images_root = tmp_path / "images"
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    with httpx.Client(transport=httpx.MockTransport(_cord_handler(rows, images))) as client:  # type: ignore[arg-type]
        list(iter_cord_samples(client, source, images_root=images_root, manifest_dir=manifest_dir))

        image_dest = images_root / "cord" / source.revision / f"{index:03d}.jpg"
        image_dest.write_bytes(b"tampered-image-bytes")

        with pytest.raises(DatasetFetchError, match="failed integrity verification"):
            list(
                iter_cord_samples(
                    client, source, images_root=images_root, manifest_dir=manifest_dir
                )
            )


def test_iter_cord_samples_detects_tampered_cached_ground_truth(tmp_path: Path) -> None:
    (index,) = _deterministic_indices(1)
    source = _source(selected_indices=[index])
    rows = {index: {"total": {"total_price": "10.000"}}}
    images = {f"https://img.example/signed/{index}.jpg": b"image-bytes"}

    images_root = tmp_path / "images"
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    with httpx.Client(transport=httpx.MockTransport(_cord_handler(rows, images))) as client:  # type: ignore[arg-type]
        list(iter_cord_samples(client, source, images_root=images_root, manifest_dir=manifest_dir))

        truth_dest = images_root / "cord" / source.revision / f"{index:03d}.ground_truth.json"
        truth_dest.write_text('{"gt_parse": {"total": {"total_price": "999.000"}}}', "utf-8")

        with pytest.raises(DatasetFetchError, match="failed integrity verification"):
            list(
                iter_cord_samples(
                    client, source, images_root=images_root, manifest_dir=manifest_dir
                )
            )


def test_iter_cord_samples_rejects_cache_missing_sidecar(tmp_path: Path) -> None:
    (index,) = _deterministic_indices(1)
    source = _source(selected_indices=[index])
    rows = {index: {"total": {"total_price": "10.000"}}}
    images = {f"https://img.example/signed/{index}.jpg": b"image-bytes"}

    images_root = tmp_path / "images"
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    with httpx.Client(transport=httpx.MockTransport(_cord_handler(rows, images))) as client:  # type: ignore[arg-type]
        list(iter_cord_samples(client, source, images_root=images_root, manifest_dir=manifest_dir))

        sidecar = images_root / "cord" / source.revision / f"{index:03d}.jpg.sha256"
        sidecar.unlink()

        with pytest.raises(DatasetFetchError, match=r"[Mm]issing SHA256 integrity sidecar"):
            list(
                iter_cord_samples(
                    client, source, images_root=images_root, manifest_dir=manifest_dir
                )
            )


_REVISION_B = "2222222222222222222222222222222222222222"


def test_iter_cord_samples_cache_root_is_keyed_by_revision(tmp_path: Path) -> None:
    """A config pin change (different revision) must never reuse old cached bytes.

    Cache roots are keyed by ``source.revision`` (``cord/<revision>/...``), so
    switching a pinned revision resolves to an entirely distinct cache path --
    even though the old revision's cached files (with still-internally-valid
    SHA256 sidecars) remain on disk -- guaranteeing the new pin's bytes are
    always freshly fetched and verified rather than silently reused from an
    older pin.
    """
    (index,) = _deterministic_indices(1)
    source_a = _source(selected_indices=[index], revision=_REVISION)
    source_b = _source(selected_indices=[index], revision=_REVISION_B)
    rows = {index: {"total": {"total_price": "10.000"}}}
    images_a = {f"https://img.example/signed/{index}.jpg": b"image-bytes-a"}
    images_b = {f"https://img.example/signed/{index}.jpg": b"image-bytes-b"}

    images_root = tmp_path / "images"
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    with httpx.Client(
        transport=httpx.MockTransport(_cord_handler(rows, images_a, revision_sha=_REVISION))
    ) as client:  # type: ignore[arg-type]
        list(
            iter_cord_samples(client, source_a, images_root=images_root, manifest_dir=manifest_dir)
        )

    with httpx.Client(
        transport=httpx.MockTransport(_cord_handler(rows, images_b, revision_sha=_REVISION_B))
    ) as client:  # type: ignore[arg-type]
        samples_b = list(
            iter_cord_samples(client, source_b, images_root=images_root, manifest_dir=manifest_dir)
        )

    path_a = images_root / "cord" / source_a.revision / f"{index:03d}.jpg"
    path_b = images_root / "cord" / source_b.revision / f"{index:03d}.jpg"
    assert path_a != path_b
    assert path_a.is_file()
    assert path_b.is_file()
    assert path_a.read_bytes() == b"image-bytes-a"
    assert path_b.read_bytes() == b"image-bytes-b"
    # The revision-B run's resulting sample must reflect revision-B's own
    # bytes, not revision-A's stale cache, even though revision-A's cache
    # (with a valid sidecar) is still present on disk.
    assert (manifest_dir / samples_b[0].image_path).resolve() == path_b.resolve()
