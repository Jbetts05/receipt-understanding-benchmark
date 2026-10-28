"""Tests for the Content Understanding adapter (analyze/poll, custom + OCR modes)."""

from __future__ import annotations

import datetime
from typing import Any

import httpx
import pytest
import respx

import receipt_bench.providers.content_understanding as content_understanding
from receipt_bench.providers.base import InvalidSolutionOutput, ProviderResponseError
from receipt_bench.providers.content_understanding import (
    ContentUnderstandingClient,
    ContentUnderstandingCustomAdapter,
    ContentUnderstandingOcrExtractor,
    ContentUnderstandingPrebuiltReceiptAdapter,
)

_BASE_URL = "https://fake-account.cognitiveservices.azure.com"
_ANALYZE_URL = f"{_BASE_URL}/contentunderstanding/analyzers/expense_receipt_v1:analyzeBinary"
_PREBUILT_RECEIPT_ANALYZE_URL = (
    f"{_BASE_URL}/contentunderstanding/analyzers/prebuilt-receipt:analyzeBinary"
)
_OPERATION_URL = f"{_BASE_URL}/contentunderstanding/operations/op-1"


def _client() -> ContentUnderstandingClient:
    return ContentUnderstandingClient(
        client=httpx.Client(base_url=_BASE_URL, headers={"Ocp-Apim-Subscription-Key": "fake-key"})
    )


@respx.mock
def test_ensure_custom_analyzer_creates_and_waits_for_schema_hash() -> None:
    analyzer_url = (
        f"{_BASE_URL}/contentunderstanding/analyzers/custom_abc"
    )
    respx.get(analyzer_url, params={"api-version": "2025-11-01"}).mock(
        return_value=httpx.Response(404)
    )
    put = respx.put(
        analyzer_url,
        params={"api-version": "2025-11-01"},
    ).mock(
        return_value=httpx.Response(
            202,
            headers={"Operation-Location": _OPERATION_URL},
        )
    )
    respx.get(_OPERATION_URL).mock(
        return_value=httpx.Response(200, json={"status": "Succeeded"})
    )
    definition = {
        "baseAnalyzerId": "prebuilt-document",
        "tags": {"schemaSha256": "a" * 64},
    }

    _client().ensure_analyzer(
        "custom_abc",
        definition,
        schema_hash="a" * 64,
    )

    assert put.called
    assert put.calls[0].request.content


@respx.mock
def test_ensure_custom_analyzer_reuses_matching_definition() -> None:
    analyzer_url = (
        f"{_BASE_URL}/contentunderstanding/analyzers/custom_abc"
    )
    get = respx.get(
        analyzer_url,
        params={"api-version": "2025-11-01"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"tags": {"schemaSha256": "a" * 64}},
        )
    )

    _client().ensure_analyzer(
        "custom_abc",
        {"tags": {"schemaSha256": "a" * 64}},
        schema_hash="a" * 64,
    )

    assert get.called
    assert not respx.put(analyzer_url).called


@respx.mock
def test_delete_custom_analyzer_is_idempotent() -> None:
    analyzer_url = f"{_BASE_URL}/contentunderstanding/analyzers/custom_abc"
    deleted = respx.delete(
        analyzer_url,
        params={"api-version": "2025-11-01"},
    ).mock(return_value=httpx.Response(404))

    _client().delete_analyzer("custom_abc")

    assert deleted.called


