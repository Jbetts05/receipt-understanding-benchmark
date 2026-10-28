"""Tests for the adapter registry/factory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anthropic
import httpx
import pytest

from receipt_bench.config import AzureSettings, BenchmarkConfig, RetryConfig, SolutionConfig
from receipt_bench.providers.anthropic_foundry import AnthropicFoundryAdapter
from receipt_bench.providers.azure_openai import AzureOpenAIAdapter
from receipt_bench.providers.azure_vision import AzureVisionReadExtractor
from receipt_bench.providers.base import AdapterConfigurationError
from receipt_bench.providers.content_understanding import (
    ContentUnderstandingCustomAdapter,
    ContentUnderstandingPrebuiltReceiptAdapter,
)
from receipt_bench.providers.document_intelligence import DocumentIntelligenceAdapter
from receipt_bench.providers.foundry_openai import FoundryOpenAIAdapter
from receipt_bench.providers.mistral_ocr import MistralOcrExtractor
from receipt_bench.providers.ocr_pipeline import OcrPipelineAdapter
from receipt_bench.providers.registry import build_adapters

ROOT = Path(__file__).resolve().parents[2]


def _real_benchmark_config() -> BenchmarkConfig:
    return BenchmarkConfig.load(ROOT / "configs" / "solutions.yaml")


def _minimal_config(*solutions: SolutionConfig) -> BenchmarkConfig:
    return BenchmarkConfig(
        schema_path=Path("schemas/expense-receipt.schema.json"),
        dataset_manifest=Path("data/manifests/benchmark.jsonl"),
        output_root=Path("results/runs"),
        random_seed=1,
        retry=RetryConfig(),
        solutions=list(solutions),
    )


def test_legacy_build_adapters_excludes_product_only_ocr_text_rows(
    azure_settings: AzureSettings, receipt_schema: dict[str, Any]
) -> None:
    config = _real_benchmark_config()

    adapters = build_adapters(config, azure_settings, receipt_schema)

    assert set(adapters.keys()) == {
        solution.id
        for solution in config.solutions
        if solution.kind not in {"native_pipeline", "ocr_text"}
    }
    assert isinstance(adapters["gpt-5.6-sol"], AzureOpenAIAdapter)
    assert isinstance(adapters["grok-4.6"], FoundryOpenAIAdapter)
    assert isinstance(adapters["mistral-large-3"], FoundryOpenAIAdapter)
    assert isinstance(adapters["document-intelligence-receipt"], DocumentIntelligenceAdapter)
    assert isinstance(adapters["content-understanding-custom"], ContentUnderstandingCustomAdapter)
    assert isinstance(
        adapters["content-understanding-prebuilt-receipt"],
        ContentUnderstandingPrebuiltReceiptAdapter,
    )
    assert isinstance(adapters["mistral-document-ai-2512"], OcrPipelineAdapter)
    assert isinstance(adapters["azure-vision-read"], OcrPipelineAdapter)
    assert isinstance(adapters["content-understanding-read"], OcrPipelineAdapter)


def test_build_adapters_sends_bearer_auth_header_to_mistral_ocr_endpoint(
    azure_settings: AzureSettings, receipt_schema: dict[str, Any]
) -> None:
    """The Azure-hosted Mistral OCR endpoint authenticates via a bearer token built
    from the actual configured API key, not a placeholder, so the header is asserted
    against the real expected value without ever logging/printing the key itself."""
    config = _real_benchmark_config()

    adapters = build_adapters(config, azure_settings, receipt_schema)

    mistral_adapter = adapters["mistral-document-ai-2512"]
    assert isinstance(mistral_adapter, OcrPipelineAdapter)
    extractor = mistral_adapter._extractor  # type: ignore[attr-defined]
    assert isinstance(extractor, MistralOcrExtractor)
    http_client = extractor._client  # type: ignore[attr-defined]
    assert isinstance(http_client, httpx.Client)
    configured_key = azure_settings.azure_foundry_api_key
    assert configured_key is not None
    expected = "Bearer " + configured_key.get_secret_value()
    assert http_client.headers["Authorization"] == expected


def test_build_adapters_constructs_anthropic_foundry_client(
    azure_settings: AzureSettings, receipt_schema: dict[str, Any]
) -> None:
    """Claude uses the official ``anthropic.AnthropicFoundry`` client, not generic ``Anthropic``."""
    config = _minimal_config(
        SolutionConfig(
            id="claude-test",
            kind="direct",
            api="anthropic-foundry",
            deployment="receipt-claude-opus-5",
            model="claude-opus-5",
        )
    )

    adapters = build_adapters(config, azure_settings, receipt_schema)

    claude_adapter = adapters["claude-test"]
    assert isinstance(claude_adapter, AnthropicFoundryAdapter)
    client = claude_adapter._client  # type: ignore[attr-defined]
    assert isinstance(client, anthropic.AnthropicFoundry)


def test_build_adapters_disables_openai_sdk_retries(
    azure_settings: AzureSettings, receipt_schema: dict[str, Any]
) -> None:
    """Azure OpenAI and Foundry OpenAI-compatible clients must disable the ``openai``
    SDK's built-in retry loop (``max_retries=0``) so the runner's own retry accounting
    is the sole, authoritative source of retry attempts."""
    config = _real_benchmark_config()

    adapters = build_adapters(config, azure_settings, receipt_schema)

    gpt_adapter = adapters["gpt-5.6-sol"]
    assert isinstance(gpt_adapter, AzureOpenAIAdapter)
    assert gpt_adapter._client.max_retries == 0  # type: ignore[attr-defined]

    grok_adapter = adapters["grok-4.6"]
    assert isinstance(grok_adapter, FoundryOpenAIAdapter)
    assert grok_adapter._client.max_retries == 0  # type: ignore[attr-defined]


def test_build_adapters_disables_anthropic_sdk_retries(
    azure_settings: AzureSettings, receipt_schema: dict[str, Any]
) -> None:
    """The ``anthropic.AnthropicFoundry`` client must also disable its built-in retries."""
    config = _minimal_config(
        SolutionConfig(
            id="claude-test",
            kind="direct",
            api="anthropic-foundry",
            deployment="receipt-claude-opus-5",
            model="claude-opus-5",
        )
    )

    adapters = build_adapters(config, azure_settings, receipt_schema)

    claude_adapter = adapters["claude-test"]
    assert isinstance(claude_adapter, AnthropicFoundryAdapter)
    assert claude_adapter._client.max_retries == 0  # type: ignore[attr-defined]


def test_build_adapters_disables_document_intelligence_retry_policy(
    azure_settings: AzureSettings, receipt_schema: dict[str, Any]
) -> None:
    """``DocumentIntelligenceClient`` accepts ``retry_total`` as an azure-core pipeline
    kwarg forwarded to its generated ``RetryPolicy``; it must be set to 0 so the client's
    own automatic retry policy performs no retries beyond the initial attempt."""
    config = _real_benchmark_config()

    adapters = build_adapters(config, azure_settings, receipt_schema)

    di_adapter = adapters["document-intelligence-receipt"]
    assert isinstance(di_adapter, DocumentIntelligenceAdapter)
    client = di_adapter._client  # type: ignore[attr-defined]
    assert client._config.retry_policy.total_retries == 0


def test_build_adapters_disables_vision_retry_policy(
    azure_settings: AzureSettings, receipt_schema: dict[str, Any]
) -> None:
    """``ImageAnalysisClient`` must likewise have its azure-core retry policy disabled."""
    config = _real_benchmark_config()

    adapters = build_adapters(config, azure_settings, receipt_schema)

    vision_adapter = adapters["azure-vision-read"]
    assert isinstance(vision_adapter, OcrPipelineAdapter)
    extractor = vision_adapter._extractor  # type: ignore[attr-defined]
    assert isinstance(extractor, AzureVisionReadExtractor)
    client = extractor._client  # type: ignore[attr-defined]
    assert client._config.retry_policy.total_retries == 0


def test_build_adapters_raises_on_missing_deployment(
    azure_settings: AzureSettings, receipt_schema: dict[str, Any]
) -> None:
    config = _minimal_config(
        SolutionConfig(id="broken", kind="direct", api="azure-openai", deployment=None)
    )

    with pytest.raises(AdapterConfigurationError, match="missing 'deployment'"):
        build_adapters(config, azure_settings, receipt_schema)


def test_build_adapters_raises_on_missing_analyzer_id(
    azure_settings: AzureSettings, receipt_schema: dict[str, Any]
) -> None:
    config = _minimal_config(
        SolutionConfig(id="broken", kind="native", api="content-understanding", analyzer_id=None)
    )

    with pytest.raises(AdapterConfigurationError, match="missing 'analyzer_id'"):
        build_adapters(config, azure_settings, receipt_schema)


def test_build_adapters_raises_on_missing_normalizer(
    azure_settings: AzureSettings, receipt_schema: dict[str, Any]
) -> None:
    config = _minimal_config(
        SolutionConfig(
            id="broken",
            kind="ocr_pipeline",
            api="azure-vision",
            normalizer=None,
        )
    )

    with pytest.raises(AdapterConfigurationError, match="missing 'normalizer'"):
        build_adapters(config, azure_settings, receipt_schema)


def test_build_adapters_raises_on_unresolvable_normalizer_id(
    azure_settings: AzureSettings, receipt_schema: dict[str, Any]
) -> None:
    config = _minimal_config(
        SolutionConfig(
            id="broken",
            kind="ocr_pipeline",
            api="azure-vision",
            normalizer="does-not-exist",
        )
    )

    with pytest.raises(AdapterConfigurationError, match="Unknown normalizer solution id"):
        build_adapters(config, azure_settings, receipt_schema)


def test_build_adapters_raises_on_unknown_api(
    azure_settings: AzureSettings, receipt_schema: dict[str, Any]
) -> None:
    config = _minimal_config(
        SolutionConfig(id="broken", kind="direct", api="unknown-api", deployment="dep")
    )

    with pytest.raises(AdapterConfigurationError, match="unsupported api"):
        build_adapters(config, azure_settings, receipt_schema)


def test_build_adapters_raises_on_unsupported_ocr_pipeline_api(
    azure_settings: AzureSettings, receipt_schema: dict[str, Any]
) -> None:
    config = _minimal_config(
        SolutionConfig(
            id="broken",
            kind="ocr_pipeline",
            api="unknown-ocr-api",
            normalizer="gpt-5.4-mini",
        ),
        SolutionConfig(
            id="gpt-5.4-mini",
            kind="direct",
            api="azure-openai",
            deployment="receipt-gpt-54-mini",
            reasoning_effort="low",
        ),
    )

    with pytest.raises(AdapterConfigurationError, match="unsupported OCR-pipeline api"):
        build_adapters(config, azure_settings, receipt_schema)
