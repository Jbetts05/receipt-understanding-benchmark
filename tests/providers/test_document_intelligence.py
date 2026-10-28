"""Tests for the Document Intelligence ``prebuilt-receipt`` adapter."""

from __future__ import annotations

from typing import Any

import pytest
from azure.ai.documentintelligence.models import (
    AnalyzedDocument,
    AnalyzeResult,
    CurrencyValue,
    DocumentField,
)
from azure.core.exceptions import ServiceRequestError

from receipt_bench.providers.base import ProviderResponseError
from receipt_bench.providers.document_intelligence import DocumentIntelligenceAdapter


class FakePoller:
    def __init__(self, result: AnalyzeResult) -> None:
        self._result = result

    def result(self) -> AnalyzeResult:
        return self._result


class FakeDocumentIntelligenceClient:
    def __init__(self, result: AnalyzeResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def begin_analyze_document(self, **kwargs: Any) -> FakePoller:
        self.calls.append(kwargs)
        return FakePoller(self._result)


class FailingDocumentIntelligenceClient:
    """Fakes a client whose call raises an azure-core SDK error immediately."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def begin_analyze_document(self, **kwargs: Any) -> FakePoller:
        raise self._error


def _string_field(value: str, confidence: float = 0.9) -> DocumentField:
    return DocumentField(type="string", value_string=value, content=value, confidence=confidence)


def _currency_field(
    amount: float, currency_code: str = "USD", confidence: float = 0.95
) -> DocumentField:
    return DocumentField(
        type="currency",
        value_currency=CurrencyValue(amount=amount, currency_code=currency_code),
        confidence=confidence,
    )


def _object_field(fields: dict[str, DocumentField]) -> DocumentField:
    return DocumentField(type="object", value_object=fields)


def _array_field(items: list[DocumentField]) -> DocumentField:
    return DocumentField(type="array", value_array=items)


def _build_analyze_result() -> AnalyzeResult:
    line_item = _object_field(
        {
            "Description": _string_field("Coffee"),
            "Quantity": DocumentField(type="number", value_number=2.0),
            "TotalPrice": _currency_field(6.0),
        }
    )
    payment = _object_field(
        {
            "Method": _string_field("visa"),
            "Amount": _currency_field(12.0),
        }
    )
    document = AnalyzedDocument(
        doc_type="receipt.retailMeal",
        confidence=0.98,
        fields={
            "MerchantName": _string_field("Contoso Cafe"),
            "MerchantAddress": _string_field("1 Main St"),
            "MerchantPhoneNumber": _string_field("555-0100"),
            "Total": _currency_field(12.0),
            "Subtotal": _currency_field(10.0),
            "TotalTax": _currency_field(2.0),
            "CountryRegion": DocumentField(type="countryRegion", value_country_region="GB"),
            "Items": _array_field([line_item]),
            "Payments": _array_field([payment]),
        },
    )
    return AnalyzeResult(
        api_version="2024-11-30",
        model_id="prebuilt-receipt",
        content="Contoso Cafe",
        pages=[],
        documents=[document],
    )


def test_execute_maps_native_fields_deterministically() -> None:
    result = _build_analyze_result()
    client = FakeDocumentIntelligenceClient(result)
    adapter = DocumentIntelligenceAdapter(
        "document-intelligence-receipt", client=client  # type: ignore[arg-type]
    )

    response = adapter.execute(b"fake-image-bytes", "image/png")

    receipt = response.receipt
    assert receipt.merchant.name == "Contoso Cafe"
    assert receipt.merchant.address == "1 Main St"
    assert receipt.merchant.phone == "555-0100"
    assert receipt.merchant.tax_id is None
    assert receipt.transaction.country == "GB"
    assert receipt.transaction.currency == "USD"
    assert receipt.transaction.receipt_number is None
    assert receipt.amounts.total == 12.0
    assert receipt.amounts.subtotal == 10.0
    assert receipt.amounts.tax == 2.0
    assert receipt.amounts.discount is None
    assert receipt.payment_methods is not None
    assert receipt.payment_methods[0].method == "visa"
    assert receipt.payment_methods[0].amount == 12.0
    assert receipt.payment_methods[0].card_last4 is None
    assert receipt.line_items is not None
    assert receipt.line_items[0].description == "Coffee"
    assert receipt.line_items[0].quantity == 2.0
    assert receipt.line_items[0].total_price == 6.0
    assert receipt.line_items[0].discount is None
    assert receipt.line_items[0].tax is None

    measurement = response.components[0]
    assert measurement.component == "native_extraction"
    field_confidence = measurement.usage.provider_metadata["field_confidence"]
    assert field_confidence["MerchantName"] == 0.9
    assert field_confidence["document"] == 0.98
    assert isinstance(response.raw_response, dict)

    call = client.calls[0]
    assert call["model_id"] == "prebuilt-receipt"
    assert call["content_type"] == "image/png"


def test_execute_raises_when_no_documents_analyzed() -> None:
    result = AnalyzeResult(
        api_version="2024-11-30", model_id="prebuilt-receipt", content="", pages=[], documents=[]
    )
    client = FakeDocumentIntelligenceClient(result)
    adapter = DocumentIntelligenceAdapter(
        "document-intelligence-receipt", client=client  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderResponseError, match="no documents were analyzed"):
        adapter.execute(b"fake-image-bytes", "image/png")


def test_execute_wraps_sdk_errors_as_provider_response_error() -> None:
    sdk_error = ServiceRequestError("connection reset")
    client = FailingDocumentIntelligenceClient(sdk_error)
    adapter = DocumentIntelligenceAdapter(
        "document-intelligence-receipt", client=client  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderResponseError, match="analyze call failed") as exc_info:
        adapter.execute(b"fake-image-bytes", "image/png")
    assert exc_info.value.__cause__ is sdk_error


def test_execute_preserves_nonstandard_currency_for_answer_scoring() -> None:
    document = AnalyzedDocument(
        doc_type="receipt.retailMeal",
        confidence=0.98,
        fields={
            "MerchantName": _string_field("Contoso Cafe"),
            "Total": _currency_field(12.0, currency_code="US"),
        },
    )
    result = AnalyzeResult(
        api_version="2024-11-30",
        model_id="prebuilt-receipt",
        content="Contoso Cafe",
        pages=[],
        documents=[document],
    )
    client = FakeDocumentIntelligenceClient(result)
    adapter = DocumentIntelligenceAdapter(
        "document-intelligence-receipt", client=client  # type: ignore[arg-type]
    )

    response = adapter.execute(b"fake-image-bytes", "image/png")

    assert response.receipt.transaction.currency == "US"
    assert isinstance(response.raw_response, dict)
    assert len(response.components) == 1
    measurement = response.components[0]
    assert measurement.component == "native_extraction"
    assert measurement.usage.provider_metadata["field_confidence"]["document"] == 0.98
    assert measurement.usage.provider_metadata["field_confidence"]["MerchantName"] == 0.9