def _custom_fields_payload() -> dict[str, Any]:
    """A decoded-shape Content Understanding response mirroring the deployed analyzer schema."""
    return {
        "fields": {
            "Merchant": {
                "type": "object",
                "valueObject": {
                    "Name": {"type": "string", "valueString": "Contoso Cafe"},
                    "Address": {"type": "string", "valueString": "1 Main St"},
                    "Phone": {"type": "string", "valueString": "555-0100"},
                    "TaxId": {"type": "string", "valueString": "TX-123"},
                },
            },
            "Transaction": {
                "type": "object",
                "valueObject": {
                    "Date": {"type": "date", "valueDate": "2026-08-28"},
                    "Time": {"type": "time", "valueTime": "12:15:00"},
                    "ReceiptNumber": {"type": "string", "valueString": "R-42"},
                    "Currency": {"type": "string", "valueString": "GBP"},
                    "Country": {"type": "string", "valueString": "GB"},
                },
            },
            "Amounts": {
                "type": "object",
                "valueObject": {
                    "Subtotal": {"type": "number", "valueNumber": 10.0},
                    "Discount": {"type": "number", "valueNumber": 0.0},
                    "Tax": {"type": "number", "valueNumber": 2.0},
                    "Tip": {"type": "number", "valueNumber": 0.0},
                    "Total": {"type": "number", "valueNumber": 12.0},
                },
            },
            "PaymentMethods": {
                "type": "array",
                "valueArray": [
                    {
                        "type": "object",
                        "valueObject": {
                            "Method": {"type": "string", "valueString": "visa"},
                            "Amount": {"type": "number", "valueNumber": 12.0},
                            "CardLast4": {"type": "string", "valueString": "1234"},
                        },
                    }
                ],
            },
            "ReceiptType": {"type": "string", "valueString": "meal"},
            "LineItems": {
                "type": "array",
                "valueArray": [
                    {
                        "type": "object",
                        "valueObject": {
                            "Description": {"type": "string", "valueString": "Coffee"},
                            "Quantity": {"type": "number", "valueNumber": 2.0},
                            "Unit": {"type": "string", "valueString": "each"},
                            "UnitPrice": {"type": "number", "valueNumber": 3.0},
                            "TotalPrice": {"type": "number", "valueNumber": 6.0},
                        },
                    }
                ],
            },
        }
    }


def _money_field(amount: float | None, currency: str | None = "USD") -> dict[str, Any]:
    value_object: dict[str, Any] = {
        "CurrencyCode": {"type": "string", "valueString": currency},
        "Amount": {"type": "number", "valueNumber": amount},
    }
    return {"type": "object", "valueObject": value_object}


def _prebuilt_receipt_fields_payload(*, tax_details_only: bool = False) -> dict[str, Any]:
    """A ``receipt.generic`` response matching the deployed 2025-11-01 schema."""
    fields: dict[str, Any] = {
        "MerchantName": {"type": "string", "valueString": "Contoso Cafe"},
        "MerchantAddress": {"type": "string", "valueString": "1 Main St"},
        "MerchantPhoneNumber": {"type": "string", "valueString": "555-0100"},
        "TransactionDate": {"type": "date", "valueDate": "2026-08-28"},
        "TransactionTime": {"type": "time", "valueTime": "12:15:00"},
        "CountryRegion": {"type": "string", "valueString": "US"},
        "SubtotalAmount": _money_field(10.0),
        "TipAmount": _money_field(1.0),
        "TotalAmount": _money_field(13.0),
        "LineItems": {
            "type": "array",
            "valueArray": [
                {
                    "type": "object",
                    "valueObject": {
                        "Description": {"type": "string", "valueString": "Coffee"},
                        "Quantity": {"type": "number", "valueNumber": 2.0},
                        "UnitPrice": _money_field(1.5),
                        "TotalAmount": _money_field(3.0),
                        "ProductCode": {"type": "string", "valueString": "SKU-1"},
                        "QuantityUnit": {"type": "string", "valueString": "each"},
                    },
                }
            ],
        },
    }
    if tax_details_only:
        fields["TaxDetails"] = {
            "type": "array",
            "valueArray": [
                {
                    "type": "object",
                    "valueObject": {"Amount": _money_field(1.2)},
                },
                {
                    "type": "object",
                    "valueObject": {"Amount": _money_field(0.8)},
                },
            ],
        }
    else:
        fields["TotalTaxAmount"] = _money_field(2.0)
    return {"category": "receipt.generic", "fields": fields}


