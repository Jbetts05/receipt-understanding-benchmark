"""Content Understanding adapter (GA 2025-11-01).

Supports project-specific custom analyzers and the three prebuilt analyzer paths
configured in ``solutions.yaml``:

* ``expense_receipt_v1`` (custom, ``kind: native``) - maps the custom analyzer's
  PascalCase field schema (``configs/content-understanding/expense_receipt_v1.json``)
  deterministically into ``CanonicalReceipt``'s snake_case shape. The two schemas do
  not share property names one-for-one, so an explicit field-by-field mapping is used
  (mirroring the ``document_intelligence`` adapter's approach) rather than passing the
  decoded payload straight to ``model_validate``. Fields absent from ``fieldSchema``
  (``tax_id`` is present, but nothing else is unmapped) fall back to ``None``.
* ``cord_receipt_v1`` (custom, ``kind: native``) - emits the pinned CORD project
  fields directly, including nested menu, subtotal, and total structures.
* ``prebuilt-receipt`` (domain analyzer, ``kind: native``) - maps the deployed
  ``receipt.generic`` and ``receipt.hotel`` field schemas, including their nested
  ``{CurrencyCode, Amount}`` money objects, deterministically into
  ``CanonicalReceipt``. See ``_map_prebuilt_receipt_fields``.
* ``prebuilt-read`` / ``prebuilt-layout`` (``kind: ocr_pipeline``) - used purely as
  OCR/ingestion extractors, taking the ordered markdown for the shared normalizer.

Uses direct REST via ``httpx`` (the GA 2025-11-01 API is recent enough that no
stable SDK dependency is declared in ``pyproject.toml``). Local image bytes are
uploaded via the ``:analyzeBinary`` operation (raw binary body, ``Content-Type``
set to the image's media type) rather than the JSON ``:analyze`` operation (which
is for ``url``-based or multi-input "pro mode" requests). Analysis is
asynchronous: the initial call returns ``202 Accepted`` with an
``Operation-Location`` header, which is polled (honoring ``Retry-After``) until
the operation succeeds, fails, or a bounded timeout elapses.

For native analyzers, the
top-level ``usage.tokens`` (a flat dict keyed like ``"<model>-input"``/
``"<model>-output"``, e.g. ``gpt-5.2-input``/``gpt-5.4-mini-output``/
``text-embedding-3-large-input`` for the models Content Understanding internally
invokes) is split per model into zero-latency ``ComponentMeasurement(component=
"model:<model-name>")`` records (see ``_model_token_measurements``), so the
evaluation layer can price each contributing model independently by mapping
``model:<id>`` to ``<id>``. The primary ``native_extraction`` measurement retains
the real wall-clock latency plus only the service-level ``pages``/
``contextualization_tokens`` usage signals - it does not duplicate token counts.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import ValidationError

from receipt_bench.models import CanonicalReceipt, ComponentMeasurement, Usage
from receipt_bench.providers._timing import elapsed_ms
from receipt_bench.providers.base import (
    AdapterResponse,
    InvalidSolutionOutput,
    ProviderResponseError,
    SolutionAdapter,
)
from receipt_bench.providers.ocr_pipeline import OcrExtraction
from receipt_bench.storage.validation import JsonObject

_API_VERSION = "2025-11-01"
_DEFAULT_POLL_INTERVAL_SECONDS = 2.0
_MAX_POLL_SECONDS = 120.0
# "Canceled" (single L, US spelling) is the actual ``OperationState`` enum value in
# the GA 2025-11-01 API; "Cancelled" never appears in the wire format.
_TERMINAL_FAILURE_STATUSES = frozenset({"Failed", "Canceled"})


def _decode_content_field(field: dict[str, Any]) -> Any:
    """Recursively decode a Content Understanding typed field into a plain value."""
    field_type = field.get("type")
    if field_type == "string":
        return field.get("valueString")
    if field_type == "date":
        return field.get("valueDate")
    if field_type == "time":
        return field.get("valueTime")
    if field_type == "number":
        return field.get("valueNumber")
    if field_type == "integer":
        return field.get("valueInteger")
    if field_type == "boolean":
        return field.get("valueBoolean")
    if field_type == "array":
        return [_decode_content_field(item) for item in field.get("valueArray", [])]
    if field_type == "object":
        return {
            key: _decode_content_field(value)
            for key, value in field.get("valueObject", {}).items()
        }
    raise ProviderResponseError(f"content-understanding: unsupported field type {field_type!r}")


class ContentUnderstandingClient:
    """Thin REST wrapper for the Content Understanding analyze/poll workflow."""

    def __init__(self, *, client: httpx.Client) -> None:
        self._client = client

    def analyze(self, analyzer_id: str, image_bytes: bytes, media_type: str) -> dict[str, Any]:
        try:
            response = self._client.post(
                f"/contentunderstanding/analyzers/{analyzer_id}:analyzeBinary",
                params={"api-version": _API_VERSION},
                content=image_bytes,
                headers={"Content-Type": media_type},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderResponseError(
                f"content-understanding: analyze call failed: {exc}"
            ) from exc
        operation_location = response.headers.get("operation-location")
        if not operation_location:
            raise ProviderResponseError(
                "content-understanding: analyze response had no Operation-Location header"
            )
        return self._poll(operation_location)

    def ensure_analyzer(
        self,
        analyzer_id: str,
        definition: JsonObject,
        *,
        schema_hash: str,
    ) -> None:
        try:
            response = self._client.get(
                f"/contentunderstanding/analyzers/{analyzer_id}",
                params={"api-version": _API_VERSION},
            )
        except httpx.HTTPError as exc:
            raise ProviderResponseError(
                f"content-understanding: analyzer lookup failed: {exc}"
            ) from exc
        def matches_schema(candidate: httpx.Response) -> bool:
            if candidate.status_code != 200:
                return False
            payload = candidate.json()
            tags = payload.get("tags")
            return (
                isinstance(tags, dict)
                and tags.get("schemaSha256") == schema_hash
            )

        if response.status_code == 200:
            if not matches_schema(response):
                raise ProviderResponseError(
                    "content-understanding: existing custom analyzer schema differs"
                )
            return
        if response.status_code != 404:
            raise ProviderResponseError(
                "content-understanding: analyzer lookup returned "
                f"HTTP {response.status_code}"
            )
        try:
            created = self._client.put(
                f"/contentunderstanding/analyzers/{analyzer_id}",
                params={"api-version": _API_VERSION},
                json=definition,
            )
            if created.status_code == 409:
                raced = self._client.get(
                    f"/contentunderstanding/analyzers/{analyzer_id}",
                    params={"api-version": _API_VERSION},
                )
                if matches_schema(raced):
                    return
            if created.status_code not in {200, 201, 202}:
                raise ProviderResponseError(
                    "content-understanding: analyzer creation returned "
                    f"HTTP {created.status_code}: {created.text}"
                )
        except httpx.HTTPError as exc:
            raise ProviderResponseError(
                f"content-understanding: analyzer creation failed: {exc}"
            ) from exc
        operation_location = created.headers.get("operation-location")
        if operation_location:
            self._poll(operation_location)

    def delete_analyzer(self, analyzer_id: str) -> None:
        try:
            response = self._client.delete(
                f"/contentunderstanding/analyzers/{analyzer_id}",
                params={"api-version": _API_VERSION},
            )
        except httpx.HTTPError as exc:
            raise ProviderResponseError(
                f"content-understanding: analyzer deletion failed: {exc}"
            ) from exc
        if response.status_code not in {200, 202, 204, 404}:
            raise ProviderResponseError(
                "content-understanding: analyzer deletion returned "
                f"HTTP {response.status_code}: {response.text}"
            )

    def _poll(self, operation_location: str) -> dict[str, Any]:
        deadline = time.monotonic() + _MAX_POLL_SECONDS
        while True:
            try:
                response = self._client.get(operation_location)
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
            except httpx.HTTPError as exc:
                raise ProviderResponseError(
                    f"content-understanding: poll call failed: {exc}"
                ) from exc
            except json.JSONDecodeError as exc:
                raise ProviderResponseError(
                    f"content-understanding: poll response body was not valid JSON: {exc}"
                ) from exc
            status = payload.get("status")
            if status == "Succeeded":
                return payload
            if status in _TERMINAL_FAILURE_STATUSES:
                raise ProviderResponseError(f"content-understanding: operation {status}: {payload}")
            if time.monotonic() >= deadline:
                raise ProviderResponseError("content-understanding: polling timed out")
            retry_after_header = response.headers.get("retry-after")
            if retry_after_header:
                delay = float(retry_after_header)
            else:
                delay = _DEFAULT_POLL_INTERVAL_SECONDS
            time.sleep(delay)


def _object_of(decoded: dict[str, Any], key: str) -> dict[str, Any]:
    value = decoded.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProviderResponseError(
            f"content-understanding: expected an object for field {key!r}, "
            f"got {type(value).__name__}"
        )
    return value


def _array_of(decoded: dict[str, Any], key: str) -> list[Any]:
    value = decoded.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProviderResponseError(
            f"content-understanding: expected an array for field {key!r}, "
            f"got {type(value).__name__}"
        )
    return value


def _optional_str(container: dict[str, Any], key: str) -> str | None:
    value = container.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProviderResponseError(
            f"content-understanding: expected a string for field {key!r}, "
            f"got {type(value).__name__}"
        )
    return value


def _optional_number(container: dict[str, Any], key: str) -> float | None:
    value = container.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderResponseError(
            f"content-understanding: expected a number for field {key!r}, "
            f"got {type(value).__name__}"
        )
    return float(value)


def _optional_money(container: dict[str, Any], key: str) -> tuple[float | None, str | None]:
    """Return amount and ISO currency from a decoded Content Understanding money field.

    The deployed receipt analyzers represent monetary values as objects with
    ``Amount`` and ``CurrencyCode`` properties. Scalar numbers remain accepted for
    compatibility with older analyzer definitions.
    """
    value = container.get(key)
    if value is None:
        return None, None
    if isinstance(value, bool):
        raise ProviderResponseError(
            f"content-understanding: expected money for field {key!r}, got bool"
        )
    if isinstance(value, (int, float)):
        return float(value), None
    if not isinstance(value, dict):
        raise ProviderResponseError(
            f"content-understanding: expected money for field {key!r}, "
            f"got {type(value).__name__}"
        )
    return _optional_number(value, "Amount"), _optional_str(value, "CurrencyCode")


def _first_money_amount(container: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in container:
            amount, _ = _optional_money(container, key)
            return amount
    return None


def _first_optional_str(container: dict[str, Any], *keys: str) -> str | None:
    """Return the first *present* key's decoded string value, trying ``keys`` in order.

    Used where the exact documented field name is ambiguous (e.g. line-item
    ``Description``/``Name``); only one of the alternates is expected to appear on
    any given analyzer version, so the first key actually present in the decoded
    payload wins rather than guessing based on non-``None`` value.
    """
    for key in keys:
        if key in container:
            return _optional_str(container, key)
    return None


def _first_array(container: dict[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        if key in container:
            return _array_of(container, key)
    return []


def _as_object_entry(entry: Any, *, array_field: str) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ProviderResponseError(
            f"content-understanding: expected an object entry in {array_field!r}, "
            f"got {type(entry).__name__}"
        )
    return entry


def _map_payment_method(entry: Any) -> dict[str, Any]:
    fields = _as_object_entry(entry, array_field="PaymentMethods")
    return {
        "method": _optional_str(fields, "Method"),
        "amount": _optional_number(fields, "Amount"),
        "card_last4": _optional_str(fields, "CardLast4"),
    }


def _map_line_item(entry: Any) -> dict[str, Any]:
    fields = _as_object_entry(entry, array_field="LineItems")
    return {
        "description": _optional_str(fields, "Description"),
        "product_code": _optional_str(fields, "ProductCode"),
        "quantity": _optional_number(fields, "Quantity"),
        "unit": _optional_str(fields, "Unit"),
        "unit_price": _optional_number(fields, "UnitPrice"),
        "discount": _optional_number(fields, "Discount"),
        "tax": _optional_number(fields, "Tax"),
        "total_price": _optional_number(fields, "TotalPrice"),
    }


def map_expense_receipt_fields(decoded: dict[str, Any]) -> dict[str, Any]:
    """Map the ``expense_receipt_v1`` custom analyzer's decoded fields to canonical shape.

    The custom analyzer's field schema (``configs/content-understanding/
    expense_receipt_v1.json``) uses PascalCase property names that do not match
    ``CanonicalReceipt``'s snake_case ones, so each property is mapped explicitly
    rather than relying on the decoded payload validating directly.
    """
    merchant = _object_of(decoded, "Merchant")
    transaction = _object_of(decoded, "Transaction")
    amounts = _object_of(decoded, "Amounts")
    payment_method_entries = _array_of(decoded, "PaymentMethods")
    line_item_entries = _array_of(decoded, "LineItems")

    return {
        "merchant": {
            "name": _optional_str(merchant, "Name"),
            "address": _optional_str(merchant, "Address"),
            "phone": _optional_str(merchant, "Phone"),
            "tax_id": _optional_str(merchant, "TaxId"),
        },
        "transaction": {
            "date": _optional_str(transaction, "Date"),
            "time": _optional_str(transaction, "Time"),
            "receipt_number": _optional_str(transaction, "ReceiptNumber"),
            "currency": _optional_str(transaction, "Currency"),
            "country": _optional_str(transaction, "Country"),
        },
        "amounts": {
            "subtotal": _optional_number(amounts, "Subtotal"),
            "discount": _optional_number(amounts, "Discount"),
            "tax": _optional_number(amounts, "Tax"),
            "tip": _optional_number(amounts, "Tip"),
            "total": _optional_number(amounts, "Total"),
        },
        "payment_methods": (
            [_map_payment_method(entry) for entry in payment_method_entries]
            if payment_method_entries
            else None
        ),
        "receipt_type": _optional_str(decoded, "ReceiptType"),
        "line_items": (
            [_map_line_item(entry) for entry in line_item_entries] if line_item_entries else None
        ),
    }


def _map_prebuilt_receipt_line_item(entry: Any) -> dict[str, Any]:
    fields = _as_object_entry(entry, array_field="LineItems")
    return {
        "description": _first_optional_str(fields, "Description", "Name"),
        "product_code": _optional_str(fields, "ProductCode"),
        "quantity": _optional_number(fields, "Quantity"),
        "unit": _optional_str(fields, "QuantityUnit"),
        "unit_price": _first_money_amount(fields, "UnitPrice", "Price"),
        "discount": None,
        "tax": None,
        "total_price": _first_money_amount(fields, "TotalAmount", "TotalPrice"),
    }


def _tax_details_total(entries: list[Any]) -> float | None:
    """Sum ``Amount`` across ``TaxDetails`` entries when no single ``TotalTax`` exists.

    Returns ``None`` (rather than ``0.0``) when no entry carries a numeric ``Amount``,
    since an all-missing sum would otherwise be indistinguishable from a genuine zero
    tax amount.
    """
    total = 0.0
    found_any = False
    for entry in entries:
        fields = _as_object_entry(entry, array_field="TaxDetails")
        amount = _first_money_amount(fields, "Amount")
        if amount is not None:
            total += amount
            found_any = True
    return total if found_any else None


def _resolve_prebuilt_receipt_tax(decoded: dict[str, Any]) -> float | None:
    """Prefer aggregate tax money; fall back to summing ``TaxDetails``."""
    for key in ("TotalTaxAmount", "TotalTax"):
        if key in decoded:
            amount = _first_money_amount(decoded, key)
            if amount is not None:
                return amount
    return _tax_details_total(_array_of(decoded, "TaxDetails"))


def _prebuilt_receipt_currency(decoded: dict[str, Any]) -> str | None:
    for key in (
        "TotalAmount",
        "GrossAmount",
        "SubtotalAmount",
        "TotalTaxAmount",
        "TipAmount",
        "Balance",
    ):
        if key not in decoded:
            continue
        _, currency = _optional_money(decoded, key)
        if currency is not None:
            return currency

    for entry in _array_of(decoded, "LineItems"):
        fields = _as_object_entry(entry, array_field="LineItems")
        for key in ("TotalAmount", "UnitPrice"):
            if key not in fields:
                continue
            _, currency = _optional_money(fields, key)
            if currency is not None:
                return currency
    return None


def _map_prebuilt_receipt_fields(
    decoded: dict[str, Any], content: dict[str, Any]
) -> dict[str, Any]:
    """Map the deployed ``prebuilt-receipt`` category schemas.

    ``prebuilt-receipt`` routes each document to ``receipt.generic``,
    ``receipt.hotel``, or ``other``. The two receipt schemas were verified from the
    deployed 2025-11-01 analyzer definitions; their monetary fields are nested money
    objects rather than the scalar aliases used by Document Intelligence.
    """
    category = content.get("category")
    if category not in {"receipt.generic", "receipt.hotel", "other", None}:
        raise ProviderResponseError(
            f"content-understanding: unsupported prebuilt receipt category {category!r}"
        )
    if category == "other":
        return {"receipt_type": "other"}

    is_hotel = category == "receipt.hotel"
    line_item_entries = _array_of(decoded, "LineItems")

    return {
        "merchant": {
            "name": _optional_str(decoded, "MerchantName"),
            "address": _optional_str(decoded, "MerchantAddress"),
            "phone": _optional_str(decoded, "MerchantPhoneNumber"),
            "tax_id": None,
        },
        "transaction": {
            "date": None if is_hotel else _optional_str(decoded, "TransactionDate"),
            "time": None if is_hotel else _optional_str(decoded, "TransactionTime"),
            "receipt_number": None,
            "currency": _prebuilt_receipt_currency(decoded),
            "country": _optional_str(decoded, "CountryRegion"),
        },
        "amounts": {
            "subtotal": (
                None
                if is_hotel
                else _first_money_amount(decoded, "SubtotalAmount", "Subtotal")
            ),
            "discount": None,
            "tax": _resolve_prebuilt_receipt_tax(decoded),
            "tip": None if is_hotel else _first_money_amount(decoded, "TipAmount", "Tip"),
            "total": _first_money_amount(
                decoded, "GrossAmount" if is_hotel else "TotalAmount", "Total"
            ),
        },
        "payment_methods": None,
        "receipt_type": "hotel" if is_hotel else "generic",
        "line_items": (
            [_map_prebuilt_receipt_line_item(entry) for entry in line_item_entries]
            if line_item_entries
            else None
        ),
    }


def _first_content(result: dict[str, Any]) -> dict[str, Any]:
    contents = result.get("result", {}).get("contents")
    if not contents:
        raise ProviderResponseError("content-understanding: result contained no contents")
    first_content: dict[str, Any] = contents[0]
    return first_content


def _extract_pages(usage_payload: dict[str, Any]) -> int | None:
    """Document page counts are billed at one of three tiers
    (``documentPagesStandard`` for layout+OCR, ``documentPagesBasic`` for OCR-only,
    ``documentPagesMinimal`` for digital formats needing no OCR); at most one is
    expected to be populated for a single-image analyze call, so the first present
    value is used.
    """
    for pages_field in ("documentPagesStandard", "documentPagesBasic", "documentPagesMinimal"):
        candidate = usage_payload.get(pages_field)
        if candidate is None:
            continue
        if not isinstance(candidate, int):
            raise ProviderResponseError(
                f"content-understanding: expected an integer for usage.{pages_field}, "
                f"got {type(candidate).__name__}"
            )
        return candidate
    return None


def _extract_contextualization_tokens(usage_payload: dict[str, Any]) -> int | None:
    contextualization_tokens = usage_payload.get("contextualizationTokens")
    if contextualization_tokens is not None and not isinstance(contextualization_tokens, int):
        raise ProviderResponseError(
            "content-understanding: expected an integer for usage.contextualizationTokens, "
            f"got {type(contextualization_tokens).__name__}"
        )
    return contextualization_tokens


def _model_token_measurements(
    tokens_payload: dict[str, Any], usage_payload: dict[str, Any]
) -> list[ComponentMeasurement]:
    """Split ``usage.tokens`` into one zero-latency ``model:<name>`` measurement per model.

    ``usage.tokens`` is a flat dict keyed like ``"<model>-input"``/``"<model>-output"``
    (and other token-kind suffixes, e.g. ``"<model>-cachedInput"``). The model name is
    recovered by stripping the *last* ``-<kind>`` segment: model names such as
    ``gpt-5.4-mini``/``text-embedding-3-large`` contain hyphens themselves, but the
    token-kind suffix is always the final hyphen-delimited segment, so splitting on
    the last ``-`` reliably separates the two. Suffixes other than ``input``/
    ``output`` still count toward that model's ``total_tokens`` but are not
    attributed to either side, since ``Usage`` has no dedicated field for them.
    Each returned measurement carries the full ``usage`` payload as
    ``provider_metadata`` so nothing is lost relative to the un-split view.
    """
    order: list[str] = []
    input_totals: dict[str, int] = {}
    output_totals: dict[str, int] = {}
    grand_totals: dict[str, int] = {}
    for key, value in tokens_payload.items():
        if not isinstance(value, int):
            raise ProviderResponseError(
                f"content-understanding: expected an integer for usage.tokens[{key!r}], "
                f"got {type(value).__name__}"
            )
        if "-" not in key:
            raise ProviderResponseError(
                f"content-understanding: usage.tokens key {key!r} has no '-<kind>' "
                "suffix to attribute it to a model"
            )
        model_name, _, suffix = key.rpartition("-")
        if model_name not in grand_totals:
            order.append(model_name)
            grand_totals[model_name] = 0
        grand_totals[model_name] += value
        if suffix == "input":
            input_totals[model_name] = input_totals.get(model_name, 0) + value
        elif suffix == "output":
            output_totals[model_name] = output_totals.get(model_name, 0) + value

    return [
        ComponentMeasurement(
            component=f"model:{model_name}",
            latency_ms=0.0,
            usage=Usage(
                input_tokens=input_totals.get(model_name),
                output_tokens=output_totals.get(model_name),
                total_tokens=grand_totals[model_name],
                provider_metadata=usage_payload,
            ),
        )
        for model_name in order
    ]


def _sum_tokens(tokens_payload: dict[str, Any]) -> tuple[int, int, int]:
    """Sum ``usage.tokens`` counts (keyed like ``"{model}-input"``/``"{model}-output"``).

    Returns ``(input_tokens, output_tokens, total_tokens)``. Keys that don't end in
    ``-input``/``-output`` (e.g. a ``-cachedInput`` variant) still count toward the
    total but are not attributed to either side.
    """
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    for key, value in tokens_payload.items():
        if not isinstance(value, int):
            raise ProviderResponseError(
                f"content-understanding: expected an integer for usage.tokens[{key!r}], "
                f"got {type(value).__name__}"
            )
        total_tokens += value
        if key.endswith("-input"):
            input_tokens += value
        elif key.endswith("-output"):
            output_tokens += value
    return input_tokens, output_tokens, total_tokens


def _usage_from_operation(payload: dict[str, Any]) -> Usage:
    """Map the analyze operation's ``usage`` (``UsageDetails``) into ``Usage``.

    ``usage`` is a sibling of ``result`` in the GA 2025-11-01 operation-status
    envelope (not nested inside it). Only fields actually present in the payload
    are populated - nothing is invented when the provider omits a field. Used by
    the OCR-pipeline extractor (``ContentUnderstandingOcrExtractor``), which keeps
    a single aggregated ``Usage`` per call; native analyzer adapters use
    ``_component_measurements_from_operation`` instead so per-model token costs can
    be split into distinct ``model:<name>`` measurements.
    """
    usage_payload = payload.get("usage")
    if usage_payload is None:
        return Usage()
    if not isinstance(usage_payload, dict):
        raise ProviderResponseError(
            f"content-understanding: expected an object for 'usage', "
            f"got {type(usage_payload).__name__}"
        )

    pages = _extract_pages(usage_payload)
    contextualization_tokens = _extract_contextualization_tokens(usage_payload)

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    tokens_payload = usage_payload.get("tokens")
    if tokens_payload is not None:
        if not isinstance(tokens_payload, dict):
            raise ProviderResponseError(
                f"content-understanding: expected an object for 'usage.tokens', "
                f"got {type(tokens_payload).__name__}"
            )
        input_tokens, output_tokens, total_tokens = _sum_tokens(tokens_payload)

    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        pages=pages,
        contextualization_tokens=contextualization_tokens,
        provider_metadata=usage_payload,
    )


def _component_measurements_from_operation(
    payload: dict[str, Any], *, component: str, latency_ms: float
) -> list[ComponentMeasurement]:
    """Build the full component-measurement list for a native (non-OCR-pipeline) analyze call.

    The first measurement (named ``component``) carries the actual wall-clock
    ``latency_ms`` plus only the service-level usage signals (``pages``/
    ``contextualization_tokens``); token counts are intentionally *not* duplicated
    onto it. Each model contributing to ``usage.tokens`` gets its own zero-latency
    ``model:<name>`` measurement (see ``_model_token_measurements``) so the
    evaluation layer can price per-model token costs (mapping ``model:<id>`` to
    ``<id>``) instead of a single blended total.
    """
    usage_payload = payload.get("usage")
    if usage_payload is None:
        return [ComponentMeasurement(component=component, latency_ms=latency_ms, usage=Usage())]
    if not isinstance(usage_payload, dict):
        raise ProviderResponseError(
            f"content-understanding: expected an object for 'usage', "
            f"got {type(usage_payload).__name__}"
        )

    pages = _extract_pages(usage_payload)
    contextualization_tokens = _extract_contextualization_tokens(usage_payload)
    service_measurement = ComponentMeasurement(
        component=component,
        latency_ms=latency_ms,
        usage=Usage(
            pages=pages,
            contextualization_tokens=contextualization_tokens,
            provider_metadata=usage_payload,
        ),
    )

    tokens_payload = usage_payload.get("tokens")
    if tokens_payload is None:
        return [service_measurement]
    if not isinstance(tokens_payload, dict):
        raise ProviderResponseError(
            f"content-understanding: expected an object for 'usage.tokens', "
            f"got {type(tokens_payload).__name__}"
        )
    return [service_measurement, *_model_token_measurements(tokens_payload, usage_payload)]


def extract_content_understanding_fields(
    client: ContentUnderstandingClient,
    analyzer_id: str,
    image_bytes: bytes,
    media_type: str,
) -> tuple[dict[str, Any], dict[str, Any], tuple[ComponentMeasurement, ...]]:
    """Run a custom analyzer and return its decoded project-native fields."""

    start = time.monotonic()
    result = client.analyze(analyzer_id, image_bytes, media_type)
    latency_ms = elapsed_ms(start)
    components = tuple(
        _component_measurements_from_operation(
            result,
            component="native_extraction",
            latency_ms=latency_ms,
        )
    )
    try:
        content = _first_content(result)
        fields = content.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise ProviderResponseError(
                "content-understanding: result contained no field object"
            )
        decoded: dict[str, Any] = {}
        for name, value in fields.items():
            if not isinstance(name, str) or not isinstance(value, dict):
                raise ProviderResponseError(
                    "content-understanding: result contained an invalid field entry"
                )
            decoded[name] = _decode_content_field(value)
    except ProviderResponseError as exc:
        raise InvalidSolutionOutput(
            f"native_extraction: response fields could not be decoded: {exc}",
            raw_response=result,
            components=list(components),
        ) from exc
    return decoded, result, components


def _execute_native_analysis(
    client: ContentUnderstandingClient,
    analyzer_id: str,
    image_bytes: bytes,
    media_type: str,
    *,
    field_mapper: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
) -> AdapterResponse:
    """Shared analyze/decode/validate flow for native (non-OCR-pipeline) analyzers.

    Only ``field_mapper`` differs between the custom-analyzer and ``prebuilt-receipt``
    adapters below; the analyze/poll call, field decoding, usage extraction, and
    ``InvalidSolutionOutput`` boundary are identical.
    """
    decoded, result, components = extract_content_understanding_fields(
        client,
        analyzer_id,
        image_bytes,
        media_type,
    )
    try:
        content = _first_content(result)
        receipt = CanonicalReceipt.model_validate(field_mapper(decoded, content))
    except (ProviderResponseError, ValidationError) as exc:
        raise InvalidSolutionOutput(
            f"native_extraction: response could not be parsed into CanonicalReceipt: {exc}",
            raw_response=result,
            components=list(components),
        ) from exc
    return AdapterResponse(
        receipt=receipt,
        raw_response=result,
        components=list(components),
    )


class ContentUnderstandingCustomAdapter(SolutionAdapter):
    """Native custom-analyzer extraction (``expense_receipt_v1``)."""

    def __init__(
        self, solution_id: str, *, client: ContentUnderstandingClient, analyzer_id: str
    ) -> None:
        super().__init__(solution_id)
        self._client = client
        self._analyzer_id = analyzer_id

    def execute(self, image_bytes: bytes, media_type: str) -> AdapterResponse:
        return _execute_native_analysis(
            self._client,
            self._analyzer_id,
            image_bytes,
            media_type,
            field_mapper=lambda decoded, _content: map_expense_receipt_fields(decoded),
        )


class ContentUnderstandingPrebuiltReceiptAdapter(SolutionAdapter):
    """Native ``prebuilt-receipt`` domain-analyzer extraction.

    Maps the deployed ``receipt.generic`` and ``receipt.hotel`` schemas, verified via
    ``GET /contentunderstanding/analyzers/<category>`` for API version 2025-11-01.
    """

    def __init__(
        self, solution_id: str, *, client: ContentUnderstandingClient, analyzer_id: str
    ) -> None:
        super().__init__(solution_id)
        self._client = client
        self._analyzer_id = analyzer_id

    def execute(self, image_bytes: bytes, media_type: str) -> AdapterResponse:
        return _execute_native_analysis(
            self._client,
            self._analyzer_id,
            image_bytes,
            media_type,
            field_mapper=_map_prebuilt_receipt_fields,
        )


class ContentUnderstandingOcrExtractor:
    """OCR/ingestion extraction via ``prebuilt-read``/``prebuilt-layout`` analyzers."""

    def __init__(self, *, client: ContentUnderstandingClient, analyzer_id: str) -> None:
        self._client = client
        self._analyzer_id = analyzer_id

    def extract_text(self, image_bytes: bytes, media_type: str) -> OcrExtraction:
        start = time.monotonic()
        result = self._client.analyze(self._analyzer_id, image_bytes, media_type)
        latency_ms = elapsed_ms(start)

        contents = result.get("result", {}).get("contents")
        if not contents:
            raise ProviderResponseError("content-understanding: result contained no contents")
        markdown_segments = []
        for content in contents:
            markdown = content.get("markdown")
            if not markdown:
                raise ProviderResponseError(
                    "content-understanding: a content block had no markdown"
                )
            markdown_segments.append(markdown)
        text = "\n\n".join(markdown_segments)

        usage = _usage_from_operation(result)
        measurement = ComponentMeasurement(component="ocr", latency_ms=latency_ms, usage=usage)
        return OcrExtraction(text=text, raw_response=result, measurement=measurement)
