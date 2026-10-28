from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from receipt_bench.storage.paths import sample_ocr_text
from receipt_bench.storage.protocols import NotFoundError
from receipt_bench.storage.validation import JsonObject, JsonValue
from receipt_bench.web.catalog import SolutionCatalog
from receipt_bench.web.service import (
    _CUSTOM_PROMPT_TEMPLATES,
    ProductService,
    ProjectContext,
    _compare_objects,
    _custom_prompt_contract_hash,
)
from receipt_bench.worker.contracts import ExecutionError, ParsedExecution, RawExecution

if TYPE_CHECKING:
    from receipt_bench.projects.models import Project
    from receipt_bench.providers.project_adapters import ProjectAdapter
    from receipt_bench.providers.project_executor import (
        ProjectPairExecutor,
        ResolvedPairContext,
    )
    from receipt_bench.storage.models import TaskRecord

AdapterLoader = Callable[[], dict[str, "ProjectAdapter"]]
PromptMode = Literal["direct-multimodal", "ocr-normalizer"]


def _validate_pinned_contract(
    task: TaskRecord,
    context: ProjectContext,
    prompt_mode: PromptMode,
) -> None:
    expected_schema_hash = task.metadata.get("schema_hash")
    expected_prompt_mode = task.metadata.get("prompt_mode")
    expected_prompt_hash = task.metadata.get("prompt_hash")
    expected_prompt_version = task.metadata.get("prompt_version")
    if (
        expected_schema_hash != context.schema_hash
        or expected_prompt_mode != prompt_mode
        or expected_prompt_hash != context.prompt_hashes[prompt_mode]
        or expected_prompt_version != context.prompt_versions[prompt_mode]
    ):
        raise ExecutionError(
            "immutable_contract_mismatch",
            "The pinned project contract no longer matches the active runtime.",
            retryable=False,
        )


def _schema_example(schema: JsonObject, *, depth: int = 0) -> JsonValue:
    if depth > 12:
        return None
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, dict):
            return {}
        required_names = (
            [item for item in required if isinstance(item, str)]
            if isinstance(required, list)
            else list(properties)
        )
        return {
            name: _schema_example(child, depth=depth + 1)
            for name in required_names
            if isinstance((child := properties.get(name)), dict)
        }
    if schema_type == "array":
        minimum = schema.get("minItems")
        count = min(max(minimum, 0), 3) if isinstance(minimum, int) else 0
        item_schema = schema.get("items")
        return (
            [_schema_example(item_schema, depth=depth + 1) for _ in range(count)]
            if isinstance(item_schema, dict)
            else []
        )
    if schema_type == "number" or schema_type == "integer":
        return 0
    if schema_type == "boolean":
        return False
    if schema_type == "null":
        return None
    return ""


