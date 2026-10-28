from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from receipt_bench.datasets.config import CordSourceConfig
from receipt_bench.datasets.errors import DatasetFetchError, DatasetParsingError
from receipt_bench.datasets.http import (
    DEFAULT_TIMEOUT,
    USER_AGENT,
    read_verified,
    relative_posix_path,
    sha256_bytes,
    write_verified,
)
from receipt_bench.datasets.money import parse_cord_money, parse_cord_quantity
from receipt_bench.evaluation.pointers import pointer_to_field
from receipt_bench.models import Amounts, CanonicalReceipt, LineItem, ReceiptSample


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


DATASET_INFO_URL_TEMPLATE = "https://huggingface.co/api/datasets/{dataset}"


def verify_cord_revision(client: httpx.Client, source: CordSourceConfig) -> None:
    """Preflight-check that the pinned CORD revision is still the dataset's current HEAD.

    The ``datasets-server`` rows API used by ``fetch_cord_row`` has no
    revision/commit parameter and always resolves against the dataset's
    default branch, so pinning ``source.revision`` in configuration does not
    by itself guarantee that rows fetched today match that pinned commit if
    the upstream dataset has since moved. This issues a single GET against
    the Hugging Face Hub dataset-info API and requires the returned ``sha``
    to equal ``source.revision`` exactly. Raises ``DatasetFetchError`` for
    any transport failure, non-200 status, malformed/missing ``sha`` in the
    response, or a mismatch between the current HEAD and the pinned
    revision -- there is no silent fallback.
    """
    url = DATASET_INFO_URL_TEMPLATE.format(dataset=source.dataset)
    try:
        response = client.get(url, timeout=DEFAULT_TIMEOUT, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError as exc:
        raise DatasetFetchError(
            f"CORD revision preflight: request to {url} failed: {exc}"
        ) from exc
    if response.status_code != 200:
        raise DatasetFetchError(
            f"CORD revision preflight: unexpected HTTP status {response.status_code} from {url}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise DatasetFetchError(
            f"CORD revision preflight: response from {url} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise DatasetFetchError("CORD revision preflight: response root must be a JSON object")
    sha = payload.get("sha")
    if not isinstance(sha, str) or not sha:
        raise DatasetFetchError(
            "CORD revision preflight: response is missing a valid 'sha' field"
        )
    if sha != source.revision:
        raise DatasetFetchError(
            f"CORD revision preflight: dataset {source.dataset!r} current HEAD sha "
            f"{sha!r} no longer matches pinned revision {source.revision!r}; the rows "
            "API always resolves against the current default branch, so this run "
            "cannot be trusted as reproducible against the pinned commit. Re-pin "
            "the revision (and re-verify all downstream ground truth) before "
            "proceeding."
        )


def fetch_cord_row(client: httpx.Client, source: CordSourceConfig, index: int) -> dict[str, Any]:
    """Fetch one CORD v2 test-split row through the HF ``datasets-server`` rows API.

    Raises ``DatasetFetchError`` for any transport failure, non-200 status, or
    response that is not exactly the expected single-row shape (the datasets
    server can otherwise silently return an empty or differently-indexed row
    for an out-of-range or misconfigured offset).
    """
    url = source.rows_api_template.format(offset=index)
    try:
        response = client.get(url, timeout=DEFAULT_TIMEOUT, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError as exc:
        raise DatasetFetchError(f"CORD row {index}: request to {url} failed: {exc}") from exc
    if response.status_code != 200:
        raise DatasetFetchError(
            f"CORD row {index}: unexpected HTTP status {response.status_code} from {url}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise DatasetFetchError(
            f"CORD row {index}: response from {url} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise DatasetFetchError(f"CORD row {index}: response root must be a JSON object")
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) != 1:
        raise DatasetFetchError(
            f"CORD row {index}: expected exactly one row in response, got {rows!r}"
        )
    entry = rows[0]
    if not isinstance(entry, dict) or entry.get("row_idx") != index:
        raise DatasetFetchError(
            f"CORD row {index}: response row_idx does not match requested offset ({entry!r})"
        )
    row = entry.get("row")
    if not isinstance(row, dict):
        raise DatasetFetchError(f"CORD row {index}: response 'row' field must be an object")
    return row


def extract_image_url(row: dict[str, Any], *, index: int) -> str:
    """Extract the signed image URL from a CORD row payload.

    Raises ``DatasetFetchError`` if the ``image``/``image.src`` fields are
    missing or malformed.
    """
    image = row.get("image")
    if not isinstance(image, dict):
        raise DatasetFetchError(f"CORD row {index}: 'image' field missing or malformed")
    src = image.get("src")
    if not isinstance(src, str) or not src:
        raise DatasetFetchError(f"CORD row {index}: 'image.src' URL missing or malformed")
    return src


def extract_ground_truth_text(row: dict[str, Any], *, index: int) -> str:
    """Extract the raw ``ground_truth`` JSON string from a CORD row payload.

    Raises ``DatasetFetchError`` if the field is missing, empty, or not a string.
    """
    raw = row.get("ground_truth")
    if not isinstance(raw, str) or not raw:
        raise DatasetFetchError(f"CORD row {index}: 'ground_truth' field missing or malformed")
    return raw


def parse_ground_truth_text(raw: str, *, index: int) -> dict[str, Any]:
    """Parse a CORD row's raw ``ground_truth`` JSON string into its ``gt_parse`` object.

    Raises ``DatasetParsingError`` if the text is not valid JSON, the root is
    not an object, or ``gt_parse`` is missing/not an object.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DatasetParsingError(
            f"CORD row {index}: ground_truth is not valid JSON ({exc.msg})"
        ) from exc
    if not isinstance(payload, dict):
        raise DatasetParsingError(f"CORD row {index}: ground_truth JSON root must be an object")
    gt_parse = payload.get("gt_parse")
    if not isinstance(gt_parse, dict):
        raise DatasetParsingError(f"CORD row {index}: ground_truth.gt_parse must be an object")
    return gt_parse


def _map_menu_entry(
    entry: dict[str, Any], *, index: int, item_index: int
) -> tuple[LineItem, list[str]]:
    """Map one native CORD ``menu`` entry to a ``LineItem`` and its label pointers.

    Only ``nm`` -> ``description``, ``cnt`` -> ``quantity``, ``unitprice`` ->
    ``unit_price``, and the line total (``price``, falling back to
    ``itemsubtotal`` when ``price`` is absent) -> ``total_price`` are ever
    mapped; every other native menu field (``num``, ``sub``, ``discountprice``,
    ``vatyn``, ...) is ignored. ``price`` and ``itemsubtotal`` both represent
    the line's total (CORD documents ``itemsubtotal`` as usually equal to
    ``price`` unless a line-level adjustment applies); this package treats
    ``price`` as authoritative for ``total_price`` and only falls back to
    ``itemsubtotal`` when ``price`` itself is not present, so at most one
    native field ever feeds that single canonical slot.
    """
    pointers: list[str] = []
    kwargs: dict[str, Any] = {}

    name = _clean_text(entry.get("nm"))
    if name is not None:
        kwargs["description"] = name
        pointers.append(f"/line_items/{item_index}/description")

    if entry.get("cnt") is not None:
        kwargs["quantity"] = parse_cord_quantity(
            entry["cnt"], context=f"CORD row {index} menu[{item_index}].cnt"
        )
        pointers.append(f"/line_items/{item_index}/quantity")

    if entry.get("unitprice") is not None:
        kwargs["unit_price"] = float(
            parse_cord_money(
                entry["unitprice"], context=f"CORD row {index} menu[{item_index}].unitprice"
            )
        )
        pointers.append(f"/line_items/{item_index}/unit_price")

    total_price: int | None = None
    if entry.get("price") is not None:
        total_price = parse_cord_money(
            entry["price"], context=f"CORD row {index} menu[{item_index}].price"
        )
    elif entry.get("itemsubtotal") is not None:
        total_price = parse_cord_money(
            entry["itemsubtotal"], context=f"CORD row {index} menu[{item_index}].itemsubtotal"
        )
    if total_price is not None:
        kwargs["total_price"] = float(total_price)
        pointers.append(f"/line_items/{item_index}/total_price")

    return LineItem(**kwargs), pointers


def map_cord_labels(gt_parse: dict[str, Any], *, index: int) -> tuple[CanonicalReceipt, list[str]]:
    """Map a native CORD ``gt_parse`` object to a ``CanonicalReceipt`` and label pointers.

    Only ``sub_total.subtotal_price`` -> ``/amounts/subtotal``,
    ``sub_total.discount_price`` -> ``/amounts/discount``,
    ``sub_total.tax_price`` -> ``/amounts/tax``, ``total.total_price`` ->
    ``/amounts/total``, and the ``menu`` line items (see ``_map_menu_entry``)
    are ever mapped; every other native field (merchant/date/receipt-number/
    currency/payment-method/receipt-type-like fields such as ``menu.num``,
    ``total.creditcardprice``, ``total.menuqty_cnt``, ...) is ignored. Native
    ``discount_price`` values are textually signed (e.g. ``"-60.000"``); the
    magnitude is taken since ``Amounts.discount`` is a quantity to be
    subtracted, not a signed delta. ``menu`` may be a single object (one line
    item) or an array; both are normalized to a list. Raises
    ``DatasetParsingError`` if ``menu`` (when present) is neither.
    """
    pointers: list[str] = []
    amounts_kwargs: dict[str, float] = {}

    sub_total = gt_parse.get("sub_total")
    if isinstance(sub_total, dict):
        if sub_total.get("subtotal_price") is not None:
            amounts_kwargs["subtotal"] = float(
                parse_cord_money(
                    sub_total["subtotal_price"],
                    context=f"CORD row {index} sub_total.subtotal_price",
                )
            )
            pointers.append("/amounts/subtotal")
        if sub_total.get("discount_price") is not None:
            amounts_kwargs["discount"] = float(
                abs(
                    parse_cord_money(
                        sub_total["discount_price"],
                        context=f"CORD row {index} sub_total.discount_price",
                    )
                )
            )
            pointers.append("/amounts/discount")
        if sub_total.get("tax_price") is not None:
            amounts_kwargs["tax"] = float(
                parse_cord_money(
                    sub_total["tax_price"], context=f"CORD row {index} sub_total.tax_price"
                )
            )
            pointers.append("/amounts/tax")

    total = gt_parse.get("total")
    if isinstance(total, dict) and total.get("total_price") is not None:
        amounts_kwargs["total"] = float(
            parse_cord_money(total["total_price"], context=f"CORD row {index} total.total_price")
        )
        pointers.append("/amounts/total")

    line_items: list[LineItem] | None = None
    menu_raw = gt_parse.get("menu")
    if menu_raw is not None:
        if isinstance(menu_raw, dict):
            menu_entries: list[Any] = [menu_raw]
        elif isinstance(menu_raw, list):
            menu_entries = menu_raw
        else:
            raise DatasetParsingError(f"CORD row {index}: 'menu' must be an object or an array")

        # CORD's menu array is a complete collection annotation, so unmatched
        # predicted items are genuine extras rather than unlabeled data.
        pointers.append("/line_items")
        items: list[LineItem] = []
        for item_index, raw_entry in enumerate(menu_entries):
            if not isinstance(raw_entry, dict):
                raise DatasetParsingError(f"CORD row {index}: menu[{item_index}] must be an object")
            item, item_pointers = _map_menu_entry(raw_entry, index=index, item_index=item_index)
            items.append(item)
            pointers.extend(item_pointers)
        if items:
            line_items = items

    receipt = CanonicalReceipt(amounts=Amounts(**amounts_kwargs), line_items=line_items)
    return receipt, pointers


def iter_cord_samples(
    client: httpx.Client,
    source: CordSourceConfig,
    *,
    images_root: Path,
    manifest_dir: Path,
    force: bool = False,
) -> Iterator[ReceiptSample]:
    """Acquire and normalize the 20 selected CORD v2 test-split samples.

    For each selected index, only the rows API and the signed image URL it
    returns are ever fetched (no parquet files); the ground truth text and
    image bytes are cached to disk, alongside a ``.sha256`` integrity sidecar
    for each (see ``receipt_bench.datasets.http.write_verified``), so a
    repeat call is idempotent -- re-verified from disk against those sidecars,
    without a network call -- unless ``force`` is set. A cached file with a
    missing, malformed, or mismatched sidecar always fails explicitly rather
    than being silently reused (see ``read_verified``). Because the rows API
    returns a freshly-signed, time-limited image URL on every call, the image
    is downloaded immediately after that URL is obtained rather than
    persisted as a URL for later use.

    A single preflight call to ``verify_cord_revision`` runs first, once per
    call to this generator (i.e. once per ``prepare_dataset`` run), and is
    not skipped by per-row caching: it guards against the rows API silently
    resolving against a moved default branch even when every row/image is
    already cached locally.

    The cache root is keyed by ``source.revision`` (``cord/<revision>/...``),
    not just the dataset name, so that a config pin change (moving to a
    different revision) always resolves to a distinct cache path -- and
    therefore always fetches and verifies fresh bytes for the new pin --
    rather than silently reusing bytes downloaded under an older revision
    merely because their SHA256 sidecars happen to still be internally
    self-consistent.
    """
    verify_cord_revision(client, source)

    dataset_root = images_root / "cord" / source.revision
    for index in source.selected_indices:
        image_dest = dataset_root / f"{index:03d}.jpg"
        truth_dest = dataset_root / f"{index:03d}.ground_truth.json"

        if not force and image_dest.is_file() and truth_dest.is_file():
            image_bytes = read_verified(image_dest)
            truth_raw = read_verified(truth_dest).decode("utf-8")
        else:
            row = fetch_cord_row(client, source, index)
            image_url = extract_image_url(row, index=index)
            try:
                image_response = client.get(
                    image_url, timeout=DEFAULT_TIMEOUT, headers={"User-Agent": USER_AGENT}
                )
            except httpx.HTTPError as exc:
                raise DatasetFetchError(
                    f"CORD row {index}: image request to {image_url} failed: {exc}"
                ) from exc
            if image_response.status_code != 200:
                raise DatasetFetchError(
                    f"CORD row {index}: unexpected HTTP status {image_response.status_code} "
                    f"fetching image {image_url}"
                )
            image_bytes = image_response.content
            if not image_bytes:
                raise DatasetFetchError(f"CORD row {index}: empty image response from {image_url}")
            write_verified(image_dest, image_bytes)

            truth_raw = extract_ground_truth_text(row, index=index)
            write_verified(truth_dest, truth_raw.encode("utf-8"))

        gt_parse = parse_ground_truth_text(truth_raw, index=index)
        receipt, pointers = map_cord_labels(gt_parse, index=index)
        labeled_fields = sorted({pointer_to_field(pointer) for pointer in pointers})

        yield ReceiptSample(
            sample_id=f"cord-{index:03d}",
            dataset="cord-v2",
            split=source.split,
            image_path=relative_posix_path(image_dest, manifest_dir),
            image_sha256=sha256_bytes(image_bytes),
            source_language="id",
            ground_truth=receipt,
            labeled_fields=labeled_fields,
        )


__all__ = [
    "extract_ground_truth_text",
    "extract_image_url",
    "fetch_cord_row",
    "iter_cord_samples",
    "map_cord_labels",
    "parse_ground_truth_text",
    "verify_cord_revision",
]
