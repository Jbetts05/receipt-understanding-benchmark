from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from receipt_bench.config import (
    AzureAiAuthMode,
    AzureAiCredentialKind,
    AzureSettings,
    BenchmarkConfig,
    HostedSecretReferences,
    load_hosted_azure_settings,
)
from receipt_bench.projects.registry import ProjectRegistry
from receipt_bench.web.catalog import default_solution_catalog

ROOT = Path(__file__).resolve().parents[1]


def _azure_settings(
    *,
    auth_mode: AzureAiAuthMode = "auto",
    credential_kind: AzureAiCredentialKind = "default",
    client_id: str | None = None,
    api_key: str | None = "existing-local-key",
) -> AzureSettings:
    return AzureSettings(
        azure_subscription_id="subscription",
        azure_resource_group="rg-receipt-bench-test",
        azure_location="swedencentral",
        azure_foundry_account="airbtest0001",
        azure_foundry_project="receipt-bench",
        azure_ai_auth_mode=auth_mode,
        azure_ai_credential_kind=credential_kind,
        azure_client_id=client_id,
        azure_foundry_api_key=api_key,
        azure_foundry_openai_endpoint="https://openai.example",
        azure_foundry_models_endpoint="https://models.example",
        azure_anthropic_endpoint="https://anthropic.example",
        azure_mistral_ocr_endpoint="https://mistral.example",
        azure_content_understanding_endpoint="https://content.example",
        azure_document_intelligence_endpoint="https://document.example",
        azure_vision_endpoint="https://vision.example",
        _env_file=None,
    )


def test_auto_auth_preserves_existing_local_key_configuration() -> None:
    settings = _azure_settings()

    assert settings.azure_ai_auth_mode == "auto"
    assert settings.resolved_auth_mode == "key"


def test_auto_auth_prefers_entra_when_no_key_exists() -> None:
    settings = _azure_settings(api_key=None)

    assert settings.resolved_auth_mode == "entra"


def test_explicit_entra_auth_ignores_an_existing_key() -> None:
    settings = _azure_settings(auth_mode="entra")

    assert settings.resolved_auth_mode == "entra"


def test_credential_kind_and_managed_identity_client_id_load_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_AI_CREDENTIAL_KIND", "managed_identity")
    monkeypatch.setenv("AZURE_CLIENT_ID", "environment-client-id")
    values = _azure_settings().model_dump(
        exclude={"azure_ai_credential_kind", "azure_client_id"}
    )

    settings = AzureSettings(**values, _env_file=None)

    assert settings.azure_ai_credential_kind == "managed_identity"
    assert settings.azure_client_id == "environment-client-id"


@pytest.mark.parametrize("api_key", [None, "", "   "])
def test_key_auth_requires_a_non_empty_key(api_key: str | None) -> None:
    with pytest.raises(ValueError, match="azure_foundry_api_key must be non-empty"):
        _azure_settings(auth_mode="key", api_key=api_key)


def test_solution_config_is_complete_and_unique() -> None:
    config = BenchmarkConfig.load(ROOT / "configs" / "solutions.yaml")
    solution_ids = [solution.id for solution in config.solutions]

    assert len(solution_ids) == 22
    assert len(solution_ids) == len(set(solution_ids))
    assert config.concurrency == 1
    assert config.warmup_requests == 2
    normalizers = {
        solution.normalizer
        for solution in config.solutions
        if solution.kind in {"native_pipeline", "ocr_pipeline"}
    }
    assert normalizers == {"gpt-5.6-luna"}
    assert sum(solution.kind == "ocr_text" for solution in config.solutions) == 5
    assert sum(solution.kind == "native_pipeline" for solution in config.solutions) == 2
    custom = next(
        solution
        for solution in config.solutions
        if solution.id == "content-understanding-custom"
    )
    assert custom.project_analyzers == {
        "sroie": "expense_receipt_v1",
        "cord-v2": "cord_receipt_v1",
    }
    configured = set(solution_ids)
    assert configured == {item.id for item in default_solution_catalog().list()}
    for project in ProjectRegistry.with_builtins(repository_root=ROOT).list():
        assert configured == {
            item.solution_id for item in project.compatibility
        }


def test_assisted_solution_catalog_names_include_the_normalizer() -> None:
    catalog = default_solution_catalog()
    expected_labels = {
        "mistral-document-ai-2512": "Mistral Document AI + GPT-5.6 Luna",
        "mistral-ocr-4.0": "Mistral OCR 4.0 + GPT-5.6 Luna",
        "azure-vision-read": "Azure Vision Read + GPT-5.6 Luna",
        "content-understanding-read": "Content Understanding Read + GPT-5.6 Luna",
        "content-understanding-layout": "Content Understanding Layout + GPT-5.6 Luna",
        "content-understanding-prebuilt-receipt-luna": (
            "Content Understanding receipt + GPT-5.6 Luna"
        ),
        "document-intelligence-receipt-luna": (
            "Document Intelligence receipt + GPT-5.6 Luna"
        ),
    }

    assert {
        solution_id: catalog.get(solution_id).label
        for solution_id in expected_labels
    } == expected_labels


def test_content_understanding_analyzer_is_grounded() -> None:
    analyzer = json.loads(
        (
            ROOT
            / "configs"
            / "content-understanding"
            / "expense_receipt_v1.json"
        ).read_text(encoding="utf-8")
    )

    assert analyzer["baseAnalyzerId"] == "prebuilt-document"
    assert analyzer["models"]["completion"] == "gpt-5.4-mini"
    assert analyzer["config"]["estimateFieldSourceAndConfidence"] is True
    assert "LineItems" in analyzer["fieldSchema"]["fields"]

    cord_analyzer = json.loads(
        (
            ROOT
            / "configs"
            / "content-understanding"
            / "cord_receipt_v1.json"
        ).read_text(encoding="utf-8")
    )
    assert cord_analyzer["baseAnalyzerId"] == "prebuilt-document"
    assert cord_analyzer["models"]["completion"] == "gpt-5.4-mini"
    assert set(cord_analyzer["fieldSchema"]["fields"]) == {
        "menu",
        "sub_total",
        "total",
    }


