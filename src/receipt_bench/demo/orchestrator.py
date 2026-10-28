from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from receipt_bench.config import AzureSettings, BenchmarkConfig
from receipt_bench.demo.analysis import field_comparisons
from receipt_bench.demo.api_models import (
    ComponentView,
    DemoPreset,
    DemoSolution,
    ErrorView,
    SolutionResult,
    TerminalStatus,
)
from receipt_bench.demo.domain import DemoInput
from receipt_bench.demo.mock import build_mock_result, delay_seconds
from receipt_bench.demo.settings import DemoSettings
from receipt_bench.demo.store import RunRecord
from receipt_bench.evaluation.pricing import (
    PricingSnapshot,
    estimate_component_cost,
    rate_for,
)
from receipt_bench.evaluation.scoring import check_arithmetic_consistency, validate_schema
from receipt_bench.models import (
    ComponentMeasurement,
    NonFiniteNumberError,
    SolutionOutput,
    ensure_finite_numbers,
)
from receipt_bench.providers.base import (
    InvalidSolutionOutput,
    ProviderResponseError,
    SolutionAdapter,
)
from receipt_bench.providers.registry import build_adapters

LOGGER = logging.getLogger(__name__)

AdapterFactory = Callable[
    [BenchmarkConfig, AzureSettings, dict[str, Any]],
    Mapping[str, SolutionAdapter],
]

_LABELS = {
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "gpt-5.6-terra": "GPT-5.6 Terra",
    "gpt-5.6-luna": "GPT-5.6 Luna",
    "gpt-5.4-mini": "GPT-5.4 Mini",
    "gpt-5.2": "GPT-5.2",
    "grok-4.6": "Grok 4.6",
    "mistral-large-3": "Mistral Large 3",
    "content-understanding-custom": "Content Understanding · Custom",
    "content-understanding-prebuilt-receipt": "Content Understanding · Receipt",
    "document-intelligence-receipt": "Document Intelligence · Receipt",
    "mistral-document-ai-2512": "Mistral Document AI + GPT-5.6 Luna",
    "mistral-ocr-4.0": "Mistral OCR 4.0 + GPT-5.6 Luna",
    "azure-vision-read": "Azure Vision Read + GPT-5.6 Luna",
    "content-understanding-read": "Content Understanding Read + GPT-5.6 Luna",
    "content-understanding-layout": "Content Understanding Layout + GPT-5.6 Luna",
}

_FAST_IDS = [
    "gpt-5.6-sol",
    "gpt-5.4-mini",
    "content-understanding-custom",
    "document-intelligence-receipt",
    "azure-vision-read",
]


def solution_catalog(config: BenchmarkConfig) -> list[DemoSolution]:
    return [
        DemoSolution(
            id=solution.id,
            label=_LABELS.get(solution.id, solution.id),
            order=order,
            kind=solution.kind,
            api=solution.api,
            lifecycle=solution.lifecycle,
            model=solution.model,
            normalizer=solution.normalizer,
        )
        for order, solution in enumerate(config.solutions)
    ]


def solution_presets(solutions: list[DemoSolution]) -> list[DemoPreset]:
    all_ids = [solution.id for solution in solutions]
    fast_ids = [solution_id for solution_id in _FAST_IDS if solution_id in all_ids]
    return [
        DemoPreset(
            id="full",
            label="Full",
            description=f"All {len(all_ids)} configured solution paths.",
            solution_ids=all_ids,
        ),
        DemoPreset(
            id="fast",
            label="Fast",
            description="Five representative direct, native, and OCR-pipeline paths.",
            solution_ids=fast_ids,
        ),
        DemoPreset(
            id="custom",
            label="Custom",
            description="Choose any configured solution paths.",
            solution_ids=[],
        ),
    ]


def _default_adapter_factory(
    config: BenchmarkConfig,
    settings: AzureSettings,
    schema: dict[str, Any],
) -> Mapping[str, SolutionAdapter]:
    return build_adapters(config, settings, schema)


def component_provider_keys(solution: DemoSolution) -> tuple[str, ...]:
    """Return every provider capacity consumed by a solution pipeline."""
    providers = {solution.api}
    if solution.normalizer is not None:
        providers.add("azure-openai")
    return tuple(sorted(providers))


class ApplicationConcurrency:
    """Application-wide limits shared by every run and API client."""

    def __init__(self, settings: DemoSettings, solutions: list[DemoSolution]) -> None:
        self._global = asyncio.Semaphore(settings.max_parallel)
        provider_keys = {
            provider
            for solution in solutions
            for provider in component_provider_keys(solution)
        }
        self._providers = {
            provider: asyncio.Semaphore(settings.provider_concurrency)
            for provider in provider_keys
        }
        self._solutions = {
            solution.id: asyncio.Semaphore(settings.solution_concurrency)
            for solution in solutions
        }

    @asynccontextmanager
    async def slot(self, solution: DemoSolution) -> AsyncIterator[None]:
        semaphores = [
            self._global,
            *(self._providers[key] for key in component_provider_keys(solution)),
            self._solutions[solution.id],
        ]
        acquired: list[asyncio.Semaphore] = []
        try:
            for semaphore in semaphores:
                await semaphore.acquire()
                acquired.append(semaphore)
            yield
        finally:
            for semaphore in reversed(acquired):
                semaphore.release()