@respx.mock
def test_prebuilt_receipt_adapter_maps_documented_common_fields() -> None:
    respx.post(_PREBUILT_RECEIPT_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "Succeeded",
                "result": {"contents": [_prebuilt_receipt_fields_payload()]},
                "usage": {
                    "documentPagesBasic": 1,
                    "tokens": {"gpt-5.2-input": 90, "gpt-5.2-output": 30},
                },
            },
        )
    )
    adapter = ContentUnderstandingPrebuiltReceiptAdapter(
        "content-understanding-prebuilt-receipt", client=_client(), analyzer_id="prebuilt-receipt"
    )

    response = adapter.execute(b"fake-image-bytes", "image/png")

    receipt = response.receipt
    assert receipt.merchant.name == "Contoso Cafe"
    assert receipt.merchant.address == "1 Main St"
    assert receipt.merchant.phone == "555-0100"
    assert receipt.merchant.tax_id is None
    assert receipt.transaction.date == datetime.date(2026, 8, 28)
    assert receipt.transaction.time == datetime.time(12, 15, 0)
    assert receipt.transaction.country == "US"
    assert receipt.transaction.currency == "USD"
    assert receipt.transaction.receipt_number is None
    assert receipt.amounts.subtotal == 10.0
    assert receipt.amounts.tax == 2.0
    assert receipt.amounts.tip == 1.0
    assert receipt.amounts.total == 13.0
    assert receipt.amounts.discount is None
    assert receipt.payment_methods is None
    assert receipt.receipt_type == "generic"
    assert receipt.line_items is not None
    assert receipt.line_items[0].description == "Coffee"
    assert receipt.line_items[0].quantity == 2.0
    assert receipt.line_items[0].unit_price == 1.5
    assert receipt.line_items[0].total_price == 3.0
    assert receipt.line_items[0].product_code == "SKU-1"
    assert receipt.line_items[0].unit == "each"
    assert receipt.line_items[0].discount is None
    assert receipt.line_items[0].tax is None
    assert response.components[0].component == "native_extraction"
    service_usage = response.components[0].usage
    assert service_usage.pages == 1
    assert service_usage.input_tokens is None
    assert service_usage.output_tokens is None
    assert service_usage.total_tokens is None
    assert len(response.components) == 2
    assert response.components[1].component == "model:gpt-5.2"
    assert response.components[1].latency_ms == 0.0
    model_usage = response.components[1].usage
    assert model_usage.input_tokens == 90
    assert model_usage.output_tokens == 30
    assert model_usage.total_tokens == 120
    assert model_usage.pages is None
    assert isinstance(response.raw_response, dict)


@respx.mock
def test_prebuilt_receipt_adapter_maps_hotel_schema() -> None:
    payload = {
        "category": "receipt.hotel",
        "fields": {
            "MerchantName": {"type": "string", "valueString": "Contoso Hotel"},
            "MerchantAddress": {"type": "string", "valueString": "2 Main St"},
            "GrossAmount": _money_field(120.0, "GBP"),
            "Balance": _money_field(0.0, "GBP"),
            "ArrivalDate": {"type": "date", "valueDate": "2026-08-27"},
            "DepartureDate": {"type": "date", "valueDate": "2026-08-28"},
            "CountryRegion": {"type": "string", "valueString": "GB"},
            "LineItems": {
                "type": "array",
                "valueArray": [
                    {
                        "type": "object",
                        "valueObject": {
                            "Description": {"type": "string", "valueString": "Room"},
                            "TotalAmount": _money_field(120.0, "GBP"),
                            "Category": {"type": "string", "valueString": "Room"},
                        },
                    }
                ],
            },
        },
    }
    respx.post(_PREBUILT_RECEIPT_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={"status": "Succeeded", "result": {"contents": [payload]}},
        )
    )
    adapter = ContentUnderstandingPrebuiltReceiptAdapter(
        "content-understanding-prebuilt-receipt", client=_client(), analyzer_id="prebuilt-receipt"
    )

    response = adapter.execute(b"fake-image-bytes", "image/png")

    receipt = response.receipt
    assert receipt.merchant.name == "Contoso Hotel"
    assert receipt.transaction.currency == "GBP"
    assert receipt.transaction.country == "GB"
    assert receipt.transaction.date is None
    assert receipt.amounts.total == 120.0
    assert receipt.receipt_type == "hotel"
    assert receipt.line_items is not None
    assert receipt.line_items[0].description == "Room"
    assert receipt.line_items[0].total_price == 120.0