class MockPairExecutor:
    """Deterministic local executor that never imports or calls provider SDKs."""

    def __init__(self, service: ProductService, catalog: SolutionCatalog) -> None:
        self._service = service
        self._catalog = catalog

    async def execute(
        self,
        task: TaskRecord,
        image_bytes: bytes,
        media_type: str,
    ) -> RawExecution:
        del image_bytes, media_type
        project, sample = await self._service.sample_context(
            task.project_id,
            task.sample_id,
        )
        solution = self._catalog.get(task.solution_id)
        prompt_mode: PromptMode = (
            "ocr-normalizer"
            if solution.kind in {"native_pipeline", "ocr_pipeline", "ocr_text"}
            else "direct-multimodal"
        )
        _validate_pinned_contract(task, project, prompt_mode)
        gold = await self._service._artifact_object(sample.gold_path)
        output = cast(
            JsonObject,
            (
                {"text": "Deterministic mock OCR text"}
                if solution.kind == "ocr_text"
                else gold
                if gold is not None
                else _schema_example(project.schema)
            ),
        )
        component_id = (
            "ocr"
            if solution.kind in {"ocr_pipeline", "ocr_text"}
            else "native"
            if solution.kind in {"native", "native_pipeline"}
            else "extractor"
        )
        components: list[JsonObject] = [
            {
                "id": component_id,
                "label": solution.label,
                "status": "succeeded",
                "latency_ms": float(10 + solution.order),
                "input_tokens": None,
                "output_tokens": None,
                "pages": 1,
                "estimated_cost_usd": 0.0,
                "attempt": 1,
                "input": None,
                "output": json.dumps(
                    output,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                ),
                "error": None,
            }
        ]
        if solution.kind in {"native_pipeline", "ocr_pipeline"}:
            components.append(
                {
                    "id": "normalizer",
                    "label": "GPT-5.6 Luna normalizer",
                    "status": "succeeded",
                    "latency_ms": float(20 + solution.order),
                    "input_tokens": 64,
                    "output_tokens": 32,
                    "pages": None,
                    "estimated_cost_usd": 0.0,
                    "attempt": 1,
                    "input": "Deterministic mock OCR text",
                    "output": json.dumps(
                        output,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                    ),
                    "error": None,
                }
            )
        latency = sum(
            float(value)
            for component in components
            if isinstance((value := component.get("latency_ms")), int | float)
            and not isinstance(value, bool)
        )
        return RawExecution(
            raw={
                "mock": True,
                "sample_id": task.sample_id,
                "solution_id": task.solution_id,
                "output": output,
            },
            telemetry=cast(JsonObject, {
                "components": components,
                "total_tokens": (
                    96
                    if solution.kind in {"native_pipeline", "ocr_pipeline"}
                    else None
                ),
                "total_pages": 1,
                "latency_ms": latency,
                "estimated_cost_usd": 0.0,
                "attempts": 1,
                "errors": [],
            }),
        )

    async def parse(self, task: TaskRecord, raw: JsonValue) -> ParsedExecution:
        if not isinstance(raw, dict) or not isinstance(raw.get("output"), dict):
            return ParsedExecution(parsed={}, comparison={"fields": []})
        parsed = cast(JsonObject, raw["output"])
        project, sample = await self._service.sample_context(
            task.project_id,
            task.sample_id,
        )
        solution = self._catalog.get(task.solution_id)
        prompt_mode: PromptMode = (
            "ocr-normalizer"
            if solution.kind in {"native_pipeline", "ocr_pipeline", "ocr_text"}
            else "direct-multimodal"
        )
        _validate_pinned_contract(task, project, prompt_mode)
        if solution.kind == "ocr_text":
            from receipt_bench.projects.ocr_evaluation import evaluate_ocr_text

            try:
                reference = (
                    await self._service.repositories.artifacts.get_bytes(
                        _ocr_artifact_path(project.id, sample.id)
                    )
                ).decode("utf-8")
            except (NotFoundError, UnicodeDecodeError) as exc:
                raise ExecutionError(
                    "ocr_reference_unavailable",
                    "No OCR reference is available for this receipt.",
                    retryable=False,
                ) from exc
            text = parsed.get("text")
            if not isinstance(text, str):
                raise ExecutionError(
                    "unparseable_ocr_text",
                    "The OCR service returned no comparable text.",
                    retryable=False,
                )
            return ParsedExecution(
                parsed=parsed,
                comparison=evaluate_ocr_text(
                    project_id=project.id,
                    reference=reference,
                    hypothesis=text,
                ).model_dump(mode="json"),
            )
        gold = await self._service._artifact_object(sample.gold_path)
        if gold is None:
            comparisons = []
        elif project.kind == "built_in":
            from receipt_bench.projects.evaluation import evaluate_answer

            evaluation = evaluate_answer(
                project=self._service.registry.get(project.id),
                sample_id=task.sample_id,
                solution_id=task.solution_id,
                gold=gold,
                actual=parsed,
            )
            comparisons = [
                item.model_dump(mode="json") for item in evaluation.fields
            ]
        else:
            comparisons = [
                item.model_dump(mode="json")
                for item in _compare_objects(gold, parsed)
            ]
        return ParsedExecution(
            parsed=parsed,
            comparison=cast(
                JsonObject,
                {"fields": comparisons},
            ),
        )


