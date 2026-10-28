from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from receipt_bench.evaluation.pricing import (
    PricingSnapshot,
    estimate_component_cost,
    rate_for,
)
from receipt_bench.models import ComponentMeasurement
from receipt_bench.projects.evaluation import evaluate_answer
from receipt_bench.projects.models import JsonObject, ensure_json_object
from receipt_bench.projects.ocr_evaluation import evaluate_ocr_text
from receipt_bench.providers.base import InvalidSolutionOutput, ProviderResponseError
from receipt_bench.providers.project_adapters import (
    ProjectAdapter,
    ProjectExecutionContext,
)
from receipt_bench.storage.models import TaskRecord
from receipt_bench.storage.validation import JsonValue
from receipt_bench.worker.contracts import (
    ExecutionError,
    PairExecutor,
    ParsedExecution,
    RawExecution,
)


async def _run_sync[**P, T](
    call: Callable[P, T],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Keep non-cancellable provider work fenced until its thread exits."""

    thread_call = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    cancelled: asyncio.CancelledError | None = None
    while not thread_call.done():
        try:
            await asyncio.shield(thread_call)
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    try:
        result = thread_call.result()
    except BaseException:
        if cancelled is not None:
            raise cancelled from None
        raise
    if cancelled is not None:
        raise cancelled
    return result


@dataclass(frozen=True)
class ResolvedPairContext:
    execution: ProjectExecutionContext
    gold: JsonObject | None
    ocr_reference: str | None = None


class PairContextResolver(Protocol):
    def resolve(self, task: TaskRecord) -> ResolvedPairContext: ...


class ProjectPairExecutor(PairExecutor):
    """Bridge project-native provider adapters into the durable worker contract."""

    def __init__(
        self,
        *,
        adapters: dict[str, ProjectAdapter],
        context_resolver: PairContextResolver,
        pricing: PricingSnapshot | None = None,
    ) -> None:
        self._adapters = dict(adapters)
        self._contexts = context_resolver
        self._pricing = pricing

    def _telemetry(
        self,
        task: TaskRecord,
        context: ResolvedPairContext,
        components: tuple[ComponentMeasurement, ...] | list[ComponentMeasurement],
    ) -> JsonObject:
        normalizer_model = (
            "gpt-5.6-luna"
            if context.execution.prompt.mode == "ocr-normalizer"
            else None
        )
        rendered: list[JsonObject] = []
        costs: list[float | None] = []
        for component in components:
            cost = estimate_component_cost(
                rate_for(
                    self._pricing,
                    task.solution_id,
                    component.component,
                    normalizer_model=normalizer_model,
                ),
                component.usage,
            )
            costs.append(cost)
            rendered.append(
                ensure_json_object(
                    {
                    **component.model_dump(mode="json"),
                    "estimated_cost_usd": cost,
                    },
                    context="Component telemetry",
                )
            )
        total_cost = (
            sum(cost for cost in costs if cost is not None)
            if all(cost is not None for cost in costs)
            else None
        )
        return ensure_json_object(
            {
                "components": rendered,
                "total_tokens": sum(
                    component.usage.total_tokens or 0 for component in components
                )
                or None,
                "total_pages": sum(
                    component.usage.pages or 0 for component in components
                )
                or None,
                "latency_ms": sum(component.latency_ms for component in components),
                "estimated_cost_usd": total_cost,
                "attempts": 1,
                "errors": [],
            },
            context="Pipeline telemetry",
        )

    def _adapter(self, task: TaskRecord) -> ProjectAdapter:
        try:
            return self._adapters[task.solution_id]
        except KeyError as exc:
            raise ExecutionError(
                "adapter_not_configured",
                "No provider adapter is configured for this solution.",
                retryable=False,
            ) from exc

    async def execute(
        self,
        task: TaskRecord,
        image_bytes: bytes,
        media_type: str,
    ) -> RawExecution:
        context = self._contexts.resolve(task)
        adapter = self._adapter(task)
        try:
            response = await _run_sync(
                adapter.execute_raw,
                context.execution,
                image_bytes,
                media_type,
            )
        except ProviderResponseError as exc:
            raise ExecutionError(
                "provider_error",
                "The provider call failed.",
                retryable=True,
                raw=exc.raw_response,
                telemetry=self._telemetry(task, context, exc.components),
            ) from exc
        except InvalidSolutionOutput as exc:
            raise ExecutionError(
                "invalid_solution_output",
                "The provider response could not be parsed.",
                retryable=False,
                status="invalid",
                raw=exc.raw_response,
                telemetry=self._telemetry(task, context, exc.components),
            ) from exc
        except (TypeError, ValueError, ValidationError) as exc:
            raise ExecutionError(
                "invalid_solution_output",
                "The provider response could not be interpreted.",
                retryable=False,
                status="invalid",
                telemetry=self._telemetry(task, context, ()),
            ) from exc
        return RawExecution(
            raw=response.raw,
            telemetry=self._telemetry(task, context, response.components),
        )

    async def parse(
        self,
        task: TaskRecord,
        raw: JsonValue,
    ) -> ParsedExecution:
        if not isinstance(raw, dict):
            raise ExecutionError(
                "invalid_raw_artifact",
                "The provider artifact is not a JSON object.",
                retryable=False,
                status="invalid",
            )
        context = self._contexts.resolve(task)
        adapter = self._adapter(task)
        try:
            parsed = await _run_sync(
                adapter.parse_raw,
                context.execution,
                raw,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ExecutionError(
                "unparseable_answer",
                "The provider answer is not valid project JSON.",
                retryable=False,
                status="invalid",
                raw=raw,
            ) from exc

        comparison: JsonValue | None = None
        if context.execution.compatibility.solution_kind == "ocr-text":
            text = parsed.get("text")
            if not isinstance(text, str):
                raise ExecutionError(
                    "unparseable_ocr_text",
                    "The OCR service returned no comparable text.",
                    retryable=False,
                    status="invalid",
                    raw=raw,
                )
            if context.ocr_reference is None:
                raise ExecutionError(
                    "ocr_reference_unavailable",
                    "No OCR reference is available for this receipt.",
                    retryable=False,
                )
            comparison = evaluate_ocr_text(
                project_id=context.execution.project.project_id,
                reference=context.ocr_reference,
                hypothesis=text,
            ).model_dump(mode="json")
        elif context.gold is not None:
            comparison = evaluate_answer(
                project=context.execution.project,
                sample_id=task.sample_id,
                solution_id=task.solution_id,
                gold=context.gold,
                actual=parsed,
            ).model_dump(mode="json")
        return ParsedExecution(parsed=parsed, comparison=comparison)


__all__ = [
    "PairContextResolver",
    "ProjectPairExecutor",
    "ResolvedPairContext",
]