@respx.mock
def test_prebuilt_receipt_adapter_sums_tax_details_when_total_tax_absent() -> None:
    respx.post(_PREBUILT_RECEIPT_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "Succeeded",
                "result": {"contents": [_prebuilt_receipt_fields_payload(tax_details_only=True)]},
            },
        )
    )
    adapter = ContentUnderstandingPrebuiltReceiptAdapter(
        "content-understanding-prebuilt-receipt", client=_client(), analyzer_id="prebuilt-receipt"
    )

    response = adapter.execute(b"fake-image-bytes", "image/png")

    assert response.receipt.amounts.tax == pytest.approx(2.0)


@respx.mock
def test_prebuilt_receipt_adapter_falls_back_when_aggregate_tax_is_null() -> None:
    payload = _prebuilt_receipt_fields_payload(tax_details_only=True)
    payload["fields"]["TotalTaxAmount"] = _money_field(None)
    respx.post(_PREBUILT_RECEIPT_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={"status": "Succeeded", "result": {"contents": [payload]}},
        )
    )
    adapter = ContentUnderstandingPrebuiltReceiptAdapter(
        "content-understanding-prebuilt-receipt", client=_client(), analyzer_id="prebuilt-receipt"
    )

    response = adapter.execute(b"fake-image-bytes", "image/png")

    assert response.receipt.amounts.tax == pytest.approx(2.0)


@respx.mock
def test_prebuilt_receipt_adapter_preserves_unparseable_date_for_answer_scoring() -> None:
    payload = _prebuilt_receipt_fields_payload()
    payload["fields"]["TransactionDate"]["valueDate"] = "not-a-date"
    respx.post(_PREBUILT_RECEIPT_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "Succeeded",
                "result": {"contents": [payload]},
                "usage": {"documentPagesBasic": 1},
            },
        )
    )
    adapter = ContentUnderstandingPrebuiltReceiptAdapter(
        "content-understanding-prebuilt-receipt", client=_client(), analyzer_id="prebuilt-receipt"
    )

    response = adapter.execute(b"fake-image-bytes", "image/png")

    assert response.receipt.transaction.date == "not-a-date"
    assert isinstance(response.raw_response, dict)
    assert len(response.components) == 1
    assert response.components[0].component == "native_extraction"
    assert response.components[0].usage.pages == 1


@respx.mock
def test_prebuilt_receipt_adapter_treats_mapping_shape_errors_as_invalid_output() -> None:
    payload = _prebuilt_receipt_fields_payload()
    payload["fields"]["TotalAmount"]["valueObject"]["Amount"] = {
        "type": "object",
        "valueObject": {},
    }
    respx.post(_PREBUILT_RECEIPT_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "Succeeded",
                "result": {"contents": [payload]},
                "usage": {"documentPagesBasic": 1},
            },
        )
    )
    adapter = ContentUnderstandingPrebuiltReceiptAdapter(
        "content-understanding-prebuilt-receipt", client=_client(), analyzer_id="prebuilt-receipt"
    )

    with pytest.raises(InvalidSolutionOutput) as exc_info:
        adapter.execute(b"fake-image-bytes", "image/png")

    exc = exc_info.value
    assert "expected a number for field 'Amount', got dict" in str(exc)
    assert exc.raw_response["status"] == "Succeeded"
    assert exc.components[0].usage.pages == 1


@respx.mock
def test_prebuilt_receipt_adapter_ignores_fields_outside_the_deployed_schema() -> None:
    payload = _prebuilt_receipt_fields_payload()
    payload["fields"]["MerchantTaxId"] = {"type": "string", "valueString": "TX-999"}
    payload["fields"]["Currency"] = {"type": "string", "valueString": "USD"}
    payload["fields"]["ReceiptNumber"] = {"type": "string", "valueString": "R-1"}
    payload["fields"]["PaymentMethods"] = {
        "type": "array",
        "valueArray": [
            {
                "type": "object",
                "valueObject": {"Method": {"type": "string", "valueString": "visa"}},
            }
        ],
    }
    payload["fields"]["LineItems"]["valueArray"][0]["valueObject"]["UnknownItemField"] = {
        "type": "string",
        "valueString": "ignored",
    }
    respx.post(_PREBUILT_RECEIPT_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "Succeeded", "result": {"contents": [payload]}}
        )
    )
    adapter = ContentUnderstandingPrebuiltReceiptAdapter(
        "content-understanding-prebuilt-receipt", client=_client(), analyzer_id="prebuilt-receipt"
    )

    response = adapter.execute(b"fake-image-bytes", "image/png")

    receipt = response.receipt
    assert receipt.merchant.tax_id is None
    assert receipt.transaction.currency == "USD"
    assert receipt.transaction.receipt_number is None
    assert receipt.payment_methods is None
    assert receipt.line_items is not None
    assert receipt.line_items[0].product_code == "SKU-1"