class _FixedContextResolver:
    def __init__(self, context: ResolvedPairContext) -> None:
        self._context = context

    def resolve(self, task: TaskRecord) -> ResolvedPairContext:
        del task
        return self._context


class HostedPairExecutor:
    """Lazy live executor backed by parent provider/configuration modules."""

    def __init__(
        self,
        service: ProductService,
        catalog: SolutionCatalog,
        *,
        adapter_loader: AdapterLoader,
    ) -> None:
        self._service = service
        self._catalog = catalog
        self._adapter_loader = adapter_loader
        self._adapters: dict[str, ProjectAdapter] | None = None
        self._adapter_lock = asyncio.Lock()
        service.set_project_analyzer_cleanup(self._delete_project_analyzer)
        from receipt_bench.evaluation.pricing import (
            load_pricing_snapshot,
            validate_pricing_coverage,
        )

        self._pricing = load_pricing_snapshot(
            service.settings.repository_root / "configs" / "pricing.yaml"
        )
        solutions = catalog.list()
        required_rate_keys = {solution.id for solution in solutions}
        required_rate_keys.update(
            normalizer
            for solution in solutions
            if (normalizer := solution.normalizer) is not None
        )
        required_rate_keys.update(
            model
            for solution in solutions
            for model in solution.internal_models
        )
        validate_pricing_coverage(self._pricing, required_rate_keys)

    async def _delete_project_analyzer(self, analyzer_id: str) -> None:
        from receipt_bench.providers.project_adapters import (
            ContentUnderstandingCustomProjectAdapter,
        )

        adapters = await self._load_adapters()
        adapter = adapters.get("content-understanding-custom")
        if not isinstance(adapter, ContentUnderstandingCustomProjectAdapter):
            raise RuntimeError(
                "Content Understanding project analyzer adapter is unavailable"
            )
        await asyncio.to_thread(adapter.delete_generated_analyzer, analyzer_id)

    async def _load_adapters(self) -> dict[str, ProjectAdapter]:
        if self._adapters is not None:
            return self._adapters
        async with self._adapter_lock:
            if self._adapters is None:
                self._adapters = await asyncio.to_thread(self._adapter_loader)
        return self._adapters

    async def _resolved_context(self, task: TaskRecord) -> ResolvedPairContext:
        from receipt_bench.providers.project_adapters import (
            ProjectExecutionContext,
            RuntimePromptExample,
        )
        from receipt_bench.providers.project_executor import ResolvedPairContext

        context = await self._service.context(task.project_id, require_available=True)
        project = (
            self._service.registry.get(context.id)
            if context.kind == "built_in"
            else _custom_runtime_project(context, self._catalog)
        )
        solution = self._catalog.get(task.solution_id)
        prompt_mode: PromptMode = (
            "ocr-normalizer"
            if solution.kind in {"native_pipeline", "ocr_pipeline", "ocr_text"}
            else "direct-multimodal"
        )
        prompt = project.prompt(prompt_mode)
        _validate_pinned_contract(task, context, prompt_mode)
        compatibility = project.solution_compatibility(task.solution_id)
        examples: list[RuntimePromptExample] = []
        for sample_id in prompt.example_sample_ids:
            _, sample = await self._service.sample_context(project.project_id, sample_id)
            gold = await self._service._artifact_object(sample.gold_path)
            if gold is None:
                raise ExecutionError(
                    "prompt_example_unavailable",
                    "A configured prompt example is unavailable.",
                    retryable=False,
                )
            if prompt_mode == "direct-multimodal":
                try:
                    image = await self._service.repositories.artifacts.get_bytes(
                        sample.image_path
                    )
                except NotFoundError as exc:
                    raise ExecutionError(
                        "prompt_example_unavailable",
                        "A configured prompt example is unavailable.",
                        retryable=False,
                    ) from exc
                examples.append(
                    RuntimePromptExample(
                        sample_id=sample.id,
                        answer=gold,
                        image_bytes=image,
                        media_type=sample.media_type,
                    )
                )
            else:
                try:
                    ocr_bytes = await self._service.repositories.artifacts.get_bytes(
                        _ocr_artifact_path(project.project_id, sample.id)
                    )
                    ocr_text = ocr_bytes.decode("utf-8")
                except (NotFoundError, UnicodeDecodeError) as exc:
                    raise ExecutionError(
                        "prompt_example_unavailable",
                        "A configured prompt example is unavailable.",
                        retryable=False,
                    ) from exc
                examples.append(
                    RuntimePromptExample(
                        sample_id=sample.id,
                        answer=gold,
                        ocr_text=ocr_text,
                    )
                )
        ocr_reference: str | None = None
        if solution.kind == "ocr_text":
            try:
                ocr_reference = (
                    await self._service.repositories.artifacts.get_bytes(
                        _ocr_artifact_path(project.project_id, task.sample_id)
                    )
                ).decode("utf-8")
            except (NotFoundError, UnicodeDecodeError) as exc:
                raise ExecutionError(
                    "ocr_reference_unavailable",
                    "No OCR reference is available for this receipt.",
                    retryable=False,
                ) from exc
        return ResolvedPairContext(
            execution=ProjectExecutionContext(
                project=project,
                compatibility=compatibility,
                prompt=prompt,
                examples=tuple(examples),
            ),
            gold=await self._service._artifact_object(
                next(
                    sample.gold_path
                    for sample in context.samples
                    if sample.id == task.sample_id
                )
            ),
            ocr_reference=ocr_reference,
        )

    async def _executor(self, task: TaskRecord) -> ProjectPairExecutor:
        from receipt_bench.providers.project_executor import ProjectPairExecutor

        adapters = await self._load_adapters()
        if task.solution_id not in adapters:
            raise ExecutionError(
                "adapter_not_configured",
                "No provider adapter is configured for this solution.",
                retryable=False,
            )
        resolved = await self._resolved_context(task)
        return ProjectPairExecutor(
            adapters=adapters,
            context_resolver=_FixedContextResolver(resolved),
            pricing=self._pricing,
        )

    async def execute(
        self,
        task: TaskRecord,
        image_bytes: bytes,
        media_type: str,
    ) -> RawExecution:
        try:
            executor = await self._executor(task)
            return await executor.execute(task, image_bytes, media_type)
        except ExecutionError:
            raise
        except Exception as exc:
            raise ExecutionError(
                "hosted_runtime_unavailable",
                "The hosted provider runtime is unavailable.",
                retryable=True,
            ) from exc

    async def parse(self, task: TaskRecord, raw: JsonValue) -> ParsedExecution:
        try:
            executor = await self._executor(task)
            return await executor.parse(task, raw)
        except ExecutionError:
            raise
        except Exception as exc:
            raise ExecutionError(
                "hosted_runtime_unavailable",
                "The hosted provider runtime is unavailable.",
                retryable=True,
            ) from exc


