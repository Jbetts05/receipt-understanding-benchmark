from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, cast

import yaml
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.keyvault.secrets import SecretClient
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

AzureAiAuthMode = Literal["auto", "entra", "key"]
AzureAiCredentialKind = Literal["default", "managed_identity"]
ResolvedAzureAiAuthMode = Literal["entra", "key"]


def _managed_identity_credential(client_id: str | None) -> ManagedIdentityCredential:
    if client_id is not None:
        return ManagedIdentityCredential(client_id=client_id)
    return ManagedIdentityCredential()


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetryConfig(ConfigModel):
    max_attempts: int = Field(default=3, ge=1, le=10)
    initial_delay_seconds: float = Field(default=1.0, gt=0)
    max_delay_seconds: float = Field(default=30.0, gt=0)


class SolutionConfig(ConfigModel):
    id: str
    kind: str
    deployment: str | None = None
    model: str | None = None
    api: str
    lifecycle: str = "ga"
    normalizer: str | None = None
    analyzer_id: str | None = None
    project_analyzers: dict[str, str] = Field(default_factory=dict)
    reasoning_effort: str | None = None


class BenchmarkConfig(ConfigModel):
    schema_path: Path
    dataset_manifest: Path
    output_root: Path
    concurrency: int = Field(default=1, ge=1)
    warmup_requests: int = Field(default=2, ge=0)
    random_seed: int
    retry: RetryConfig = Field(default_factory=RetryConfig)
    solutions: list[SolutionConfig]

    @classmethod
    def load(cls, path: Path) -> BenchmarkConfig:
        if not path.is_file():
            raise FileNotFoundError(f"Benchmark config not found: {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(raw)


class AzureSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    azure_subscription_id: str
    azure_resource_group: str
    azure_location: str
    azure_foundry_account: str
    azure_foundry_project: str
    azure_ai_auth_mode: AzureAiAuthMode = "auto"
    azure_ai_credential_kind: AzureAiCredentialKind = "default"
    azure_client_id: str | None = None
    azure_foundry_api_key: SecretStr | None = None
    azure_foundry_openai_endpoint: str
    azure_foundry_models_endpoint: str
    azure_anthropic_endpoint: str
    azure_mistral_ocr_endpoint: str
    azure_content_understanding_endpoint: str
    azure_document_intelligence_endpoint: str
    azure_vision_endpoint: str

    @field_validator("azure_client_id", "azure_foundry_api_key", mode="before")
    @classmethod
    def normalize_optional_credentials(cls, value: object) -> object:
        if value is None:
            return None
        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(raw_value, str):
            normalized = raw_value.strip()
            return normalized or None
        return value

    @model_validator(mode="after")
    def require_key_for_key_mode(self) -> AzureSettings:
        if self.azure_ai_auth_mode == "key" and self.azure_foundry_api_key is None:
            raise ValueError(
                "azure_foundry_api_key must be non-empty when azure_ai_auth_mode='key'"
            )
        return self

    @property
    def resolved_auth_mode(self) -> ResolvedAzureAiAuthMode:
        if self.azure_ai_auth_mode == "auto":
            return "key" if self.azure_foundry_api_key is not None else "entra"
        return self.azure_ai_auth_mode

    def create_token_credential(self) -> TokenCredential:
        """Create the configured Entra credential for provider clients."""
        if self.azure_ai_credential_kind == "managed_identity":
            return _managed_identity_credential(self.azure_client_id)
        return DefaultAzureCredential()


class HostedSecretReferences(BaseSettings):
    """Hosted app settings that point to Key Vault secret names, never values."""

    model_config = SettingsConfigDict(extra="ignore")

    azure_subscription_id: str
    azure_resource_group: str
    azure_location: str
    azure_foundry_account: str
    azure_foundry_project: str
    azure_key_vault_uri: str
    azure_ai_auth_mode: AzureAiAuthMode = "auto"
    azure_ai_credential_kind: AzureAiCredentialKind = "default"
    azure_client_id: str | None = None
    azure_foundry_api_key_secret_name: str | None = None
    azure_foundry_openai_endpoint_secret_name: str
    azure_foundry_models_endpoint_secret_name: str
    azure_anthropic_endpoint_secret_name: str
    azure_mistral_ocr_endpoint_secret_name: str
    azure_content_understanding_endpoint_secret_name: str
    azure_document_intelligence_endpoint_secret_name: str
    azure_vision_endpoint_secret_name: str

    @field_validator("azure_foundry_api_key_secret_name", mode="before")
    @classmethod
    def normalize_optional_api_key_secret_name(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @model_validator(mode="after")
    def require_api_key_reference_for_key_mode(self) -> HostedSecretReferences:
        if (
            self.azure_ai_auth_mode == "key"
            and self.azure_foundry_api_key_secret_name is None
        ):
            raise ValueError(
                "azure_foundry_api_key_secret_name is required when "
                "azure_ai_auth_mode='key'"
            )
        return self


class SecretValue(Protocol):
    value: str | None


class SecretReader(Protocol):
    def get_secret(self, name: str) -> SecretValue: ...


def load_hosted_azure_settings(
    references: HostedSecretReferences | None = None,
    *,
    secret_reader: SecretReader | None = None,
) -> AzureSettings:
    """Resolve hosted provider configuration through managed identity and Key Vault."""
    resolved = references or HostedSecretReferences()  # type: ignore[call-arg]
    reader = secret_reader or cast(
        SecretReader,
        SecretClient(
            vault_url=resolved.azure_key_vault_uri,
            credential=_managed_identity_credential(resolved.azure_client_id),
        ),
    )

    def read(name: str) -> str:
        value = reader.get_secret(name).value
        if value is None or not value.strip():
            raise ValueError(f"Required Key Vault secret {name!r} is empty")
        return value

    api_key = (
        SecretStr(read(resolved.azure_foundry_api_key_secret_name))
        if (
            resolved.azure_ai_auth_mode != "entra"
            and resolved.azure_foundry_api_key_secret_name is not None
        )
        else None
    )
    settings = AzureSettings(
        azure_subscription_id=resolved.azure_subscription_id,
        azure_resource_group=resolved.azure_resource_group,
        azure_location=resolved.azure_location,
        azure_foundry_account=resolved.azure_foundry_account,
        azure_foundry_project=resolved.azure_foundry_project,
        azure_ai_auth_mode=resolved.azure_ai_auth_mode,
        azure_ai_credential_kind=resolved.azure_ai_credential_kind,
        azure_client_id=resolved.azure_client_id,
        azure_foundry_api_key=api_key,
        azure_foundry_openai_endpoint=read(
            resolved.azure_foundry_openai_endpoint_secret_name
        ),
        azure_foundry_models_endpoint=read(
            resolved.azure_foundry_models_endpoint_secret_name
        ),
        azure_anthropic_endpoint=read(resolved.azure_anthropic_endpoint_secret_name),
        azure_mistral_ocr_endpoint=read(
            resolved.azure_mistral_ocr_endpoint_secret_name
        ),
        azure_content_understanding_endpoint=read(
            resolved.azure_content_understanding_endpoint_secret_name
        ),
        azure_document_intelligence_endpoint=read(
            resolved.azure_document_intelligence_endpoint_secret_name
        ),
        azure_vision_endpoint=read(resolved.azure_vision_endpoint_secret_name),
    )
    return settings
