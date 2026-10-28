from __future__ import annotations

import re
from typing import Any

from receipt_bench.models import CanonicalReceipt

_INDEX_SEGMENT = re.compile(r"^\d+$")
_REPEATED_ENTITY_PREFIXES = ("line_items.", "payment_methods.")
EXTRA_FIELD_MARKER = "__extra__"


def flatten_json(value: Any, pointer: str = "") -> dict[str, Any]:
    """Flatten a JSON-compatible structure into RFC 6901 JSON Pointer -> leaf value pairs.

    Container nodes (objects/arrays) are recursed into; only leaves (``str``, ``float``,
    ``int``, ``bool``, ``None``) are emitted. Empty containers are omitted entirely.
    """
    if isinstance(value, dict):
        flat: dict[str, Any] = {}
        for key, child in value.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            flat.update(flatten_json(child, f"{pointer}/{escaped}"))
        return flat
    if isinstance(value, list):
        flat = {}
        for index, child in enumerate(value):
            flat.update(flatten_json(child, f"{pointer}/{index}"))
        return flat
    return {pointer or "/": value}


def flatten_receipt(receipt: CanonicalReceipt) -> dict[str, Any]:
    """Flatten a canonical receipt into JSON Pointer -> leaf value pairs."""
    payload = receipt.model_dump(mode="json")
    return flatten_json(payload)


def pointer_to_field(pointer: str) -> str:
    """Generalize a JSON pointer into a stable dotted field name.

    Array indices are dropped so that, for example, ``/line_items/0/description`` and
    ``/line_items/3/description`` both generalize to ``line_items.description``. This is
    the field-name convention used for ``ReceiptSample.labeled_fields`` and for grouping
    ``FieldScore`` entries during aggregation.
    """
    segments = [segment for segment in pointer.split("/") if segment != ""]
    named = [
        segment.replace("~1", "/").replace("~0", "~")
        for segment in segments
        if not _INDEX_SEGMENT.match(segment)
    ]
    return ".".join(named)


def extra_field_name(list_field_name: str) -> str:
    """The synthetic field name for an unmatched predicted entry in a repeated list.

    A predicted line item or payment method with no ground-truth counterpart (a
    "hallucinated" or otherwise extra entry) is scored as a labeled false-positive
    ``FieldScore`` row under this name, e.g. ``extra_field_name("line_items")`` is
    ``"line_items.__extra__"``, instead of being silently dropped.
    """
    return f"{list_field_name}.{EXTRA_FIELD_MARKER}"


def is_extra_field(field: str) -> bool:
    """Whether ``field`` is a synthetic extra/false-positive row (see :func:`extra_field_name`)."""
    return field.endswith(f".{EXTRA_FIELD_MARKER}")


def is_repeated_entity_field(field: str) -> bool:
    """Whether ``field`` belongs to a repeated-entity list (line items or payment methods).

    Only repeated-entity fields can ever produce "extra" entries, since scalar
    fields have no notion of an unmatched predicted item.
    """
    return field.startswith(_REPEATED_ENTITY_PREFIXES)
