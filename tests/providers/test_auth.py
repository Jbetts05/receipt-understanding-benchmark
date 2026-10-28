"""No-network tests for Entra provider authentication and token refresh."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from azure.core.credentials import AccessToken

import receipt_bench.providers.registry as registry_module
from receipt_bench.config import AzureSettings, BenchmarkConfig, RetryConfig
from receipt_bench.providers.auth import (
    AZURE_AI_FOUNDRY_SCOPE,
    AZURE_COGNITIVE_SERVICES_SCOPE,
    RefreshingBearerTokenAuth,
)
from receipt_bench.providers.registry import AdapterRegistry


class FakeTokenCredential:
    def __init__(self, tokens: list[AccessToken]) -> None:
        self._tokens = list(tokens)
        self.calls: list[tuple[str, ...]] = []

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        self.calls.append(scopes)
        if not self._tokens:
            raise AssertionError("FakeTokenCredential has no scripted token remaining")
        return self._tokens.pop(0)


def _request_headers(auth: httpx.Auth, count: int) -> list[str]:
    headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers["Authorization"])
        return httpx.Response(200)

    with httpx.Client(
        auth=auth,
        transport=httpx.MockTransport(handler),
    ) as client:
        for _ in range(count):
            client.get("https://example.test/resource")
    return headers


def _authorization_header(auth: RefreshingBearerTokenAuth) -> str:
    request = httpx.Request("GET", "https://example.test/resource")
    flow = auth.auth_flow(request)
    authorized_request = next(flow)
    return authorized_request.headers["Authorization"]


def _empty_config() -> BenchmarkConfig:
    return BenchmarkConfig(
        schema_path=Path("schemas/expense-receipt.schema.json"),
        dataset_manifest=Path("data/manifests/benchmark.jsonl"),
        output_root=Path("results/runs"),
        random_seed=1,
        retry=RetryConfig(),
        solutions=[],
    )


def test_refreshing_bearer_auth_reuses_a_valid_token_without_network_io() -> None:
    credential = FakeTokenCredential(
        [AccessToken("cached-token", int(time.time()) + 3600)]
    )
    auth = RefreshingBearerTokenAuth(credential, AZURE_AI_FOUNDRY_SCOPE)

    headers = _request_headers(auth, 2)

    assert headers == ["Bearer cached-token", "Bearer cached-token"]
    assert credential.calls == [(AZURE_AI_FOUNDRY_SCOPE,)]


def test_refreshing_bearer_auth_refreshes_a_near_expiry_token() -> None:
    credential = FakeTokenCredential(
        [
            AccessToken("expiring-token", int(time.time()) + 1),
            AccessToken("refreshed-token", int(time.time()) + 3600),
        ]
    )
    auth = RefreshingBearerTokenAuth(credential, AZURE_COGNITIVE_SERVICES_SCOPE)

    headers = _request_headers(auth, 2)

    assert headers == ["Bearer expiring-token", "Bearer refreshed-token"]
    assert credential.calls == [
        (AZURE_COGNITIVE_SERVICES_SCOPE,),
        (AZURE_COGNITIVE_SERVICES_SCOPE,),
    ]


def test_entra_registry_wires_official_scopes_and_disables_sdk_retries(
    entra_azure_settings: AzureSettings,
    monkeypatch: Any,
) -> None:
    openai_calls: list[dict[str, Any]] = []
    anthropic_calls: list[dict[str, Any]] = []
    document_calls: list[dict[str, Any]] = []
    vision_calls: list[dict[str, Any]] = []
    httpx_calls: list[dict[str, Any]] = []

    def capture_openai(**kwargs: Any) -> SimpleNamespace:
        openai_calls.append(kwargs)
        return SimpleNamespace(max_retries=kwargs["max_retries"])

    def capture_anthropic(**kwargs: Any) -> SimpleNamespace:
        anthropic_calls.append(kwargs)
        return SimpleNamespace(max_retries=kwargs["max_retries"])

    def capture_document(**kwargs: Any) -> SimpleNamespace:
        document_calls.append(kwargs)
        return SimpleNamespace()

    def capture_vision(**kwargs: Any) -> SimpleNamespace:
        vision_calls.append(kwargs)
        return SimpleNamespace()

    def capture_httpx(**kwargs: Any) -> SimpleNamespace:
        httpx_calls.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(registry_module, "OpenAI", capture_openai)
    monkeypatch.setattr(
        registry_module.anthropic,
        "AnthropicFoundry",
        capture_anthropic,
    )
    monkeypatch.setattr(
        registry_module,
        "DocumentIntelligenceClient",
        capture_document,
    )
    monkeypatch.setattr(registry_module, "ImageAnalysisClient", capture_vision)
    monkeypatch.setattr(registry_module.httpx, "Client", capture_httpx)
    credential = FakeTokenCredential(
        [
            AccessToken("shared-foundry-token", int(time.time()) + 3600),
            AccessToken("mistral-token", int(time.time()) + 3600),
            AccessToken("cognitive-token", int(time.time()) + 3600),
        ]
    )

    AdapterRegistry(
        benchmark_config=_empty_config(),
        azure_settings=entra_azure_settings,
        schema={},
        token_credential=credential,
    )

    assert len(openai_calls) == 2
    openai_token_provider = openai_calls[0]["api_key"]
    assert callable(openai_token_provider)
    assert openai_calls[1]["api_key"] is openai_token_provider
    assert openai_calls[0]["max_retries"] == 0
    assert openai_calls[1]["max_retries"] == 0
    assert openai_token_provider() == "shared-foundry-token"

    assert len(anthropic_calls) == 1
    assert "api_key" not in anthropic_calls[0]
    assert anthropic_calls[0]["azure_ad_token_provider"] is openai_token_provider
    assert anthropic_calls[0]["max_retries"] == 0

    assert document_calls == [
        {
            "endpoint": entra_azure_settings.azure_document_intelligence_endpoint,
            "credential": credential,
            "retry_total": 0,
        }
    ]
    assert vision_calls == [
        {
            "endpoint": entra_azure_settings.azure_vision_endpoint,
            "credential": credential,
            "retry_total": 0,
        }
    ]

    mistral_call = next(
        call
        for call in httpx_calls
        if call["base_url"] == entra_azure_settings.azure_mistral_ocr_endpoint
    )
    content_call = next(
        call
        for call in httpx_calls
        if call["base_url"] == entra_azure_settings.azure_content_understanding_endpoint
    )
    assert "headers" not in mistral_call
    assert "headers" not in content_call
    assert isinstance(mistral_call["auth"], RefreshingBearerTokenAuth)
    assert isinstance(content_call["auth"], RefreshingBearerTokenAuth)
    assert _authorization_header(mistral_call["auth"]) == "Bearer mistral-token"
    assert _authorization_header(content_call["auth"]) == "Bearer cognitive-token"
    assert credential.calls == [
        (AZURE_AI_FOUNDRY_SCOPE,),
        (AZURE_AI_FOUNDRY_SCOPE,),
        (AZURE_COGNITIVE_SERVICES_SCOPE,),
    ]