class DemoOrchestrator:
    def __init__(
        self,
        *,
        settings: DemoSettings,
        benchmark_config: BenchmarkConfig,
        schema: dict[str, Any],
        pricing: PricingSnapshot | None,
        adapter_factory: AdapterFactory | None = None,
    ) -> None:
        self._settings = settings
        self._benchmark_config = benchmark_config
        self._schema = schema
        self._pricing = pricing
        self._adapter_factory = adapter_factory or _default_adapter_factory
        self._concurrency = ApplicationConcurrency(
            settings,
            solution_catalog(benchmark_config),
        )

    async def run(self, record: RunRecord) -> None:
        await record.publish(
            "run_started",
            {
                "run_id": record.run_id,
                "comparison_mode": record.comparison_mode,
                "execution_mode": record.execution_mode,
                "mock": record.is_mock,
                "total_pairs": record.total_pairs,
                "receipt_order": [item.receipt_id for item in record.inputs],
                "solution_order": [solution.id for solution in record.solutions],
            },
        )
        try:
            adapters = await self._build_live_adapters(record)
            await self._run_pairs(record, adapters)
            await record.complete()
        except ValidationError:
            await record.fail(
                ErrorView(
                    code="azure_configuration_invalid",
                    message="Server-side Azure configuration is incomplete or invalid.",
                )
            )
        except Exception as exc:
            LOGGER.error("Demo run %s failed with %s", record.run_id, type(exc).__name__)
            await record.fail(
                ErrorView(
                    code="orchestration_failed",
                    message="The run stopped because of an unexpected server-side error.",
                )
            )
        finally:
            record.release_input_bytes()

    async def _build_live_adapters(
        self, record: RunRecord
    ) -> Mapping[str, SolutionAdapter] | None:
        if record.is_mock:
            return None
        if not self._settings.live_execution_enabled:
            raise RuntimeError("Live execution is not enabled")
        azure_settings = AzureSettings()  # type: ignore[call-arg]
        return await asyncio.to_thread(
            self._adapter_factory,
            self._benchmark_config,
            azure_settings,
            self._schema,
        )

    async def _run_pairs(
        self,
        record: RunRecord,
        adapters: Mapping[str, SolutionAdapter] | None,
    ) -> None:
        async def run_pair(input_receipt: DemoInput, solution: DemoSolution) -> None:
            async with self._concurrency.slot(solution):
                await record.publish(
                    "solution_started",
                    {
                        "receipt_id": input_receipt.receipt_id,
                        "sample_id": input_receipt.sample_id,
                        "solution_id": solution.id,
                    },
                )
                if record.is_mock:
                    await asyncio.sleep(
                        delay_seconds(
                            input_receipt,
                            solution.id,
                            self._settings.mock_delay_scale,
                        )
                    )
                    result = build_mock_result(
                        run_id=record.run_id,
                        input_receipt=input_receipt,
                        solution=solution,
                        schema=self._schema,
                        pricing=self._pricing,
                    )
                else:
                    if adapters is None or solution.id not in adapters:
                        result = self._configuration_error(input_receipt, solution)
                    else:
                        result = await self._execute_live(
                            record.run_id,
                            input_receipt,
                            solution,
                            adapters[solution.id],
                        )
                await record.add_result(result)

        tasks = [
            asyncio.create_task(run_pair(input_receipt, solution))
            for input_receipt in record.inputs
            for solution in record.solutions
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def _configuration_error(
        self, input_receipt: DemoInput, solution: DemoSolution
    ) -> SolutionResult:
        return SolutionResult(
            receipt_id=input_receipt.receipt_id,
            sample_id=input_receipt.sample_id,
            solution_id=solution.id,
            solution_order=solution.order,
            status="failed",
            mock=False,
            schema_valid=False,
            latency_ms=0.0,
            error=ErrorView(
                code="adapter_not_configured",
                message="No server-side adapter is configured for this solution.",
            ),
        )

    def _component_views(
        self,
        solution: DemoSolution,
        measurements: list[ComponentMeasurement],
    ) -> tuple[list[ComponentView], float | None]:
        if not measurements:
            return [], None
        ensure_finite_numbers(measurements)
        views: list[ComponentView] = []
        costs: list[float | None] = []
        for measurement in measurements:
            cost = estimate_component_cost(
                rate_for(
                    self._pricing,
                    solution.id,
                    measurement.component,
                    normalizer_model=solution.normalizer,
                ),
                measurement.usage,
            )
            ensure_finite_numbers(cost)
            costs.append(cost)
            views.append(
                ComponentView(
                    component=measurement.component,
                    latency_ms=measurement.latency_ms,
                    usage=measurement.usage,
                    estimated_cost_usd=cost,
                )
            )
        total = None if any(cost is None for cost in costs) else sum(
            cost for cost in costs if cost is not None
        )
        ensure_finite_numbers(total)
        return views, total

    async def _execute_live(
        self,
        run_id: str,
        input_receipt: DemoInput,
        solution: DemoSolution,
        adapter: SolutionAdapter,
    ) -> SolutionResult:
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        try:
            response = await asyncio.to_thread(
                adapter.execute,
                input_receipt.image_bytes,
                input_receipt.media_type,
            )
        except ProviderResponseError as exc:
            return self._known_error_result(
                input_receipt=input_receipt,
                solution=solution,
                status="failed",
                code="provider_error",
                message="The provider call failed. Check server logs for operational details.",
                started=started,
                measurements=list(exc.components),
            )
        except InvalidSolutionOutput as exc:
            return self._known_error_result(
                input_receipt=input_receipt,
                solution=solution,
                status="invalid",
                code="invalid_solution_output",
                message=(
                    "The provider response could not be validated against the "
                    "canonical schema."
                ),
                started=started,
                measurements=list(exc.components),
            )
        except (ValidationError, ValueError):
            return self._known_error_result(
                input_receipt=input_receipt,
                solution=solution,
                status="invalid",
                code="invalid_solution_output",
                message=(
                    "The provider response could not be validated against the "
                    "canonical schema."
                ),
                started=started,
                measurements=[],
            )
        except Exception as exc:
            LOGGER.error(
                "Solution %s failed for %s with %s",
                solution.id,
                input_receipt.sample_id,
                type(exc).__name__,
            )
            return self._known_error_result(
                input_receipt=input_receipt,
                solution=solution,
                status="failed",
                code="unexpected_provider_error",
                message="The provider adapter stopped with an unexpected server-side error.",
                started=started,
                measurements=[],
            )

        try:
            ensure_finite_numbers(response.receipt)
            ensure_finite_numbers(response.components)
            completed_at = datetime.now(UTC)
            latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
            components, cost = self._component_views(solution, list(response.components))
            output = SolutionOutput(
                run_id=run_id,
                sample_id=input_receipt.sample_id,
                solution_id=solution.id,
                started_at=started_at,
                completed_at=completed_at,
                status="succeeded",
                receipt=response.receipt,
                raw_artifact="",
                components=list(response.components),
            )
            comparisons = (
                field_comparisons(input_receipt.sample, output, self._schema)
                if input_receipt.sample is not None
                else []
            )
            result = SolutionResult(
                receipt_id=input_receipt.receipt_id,
                sample_id=input_receipt.sample_id,
                solution_id=solution.id,
                solution_order=solution.order,
                status="succeeded",
                mock=False,
                schema_valid=validate_schema(response.receipt, self._schema),
                arithmetic_consistent=check_arithmetic_consistency(response.receipt),
                latency_ms=latency_ms,
                estimated_cost_usd=cost,
                canonical_result=response.receipt,
                components=components,
                field_comparisons=comparisons,
            )
            ensure_finite_numbers(result)
            return result
        except (NonFiniteNumberError, OverflowError):
            LOGGER.warning(
                "Solution %s returned unsafe numeric data for %s",
                solution.id,
                input_receipt.sample_id,
            )
            return self._known_error_result(
                input_receipt=input_receipt,
                solution=solution,
                status="invalid",
                code="non_finite_provider_output",
                message="The provider response contained a non-finite numeric value.",
                started=started,
                measurements=[],
            )

    def _known_error_result(
        self,
        *,
        input_receipt: DemoInput,
        solution: DemoSolution,
        status: TerminalStatus,
        code: str,
        message: str,
        started: float,
        measurements: list[ComponentMeasurement],
    ) -> SolutionResult:
        try:
            components, cost = self._component_views(solution, measurements)
        except (NonFiniteNumberError, OverflowError, ValidationError):
            LOGGER.warning(
                "Discarding unsafe component telemetry for failed solution %s",
                solution.id,
            )
            components, cost = [], None
        return SolutionResult(
            receipt_id=input_receipt.receipt_id,
            sample_id=input_receipt.sample_id,
            solution_id=solution.id,
            solution_order=solution.order,
            status=status,
            mock=False,
            schema_valid=False,
            latency_ms=round((time.perf_counter() - started) * 1000.0, 1),
            estimated_cost_usd=cost,
            components=components,
            error=ErrorView(code=code, message=message),
        )


__all__ = [
    "AdapterFactory",
    "ApplicationConcurrency",
    "DemoOrchestrator",
    "component_provider_keys",
    "solution_catalog",
    "solution_presets",
]
