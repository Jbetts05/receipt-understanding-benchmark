from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from receipt_bench.datasets.config import SroieSourceConfig
from receipt_bench.datasets.dates import parse_day_first_date
from receipt_bench.datasets.errors import DatasetParsingError
from receipt_bench.datasets.http import ensure_downloaded, relative_posix_path
from receipt_bench.datasets.money import parse_sroie_amount
from receipt_bench.evaluation.pointers import pointer_to_field
from receipt_bench.models import Amounts, CanonicalReceipt, Merchant, ReceiptSample, Transaction

#: SROIE mirror layout: ``data/{kind}/{sample_id}.{ext}``.
_KIND_EXTENSIONS: dict[str, str] = {"img": "jpg", "key": "json", "box": "csv"}

#: Native SROIE key fields mapped to canonical JSON pointers. Only these four
#: fields are ever labeled or inferred; every other key present in the source
#: JSON (if any) is ignored.
_MERCHANT_NAME_KEY = "company"
_MERCHANT_ADDRESS_KEY = "address"
_TRANSACTION_DATE_KEY = "date"
_AMOUNTS_TOTAL_KEY = "total"


def sroie_source_id(index: int, width: int) -> str:
    """Format a zero-based SROIE index as its zero-padded source id (``7`` -> ``"007"``)."""
    if index < 0:
        raise ValueError(f"SROIE index must be non-negative, got {index}")
    source_id = str(index).zfill(width)
    if len(source_id) != width:
        raise ValueError(f"SROIE index {index} does not fit in {width} digits")
    return source_id


def sroie_source_ids(source: SroieSourceConfig) -> list[str]:
    """The deterministic, zero-padded SROIE source ids selected by this config."""
    return [sroie_source_id(index, source.id_width) for index in range(source.count)]


def sroie_urls(source: SroieSourceConfig, source_id: str) -> dict[str, str]:
    """The mirror URLs (image, key JSON, box CSV) for one SROIE source id."""
    return {
        kind: source.url_template.format(
            repo=source.repo, commit=source.commit, kind=kind, sample_id=source_id, ext=ext
        )
        for kind, ext in _KIND_EXTENSIONS.items()
    }


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def parse_sroie_key(raw: bytes, *, source_id: str) -> dict[str, Any]:
    """Parse a SROIE key JSON file's raw bytes into a dict.

    Raises ``DatasetParsingError`` if the bytes are not valid UTF-8 JSON or the
    JSON root is not an object.
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise DatasetParsingError(f"SROIE {source_id}: key file is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DatasetParsingError(f"SROIE {source_id}: key JSON is not valid ({exc.msg})") from exc
    if not isinstance(payload, dict):
        raise DatasetParsingError(f"SROIE {source_id}: key JSON root must be an object")
    return payload


def map_sroie_labels(
    payload: dict[str, Any], *, source_id: str
) -> tuple[CanonicalReceipt, list[str]]:
    """Map native SROIE key fields to a ``CanonicalReceipt`` and its label pointers.

    Only ``company`` -> ``/merchant/name``, ``address`` -> ``/merchant/address``,
    ``date`` -> ``/transaction/date``, and ``total`` -> ``/amounts/total`` are
    ever mapped or labeled; every other native field is ignored, and any of
    these four fields that is absent or blank is simply left unset (not
    inferred). Returns the receipt plus the JSON pointers of fields that were
    actually populated from a native label.
    """
    pointers: list[str] = []
    merchant_kwargs: dict[str, str] = {}
    transaction_kwargs: dict[str, Any] = {}
    amounts_kwargs: dict[str, float] = {}

    company = _clean_text(payload.get(_MERCHANT_NAME_KEY))
    if company is not None:
        merchant_kwargs["name"] = company
        pointers.append("/merchant/name")

    address = _clean_text(payload.get(_MERCHANT_ADDRESS_KEY))
    if address is not None:
        merchant_kwargs["address"] = address
        pointers.append("/merchant/address")

    raw_date = _clean_text(payload.get(_TRANSACTION_DATE_KEY))
    if raw_date is not None:
        transaction_kwargs["date"] = parse_day_first_date(
            raw_date, context=f"SROIE {source_id} date"
        )
        pointers.append("/transaction/date")

    raw_total = _clean_text(payload.get(_AMOUNTS_TOTAL_KEY))
    if raw_total is not None:
        amounts_kwargs["total"] = parse_sroie_amount(raw_total, context=f"SROIE {source_id} total")
        pointers.append("/amounts/total")

    receipt = CanonicalReceipt(
        merchant=Merchant(**merchant_kwargs),
        transaction=Transaction(**transaction_kwargs),
        amounts=Amounts(**amounts_kwargs),
    )
    return receipt, pointers


def iter_sroie_samples(
    client: httpx.Client,
    source: SroieSourceConfig,
    *,
    images_root: Path,
    manifest_dir: Path,
    force: bool = False,
) -> Iterator[ReceiptSample]:
    """Acquire and normalize the 80 deterministic SROIE samples.

    For each source id, downloads the image, key JSON, and box CSV atomically
    (each independently verified over HTTP and by SHA256, and idempotently
    re-verified from disk on repeat calls unless ``force`` is set), parses the
    key JSON, and maps its native labels to a ``CanonicalReceipt``. The box CSV
    is downloaded and integrity-verified for dataset completeness but is never
    parsed or mapped to any canonical field (SROIE box annotations are not part
    of the approved native-label mapping). Each yielded sample's ``split`` is
    ``source.split`` (``"trainval"`` by default -- this mirror redistributes
    SROIE's labeled trainval-style data, not the official held-out test set).

    The cache root is keyed by ``source.commit`` (``sroie/<commit>/...``), not
    just the dataset name, so that a config pin change (moving to a different
    commit) always resolves to a distinct cache path -- and therefore always
    fetches and verifies fresh bytes for the new pin -- rather than silently
    reusing bytes downloaded under an older commit merely because their SHA256
    sidecars happen to still be internally self-consistent.
    """
    dataset_root = images_root / "sroie" / source.commit
    for source_id in sroie_source_ids(source):
        urls = sroie_urls(source, source_id)
        image_dest = dataset_root / f"{source_id}.jpg"
        key_dest = dataset_root / f"{source_id}.key.json"
        box_dest = dataset_root / f"{source_id}.box.csv"

        image_result = ensure_downloaded(client, urls["img"], image_dest, force=force)
        key_result = ensure_downloaded(client, urls["key"], key_dest, force=force)
        ensure_downloaded(client, urls["box"], box_dest, force=force)

        key_payload = parse_sroie_key(key_result.content, source_id=source_id)
        receipt, pointers = map_sroie_labels(key_payload, source_id=source_id)
        labeled_fields = sorted({pointer_to_field(pointer) for pointer in pointers})

        yield ReceiptSample(
            sample_id=f"sroie-{source_id}",
            dataset="sroie",
            split=source.split,
            image_path=relative_posix_path(image_result.path, manifest_dir),
            image_sha256=image_result.sha256,
            source_language="en",
            ground_truth=receipt,
            labeled_fields=labeled_fields,
        )


__all__ = [
    "iter_sroie_samples",
    "map_sroie_labels",
    "parse_sroie_key",
    "sroie_source_id",
    "sroie_source_ids",
    "sroie_urls",
]