@respx.mock
def test_custom_adapter_maps_pascal_case_fields_to_canonical_shape() -> None:
    respx.post(_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "Succeeded",
                "result": {"contents": [_custom_fields_payload()]},
                "usage": {
                    "documentPagesBasic": 1,
                    "tokens": {"gpt-5.2-input": 120, "gpt-5.2-output": 45},
                },
            },
        )
    )
    adapter = ContentUnderstandingCustomAdapter(
        "content-understanding-custom", client=_client(), analyzer_id="expense_receipt_v1"
    )

    response = adapter.execute(b"fake-image-bytes", "image/png")

    receipt = response.receipt
    assert receipt.merchant.name == "Contoso Cafe"
    assert receipt.merchant.tax_id == "TX-123"
    assert receipt.transaction.receipt_number == "R-42"
    assert receipt.transaction.currency == "GBP"
    assert receipt.amounts.total == 12.0
    assert receipt.payment_methods is not None
    assert receipt.payment_methods[0].card_last4 == "1234"
    assert receipt.line_items is not None
    assert receipt.line_items[0].description == "Coffee"
    assert response.components[0].component == "native_extraction"
    service_usage = response.components[0].usage
    assert service_usage.pages == 1
    assert service_usage.input_tokens is None
    assert service_usage.output_tokens is None
    assert service_usage.total_tokens is None
    assert len(response.components) == 2
    assert response.components[1].component == "model:gpt-5.2"
    assert response.components[1].latency_ms == 0.0
    model_usage = response.components[1].usage
    assert model_usage.input_tokens == 120
    assert model_usage.output_tokens == 45
    assert model_usage.total_tokens == 165
    assert isinstance(response.raw_response, dict)


@respx.mock
def test_custom_adapter_preserves_nonstandard_currency_for_answer_scoring() -> None:
    payload = _custom_fields_payload()
    payload["fields"]["Transaction"]["valueObject"]["Currency"]["valueString"] = "RM"
    respx.post(_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "Succeeded",
                "result": {"contents": [payload]},
                "usage": {"documentPagesBasic": 1},
            },
        )
    )
    adapter = ContentUnderstandingCustomAdapter(
        "content-understanding-custom", client=_client(), analyzer_id="expense_receipt_v1"
    )

    response = adapter.execute(b"fake-image-bytes", "image/png")

    assert response.receipt.transaction.currency == "RM"
    assert isinstance(response.raw_response, dict)
    assert len(response.components) == 1
    measurement = response.components[0]
    assert measurement.component == "native_extraction"
    assert measurement.usage.pages == 1


@respx.mock
def test_custom_adapter_invalid_solution_output_retains_per_model_measurements() -> None:
    """When ``usage.tokens`` is present, ``InvalidSolutionOutput`` must carry every
    component - the service measurement *and* each per-model split - not just the first."""
    payload = _custom_fields_payload()
    payload["fields"]["Merchant"]["valueObject"]["Name"] = {
        "type": "object",
        "valueObject": {},
    }
    respx.post(_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "Succeeded",
                "result": {"contents": [payload]},
                "usage": {
                    "documentPagesBasic": 1,
                    "tokens": {"gpt-5.2-input": 120, "gpt-5.2-output": 45},
                },
            },
        )
    )
    adapter = ContentUnderstandingCustomAdapter(
        "content-understanding-custom", client=_client(), analyzer_id="expense_receipt_v1"
    )

    with pytest.raises(InvalidSolutionOutput) as exc_info:
        adapter.execute(b"fake-image-bytes", "image/png")

    exc = exc_info.value
    assert len(exc.components) == 2
    assert exc.components[0].component == "native_extraction"
    assert exc.components[1].component == "model:gpt-5.2"
    assert exc.components[1].usage.input_tokens == 120
    assert exc.components[1].usage.output_tokens == 45


