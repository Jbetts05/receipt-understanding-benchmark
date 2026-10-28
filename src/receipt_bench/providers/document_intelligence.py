"""Document Intelligence ``prebuilt-receipt`` adapter.

Maps the native ``prebuilt-receipt`` (2024-11-30 GA) field schema deterministically
into ``CanonicalReceipt``. Fields that the prebuilt model does not extract (for
example a merchant tax ID or a receipt number) are left as ``None`` rather than
guessed. Per-field confidence scores are preserved under
``Usage.provider_metadata`` for diagnostics, since ``CanonicalReceipt`` itself has no
confidence fields.
"""

from __future__ import annotations

import io
import time
from datetime import date as Date
from datetime import time as Time

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzedDocument, DocumentField
from azure.core.exceptions import AzureError
from pydantic import ValidationError

from receipt_bench.models import (
    Amounts,
    CanonicalReceipt,
    ComponentMeasurement,
    LineItem,
    Merchant,
    PaymentMethod,
    Transaction,
    Usage,
)
from receipt_bench.providers._timing import elapsed_ms
from receipt_bench.providers.base import (
    AdapterResponse,
    InvalidSolutionOutput,
    ProviderResponseError,
    SolutionAdapter,
)

_MODEL_ID = "prebuilt-receipt"


def _string_of(fields: dict[str, DocumentField], name: str) -> str | None:
    field = fields.get(name)
    if field is None:
        return None
    return field.content


def _amount_of(fields: dict[str, DocumentField], name: str) -> float | None:
    field = fields.get(name)
    if field is None or field.value_currency is None:
        return None
    return field.value_currency.amount


def _currency_code_of(fields: dict[str, DocumentField], name: str) -> str | None:
    field = fields.get(name)
    if field is None or field.value_currency is None:
        return None
    return field.value_currency.currency_code


def _country_of(fields: dict[str, DocumentField], name: str) -> str | None:
    field = fields.get(name)
    if field is None:
        return None
    return field.value_country_region


def _date_of(fields: dict[str, DocumentField], name: str) -> Date | None:
    field = fields.get(name)
    if field is None:
        return None
    return field.value_date


def _time_of(fields: dict[str, DocumentField], name: str) -> Time | None:
    field = fields.get(name)
    if field is None:
        return None
    return field.value_time


def _number_of(fields: dict[str, DocumentField], name: str) -> float | None:
    field = fields.get(name)
    if field is None:
        return None
    return field.value_number


def _confidence_map(fields: dict[str, DocumentField], prefix: str) -> dict[str, float]:
    confidences: dict[str, float] = {}
    for name, field in fields.items():
        if field.confidence is not None:
            confidences[f"{prefix}{name}"] = field.confidence
    return confidences


def _map_payment_method(fields: dict[str, DocumentField]) -> PaymentMethod:
    return PaymentMethod(
        method=_string_of(fields, "Method"),
        amount=_amount_of(fields, "Amount"),
        card_last4=None,
    )


def _map_line_item(fields: dict[str, DocumentField]) -> LineItem:
    return LineItem(
        description=_string_of(fields, "Description"),
        product_code=_string_of(fields, "ProductCode"),
        quantity=_number_of(fields, "Quantity"),
        unit=_string_of(fields, "QuantityUnit"),
        unit_price=_amount_of(fields, "Price"),
        discount=None,
        tax=None,
        total_price=_amount_of(fields, "TotalPrice"),
    )


def _map_object_array(
    fields: dict[str, DocumentField], name: str
) -> list[dict[str, DocumentField]]:
    field = fields.get(name)
    if field is None or field.value_array is None:
        return []
    entries: list[dict[str, DocumentField]] = []
    for item in field.value_array:
        if item.value_object is not None:
            entries.append(item.value_object)
    return entries


def _document_confidences(document: AnalyzedDocument) -> dict[str, float]:
    fields = document.fields if document.fields is not None else {}
    confidences = _confidence_map(fields, "")
    if document.confidence is not None:
        confidences["document"] = document.confidence
    return confidences


def _build_receipt(document: AnalyzedDocument) -> CanonicalReceipt:
    fields = document.fields if document.fields is not None else {}

    merchant = Merchant(
        name=_string_of(fields, "MerchantName"),
        address=_string_of(fields, "MerchantAddress"),
        phone=_string_of(fields, "MerchantPhoneNumber"),
        tax_id=None,
    )
    transaction = Transaction(
        date=_date_of(fields, "TransactionDate"),
        time=_time_of(fields, "TransactionTime"),
        receipt_number=None,
        currency=_currency_code_of(fields, "Total"),
        country=_country_of(fields, "CountryRegion"),
    )
    amounts = Amounts(
        subtotal=_amount_of(fields, "Subtotal"),
        discount=None,
        tax=_amount_of(fields, "TotalTax"),
        tip=_amount_of(fields, "Tip"),
        total=_amount_of(fields, "Total"),
    )
    payment_entries = _map_object_array(fields, "Payments")
    payment_methods = [_map_payment_method(entry) for entry in payment_entries]
    line_items = [_map_line_item(entry) for entry in _map_object_array(fields, "Items")]

    return CanonicalReceipt(
        merchant=merchant,
        transaction=transaction,
        amounts=amounts,
        payment_methods=payment_methods if payment_methods else None,
        receipt_type=_string_of(fields, "ReceiptType"),
        line_items=line_items if line_items else None,
    )


class DocumentIntelligenceAdapter(SolutionAdapter):
    """Native ``prebuilt-receipt`` extraction via Azure Document Intelligence."""

    def __init__(self, solution_id: str, *, client: DocumentIntelligenceClient) -> None:
        super().__init__(solution_id)
        self._client = client

    def execute(self, image_bytes: bytes, media_type: str) -> AdapterResponse:
        start = time.monotonic()
        try:
            poller = self._client.begin_analyze_document(
                model_id=_MODEL_ID,
                body=io.BytesIO(image_bytes),
                content_type=media_type,
            )
            result = poller.result()
        except AzureError as exc:
            raise ProviderResponseError(
                f"document-intelligence: analyze call failed: {exc}"
            ) from exc
        latency_ms = elapsed_ms(start)

        if not result.documents:
            raise ProviderResponseError("document-intelligence: no documents were analyzed")
        document = result.documents[0]

        raw_response = result.as_dict()
        confidences = _document_confidences(document)
        usage = Usage(
            pages=len(result.pages) if result.pages else None,
            provider_metadata={"field_confidence": confidences},
        )
        measurement = ComponentMeasurement(
            component="native_extraction", latency_ms=latency_ms, usage=usage
        )
        try:
            receipt = _build_receipt(document)
        except ValidationError as exc:
            raise InvalidSolutionOutput(
                f"native_extraction: response could not be parsed into CanonicalReceipt: {exc}",
                raw_response=raw_response,
                components=[measurement],
            ) from exc
        return AdapterResponse(receipt=receipt, raw_response=raw_response, components=[measurement])