def default_hosted_adapter_loader(repository_root: Path) -> AdapterLoader:
    """Return a lazy provider builder; no Azure/provider SDK imports occur here."""

    def load() -> dict[str, ProjectAdapter]:
        from receipt_bench.config import (
            BenchmarkConfig,
            load_hosted_azure_settings,
        )
        from receipt_bench.providers.registry import build_project_adapters

        configuration = BenchmarkConfig.load(repository_root / "configs" / "solutions.yaml")
        settings = load_hosted_azure_settings()
        return build_project_adapters(configuration, settings)

    return load


def _ocr_artifact_path(project_id: str, sample_id: str) -> str:
    return sample_ocr_text(project_id, sample_id)


def _custom_runtime_project(
    context: ProjectContext,
    catalog: SolutionCatalog,
) -> Project:
    from receipt_bench.projects.models import (
        ArtifactReference,
        DatasetSource,
        Project,
        ProjectSchema,
        PromptVersion,
        Sample,
        SolutionCompatibility,
    )

    schema = ProjectSchema(
        schema_id=context.schema_id,
        version=context.schema_version,
        content_hash=context.schema_hash,
        source=ArtifactReference(
            kind="schema",
            path=f"projects/{context.id}/schema.json",
            media_type="application/schema+json",
        ),
        document=context.schema,
    )

    def prompt(mode: PromptMode) -> PromptVersion:
        return PromptVersion(
            prompt_id=f"{context.prompt_id}-{mode}",
            project_id=context.id,
            schema_id=context.schema_id,
            mode=mode,
            version=context.prompt_version,
            system_instruction=context.prompt,
            user_template=_CUSTOM_PROMPT_TEMPLATES[mode],
            example_sample_ids=(),
            content_hash=_custom_prompt_contract_hash(
                project_id=context.id,
                schema_id=context.schema_id,
                prompt_id=context.prompt_id,
                mode=mode,
                version=context.prompt_version,
                instructions=context.prompt,
            ),
        )

    prompts = (
        prompt("direct-multimodal"),
        prompt("ocr-normalizer"),
    )
    samples = tuple(
        Sample(
            project_id=context.id,
            sample_id=sample.id,
            source_id=sample.id,
            source_index=sample.source_index if sample.source_index is not None else sample.ordinal,
            ordinal=sample.ordinal,
            split="user-supplied",
            language="und",
            image=ArtifactReference(
                kind="image",
                path=sample.image_path,
                media_type=sample.media_type,
            ),
            gold=ArtifactReference(
                kind="gold",
                path=sample.gold_path
                or f"projects/{context.id}/samples/{sample.id}/gold.json",
                media_type="application/json",
            ),
            scoring_role="few-shot" if sample.prompt_example else "scored",
            few_shot_reason=(
                "Selected by the project owner." if sample.prompt_example else None
            ),
        )
        for sample in context.samples
    )
    compatibility = tuple(
        SolutionCompatibility(
            solution_id=solution_id,
            solution_kind=(
                "direct-multimodal"
                if catalog.get(solution_id).kind == "direct"
                else "ocr-normalizer"
                if catalog.get(solution_id).kind in {
                    "native_pipeline",
                    "ocr_pipeline",
                }
                else "ocr-text"
                if catalog.get(solution_id).kind == "ocr_text"
                else "native-custom"
                if solution_id == "content-understanding-custom"
                else "native-prebuilt"
            ),
            level=(
                "partial-reference"
                if declaration.level == "partial_reference"
                else declaration.level
            ),
            comparable_quality=declaration.comparable_quality,
            reason=declaration.reason,
            deterministic_mapping_id=declaration.deterministic_mapping_id,
        )
        for solution_id, declaration in context.compatibility.items()
    )
    return Project(
        project_id=context.id,
        name=context.name,
        description=context.description,
        built_in=False,
        source=DatasetSource(
            dataset_id=context.id,
            revision=context.schema_hash,
            split="user-supplied",
            source_url="urn:receipt-bench:custom",
            license="Owner managed",
            attribution="Uploaded by project owner",
            redistribution="gated-no-redistribution",
        ),
        normalization_profile=cast(Any, context.normalization_profile),
        project_schema=schema,
        prompts=prompts,
        samples=samples,
        compatibility=compatibility,
    )


__all__ = [
    "AdapterLoader",
    "HostedPairExecutor",
    "MockPairExecutor",
    "default_hosted_adapter_loader",
]