@respx.mock
def test_prebuilt_receipt_adapter_splits_multiple_models_including_embedding_only_input() -> None:
    """Content Understanding may internally invoke several models for a single analyze
    call (e.g. a completion model plus an embedding model used only for input tokens);
    each must get its own zero-latency ``model:<name>`` measurement with only the sides
    actually reported, while the service measurement keeps the real latency and never
    duplicates any token counts."""
    respx.post(_PREBUILT_RECEIPT_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "Succeeded",
                "result": {"contents": [_prebuilt_receipt_fields_payload()]},
                "usage": {
                    "documentPagesBasic": 1,
                    "contextualizationTokens": 64,
                    "tokens": {
                        "gpt-5.4-mini-input": 200,
                        "gpt-5.4-mini-output": 80,
                        "text-embedding-3-large-input": 50,
                    },
                },
            },
        )
    )
    adapter = ContentUnderstandingPrebuiltReceiptAdapter(
        "content-understanding-prebuilt-receipt", client=_client(), analyzer_id="prebuilt-receipt"
    )

    response = adapter.execute(b"fake-image-bytes", "image/png")

    assert len(response.components) == 3
    service = response.components[0]
    assert service.component == "native_extraction"
    assert service.usage.pages == 1
    assert service.usage.contextualization_tokens == 64
    assert service.usage.input_tokens is None
    assert service.usage.total_tokens is None

    completion = response.components[1]
    assert completion.component == "model:gpt-5.4-mini"
    assert completion.latency_ms == 0.0
    assert completion.usage.input_tokens == 200
    assert completion.usage.output_tokens == 80
    assert completion.usage.total_tokens == 280
    assert completion.usage.pages is None

    embedding = response.components[2]
    assert embedding.component == "model:text-embedding-3-large"
    assert embedding.latency_ms == 0.0
    assert embedding.usage.input_tokens == 50
    assert embedding.usage.output_tokens is None
    assert embedding.usage.total_tokens == 50


def test_model_token_measurements_attributes_unrecognized_suffix_to_total_only() -> None:
    """A ``-cachedInput`` (or any non-``input``/``output``) suffix still counts toward
    that model's ``total_tokens`` but is not attributed to either side, matching the
    pre-split aggregate behaviour."""
    usage_payload: dict[str, Any] = {
        "tokens": {"gpt-5.2-input": 100, "gpt-5.2-output": 40, "gpt-5.2-cachedInput": 10}
    }

    measurements = content_understanding._model_token_measurements(
        usage_payload["tokens"], usage_payload
    )

    assert len(measurements) == 1
    usage = measurements[0].usage
    assert usage.input_tokens == 100
    assert usage.output_tokens == 40
    assert usage.total_tokens == 150


def test_model_token_measurements_raises_when_key_has_no_kind_suffix() -> None:
    with pytest.raises(ProviderResponseError, match="no '-<kind>' suffix"):
        content_understanding._model_token_measurements({"unlabeled": 5}, {"tokens": {}})