def test_secret_files_are_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in ignored
    assert "data/raw/" in ignored
    assert "data/manifests/benchmark.jsonl" in ignored
    assert "results/raw/" in ignored
    assert ".impeccable/" in ignored
    assert "reports/*" in ignored
    assert "!reports/README.md" in ignored


class _FakeSecretReader:
    def __init__(self, values: dict[str, str | None]) -> None:
        self.values = values
        self.requested: list[str] = []

    def get_secret(self, name: str) -> SimpleNamespace:
        self.requested.append(name)
        return SimpleNamespace(value=self.values[name])


def _hosted_references(
    *,
    auth_mode: AzureAiAuthMode = "auto",
    credential_kind: AzureAiCredentialKind = "managed_identity",
    client_id: str | None = None,
    api_key_secret_name: str | None = "api-key",
) -> HostedSecretReferences:
    return HostedSecretReferences(
        azure_subscription_id="subscription",
        azure_resource_group="rg-receipt-bench-test",
        azure_location="swedencentral",
        azure_foundry_account="airbtest0001",
        azure_foundry_project="receipt-bench",
        azure_key_vault_uri="https://private-vault.vault.azure.net",
        azure_ai_auth_mode=auth_mode,
        azure_ai_credential_kind=credential_kind,
        azure_client_id=client_id,
        azure_foundry_api_key_secret_name=api_key_secret_name,
        azure_foundry_openai_endpoint_secret_name="openai",
        azure_foundry_models_endpoint_secret_name="models",
        azure_anthropic_endpoint_secret_name="anthropic",
        azure_mistral_ocr_endpoint_secret_name="mistral",
        azure_content_understanding_endpoint_secret_name="content",
        azure_document_intelligence_endpoint_secret_name="document",
        azure_vision_endpoint_secret_name="vision",
    )


def test_hosted_key_settings_resolve_only_named_key_vault_secrets() -> None:
    values = {
        "api-key": "secret",
        "openai": "https://openai.example",
        "models": "https://models.example",
        "anthropic": "https://anthropic.example",
        "mistral": "https://mistral.example",
        "content": "https://content.example",
        "document": "https://document.example",
        "vision": "https://vision.example",
    }
    reader = _FakeSecretReader(values)

    settings = load_hosted_azure_settings(
        _hosted_references(auth_mode="key"),
        secret_reader=reader,
    )

    assert settings.azure_foundry_api_key is not None
    assert settings.azure_foundry_api_key.get_secret_value() == "secret"
    assert settings.resolved_auth_mode == "key"
    assert settings.azure_foundry_openai_endpoint == "https://openai.example"
    assert reader.requested == list(values)


def test_hosted_entra_settings_do_not_read_or_require_an_api_key_secret() -> None:
    values = {
        "openai": "https://openai.example",
        "models": "https://models.example",
        "anthropic": "https://anthropic.example",
        "mistral": "https://mistral.example",
        "content": "https://content.example",
        "document": "https://document.example",
        "vision": "https://vision.example",
    }
    reader = _FakeSecretReader(values)

    settings = load_hosted_azure_settings(
        _hosted_references(auth_mode="entra", api_key_secret_name=None),
        secret_reader=reader,
    )

    assert settings.azure_foundry_api_key is None
    assert settings.resolved_auth_mode == "entra"
    assert reader.requested == list(values)


def test_runtime_context_selects_default_or_managed_identity_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_credential = object()
    hosted_credential = object()
    managed_identity_calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        "receipt_bench.config.DefaultAzureCredential",
        lambda: local_credential,
    )

    def managed_identity_credential(**kwargs: str) -> object:
        managed_identity_calls.append(kwargs)
        return hosted_credential

    monkeypatch.setattr(
        "receipt_bench.config.ManagedIdentityCredential",
        managed_identity_credential,
    )
    hosted_values = {
        "openai": "https://openai.example",
        "models": "https://models.example",
        "anthropic": "https://anthropic.example",
        "mistral": "https://mistral.example",
        "content": "https://content.example",
        "document": "https://document.example",
        "vision": "https://vision.example",
    }

    local_settings = _azure_settings(auth_mode="entra", api_key=None)
    hosted_settings = load_hosted_azure_settings(
        _hosted_references(
            auth_mode="entra",
            client_id="managed-identity-client-id",
            api_key_secret_name=None,
        ),
        secret_reader=_FakeSecretReader(hosted_values),
    )

    assert local_settings.create_token_credential() is local_credential
    assert hosted_settings.create_token_credential() is hosted_credential
    assert managed_identity_calls == [{"client_id": "managed-identity-client-id"}]


def test_hosted_settings_reject_empty_key_vault_secret() -> None:
    values = {
        "api-key": None,
        "openai": "https://openai.example",
        "models": "https://models.example",
        "anthropic": "https://anthropic.example",
        "mistral": "https://mistral.example",
        "content": "https://content.example",
        "document": "https://document.example",
        "vision": "https://vision.example",
    }

    with pytest.raises(ValueError, match="'api-key' is empty"):
        load_hosted_azure_settings(
            _hosted_references(auth_mode="key"),
            secret_reader=_FakeSecretReader(values),
        )
