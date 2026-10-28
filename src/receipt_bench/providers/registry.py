"""Registry/factory: turn ``BenchmarkConfig`` solutions into ready-to-run adapters.

Builds one ``SolutionAdapter`` per configured solution from ``AzureSettings`` and
the loaded canonical receipt JSON Schema, composing each OCR extractor with the
shared configured normalizer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anthropic
import httpx
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.vision.imageanalysis import ImageAnalysisClient
from azure.core.credentials import AzureKeyCredential, TokenCredential
from azure.identity import get_bearer_token_provider
from openai import OpenAI

from receipt_bench.config import AzureSettings, BenchmarkConfig, SolutionConfig
from receipt_bench.providers._openai_chat import validate_reasoning_effort
from receipt_bench.providers.anthropic_foundry import AnthropicFoundryAdapter
from receipt_bench.providers.auth import (
    AZURE_AI_FOUNDRY_SCOPE,
    AZURE_COGNITIVE_SERVICES_SCOPE,
    RefreshingBearerTokenAuth,
)
from receipt_bench.providers.azure_openai import AzureOpenAIAdapter
from receipt_bench.providers.azure_vision import AzureVisionReadExtractor
from receipt_bench.providers.base import AdapterConfigurationError, SolutionAdapter
from receipt_bench.providers.content_understanding import (
    ContentUnderstandingClient,
    ContentUnderstandingCustomAdapter,
    ContentUnderstandingOcrExtractor,
    ContentUnderstandingPrebuiltReceiptAdapter,
)
from receipt_bench.providers.document_intelligence import DocumentIntelligenceAdapter
from receipt_bench.providers.foundry_openai import FoundryOpenAIAdapter
from receipt_bench.providers.mistral_ocr import MistralOcrExtractor
from receipt_bench.providers.normalizer import StructuredOutputNormalizer
from receipt_bench.providers.ocr_pipeline import OcrPipelineAdapter, OcrTextExtractor
from receipt_bench.providers.project_adapters import (
    ContentUnderstandingCustomProjectAdapter,
    NativeNormalizerProjectAdapter,
    NativeProjectAdapter,
    OcrProjectAdapter,
    OcrTextProjectAdapter,
    OpenAIProjectAdapter,
    ProjectAdapter,
)


def _require_deployment(solution: SolutionConfig) -> str:
    if solution.deployment is None:
        raise AdapterConfigurationError(f"{solution.id}: solution config is missing 'deployment'")
    return solution.deployment


def _require_analyzer_id(solution: SolutionConfig) -> str:
    if solution.analyzer_id is None:
        raise AdapterConfigurationError(f"{solution.id}: solution config is missing 'analyzer_id'")
    return solution.analyzer_id


def _require_normalizer_id(solution: SolutionConfig) -> str:
    if solution.normalizer is None:
        raise AdapterConfigurationError(f"{solution.id}: solution config is missing 'normalizer'")
    return solution.normalizer


class AdapterRegistry:
    """Builds and caches provider clients, then assembles one adapter per solution."""

    def __init__(
        self,
        *,
        benchmark_config: BenchmarkConfig,
        azure_settings: AzureSettings,
        schema: dict[str, Any],
        token_credential: TokenCredential | None = None,
    ) -> None:
        self._benchmark_config = benchmark_config
        self._azure_settings = azure_settings
        self._schema = schema
        self._solutions_by_id = {s.id: s for s in benchmark_config.solutions}

        auth_mode = azure_settings.resolved_auth_mode
        openai_api_key: str | Callable[[], str]
        sdk_credential: AzureKeyCredential | TokenCredential
        if auth_mode == "key":
            configured_key = azure_settings.azure_foundry_api_key
            if configured_key is None:
                raise AdapterConfigurationError(
                    "Azure AI key authentication requires azure_foundry_api_key"
                )
            api_key = configured_key.get_secret_value()
            openai_api_key = api_key
            sdk_credential = AzureKeyCredential(api_key)
            self._anthropic_client = anthropic.AnthropicFoundry(
                api_key=api_key,
                base_url=azure_settings.azure_anthropic_endpoint,
                max_retries=0,
            )
            self._mistral_ocr_http_client = httpx.Client(
                base_url=azure_settings.azure_mistral_ocr_endpoint,
                headers={"Authorization": "Bearer " + api_key},
            )
            content_understanding_http_client = httpx.Client(
                base_url=azure_settings.azure_content_understanding_endpoint,
                headers={"Ocp-Apim-Subscription-Key": api_key},
                timeout=httpx.Timeout(60.0),
            )
        else:
            credential = token_credential or azure_settings.create_token_credential()
            foundry_token_provider = get_bearer_token_provider(
                credential, AZURE_AI_FOUNDRY_SCOPE
            )
            openai_api_key = foundry_token_provider
            sdk_credential = credential
            self._anthropic_client = anthropic.AnthropicFoundry(
                azure_ad_token_provider=foundry_token_provider,
                base_url=azure_settings.azure_anthropic_endpoint,
                max_retries=0,
            )
            self._mistral_ocr_http_client = httpx.Client(
                base_url=azure_settings.azure_mistral_ocr_endpoint,
                auth=RefreshingBearerTokenAuth(
                    credential,
                    AZURE_AI_FOUNDRY_SCOPE,
                ),
            )
            content_understanding_http_client = httpx.Client(
                base_url=azure_settings.azure_content_understanding_endpoint,
                auth=RefreshingBearerTokenAuth(
                    credential,
                    AZURE_COGNITIVE_SERVICES_SCOPE,
                ),
                timeout=httpx.Timeout(60.0),
            )

        # Retry accounting must be transparent: every SDK client below disables its
        # own built-in retries so the runner's retry loop is the sole, authoritative
        # source of retry attempts/backoff. ``httpx.Client`` (used for the Mistral OCR
        # and Content Understanding surfaces below) performs no automatic retries by
        # default, so no equivalent option is needed there.
        self._azure_openai_client = OpenAI(
            base_url=azure_settings.azure_foundry_openai_endpoint,
            api_key=openai_api_key,
            max_retries=0,
        )
        self._foundry_openai_client = OpenAI(
            base_url=azure_settings.azure_foundry_models_endpoint,
            api_key=openai_api_key,
            max_retries=0,
        )
        # ``retry_total=0`` is an azure-core pipeline kwarg forwarded to the generated
        # client's ``RetryPolicy`` (confirmed against the installed SDK's
        # ``_configuration.py``, which builds ``policies.RetryPolicy(**kwargs)``); it
        # disables the policy's automatic retries entirely, leaving only the single
        # initial attempt per call.
        self._document_intelligence_client = DocumentIntelligenceClient(
            endpoint=azure_settings.azure_document_intelligence_endpoint,
            credential=sdk_credential,
            retry_total=0,
        )
        self._vision_client = ImageAnalysisClient(
            endpoint=azure_settings.azure_vision_endpoint,
            credential=sdk_credential,
            retry_total=0,
        )
        self._content_understanding_client = ContentUnderstandingClient(
            client=content_understanding_http_client
        )
        self._normalizers: dict[str, StructuredOutputNormalizer] = {}

    def _normalizer_for(self, normalizer_id: str) -> StructuredOutputNormalizer:
        cached = self._normalizers.get(normalizer_id)
        if cached is not None:
            return cached
        normalizer_solution = self._solutions_by_id.get(normalizer_id)
        if normalizer_solution is None:
            raise AdapterConfigurationError(f"Unknown normalizer solution id: {normalizer_id!r}")
        deployment = _require_deployment(normalizer_solution)
        reasoning_effort = (
            validate_reasoning_effort(normalizer_solution.reasoning_effort)
            if normalizer_solution.reasoning_effort is not None
            else None
        )
        normalizer = StructuredOutputNormalizer(
            client=self._azure_openai_client,
            deployment=deployment,
            reasoning_effort=reasoning_effort,
        )
        self._normalizers[normalizer_id] = normalizer
        return normalizer

    def _build_ocr_extractor(self, solution: SolutionConfig) -> OcrTextExtractor:
        if solution.api == "azure-vision":
            return AzureVisionReadExtractor(client=self._vision_client)
        if solution.api == "mistral-ocr":
            return MistralOcrExtractor(
                client=self._mistral_ocr_http_client, deployment=_require_deployment(solution)
            )
        if solution.api == "content-understanding":
            return ContentUnderstandingOcrExtractor(
                client=self._content_understanding_client,
                analyzer_id=_require_analyzer_id(solution),
            )
        raise AdapterConfigurationError(
            f"{solution.id}: unsupported OCR-pipeline api {solution.api!r}"
        )

    def _build_adapter(self, solution: SolutionConfig) -> SolutionAdapter:
        if solution.kind == "ocr_pipeline":
            extractor = self._build_ocr_extractor(solution)
            normalizer = self._normalizer_for(_require_normalizer_id(solution))
            return OcrPipelineAdapter(
                solution.id, extractor=extractor, normalizer=normalizer, schema=self._schema
            )

        if solution.api == "azure-openai":
            reasoning_effort = (
                validate_reasoning_effort(solution.reasoning_effort)
                if solution.reasoning_effort is not None
                else None
            )
            return AzureOpenAIAdapter(
                solution.id,
                client=self._azure_openai_client,
                deployment=_require_deployment(solution),
                schema=self._schema,
                reasoning_effort=reasoning_effort,
            )
        if solution.api == "foundry-openai":
            return FoundryOpenAIAdapter(
                solution.id,
                client=self._foundry_openai_client,
                deployment=_require_deployment(solution),
                schema=self._schema,
            )
        if solution.api == "anthropic-foundry":
            return AnthropicFoundryAdapter(
                solution.id,
                client=self._anthropic_client,
                deployment=_require_deployment(solution),
                schema=self._schema,
            )
        if solution.api == "document-intelligence":
            return DocumentIntelligenceAdapter(
                solution.id, client=self._document_intelligence_client
            )
        if solution.api == "content-understanding":
            analyzer_id = _require_analyzer_id(solution)
            if analyzer_id == "prebuilt-receipt":
                return ContentUnderstandingPrebuiltReceiptAdapter(
                    solution.id,
                    client=self._content_understanding_client,
                    analyzer_id=analyzer_id,
                )
            return ContentUnderstandingCustomAdapter(
                solution.id,
                client=self._content_understanding_client,
                analyzer_id=analyzer_id,
            )
        raise AdapterConfigurationError(f"{solution.id}: unsupported api {solution.api!r}")

    def _build_project_adapter(self, solution: SolutionConfig) -> ProjectAdapter:
        if solution.kind == "native_pipeline":
            normalizer_id = _require_normalizer_id(solution)
            normalizer_solution = self._solutions_by_id.get(normalizer_id)
            if normalizer_solution is None:
                raise AdapterConfigurationError(
                    f"Unknown normalizer solution id: {normalizer_id!r}"
                )
            reasoning_effort = (
                validate_reasoning_effort(normalizer_solution.reasoning_effort)
                if normalizer_solution.reasoning_effort is not None
                else None
            )
            return NativeNormalizerProjectAdapter(
                solution.id,
                adapter=self._build_adapter(solution),
                normalizer_client=self._azure_openai_client,
                normalizer_deployment=_require_deployment(normalizer_solution),
                reasoning_effort=reasoning_effort,
            )
        if solution.kind == "ocr_text":
            return OcrTextProjectAdapter(
                solution.id,
                extractor=self._build_ocr_extractor(solution),
            )
        if solution.kind == "ocr_pipeline":
            normalizer_id = _require_normalizer_id(solution)
            normalizer_solution = self._solutions_by_id.get(normalizer_id)
            if normalizer_solution is None:
                raise AdapterConfigurationError(
                    f"Unknown normalizer solution id: {normalizer_id!r}"
                )
            reasoning_effort = (
                validate_reasoning_effort(normalizer_solution.reasoning_effort)
                if normalizer_solution.reasoning_effort is not None
                else None
            )
            return OcrProjectAdapter(
                solution.id,
                extractor=self._build_ocr_extractor(solution),
                normalizer_client=self._azure_openai_client,
                normalizer_deployment=_require_deployment(normalizer_solution),
                reasoning_effort=reasoning_effort,
            )

        if solution.api == "azure-openai":
            reasoning_effort = (
                validate_reasoning_effort(solution.reasoning_effort)
                if solution.reasoning_effort is not None
                else None
            )
            return OpenAIProjectAdapter(
                solution.id,
                client=self._azure_openai_client,
                deployment=_require_deployment(solution),
                strict_schema=True,
                token_limit_param="max_completion_tokens",
                reasoning_effort=reasoning_effort,
            )
        if solution.api == "foundry-openai":
            return OpenAIProjectAdapter(
                solution.id,
                client=self._foundry_openai_client,
                deployment=_require_deployment(solution),
                strict_schema=False,
                token_limit_param="max_tokens",
                reasoning_effort=None,
            )
        if solution.id == "content-understanding-custom":
            if not solution.project_analyzers:
                raise AdapterConfigurationError(
                    "content-understanding-custom: project analyzer routing is missing"
                )
            return ContentUnderstandingCustomProjectAdapter(
                solution.id,
                client=self._content_understanding_client,
                analyzer_ids=solution.project_analyzers,
            )
        if solution.api in {"content-understanding", "document-intelligence"}:
            return NativeProjectAdapter(
                solution.id,
                adapter=self._build_adapter(solution),
            )
        raise AdapterConfigurationError(
            f"{solution.id}: unsupported project api {solution.api!r}"
        )

    def build_all(self) -> dict[str, SolutionAdapter]:
        return {
            solution.id: self._build_adapter(solution)
            for solution in self._benchmark_config.solutions
            if solution.kind not in {"native_pipeline", "ocr_text"}
        }

    def build_project_all(self) -> dict[str, ProjectAdapter]:
        """Build project-native adapters for every active solution."""
        return {
            solution.id: self._build_project_adapter(solution)
            for solution in self._benchmark_config.solutions
        }


def build_adapters(
    config: BenchmarkConfig,
    settings: AzureSettings,
    schema: dict[str, Any],
    *,
    token_credential: TokenCredential | None = None,
) -> dict[str, SolutionAdapter]:
    """Build one ``SolutionAdapter`` per solution configured in ``config``, keyed by solution id.

    This is the stable factory contract other layers (the CLI, evaluation runner) depend
    on: ``build_adapters(config, settings, schema) -> dict[str, SolutionAdapter]``.
    """
    registry = AdapterRegistry(
        benchmark_config=config,
        azure_settings=settings,
        schema=schema,
        token_credential=token_credential,
    )
    return registry.build_all()


def build_project_adapters(
    config: BenchmarkConfig,
    settings: AzureSettings,
    *,
    token_credential: TokenCredential | None = None,
) -> dict[str, ProjectAdapter]:
    """Build adapters that emit the active project's dataset-native answer shape."""
    registry = AdapterRegistry(
        benchmark_config=config,
        azure_settings=settings,
        schema={},
        token_credential=token_credential,
    )
    return registry.build_project_all()