@respx.mock
def test_client_sends_raw_binary_body_with_content_type_header() -> None:
    route = respx.post(_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(
        return_value=httpx.Response(200, json={"status": "Succeeded", "result": {"contents": []}})
    )
    client = _client()

    client.analyze("expense_receipt_v1", b"fake-image-bytes", "image/png")

    request = route.calls[0].request
    assert request.content == b"fake-image-bytes"
    assert request.headers["content-type"] == "image/png"
    assert request.url.params["api-version"] == "2025-11-01"


@respx.mock
def test_client_polls_until_succeeded_honoring_retry_after() -> None:
    respx.post(_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(
        side_effect=[
            httpx.Response(200, json={"status": "Running"}, headers={"retry-after": "0"}),
            httpx.Response(200, json={"status": "Succeeded", "result": {"contents": []}}),
        ]
    )
    client = _client()

    result = client.analyze("expense_receipt_v1", b"fake-image-bytes", "image/png")

    assert result["status"] == "Succeeded"


@respx.mock
def test_client_raises_on_terminal_failure_status() -> None:
    respx.post(_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(return_value=httpx.Response(200, json={"status": "Failed"}))
    client = _client()

    with pytest.raises(ProviderResponseError, match="Failed"):
        client.analyze("expense_receipt_v1", b"fake-image-bytes", "image/png")


@respx.mock
def test_client_raises_on_canceled_status() -> None:
    """Azure's ``OperationState`` enum spells this ``Canceled`` (single L, US spelling)."""
    respx.post(_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(return_value=httpx.Response(200, json={"status": "Canceled"}))
    client = _client()

    with pytest.raises(ProviderResponseError, match="Canceled"):
        client.analyze("expense_receipt_v1", b"fake-image-bytes", "image/png")


@respx.mock
def test_client_raises_when_no_operation_location_header() -> None:
    respx.post(_ANALYZE_URL).mock(return_value=httpx.Response(202))
    client = _client()

    with pytest.raises(ProviderResponseError, match="Operation-Location"):
        client.analyze("expense_receipt_v1", b"fake-image-bytes", "image/png")


@respx.mock
def test_client_raises_on_poll_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(content_understanding, "_MAX_POLL_SECONDS", 0.0)
    respx.post(_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(return_value=httpx.Response(200, json={"status": "Running"}))
    client = _client()

    with pytest.raises(ProviderResponseError, match="timed out"):
        client.analyze("expense_receipt_v1", b"fake-image-bytes", "image/png")


@respx.mock
def test_ocr_extractor_concatenates_markdown_across_contents() -> None:
    respx.post(f"{_BASE_URL}/contentunderstanding/analyzers/prebuilt-read:analyzeBinary").mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "Succeeded",
                "result": {"contents": [{"markdown": "page one"}, {"markdown": "page two"}]},
                "usage": {"documentPagesStandard": 2, "contextualizationTokens": 64},
            },
        )
    )
    extractor = ContentUnderstandingOcrExtractor(client=_client(), analyzer_id="prebuilt-read")

    extraction = extractor.extract_text(b"fake-image-bytes", "image/png")

    assert extraction.text == "page one\n\npage two"
    assert extraction.measurement.component == "ocr"
    assert extraction.measurement.usage.pages == 2
    assert extraction.measurement.usage.contextualization_tokens == 64


@respx.mock
def test_ocr_extractor_leaves_usage_unset_when_provider_omits_it() -> None:
    """Pages must never be invented from ``len(contents)`` when usage is absent."""
    respx.post(f"{_BASE_URL}/contentunderstanding/analyzers/prebuilt-read:analyzeBinary").mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "Succeeded",
                "result": {"contents": [{"markdown": "page one"}, {"markdown": "page two"}]},
            },
        )
    )
    extractor = ContentUnderstandingOcrExtractor(client=_client(), analyzer_id="prebuilt-read")

    extraction = extractor.extract_text(b"fake-image-bytes", "image/png")

    assert extraction.measurement.usage.pages is None


@respx.mock
def test_ocr_extractor_raises_when_content_has_no_markdown() -> None:
    respx.post(f"{_BASE_URL}/contentunderstanding/analyzers/prebuilt-layout:analyzeBinary").mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "Succeeded", "result": {"contents": [{"markdown": ""}]}}
        )
    )
    extractor = ContentUnderstandingOcrExtractor(client=_client(), analyzer_id="prebuilt-layout")

    with pytest.raises(ProviderResponseError, match="no markdown"):
        extractor.extract_text(b"fake-image-bytes", "image/png")


@respx.mock
def test_client_raises_provider_response_error_on_analyze_http_error() -> None:
    respx.post(_ANALYZE_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))
    client = _client()

    with pytest.raises(ProviderResponseError, match="analyze call failed") as exc_info:
        client.analyze("expense_receipt_v1", b"fake-image-bytes", "image/png")
    assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)


@respx.mock
def test_client_raises_provider_response_error_on_poll_http_error() -> None:
    respx.post(_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(return_value=httpx.Response(503))
    client = _client()

    with pytest.raises(ProviderResponseError, match="poll call failed") as exc_info:
        client.analyze("expense_receipt_v1", b"fake-image-bytes", "image/png")
    assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)


@respx.mock
def test_client_raises_provider_response_error_on_invalid_poll_json_body() -> None:
    respx.post(_ANALYZE_URL).mock(
        return_value=httpx.Response(202, headers={"operation-location": _OPERATION_URL})
    )
    respx.get(_OPERATION_URL).mock(return_value=httpx.Response(200, content=b"not json"))
    client = _client()

    with pytest.raises(ProviderResponseError, match="not valid JSON"):
        client.analyze("expense_receipt_v1", b"fake-image-bytes", "image/png")


def test_usage_from_operation_returns_empty_usage_when_absent() -> None:
    """Pages/tokens must never be invented when the provider omits ``usage``."""
    usage = content_understanding._usage_from_operation({"status": "Succeeded", "result": {}})

    assert usage.pages is None
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.total_tokens is None
    assert usage.contextualization_tokens is None
    assert usage.provider_metadata == {}


def test_usage_from_operation_prefers_standard_pages_over_basic_and_minimal() -> None:
    payload = {
        "usage": {
            "documentPagesStandard": 2,
            "documentPagesBasic": 5,
            "documentPagesMinimal": 9,
            "contextualizationTokens": 128,
        }
    }

    usage = content_understanding._usage_from_operation(payload)

    assert usage.pages == 2
    assert usage.contextualization_tokens == 128
    assert usage.provider_metadata == payload["usage"]


def test_usage_from_operation_falls_back_to_basic_pages_when_standard_absent() -> None:
    payload = {"usage": {"documentPagesBasic": 3}}

    usage = content_understanding._usage_from_operation(payload)

    assert usage.pages == 3


def test_usage_from_operation_falls_back_to_minimal_pages_when_others_absent() -> None:
    payload = {"usage": {"documentPagesMinimal": 1}}

    usage = content_understanding._usage_from_operation(payload)

    assert usage.pages == 1


def test_usage_from_operation_sums_completion_tokens_by_model_suffix() -> None:
    """``usage.tokens`` is a flat dict keyed like ``"{model}-input"``/``"{model}-output"``."""
    payload = {
        "usage": {
            "documentPagesStandard": 1,
            "tokens": {"gpt-5.2-input": 100, "gpt-5.2-output": 40, "gpt-5.2-cachedInput": 10},
        }
    }

    usage = content_understanding._usage_from_operation(payload)

    assert usage.input_tokens == 100
    assert usage.output_tokens == 40
    assert usage.total_tokens == 150


def test_usage_from_operation_raises_on_non_integer_token_value() -> None:
    payload = {"usage": {"tokens": {"gpt-5.2-input": "many"}}}

    with pytest.raises(ProviderResponseError, match=r"usage\.tokens"):
        content_understanding._usage_from_operation(payload)


def test_usage_from_operation_raises_on_non_object_tokens() -> None:
    payload = {"usage": {"tokens": "not-an-object"}}

    with pytest.raises(ProviderResponseError, match=r"usage\.tokens"):
        content_understanding._usage_from_operation(payload)


def test_usage_from_operation_raises_on_non_object_usage() -> None:
    with pytest.raises(ProviderResponseError, match="usage"):
        content_understanding._usage_from_operation({"usage": "not-an-object"})


def test_usage_from_operation_raises_on_non_integer_page_count() -> None:
    payload = {"usage": {"documentPagesStandard": "two"}}

    with pytest.raises(ProviderResponseError, match="documentPagesStandard"):
        content_understanding._usage_from_operation(payload)


def test_usage_from_operation_raises_on_non_integer_contextualization_tokens() -> None:
    payload = {"usage": {"contextualizationTokens": "many"}}

    with pytest.raises(ProviderResponseError, match="contextualizationTokens"):
        content_understanding._usage_from_operation(payload)
