from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import re
import secrets
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal, cast

import jsonschema  # type: ignore[import-untyped]
from pydantic import ValidationError

from receipt_bench.evaluation.aggregate import LatencyStats
from receipt_bench.projects.models import Project, canonical_sha256
from receipt_bench.projects.ocr_evaluation import evaluate_ocr_text
from receipt_bench.projects.registry import ProjectRegistry
from receipt_bench.storage.models import (
    ArtifactInfo,
    ProjectRecord,
    RunRecord,
    TaskRecord,
    Versioned,
    datetime_text,
    utc_now,
)
from receipt_bench.storage.paths import (
    project_prompt,
    project_record,
    project_sample_index,
    project_schema,
    run_aggregate,
    run_manifest,
    run_tasks,
    sample_gold,
    sample_image,
    sample_ocr_text,
    sample_upload_gold,
    sample_upload_image,
)
from receipt_bench.storage.protocols import (
    AlreadyExistsError,
    ConcurrencyError,
    ImmutableArtifactError,
    NotFoundError,
)
from receipt_bench.storage.validation import (
    JsonObject,
    JsonValue,
    StrictJsonError,
    content_sha256,
    pair_id,
    require_json_object,
    strict_json_bytes,
    strict_json_loads,
    validate_id,
)
from receipt_bench.web.catalog import SolutionCatalog, SolutionDefinition
from receipt_bench.web.dataset_package import LabeledDatasetPackage
from receipt_bench.web.models import (
    AggregateMeasure,
    ArtifactView,
    AttributionNotice,
    Compatibility,
    CustomProjectCreateRequest,
    ErrorView,
    EvidenceRef,
    FieldAggregateView,
    FieldComparisonView,
    LeakageNotice,
    OcrTextMetricsView,
    PipelineComponentView,
    PipelineTelemetryView,
    ProjectAggregateView,
    ProjectCompatibilityView,
    ProjectView,
    PromptView,
    ReceiptLine,
    ReceiptSampleView,
    ReceiptVisual,
    RightsNotice,
    RunAccepted,
    RunCreateRequest,
    RunHistoryView,
    RunManifestView,
    RunResultView,
    RunSnapshotView,
    RunTaskView,
    SchemaView,
    SolutionAggregateView,
    SolutionView,
)
from receipt_bench.web.repositories import RepositoryBundle, TaskDispatcher
from receipt_bench.web.security import validate_image
from receipt_bench.web.settings import WebSettings
from receipt_bench.worker import FinalizationOutput

_BUILTIN_CREATED_AT = datetime(2026, 8, 29, tzinfo=UTC)
_TERMINAL_TASKS = frozenset({"succeeded", "invalid", "failed"})
_CUSTOM_PROMPT_TEMPLATES = {
    "direct-multimodal": "Extract the receipt into this JSON Schema:\n{schema_json}",
    "ocr-normalizer": "Normalize this OCR text:\n{ocr_text}\nJSON Schema:\n{schema_json}",
}
LOGGER = logging.getLogger(__name__)
ProjectAnalyzerCleanup = Callable[[str], Awaitable[None]]


class ProductError(RuntimeError):
    def __init__(self, status_code: int, detail: str, *, code: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code


@dataclass(frozen=True)
class CompatibilityContext:
    level: Compatibility
    comparable_quality: bool
    reason: str
    deterministic_mapping_id: str | None = None


@dataclass
class _FieldAggregateAccumulator:
    solution_id: str
    path: str
    label: str
    matched: int = 0
    wrong: int = 0
    partial: int = 0
    missing: int = 0
    extra: int = 0
    evidence: dict[str, EvidenceRef] = field(default_factory=dict)

    def record(
        self,
        *,
        state: Literal["matched", "partial", "wrong", "missing", "extra"],
        task: TaskRecord,
    ) -> None:
        if state == "matched":
            self.matched += 1
        elif state == "wrong":
            self.wrong += 1
        elif state == "partial":
            self.partial += 1
        elif state == "missing":
            self.missing += 1
        else:
            self.extra += 1
        self.evidence[task.pair_id] = _task_evidence(task)

    def view(self) -> FieldAggregateView:
        labeled = self.matched + self.wrong + self.partial + self.missing
        denominator = (
            2 * self.matched
            + 2 * (self.wrong + self.partial)
            + self.missing
            + self.extra
        )
        evidence = list(self.evidence.values())
        return FieldAggregateView(
            solution_id=self.solution_id,
            path=self.path,
            label=self.label,
            matched=self.matched,
            wrong=self.wrong,
            partial=self.partial,
            missing=self.missing,
            extra=self.extra,
            coverage=AggregateMeasure(
                value=(
                    (self.matched + self.wrong + self.partial) / labeled
                    if labeled
                    else None
                ),
                unit="ratio",
                evidence=evidence,
            ),
            exact_match_rate=AggregateMeasure(
                value=self.matched / labeled if labeled else None,
                unit="ratio",
                evidence=evidence,
            ),
            normalized_field_f1=AggregateMeasure(
                value=2 * self.matched / denominator if denominator else None,
                unit="ratio",
                evidence=evidence,
            ),
        )


@dataclass(frozen=True)
class SampleContext:
    id: str
    project_id: str
    title: str
    filename: str
    source_index: int | None
    ordinal: int
    prompt_example: bool
    scoring_role: Literal["scored", "few_shot", "excluded"]
    exclusion_reason: str | None
    image_path: str
    gold_path: str | None
    media_type: str


@dataclass(frozen=True)
class ProjectContext:
    id: str
    name: str
    short_name: str
    kind: Literal["built_in", "custom"]
    description: str
    schema_id: str
    schema_version: str
    schema_hash: str
    schema: JsonObject
    prompt_id: str
    prompt_version: str
    prompt_hash: str
    prompt: str
    prompt_example_ids: tuple[str, ...]
    prompt_versions: dict[str, str]
    prompt_hashes: dict[str, str]
    samples: tuple[SampleContext, ...]
    compatibility: dict[str, CompatibilityContext]
    rights: RightsNotice
    leakage: LeakageNotice
    attribution: AttributionNotice
    created_at: datetime
    available: bool
    availability_reason: str | None
    normalization_profile: str
    selected_solution_ids: tuple[str, ...]
    dataset_state: Literal["draft", "frozen"]
    dataset_revision_hash: str | None
    import_mode: Literal["built_in", "manual", "labeled_package"]
    has_ocr_references: bool


def _compatibility(level: str) -> Compatibility:
    if level == "partial-reference":
        return "partial_reference"
    if level in {"full", "unsupported"}:
        return cast(Compatibility, level)
    raise ValueError(f"Unsupported compatibility level: {level}")


def _solution_prompt_mode(
    solution: SolutionDefinition,
) -> Literal["direct-multimodal", "ocr-normalizer"]:
    return (
        "ocr-normalizer"
        if solution.kind in {"native_pipeline", "ocr_pipeline", "ocr_text"}
        else "direct-multimodal"
    )


def _custom_prompt_contract_hash(
    *,
    project_id: str,
    schema_id: str,
    prompt_id: str,
    mode: Literal["direct-multimodal", "ocr-normalizer"],
    version: str,
    instructions: str,
) -> str:
    return canonical_sha256(
        {
            "prompt_id": f"{prompt_id}-{mode}",
            "project_id": project_id,
            "schema_id": schema_id,
            "mode": mode,
            "version": version,
            "system_instruction": instructions,
            "user_template": _CUSTOM_PROMPT_TEMPLATES[mode],
            "example_sample_ids": [],
        }
    )


def _metadata_object(value: JsonValue, field: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"Stored project metadata field {field!r} must be an object")
    return value


def _metadata_string(value: JsonValue, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Stored project metadata field {field!r} must be a string")
    return value


def _metadata_strings(value: JsonValue, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Stored project metadata field {field!r} must be a string array")
    return tuple(cast(list[str], value))


def _safe_short_name(name: str) -> str:
    compact = " ".join(name.split()).upper()
    return compact[:12] or "CUSTOM"


def _slug(value: str) -> str:
    result = []
    last_dash = False
    for character in value.casefold():
        if "a" <= character <= "z" or "0" <= character <= "9":
            result.append(character)
            last_dash = False
        elif result and not last_dash:
            result.append("-")
            last_dash = True
    return "".join(result).strip("-")[:80] or "custom-project"


def _custom_project_fingerprint(
    request: CustomProjectCreateRequest,
    *,
    identity_salt: JsonValue | None = None,
) -> str:
    identity: JsonObject = {
        "name": request.name,
        "description": request.description,
        "schema": request.json_schema,
        "prompt": request.prompt,
        "selected_solution_ids": cast(JsonValue, request.selected_solution_ids),
    }
    if identity_salt is not None:
        identity["identity_salt"] = identity_salt
    return content_sha256(strict_json_bytes(identity))


def _schema_stats(value: JsonValue) -> tuple[int, int]:
    maximum = 0
    nodes = 0
    stack = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        maximum = max(maximum, depth)
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
    return maximum, nodes


def _reject_remote_schema_references(value: JsonValue) -> None:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                if key in {"$ref", "$dynamicRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise ProductError(
                        422,
                        "JSON Schema references must be local fragment references",
                        code="schema_reference_forbidden",
                    )
                stack.append(child)
        elif isinstance(current, list):
            stack.extend(current)


def _reject_unsafe_or_unsupported_schema_keywords(value: JsonValue) -> None:
    rejected = {
        "$dynamicRef",
        "allOf",
        "contains",
        "dependentRequired",
        "dependentSchemas",
        "if",
        "maxContains",
        "maxProperties",
        "minContains",
        "minProperties",
        "not",
        "pattern",
        "patternProperties",
        "prefixItems",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
        "uniqueItems",
        "else",
    }
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            unsupported = sorted(rejected.intersection(current))
            if unsupported:
                raise ProductError(
                    422,
                    "JSON Schema keyword is unsafe or unsupported by configured "
                    f"structured-output models: {unsupported[0]}",
                    code="schema_not_supported",
                )
            additional = current.get("additionalProperties")
            schema_type = current.get("type")
            describes_object = schema_type == "object" or (
                isinstance(schema_type, list) and "object" in schema_type
            )
            if describes_object and additional is not False:
                raise ProductError(
                    422,
                    "Every object schema must set additionalProperties to false "
                    "for configured structured-output models",
                    code="schema_not_supported",
                )
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _provider_schema_limits(schema: JsonObject) -> tuple[int, int, int, int]:
    properties = 0
    enum_values = 0
    named_characters = 0

    def count(current: JsonValue) -> None:
        nonlocal properties, enum_values, named_characters
        if isinstance(current, dict):
            raw_properties = current.get("properties")
            if isinstance(raw_properties, dict):
                properties += len(raw_properties)
                named_characters += sum(len(name) for name in raw_properties)
            definitions = current.get("$defs")
            if isinstance(definitions, dict):
                named_characters += sum(len(name) for name in definitions)
            enum = current.get("enum")
            if isinstance(enum, list):
                enum_values += len(enum)
                named_characters += sum(
                    len(item) for item in enum if isinstance(item, str)
                )
            stack = list(current.values())
        elif isinstance(current, list):
            stack = list(current)
        else:
            return
        for child in stack:
            count(child)

    def resolve(reference: str) -> JsonObject:
        if not reference.startswith("#/"):
            raise ProductError(
                422,
                "JSON Schema references must be local fragment references",
                code="schema_reference_forbidden",
            )
        current: JsonValue = schema
        for raw_segment in reference[2:].split("/"):
            segment = raw_segment.replace("~1", "/").replace("~0", "~")
            if not isinstance(current, dict) or segment not in current:
                raise ProductError(
                    422,
                    "JSON Schema reference does not resolve",
                    code="invalid_schema",
                )
            current = current[segment]
        if not isinstance(current, dict):
            raise ProductError(
                422,
                "JSON Schema reference must resolve to an object schema",
                code="schema_not_supported",
            )
        return current

    reference_depths: dict[str, int] = {}

    def structural_height(
        current: JsonObject,
        references: frozenset[str],
    ) -> int:
        maximum = 1
        reference = current.get("$ref")
        if isinstance(reference, str):
            if reference in references:
                raise ProductError(
                    422,
                    "Recursive JSON Schema references are unsupported",
                    code="schema_not_supported",
                )
            cached = reference_depths.get(reference)
            if cached is None:
                cached = structural_height(
                    resolve(reference),
                    references | {reference},
                )
                reference_depths[reference] = cached
            maximum = max(maximum, cached)
        raw_properties = current.get("properties")
        if isinstance(raw_properties, dict):
            for child in raw_properties.values():
                if isinstance(child, dict):
                    maximum = max(
                        maximum,
                        1 + structural_height(child, references),
                    )
        items = current.get("items")
        if isinstance(items, dict):
            maximum = max(
                maximum,
                1 + structural_height(items, references),
            )
        for keyword in ("anyOf", "oneOf"):
            alternatives = current.get(keyword)
            if isinstance(alternatives, list):
                for child in alternatives:
                    if isinstance(child, dict):
                        maximum = max(
                            maximum,
                            structural_height(child, references),
                        )
        if maximum > 5:
            return maximum
        return maximum

    count(schema)
    return (
        properties,
        structural_height(schema, frozenset()),
        enum_values,
        named_characters,
    )


def validate_custom_schema(schema: JsonObject, settings: WebSettings) -> None:
    try:
        encoded = strict_json_bytes(schema)
    except StrictJsonError as exc:
        raise ProductError(
            422,
            "JSON Schema must contain only finite strict JSON values",
            code="invalid_schema",
        ) from exc
    if len(encoded) > settings.schema_max_bytes:
        raise ProductError(
            422,
            f"JSON Schema exceeds the {settings.schema_max_bytes}-byte limit",
            code="schema_too_large",
        )
    if not encoded:
        raise ProductError(422, "JSON Schema cannot be empty", code="invalid_schema")
    depth, nodes = _schema_stats(schema)
    if depth > settings.schema_max_depth:
        raise ProductError(
            422,
            f"JSON Schema exceeds the maximum depth of {settings.schema_max_depth}",
            code="schema_too_deep",
        )
    if nodes > settings.schema_max_nodes:
        raise ProductError(
            422,
            f"JSON Schema exceeds the maximum node count of {settings.schema_max_nodes}",
            code="schema_too_complex",
        )
    if schema.get("type") != "object":
        raise ProductError(
            422,
            "Custom project JSON Schema must describe an object root",
            code="invalid_schema",
        )
    _reject_remote_schema_references(schema)
    _reject_unsafe_or_unsupported_schema_keywords(schema)
    try:
        validator_class = jsonschema.validators.validator_for(schema)
        validator_class.check_schema(schema)
    except jsonschema.exceptions.SchemaError as exc:
        raise ProductError(422, "Invalid JSON Schema", code="invalid_schema") from exc
    try:
        from receipt_bench.prompting import openai_compatible_schema

        converted = openai_compatible_schema(schema)
        validator_class.check_schema(converted)
    except (TypeError, jsonschema.exceptions.SchemaError) as exc:
        raise ProductError(
            422,
            "JSON Schema uses features unsupported by configured structured-output models",
            code="schema_not_supported",
        ) from exc
    properties, provider_depth, enum_values, named_characters = (
        _provider_schema_limits(converted)
    )
    if properties > 100:
        raise ProductError(
            422,
            "JSON Schema exceeds the structured-output limit of 100 properties",
            code="schema_too_complex",
        )
    if provider_depth > 5:
        raise ProductError(
            422,
            "JSON Schema exceeds the structured-output nesting limit of five levels",
            code="schema_too_deep",
        )
    if enum_values > 500 or named_characters > 15_000:
        raise ProductError(
            422,
            "JSON Schema exceeds configured structured-output enum or name limits",
            code="schema_too_complex",
        )


def validate_gold(schema: JsonObject, gold: JsonObject) -> None:
    try:
        validator_class = jsonschema.validators.validator_for(schema)
        validator = validator_class(
            schema,
            format_checker=jsonschema.FormatChecker(),
        )
        first_error = next(iter(validator.iter_errors(gold)), None)
    except (
        jsonschema.exceptions.SchemaError,
        jsonschema.exceptions.RefResolutionError,
        RecursionError,
    ) as exc:
        raise ProductError(
            422,
            "Project JSON Schema cannot validate gold",
            code="invalid_schema",
        ) from exc
    if first_error is not None:
        path = "$" + "".join(
            f"[{item}]" if isinstance(item, int) else f".{item}"
            for item in first_error.absolute_path
        )
        raise ProductError(
            422,
            f"Gold JSON does not match the project schema at {path}",
            code="invalid_gold",
        )


class ProductService:
    def __init__(
        self,
        *,
        repositories: RepositoryBundle,
        registry: ProjectRegistry,
        catalog: SolutionCatalog,
        settings: WebSettings,
        dispatcher: TaskDispatcher | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repositories = repositories
        self.registry = registry
        self.catalog = catalog
        self.settings = settings
        self._dispatcher = dispatcher
        self._clock = clock
        self._project_analyzer_cleanup: ProjectAnalyzerCleanup | None = None

    def set_dispatcher(self, dispatcher: TaskDispatcher) -> None:
        self._dispatcher = dispatcher

    def set_project_analyzer_cleanup(
        self,
        cleanup: ProjectAnalyzerCleanup,
    ) -> None:
        self._project_analyzer_cleanup = cleanup

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Web clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _builtin_context(self, project: Project) -> ProjectContext:
        prompt = next(
            (item for item in project.prompts if item.mode == "direct-multimodal"),
            project.prompts[0],
        )
        available = project.project_id != "sroie" or self.settings.sroie_available
        if project.project_id == "sroie":
            if self.settings.profile == "hosted" and available:
                rights = RightsNotice(
                    state="cleared",
                    label="Configured rights evidence",
                    detail="Public availability is enabled by configured rights-holder evidence.",
                )
            else:
                rights = RightsNotice(
                    state="restricted",
                    label="Local / private only",
                    detail=(
                        "Public redistribution is disabled until rights-holder authorization "
                        "evidence is configured."
                    ),
                )
            leakage = LeakageNotice(
                state="known_risk",
                label="Training leakage possible",
                detail=(
                    "Trainval-style public research data; it is not presented as an untouched "
                    "held-out challenge set."
                ),
            )
        else:
            rights = RightsNotice(
                state="cleared",
                label="Redistribution cleared",
                detail="Dataset content is displayed with the required CC BY 4.0 attribution.",
            )
            leakage = LeakageNotice(
                state="test_split",
                label="Pinned test split",
                detail="Fixed CORD v2 test indices; foundation-model exposure remains possible.",
            )
        compatibility = {
            item.solution_id: CompatibilityContext(
                level=_compatibility(item.level),
                comparable_quality=item.comparable_quality,
                reason=item.reason,
                deterministic_mapping_id=item.deterministic_mapping_id,
            )
            for item in project.compatibility
        }
        samples = tuple(
            SampleContext(
                id=item.sample_id,
                project_id=project.project_id,
                title=item.sample_id,
                filename=(
                    f"{item.sample_id}."
                    f"{'png' if item.image.media_type == 'image/png' else 'jpg'}"
                ),
                source_index=item.source_index,
                ordinal=item.ordinal,
                prompt_example=item.scoring_role == "few-shot",
                scoring_role=(
                    "few_shot" if item.scoring_role == "few-shot" else item.scoring_role
                ),
                exclusion_reason=item.exclusion_reason,
                image_path=sample_image(project.project_id, item.sample_id),
                gold_path=sample_gold(project.project_id, item.sample_id),
                media_type=item.image.media_type or "image/jpeg",
            )
            for item in project.samples
        )
        return ProjectContext(
            id=project.project_id,
            name=project.name,
            short_name="SROIE" if project.project_id == "sroie" else "CORD",
            kind="built_in",
            description=project.description,
            schema_id=project.project_schema.schema_id,
            schema_version=project.project_schema.version,
            schema_hash=project.project_schema.content_hash,
            schema=project.project_schema.document,
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            prompt_hash=prompt.content_hash,
            prompt=prompt.system_instruction,
            prompt_example_ids=prompt.example_sample_ids,
            prompt_versions={item.mode: item.version for item in project.prompts},
            prompt_hashes={item.mode: item.content_hash for item in project.prompts},
            samples=samples,
            compatibility=compatibility,
            rights=rights,
            leakage=leakage,
            attribution=AttributionNotice(
                label=project.source.dataset_id,
                citation=project.source.attribution,
                license=project.source.license,
                url=project.source.source_url,
            ),
            created_at=_BUILTIN_CREATED_AT,
            available=available,
            availability_reason=(
                None
                if available
                else "SROIE public access requires configured rights-holder evidence."
            ),
            normalization_profile=project.normalization_profile,
            selected_solution_ids=tuple(
                solution.id
                for solution in self.catalog.list()
                if (
                    declaration := compatibility.get(solution.id)
                ) is not None
                and declaration.level != "unsupported"
            ),
            dataset_state="frozen",
            dataset_revision_hash=canonical_sha256(
                {
                    "project_id": project.project_id,
                    "schema": project.project_schema.content_hash,
                    "samples": [
                        {
                            "id": sample.sample_id,
                            "image": sample.image.sha256,
                            "gold": sample.gold.sha256,
                            "ocr": (
                                sample.ocr_reference.sha256
                                if sample.ocr_reference is not None
                                else None
                            ),
                        }
                        for sample in project.samples
                    ],
                }
            ),
            import_mode="built_in",
            has_ocr_references=all(
                sample.ocr_reference is not None for sample in project.samples
            ),
        )

    async def _custom_sample_entries(
        self,
        record: ProjectRecord,
    ) -> list[JsonValue]:
        sample_index_path = record.metadata.get("sample_index_path")
        if sample_index_path is None:
            raw_samples = record.metadata.get("samples", [])
        elif isinstance(sample_index_path, str):
            document = require_json_object(
                await self.repositories.artifacts.get_json(sample_index_path),
                field="stored custom sample index",
            )
            raw_samples = document.get("samples")
        else:
            raise ValueError("Stored custom sample index path is invalid")
        if not isinstance(raw_samples, list):
            raise ValueError("Stored custom sample index is invalid")
        return raw_samples

    async def _custom_context(self, stored: Versioned[ProjectRecord]) -> ProjectContext:
        record = stored.value
        metadata = record.metadata
        schema_value, prompt_value = await asyncio.gather(
            self.repositories.artifacts.get_json(record.schema_artifact_path),
            self.repositories.artifacts.get_json(record.prompt_artifact_path),
        )
        schema = require_json_object(schema_value, field="stored project schema")
        prompt_document = require_json_object(prompt_value, field="stored project prompt")
        has_ocr_references = metadata.get("has_ocr_references") is True
        try:
            from receipt_bench.providers.content_understanding_schema import (
                content_understanding_analyzer_definition,
            )

            content_understanding_analyzer_definition(
                schema,
                schema_hash=canonical_sha256(schema),
            )
            content_understanding_supported = True
        except ValueError:
            content_understanding_supported = False
        compatibility = self._custom_compatibility(
            has_ocr_references=has_ocr_references,
            content_understanding_supported=content_understanding_supported,
        )
        raw_samples = await self._custom_sample_entries(record)
        samples: list[SampleContext] = []
        for ordinal, raw_sample in enumerate(raw_samples):
            sample = _metadata_object(raw_sample, f"samples[{ordinal}]")
            source_index = sample.get("source_index")
            prompt_example = sample.get("prompt_example", False)
            gold_path = sample.get("gold_path")
            if not (source_index is None or isinstance(source_index, int)):
                raise ValueError("Stored custom source_index is invalid")
            if not isinstance(prompt_example, bool):
                raise ValueError("Stored custom prompt_example is invalid")
            if not (gold_path is None or isinstance(gold_path, str)):
                raise ValueError("Stored custom gold_path is invalid")
            samples.append(
                SampleContext(
                    id=_metadata_string(sample.get("id"), "id"),
                    project_id=record.project_id,
                    title=_metadata_string(sample.get("title"), "title"),
                    filename=_metadata_string(sample.get("filename"), "filename"),
                    source_index=source_index,
                    ordinal=ordinal,
                    prompt_example=prompt_example,
                    scoring_role="few_shot" if prompt_example else "scored",
                    exclusion_reason=None,
                    image_path=_metadata_string(sample.get("image_path"), "image_path"),
                    gold_path=gold_path,
                    media_type=_metadata_string(sample.get("media_type"), "media_type"),
                )
            )
        prompt_hash = _metadata_string(metadata.get("prompt_hash"), "prompt_hash")
        raw_prompt_hashes = metadata.get("prompt_hashes")
        if isinstance(raw_prompt_hashes, dict):
            prompt_hashes = {
                mode: _metadata_string(raw_prompt_hashes.get(mode), f"prompt_hashes.{mode}")
                for mode in _CUSTOM_PROMPT_TEMPLATES
            }
        else:
            prompt_hashes = {
                mode: prompt_hash for mode in _CUSTOM_PROMPT_TEMPLATES
            }
        dataset_revision_value = metadata.get("dataset_revision_hash")
        return ProjectContext(
            id=record.project_id,
            name=record.name,
            short_name=_metadata_string(metadata.get("short_name"), "short_name"),
            kind="custom",
            description=record.description,
            schema_id=_metadata_string(metadata.get("schema_id"), "schema_id"),
            schema_version=_metadata_string(metadata.get("schema_version"), "schema_version"),
            schema_hash=_metadata_string(metadata.get("schema_hash"), "schema_hash"),
            schema=schema,
            prompt_id=_metadata_string(metadata.get("prompt_id"), "prompt_id"),
            prompt_version=record.prompt_version,
            prompt_hash=prompt_hashes["direct-multimodal"],
            prompt=_metadata_string(prompt_document.get("instructions"), "instructions"),
            prompt_example_ids=(),
            prompt_versions={
                "direct-multimodal": record.prompt_version,
                "ocr-normalizer": record.prompt_version,
            },
            prompt_hashes=prompt_hashes,
            samples=tuple(samples),
            compatibility=compatibility,
            rights=RightsNotice(
                state="owner_managed",
                label="Owner managed",
                detail="The project owner manages image rights and indefinite retention.",
            ),
            leakage=LeakageNotice(
                state="user_supplied",
                label="User-supplied data",
                detail="No benchmark leakage assertion is made for uploaded material.",
            ),
            attribution=AttributionNotice(
                label="Custom project",
                citation="Uploaded by project owner",
                license="Not specified",
                url=None,
            ),
            created_at=record.created_at,
            available=True,
            availability_reason=None,
            normalization_profile="exact-v1",
            selected_solution_ids=_metadata_strings(
                metadata.get("selected_solution_ids", []), "selected_solution_ids"
            ),
            dataset_state=(
                "frozen" if metadata.get("dataset_state") == "frozen" else "draft"
            ),
            dataset_revision_hash=(
                dataset_revision_value
                if isinstance(dataset_revision_value, str)
                else None
            ),
            import_mode=(
                "labeled_package"
                if metadata.get("import_mode") == "labeled_package"
                else "manual"
            ),
            has_ocr_references=has_ocr_references,
        )

    async def contexts(self) -> list[ProjectContext]:
        builtins = [self._builtin_context(project) for project in self.registry.list()]
        builtin_ids = {item.id for item in builtins}
        stored = await self.repositories.projects.list(self.settings.owner_scope)
        custom: list[ProjectContext] = []
        for item in stored:
            if item.value.project_id in builtin_ids:
                continue
            try:
                custom.append(await self._custom_context(item))
            except (NotFoundError, StrictJsonError, ValueError) as exc:
                LOGGER.error(
                    "Custom project is incomplete and was excluded project=%s type=%s",
                    item.value.project_id,
                    type(exc).__name__,
                )
        return sorted(
            [*builtins, *custom],
            key=lambda item: (
                0 if item.id == "sroie" else 1 if item.id == "cord-v2" else 2,
                item.created_at,
                item.id,
            ),
        )

    async def context(self, project_id: str, *, require_available: bool = False) -> ProjectContext:
        try:
            validate_id(project_id, field="project_id")
        except ValueError as exc:
            raise ProductError(404, "Project not found", code="project_not_found") from exc
        for project in self.registry.list():
            if project.project_id == project_id:
                context = self._builtin_context(project)
                break
        else:
            try:
                stored = await self.repositories.projects.get(
                    self.settings.owner_scope, project_id
                )
            except NotFoundError as exc:
                raise ProductError(404, "Project not found", code="project_not_found") from exc
            context = await self._custom_context(stored)
        if require_available and not context.available:
            raise ProductError(
                451,
                context.availability_reason or "Project is unavailable",
                code="rights_restricted",
            )
        return context

    def _project_view(self, project: ProjectContext) -> ProjectView:
        gold_count = sum(item.gold_path is not None for item in project.samples)
        examples = sum(item.scoring_role == "few_shot" for item in project.samples)
        if project.kind == "built_in":
            prompt_contracts = [
                PromptView(
                    id=prompt.prompt_id,
                    version=prompt.version,
                    hash=f"sha256:{prompt.content_hash}",
                    mode=prompt.mode,
                    instructions=prompt.system_instruction,
                    user_template=prompt.user_template,
                    example_ids=list(prompt.example_sample_ids),
                    normalizer_model=(
                        "gpt-5.6-luna"
                        if prompt.mode == "ocr-normalizer"
                        else None
                    ),
                )
                for prompt in self.registry.get(project.id).prompts
            ]
        else:
            prompt_contracts = [
                PromptView(
                    id=f"{project.prompt_id}-{mode}",
                    version=project.prompt_versions[mode],
                    hash=f"sha256:{project.prompt_hashes[mode]}",
                    mode=cast(
                        Literal["direct-multimodal", "ocr-normalizer"],
                        mode,
                    ),
                    instructions=project.prompt,
                    user_template=_CUSTOM_PROMPT_TEMPLATES[mode],
                    example_ids=[],
                    normalizer_model=(
                        "gpt-5.6-luna" if mode == "ocr-normalizer" else None
                    ),
                )
                for mode in ("direct-multimodal", "ocr-normalizer")
            ]
        return ProjectView(
            id=project.id,
            name=project.name,
            short_name=project.short_name,
            kind=project.kind,
            description=project.description,
            sample_count=len(project.samples),
            scored_sample_count=sum(
                item.scoring_role == "scored" and item.gold_path is not None
                for item in project.samples
            ),
            prompt_example_count=examples,
            gold_sample_count=gold_count,
            schema=SchemaView(
                id=project.schema_id,
                version=project.schema_version,
                hash=f"sha256:{project.schema_hash}",
                json_schema=project.schema,
                origin=(
                    "dataset_derived"
                    if project.kind == "built_in"
                    else "user_supplied"
                ),
                derivation=(
                    "Mirrors the native SROIE company, address, date, and total "
                    "gold-answer keys."
                    if project.id == "sroie"
                    else "Formalizes the pinned CORD gt_parse fields; singleton "
                    "menu objects are normalized to arrays."
                    if project.id == "cord-v2"
                    else "Provided by the custom project owner and shared by every "
                    "receipt in this project."
                ),
            ),
            prompt=PromptView(
                id=project.prompt_id,
                version=project.prompt_version,
                hash=f"sha256:{project.prompt_hash}",
                instructions=project.prompt,
                example_ids=list(project.prompt_example_ids),
            ),
            prompt_contracts=prompt_contracts,
            rights=project.rights,
            leakage=project.leakage,
            attribution=project.attribution,
            created_at=project.created_at,
            available=project.available,
            availability_reason=project.availability_reason,
            normalization_profile=project.normalization_profile,
            selected_solution_ids=list(project.selected_solution_ids),
            compatibility=[
                ProjectCompatibilityView(
                    solution_id=solution.id,
                    compatibility=declaration.level,
                    comparable_quality=declaration.comparable_quality,
                    reason=declaration.reason,
                    deterministic_mapping_id=declaration.deterministic_mapping_id,
                )
                for solution in self.catalog.list()
                if (declaration := project.compatibility.get(solution.id)) is not None
            ],
            dataset_state=project.dataset_state,
            dataset_revision_hash=project.dataset_revision_hash,
            import_mode=project.import_mode,
            has_ocr_references=project.has_ocr_references,
        )

    async def list_projects(self) -> list[ProjectView]:
        return [self._project_view(item) for item in await self.contexts()]

    async def get_project(self, project_id: str) -> ProjectView:
        return self._project_view(await self.context(project_id))

    async def _assert_custom_project_writable(
        self,
        project: ProjectContext,
        *,
        require_mutable_dataset: bool = False,
    ) -> Versioned[ProjectRecord] | None:
        if project.kind != "custom":
            return None
        try:
            stored = await self.repositories.projects.get(
                self.settings.owner_scope,
                project.id,
            )
        except NotFoundError as exc:
            raise ProductError(
                409,
                "Project was deleted while the request was starting",
                code="project_deleting",
            ) from exc
        if stored.value.metadata.get("deletion_pending") is True:
            raise ProductError(
                409,
                "Project deletion is in progress",
                code="project_deleting",
            )
        if (
            require_mutable_dataset
            and stored.value.metadata.get("dataset_state") == "frozen"
        ):
            raise ProductError(
                409,
                "This imported dataset revision is frozen",
                code="dataset_frozen",
            )
        return stored

    @staticmethod
    def _run_is_terminal(run: RunRecord, tasks: list[TaskRecord]) -> bool:
        return (
            run.status in {"completed", "failed"}
            and run.pending_tasks == 0
            and run.completion_event_emitted
            and run.finalizer_owner is None
            and (
                run.metadata.get("deletion_pending") is True
                or len(tasks) == run.total_tasks
            )
            and all(task.terminal for task in tasks)
        )

    async def _delete_terminal_run(self, run: RunRecord, tasks: list[TaskRecord]) -> None:
        current = await self.repositories.runs.get(run.project_id, run.run_id)
        if not self._run_is_terminal(current.value, tasks):
            raise ProductError(
                409,
                "Only terminal runs with all tasks terminal can be deleted",
                code="run_active",
            )
        if current.value.metadata.get("deletion_pending") is not True:
            marked = replace(
                current.value,
                metadata={**current.value.metadata, "deletion_pending": True},
                updated_at=self._now(),
            )
            try:
                current = await self.repositories.runs.replace(
                    marked,
                    expected_etag=current.etag,
                )
            except ConcurrencyError as exc:
                raise ProductError(
                    409,
                    "Run changed while deletion was starting",
                    code="concurrent_update",
                ) from exc
        await self.repositories.artifacts.delete_prefix(f"runs/{current.value.run_id}/")
        await self.repositories.tasks.delete_for_run(current.value.run_id)
        await self.repositories.runs.delete_auxiliary(
            current.value.project_id,
            current.value.run_id,
        )
        await self.repositories.runs.delete(
            current.value.project_id,
            current.value.run_id,
        )

    async def delete_run(self, run_id: str) -> None:
        try:
            validate_id(run_id, field="run_id")
            if await self.repositories.runs.resume_delete(run_id):
                return
            stored = await self.repositories.runs.find(run_id)
        except (NotFoundError, ValueError) as exc:
            raise ProductError(404, "Run not found", code="run_not_found") from exc
        tasks = [
            item.value
            for item in await self.repositories.tasks.list_for_run(stored.value.run_id)
        ]
        await self._delete_terminal_run(stored.value, tasks)

    async def delete_project(self, project_id: str) -> None:
        try:
            validate_id(project_id, field="project_id")
        except ValueError as exc:
            raise ProductError(404, "Project not found", code="project_not_found") from exc
        if any(project.project_id == project_id for project in self.registry.list()):
            raise ProductError(
                403,
                "Built-in projects cannot be deleted",
                code="protected_project",
            )
        if await self.repositories.projects.resume_delete(
            self.settings.owner_scope,
            project_id,
        ):
            return
        try:
            stored = await self.repositories.projects.get(
                self.settings.owner_scope,
                project_id,
            )
        except NotFoundError as exc:
            raise ProductError(404, "Project not found", code="project_not_found") from exc
        if stored.value.metadata.get("kind") != "custom":
            raise ProductError(
                403,
                "Only custom projects can be deleted",
                code="protected_project",
            )
        runs = await self.repositories.runs.list_all(project_id)
        run_tasks = {
            item.value.run_id: [
                task.value
                for task in await self.repositories.tasks.list_for_run(item.value.run_id)
            ]
            for item in runs
        }
        if any(
            not self._run_is_terminal(item.value, run_tasks[item.value.run_id])
            for item in runs
        ):
            raise ProductError(
                409,
                "Project contains an active or initializing run",
                code="project_has_active_runs",
            )
        if stored.value.metadata.get("deletion_pending") is not True:
            marked = replace(
                stored.value,
                metadata={**stored.value.metadata, "deletion_pending": True},
                updated_at=self._now(),
            )
            try:
                stored = await self.repositories.projects.replace(
                    marked,
                    expected_etag=stored.etag,
                )
            except ConcurrencyError as exc:
                raise ProductError(
                    409,
                    "Project changed while deletion was starting",
                    code="concurrent_update",
                ) from exc
        runs = await self.repositories.runs.list_all(project_id)
        run_tasks = {
            item.value.run_id: [
                task.value
                for task in await self.repositories.tasks.list_for_run(item.value.run_id)
            ]
            for item in runs
        }
        if any(
            not self._run_is_terminal(item.value, run_tasks[item.value.run_id])
            for item in runs
        ):
            raise ProductError(
                409,
                "Project acquired an active run while deletion was starting",
                code="project_has_active_runs",
            )
        for item in runs:
            await self._delete_terminal_run(
                item.value,
                run_tasks[item.value.run_id],
            )
        schema_hash = stored.value.metadata.get("schema_hash")
        selected_solutions = stored.value.metadata.get("selected_solution_ids")
        if (
            self._project_analyzer_cleanup is not None
            and stored.value.metadata.get("content_understanding_supported") is True
            and isinstance(schema_hash, str)
            and isinstance(selected_solutions, list)
            and "content-understanding-custom" in selected_solutions
        ):
            projects = await self.repositories.projects.list(
                self.settings.owner_scope
            )
            shared_schema = any(
                item.value.project_id != project_id
                and item.value.metadata.get("kind") == "custom"
                and item.value.metadata.get("schema_hash") == schema_hash
                and isinstance(
                    other_selected := item.value.metadata.get(
                        "selected_solution_ids"
                    ),
                    list,
                )
                and "content-understanding-custom" in other_selected
                and item.value.metadata.get("deletion_pending") is not True
                for item in projects
            )
            if not shared_schema:
                await self._project_analyzer_cleanup(
                    f"custom_{schema_hash[:24]}"
                )
        await self.repositories.artifacts.delete_prefix(f"projects/{project_id}/")
        await self.repositories.projects.delete(self.settings.owner_scope, project_id)

    async def list_solutions(self, project_id: str) -> list[SolutionView]:
        await self.context(project_id)
        projects = await self.contexts()
        declarations = {
            project.id: {
                solution_id: (item.level, item.comparable_quality)
                for solution_id, item in project.compatibility.items()
            }
            for project in projects
        }
        return self.catalog.view(compatibility=declarations)

    def _custom_compatibility(
        self,
        *,
        has_ocr_references: bool = False,
        content_understanding_supported: bool = True,
    ) -> dict[str, CompatibilityContext]:
        result: dict[str, CompatibilityContext] = {}
        for solution in self.catalog.list():
            if solution.kind in {"direct", "native_pipeline", "ocr_pipeline"}:
                result[solution.id] = CompatibilityContext(
                    level="full",
                    comparable_quality=True,
                    reason="Output is constrained by the uploaded project schema.",
                )
            elif solution.kind == "ocr_text":
                result[solution.id] = CompatibilityContext(
                    level="full" if has_ocr_references else "partial_reference",
                    comparable_quality=has_ocr_references,
                    reason=(
                        "Standalone OCR is scored against the imported full-page "
                        "transcription."
                        if has_ocr_references
                        else "No OCR transcript was imported; this standalone OCR "
                        "configuration still measures output, latency, and cost but "
                        "does not receive an OCR quality score."
                    ),
                )
            elif solution.id == "content-understanding-custom":
                result[solution.id] = CompatibilityContext(
                    level="full" if content_understanding_supported else "unsupported",
                    comparable_quality=content_understanding_supported,
                    reason=(
                        "A no-training Content Understanding analyzer is configured "
                        "from the uploaded schema when this configuration first runs."
                        if content_understanding_supported
                        else "The uploaded schema uses constructs that cannot configure "
                        "a no-training Content Understanding analyzer."
                    ),
                    deterministic_mapping_id=(
                        "content-understanding-dynamic-schema-v1"
                        if content_understanding_supported
                        else None
                    ),
                )
            else:
                result[solution.id] = CompatibilityContext(
                    level="partial_reference",
                    comparable_quality=False,
                    reason=(
                        "This prebuilt service returns its native receipt contract. "
                        "Without a trained model or a custom field mapper, its output "
                        "is retained for inspection, latency, and cost rather than "
                        "being forced into an arbitrary uploaded schema."
                    ),
                )
        return result

    async def create_project(
        self,
        request: CustomProjectCreateRequest,
        *,
        identity_salt: JsonValue | None = None,
        metadata_overrides: JsonObject | None = None,
    ) -> ProjectView:
        validate_custom_schema(request.json_schema, self.settings)
        from receipt_bench.providers.content_understanding_schema import (
            content_understanding_analyzer_definition,
        )

        try:
            content_understanding_analyzer_definition(
                request.json_schema,
                schema_hash=canonical_sha256(request.json_schema),
            )
            content_understanding_supported = True
        except ValueError as exc:
            content_understanding_supported = False
            if "content-understanding-custom" in request.selected_solution_ids:
                raise ProductError(
                    422,
                    f"Schema cannot configure Content Understanding: {exc}",
                    code="content_understanding_schema_not_supported",
                ) from exc
        has_ocr_references = (
            metadata_overrides is not None
            and metadata_overrides.get("has_ocr_references") is True
        )
        compatibility = self._custom_compatibility(
            has_ocr_references=has_ocr_references,
            content_understanding_supported=content_understanding_supported,
        )
        unknown = sorted(set(request.selected_solution_ids) - set(compatibility))
        unsupported = sorted(
            solution_id
            for solution_id in request.selected_solution_ids
            if compatibility.get(solution_id, CompatibilityContext(
                "unsupported", False, "Unsupported"
            )).level
            == "unsupported"
        )
        if unknown:
            raise ProductError(
                422,
                f"Unknown solution IDs: {', '.join(unknown)}",
                code="unknown_solution",
            )
        if unsupported:
            raise ProductError(
                422,
                f"Unsupported solution IDs: {', '.join(unsupported)}",
                code="unsupported_solution",
            )
        schema_hash = content_sha256(strict_json_bytes(request.json_schema))
        fingerprint = _custom_project_fingerprint(
            request,
            identity_salt=identity_salt,
        )
        project_id = validate_id(f"{_slug(request.name)}-{fingerprint[:10]}")
        schema_id = f"{project_id}-schema"
        prompt_id = f"{project_id}-prompt"
        prompt_hashes = {
            mode: _custom_prompt_contract_hash(
                project_id=project_id,
                schema_id=schema_id,
                prompt_id=prompt_id,
                mode=cast(
                    Literal["direct-multimodal", "ocr-normalizer"],
                    mode,
                ),
                version="1.0.0",
                instructions=request.prompt,
            )
            for mode in _CUSTOM_PROMPT_TEMPLATES
        }
        prompt_hash = prompt_hashes["direct-multimodal"]
        if any(item.project_id == project_id for item in self.registry.list()):
            raise ProductError(409, "Project ID is reserved", code="project_exists")
        await self.repositories.projects.resume_delete(
            self.settings.owner_scope,
            project_id,
        )
        schema_path = project_schema(project_id)
        prompt_path = project_prompt(project_id, "1.0.0")
        now = self._now()
        metadata: JsonObject = {
            "kind": "custom",
            "short_name": _safe_short_name(request.name),
            "schema_id": schema_id,
            "schema_version": "1.0.0",
            "schema_hash": schema_hash,
            "prompt_id": prompt_id,
            "prompt_hash": prompt_hash,
            "prompt_hashes": cast(JsonValue, prompt_hashes),
            "selected_solution_ids": cast(JsonValue, request.selected_solution_ids),
            "compatibility": {
                solution_id: {
                    "level": item.level,
                    "comparable_quality": item.comparable_quality,
                    "reason": item.reason,
                    "deterministic_mapping_id": item.deterministic_mapping_id,
                }
                for solution_id, item in compatibility.items()
            },
            "samples": [],
            "fingerprint": fingerprint,
            "dataset_state": "draft",
            "import_mode": "manual",
            "has_ocr_references": has_ocr_references,
            "content_understanding_supported": content_understanding_supported,
            **(metadata_overrides or {}),
        }
        try:
            existing_project = await self.repositories.projects.get(
                self.settings.owner_scope,
                project_id,
            )
        except NotFoundError:
            existing_project = None
        if (
            existing_project is not None
            and existing_project.value.metadata.get("fingerprint") != fingerprint
        ):
            raise ProductError(
                409,
                "Project already exists",
                code="project_exists",
            )
        if (
            existing_project is not None
            and existing_project.value.metadata.get("deletion_pending") is True
        ):
            raise ProductError(
                409,
                "Project deletion is in progress",
                code="project_deleting",
            )
        record = (
            existing_project.value
            if existing_project is not None
            else ProjectRecord(
                owner_scope=self.settings.owner_scope,
                project_id=project_id,
                name=request.name,
                description=request.description
                or "Owner-managed custom receipt extraction project.",
                schema_artifact_path=schema_path,
                prompt_artifact_path=prompt_path,
                prompt_version="1.0.0",
                created_at=now,
                updated_at=now,
                metadata=metadata,
            )
        )
        await self.repositories.artifacts.create_json(schema_path, request.json_schema)
        await self.repositories.artifacts.create_json(
            prompt_path,
            {
                "prompt_id": prompt_id,
                "version": "1.0.0",
                "instructions": request.prompt,
                "content_hash": prompt_hash,
                "content_hashes": cast(JsonValue, prompt_hashes),
            },
        )
        await self.repositories.artifacts.create_json(
            project_record(project_id),
            {
                "project_id": project_id,
                "name": record.name,
                "description": record.description,
                "created_at": datetime_text(record.created_at),
                "schema_artifact_path": schema_path,
                "prompt_artifact_path": prompt_path,
                "fingerprint": fingerprint,
            },
        )
        if existing_project is not None:
            stored = existing_project
        else:
            try:
                stored = await self.repositories.projects.create(record)
            except AlreadyExistsError:
                stored = await self.repositories.projects.get(
                    self.settings.owner_scope,
                    project_id,
                )
                if stored.value.metadata.get("fingerprint") != fingerprint:
                    raise ProductError(
                        409,
                        "Project already exists",
                        code="project_exists",
                    ) from None
        return self._project_view(await self._custom_context(stored))

    async def import_labeled_dataset(
        self,
        package: LabeledDatasetPackage,
    ) -> ProjectView:
        validate_custom_schema(package.schema, self.settings)
        media_types: dict[str, str] = {}
        for sample in package.samples:
            validate_gold(package.schema, sample.answer)
            media_types[sample.sample_id] = validate_image(
                sample.image,
                self.settings,
                None,
            )

        from receipt_bench.providers.content_understanding_schema import (
            content_understanding_analyzer_definition,
        )

        try:
            content_understanding_analyzer_definition(
                package.schema,
                schema_hash=canonical_sha256(package.schema),
            )
            content_understanding_supported = True
        except ValueError:
            content_understanding_supported = False
        compatibility = self._custom_compatibility(
            has_ocr_references=package.has_ocr_references,
            content_understanding_supported=content_understanding_supported,
        )
        selected_solution_ids = (
            list(package.manifest.selected_solution_ids)
            if package.manifest.selected_solution_ids
            else [
                solution.id
                for solution in self.catalog.list()
                if compatibility[solution.id].level != "unsupported"
            ]
        )
        request = CustomProjectCreateRequest(
            name=package.manifest.name,
            description=package.manifest.description,
            schema=package.schema,
            prompt=package.manifest.prompt,
            selected_solution_ids=selected_solution_ids,
        )
        identity_salt: JsonObject = {
            "import_mode": "labeled_package",
            "dataset_revision_hash": package.revision_sha256,
        }
        fingerprint = _custom_project_fingerprint(
            request,
            identity_salt=identity_salt,
        )
        project_id = validate_id(
            f"{_slug(request.name)}-{fingerprint[:10]}",
            field="project_id",
        )
        import_attempt_id = secrets.token_hex(16)
        try:
            existing = await self.repositories.projects.get(
                self.settings.owner_scope,
                project_id,
            )
        except NotFoundError:
            existing = None
        if existing is not None:
            metadata = existing.value.metadata
            if (
                metadata.get("dataset_state") == "frozen"
                and metadata.get("dataset_revision_hash") == package.revision_sha256
            ):
                return self._project_view(await self._custom_context(existing))
            raise ProductError(
                409,
                "This labelled dataset import is already in progress or conflicts "
                "with an existing project",
                code="dataset_import_conflict",
            )

        created: ProjectView | None = None
        try:
            candidate = await self.create_project(
                request,
                identity_salt=identity_salt,
                metadata_overrides={
                    "dataset_state": "draft",
                    "import_mode": "labeled_package",
                    "dataset_revision_hash": package.revision_sha256,
                    "archive_sha256": package.archive_sha256,
                    "has_ocr_references": package.has_ocr_references,
                    "import_attempt_id": import_attempt_id,
                    "content_understanding_supported": (
                        content_understanding_supported
                    ),
                },
            )
            claim = await self.repositories.projects.get(
                self.settings.owner_scope,
                candidate.id,
            )
            if claim.value.metadata.get("import_attempt_id") != import_attempt_id:
                if (
                    claim.value.metadata.get("dataset_state") == "frozen"
                    and claim.value.metadata.get("dataset_revision_hash")
                    == package.revision_sha256
                ):
                    return self._project_view(await self._custom_context(claim))
                raise ProductError(
                    409,
                    "This labelled dataset revision is already being imported",
                    code="dataset_import_conflict",
                )
            created = candidate
            for sample in package.samples:
                await self.upload_sample(
                    created.id,
                    filename=sample.filename,
                    media_type=media_types[sample.sample_id],
                    data=sample.image,
                    gold=sample.answer,
                    sample_id_override=sample.sample_id,
                    title_override=sample.title,
                    ocr_text=sample.ocr_text,
                )

            finalized: Versioned[ProjectRecord] | None = None
            for _ in range(16):
                current = await self.repositories.projects.get(
                    self.settings.owner_scope,
                    created.id,
                )
                metadata = {
                    **current.value.metadata,
                    "dataset_state": "frozen",
                    "dataset_revision_hash": package.revision_sha256,
                    "archive_sha256": package.archive_sha256,
                    "sample_count": len(package.samples),
                }
                metadata.pop("import_attempt_id", None)
                updated = replace(
                    current.value,
                    metadata=metadata,
                    updated_at=self._now(),
                )
                try:
                    finalized = await self.repositories.projects.replace(
                        updated,
                        expected_etag=current.etag,
                    )
                    break
                except ConcurrencyError:
                    continue
                except Exception:
                    readback = await self.repositories.projects.get(
                        self.settings.owner_scope,
                        created.id,
                    )
                    if (
                        readback.value.metadata.get("dataset_state") == "frozen"
                        and readback.value.metadata.get("dataset_revision_hash")
                        == package.revision_sha256
                    ):
                        finalized = readback
                        break
                    raise
            if finalized is None:
                raise ProductError(
                    409,
                    "Dataset finalization did not settle",
                    code="concurrent_update",
                )
            return self._project_view(await self._custom_context(finalized))
        except Exception as exc:
            if created is not None:
                try:
                    await self.delete_project(created.id)
                except Exception as rollback_exc:
                    raise ProductError(
                        500,
                        "Dataset import failed and its partial project could not be "
                        "removed",
                        code="dataset_import_rollback_failed",
                    ) from rollback_exc
            raise exc

    async def _artifact_object(self, path: str | None) -> JsonObject | None:
        if path is None:
            return None
        try:
            value = await self.repositories.artifacts.get_json(path)
        except NotFoundError:
            return None
        return value if isinstance(value, dict) else None

    async def sample_context(
        self,
        project_id: str,
        sample_id: str,
    ) -> tuple[ProjectContext, SampleContext]:
        project = await self.context(project_id, require_available=True)
        try:
            validate_id(sample_id, field="sample_id")
        except ValueError as exc:
            raise ProductError(404, "Sample not found", code="sample_not_found") from exc
        try:
            sample = next(item for item in project.samples if item.id == sample_id)
        except StopIteration as exc:
            raise ProductError(404, "Sample not found", code="sample_not_found") from exc
        return project, sample

    async def _sample_view(
        self,
        project: ProjectContext,
        sample: SampleContext,
        *,
        prior_run_state: Literal["complete", "partial", "failed", "never"] = "never",
    ) -> ReceiptSampleView:
        gold = await self._artifact_object(sample.gold_path)
        image_available = await self.repositories.artifacts.exists(sample.image_path)
        visual = _visual_from_gold(gold, sample.filename)
        title = _title_from_gold(gold, sample.title)
        return ReceiptSampleView(
            id=sample.id,
            project_id=project.id,
            title=title,
            filename=sample.filename,
            source_index=sample.source_index,
            prompt_example=sample.prompt_example,
            gold_availability="available" if gold is not None else "none",
            prior_run_state=prior_run_state,
            gold=gold,
            visual=visual,
            image_url=(
                f"/api/projects/{project.id}/samples/{sample.id}/image"
                if image_available
                else None
            ),
            scoring_role=sample.scoring_role,
            exclusion_reason=sample.exclusion_reason,
            image_available=image_available,
        )

    async def _prior_run_states(
        self,
        project: ProjectContext,
    ) -> dict[str, Literal["complete", "partial", "failed"]]:
        unresolved = {sample.id for sample in project.samples}
        states: dict[str, Literal["complete", "partial", "failed"]] = {}
        for stored in await self.repositories.runs.list_all(project.id):
            if not unresolved:
                break
            run = stored.value
            if run.metadata.get("dispatch_complete") is not True:
                continue
            indexed_samples = run.metadata.get("sample_ids")
            if (
                isinstance(indexed_samples, list)
                and all(isinstance(item, str) for item in indexed_samples)
                and unresolved.isdisjoint(indexed_samples)
            ):
                continue
            grouped: dict[str, list[TaskRecord]] = {}
            for task in (
                item.value
                for item in await self.repositories.tasks.list_for_run(run.run_id)
                if item.value.sample_id in unresolved
            ):
                grouped.setdefault(task.sample_id, []).append(task)
            for sample_id, tasks in grouped.items():
                if any(not task.terminal for task in tasks):
                    state: Literal["complete", "partial", "failed"] = "partial"
                elif all(task.status == "succeeded" for task in tasks):
                    state = "complete"
                elif any(task.status == "succeeded" for task in tasks):
                    state = "partial"
                else:
                    state = "failed"
                states[sample_id] = state
                unresolved.discard(sample_id)
        return states

    async def list_samples(self, project_id: str) -> list[ReceiptSampleView]:
        project = await self.context(project_id, require_available=True)
        prior_states = await self._prior_run_states(project)
        return list(
            await asyncio.gather(
                *(
                    self._sample_view(
                        project,
                        sample,
                        prior_run_state=prior_states.get(sample.id, "never"),
                    )
                    for sample in project.samples
                )
            )
        )

    async def upload_sample(
        self,
        project_id: str,
        *,
        filename: str,
        media_type: str,
        data: bytes,
        gold: JsonObject | None,
        sample_id_override: str | None = None,
        title_override: str | None = None,
        ocr_text: str | None = None,
    ) -> ReceiptSampleView:
        project = await self.context(project_id, require_available=True)
        if project.kind != "custom":
            raise ProductError(
                409,
                "Built-in projects are immutable",
                code="immutable_project",
            )
        initial = await self._assert_custom_project_writable(
            project,
            require_mutable_dataset=True,
        )
        if initial is None:
            raise RuntimeError("Custom project is missing its durable record")
        if gold is not None:
            validate_gold(project.schema, gold)
        gold_bytes = strict_json_bytes(gold) if gold is not None else b""
        ocr_bytes = ocr_text.encode("utf-8") if ocr_text is not None else b""
        fingerprint = hashlib.sha256(
            data + b"\0" + gold_bytes + b"\0" + ocr_bytes
        ).hexdigest()
        sample_id = (
            validate_id(sample_id_override, field="sample_id")
            if sample_id_override is not None
            else validate_id(f"sample-{fingerprint[:20]}", field="sample_id")
        )
        try:
            initial_samples = await self._custom_sample_entries(initial.value)
        except (NotFoundError, StrictJsonError, ValueError) as exc:
            raise ProductError(
                500,
                "Stored project sample index is invalid",
                code="storage_corrupt",
            ) from exc
        existing = next(
            (
                item
                for item in initial_samples
                if isinstance(item, dict) and item.get("id") == sample_id
            ),
            None,
        )
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                raise ProductError(409, "Sample ID collision", code="sample_exists")
            refreshed = await self._custom_context(initial)
            sample = next(item for item in refreshed.samples if item.id == sample_id)
            return await self._sample_view(refreshed, sample)
        if len(initial_samples) >= self.settings.max_samples_per_project:
            raise ProductError(
                409,
                "Project sample limit reached",
                code="sample_limit",
            )

        upload_id = secrets.token_hex(12)
        image_path = sample_upload_image(project.id, sample_id, upload_id)
        gold_path = (
            sample_upload_gold(project.id, sample_id, upload_id)
            if gold is not None
            else None
        )
        ocr_path = sample_ocr_text(project.id, sample_id) if ocr_text is not None else None
        sample_entry: JsonObject = {
            "id": sample_id,
            "title": (
                title_override.strip()[:200]
                if title_override is not None and title_override.strip()
                else filename.rsplit(".", maxsplit=1)[0] or sample_id
            ),
            "filename": filename,
            "source_index": None,
            "prompt_example": False,
            "image_path": image_path,
            "gold_path": gold_path,
            "ocr_path": ocr_path,
            "media_type": media_type,
            "fingerprint": fingerprint,
        }
        stored: Versioned[ProjectRecord] | None = None
        committed = False
        cleanup_safe = True
        pending_index_path: str | None = None
        try:
            await self.repositories.artifacts.create_bytes(
                image_path,
                data,
                content_type=media_type,
                metadata={"filename": filename},
            )
            if gold is not None and gold_path is not None:
                await self.repositories.artifacts.create_json(gold_path, gold)
            if ocr_text is not None and ocr_path is not None:
                await self.repositories.artifacts.create_bytes(
                    ocr_path,
                    ocr_bytes,
                    content_type="text/plain",
                )
            for _ in range(16):
                try:
                    current = await self.repositories.projects.get(
                        self.settings.owner_scope, project.id
                    )
                except NotFoundError as exc:
                    raise ProductError(
                        409,
                        "Project was deleted while the sample was uploading",
                        code="project_deleting",
                    ) from exc
                if current.value.metadata.get("deletion_pending") is True:
                    raise ProductError(
                        409,
                        "Project deletion is in progress",
                        code="project_deleting",
                    )
                if current.value.metadata.get("dataset_state") == "frozen":
                    raise ProductError(
                        409,
                        "This imported dataset revision is frozen",
                        code="dataset_frozen",
                    )
                try:
                    samples = await self._custom_sample_entries(current.value)
                except (NotFoundError, StrictJsonError, ValueError) as exc:
                    raise ProductError(
                        500,
                        "Stored project sample index is invalid",
                        code="storage_corrupt",
                    ) from exc
                existing = next(
                    (
                        item
                        for item in samples
                        if isinstance(item, dict) and item.get("id") == sample_id
                    ),
                    None,
                )
                if existing is not None:
                    if existing.get("fingerprint") != fingerprint:
                        raise ProductError(
                            409,
                            "Sample ID collision",
                            code="sample_exists",
                        )
                    stored = current
                    break
                if len(samples) >= self.settings.max_samples_per_project:
                    raise ProductError(
                        409,
                        "Project sample limit reached",
                        code="sample_limit",
                    )
                sample_index: JsonObject = {"samples": [*samples, sample_entry]}
                sample_index_digest = content_sha256(
                    strict_json_bytes(sample_index)
                )
                pending_index_path = project_sample_index(
                    project.id,
                    f"{sample_index_digest}-{secrets.token_hex(8)}",
                )
                await self.repositories.artifacts.create_json(
                    pending_index_path,
                    sample_index,
                )
                updated_metadata = dict(current.value.metadata)
                updated_metadata.pop("samples", None)
                updated_metadata["sample_index_path"] = pending_index_path
                updated_record = replace(
                    current.value,
                    updated_at=self._now(),
                    metadata=updated_metadata,
                )
                try:
                    stored = await self.repositories.projects.replace(
                        updated_record,
                        expected_etag=current.etag,
                    )
                    committed = True
                    break
                except ConcurrencyError:
                    await self.repositories.artifacts.delete(pending_index_path)
                    pending_index_path = None
                    continue
                except Exception:
                    try:
                        readback = await self.repositories.projects.get(
                            self.settings.owner_scope,
                            project.id,
                        )
                    except Exception:
                        cleanup_safe = False
                        raise
                    if (
                        readback.value.metadata.get("sample_index_path")
                        == pending_index_path
                    ):
                        stored = readback
                        committed = True
                        break
                    cleanup_safe = False
                    raise
        finally:
            if not committed and cleanup_safe:
                cleanup_paths = [
                    path
                    for path in (pending_index_path, ocr_path, gold_path, image_path)
                    if path is not None
                ]
                for path in cleanup_paths:
                    await self.repositories.artifacts.delete(path)
        if stored is None:
            raise ProductError(
                409,
                "Concurrent sample update did not settle",
                code="concurrent_update",
            )
        refreshed = await self._custom_context(stored)
        sample = next(item for item in refreshed.samples if item.id == sample_id)
        return await self._sample_view(refreshed, sample)

    async def image(self, project_id: str, sample_id: str) -> tuple[bytes, ArtifactInfo, str]:
        _, sample = await self.sample_context(project_id, sample_id)
        try:
            data, info = await asyncio.gather(
                self.repositories.artifacts.get_bytes(sample.image_path),
                self.repositories.artifacts.get_info(sample.image_path),
            )
        except NotFoundError as exc:
            raise ProductError(404, "Sample image is unavailable", code="image_not_found") from exc
        return data, info, sample.filename

    def _run_tasks(
        self,
        *,
        project: ProjectContext,
        run: RunRecord,
        selected_samples: list[SampleContext],
        selected_solutions: list[tuple[SolutionDefinition, CompatibilityContext]],
        execution_mode: Literal["mock", "live"],
    ) -> list[TaskRecord]:
        tasks: list[TaskRecord] = []
        for sample in selected_samples:
            for solution, declaration in selected_solutions:
                prompt_mode = _solution_prompt_mode(solution)
                tasks.append(
                    TaskRecord(
                        project_id=project.id,
                        run_id=run.run_id,
                        sample_id=sample.id,
                        solution_id=solution.id,
                        image_artifact_path=sample.image_path,
                        media_type=sample.media_type,
                        provider_keys=solution.provider_keys,
                        created_at=run.created_at,
                        updated_at=run.created_at,
                        metadata={
                            "sample_order": sample.ordinal,
                            "solution_order": solution.order,
                            "compatibility": declaration.level,
                            "comparable_quality": declaration.comparable_quality,
                            "schema_hash": project.schema_hash,
                            "prompt_mode": prompt_mode,
                            "prompt_version": project.prompt_versions[prompt_mode],
                            "prompt_hash": project.prompt_hashes[prompt_mode],
                            "execution_mode": execution_mode,
                        },
                    )
                )
        return tasks

    @staticmethod
    def _same_task_identity(existing: TaskRecord, expected: TaskRecord) -> bool:
        return (
            existing.project_id == expected.project_id
            and existing.run_id == expected.run_id
            and existing.sample_id == expected.sample_id
            and existing.solution_id == expected.solution_id
            and existing.image_artifact_path == expected.image_artifact_path
            and existing.media_type == expected.media_type
            and existing.provider_keys == expected.provider_keys
            and existing.created_at == expected.created_at
            and existing.metadata == expected.metadata
        )

    async def _initialize_run(
        self,
        *,
        project: ProjectContext,
        run: RunRecord,
        selected_samples: list[SampleContext],
        selected_solutions: list[tuple[SolutionDefinition, CompatibilityContext]],
        execution_mode: Literal["mock", "live"],
    ) -> Versioned[RunRecord]:
        dispatcher = self._dispatcher
        if dispatcher is None:
            raise ProductError(
                503,
                "Durable task dispatch is unavailable",
                code="dispatch_unavailable",
            )
        expected_tasks = self._run_tasks(
            project=project,
            run=run,
            selected_samples=selected_samples,
            selected_solutions=selected_solutions,
            execution_mode=execution_mode,
        )
        if len(expected_tasks) != run.total_tasks:
            raise ProductError(
                500,
                "Run manifest does not match its durable task count",
                code="run_initialization_invalid",
            )
        task_slots = asyncio.Semaphore(32)

        async def persist_task(task: TaskRecord) -> TaskRecord:
            async with task_slots:
                try:
                    await self.repositories.tasks.create(task)
                    return task
                except AlreadyExistsError:
                    existing = (
                        await self.repositories.tasks.get(run.run_id, task.pair_id)
                    ).value
                    if not self._same_task_identity(existing, task):
                        raise ProductError(
                            409,
                            "Durable task identity collision",
                            code="task_collision",
                        ) from None
                    return existing

        task_records = list(
            await asyncio.gather(*(persist_task(task) for task in expected_tasks))
        )
        await self.repositories.events.append(
            run.run_id,
            "run_started",
            {
                "type": "run_started",
                "run_id": run.run_id,
                "project_id": project.id,
                "total_pairs": run.total_tasks,
            },
            idempotency_key=f"run-started-{run.run_id}",
        )
        await dispatcher.enqueue(tuple(task_records))
        return await self._mark_dispatch_complete(project.id, run.run_id)

    async def recover_incomplete_runs(self) -> None:
        if self._dispatcher is None:
            return
        for stored in await self.repositories.runs.list_unfinished():
            run = stored.value
            if run.metadata.get("dispatch_complete") is True:
                continue
            try:
                manifest = require_json_object(
                    await self.repositories.artifacts.get_json(
                        run.manifest_artifact_path
                    ),
                    field="run manifest",
                )
            except NotFoundError:
                initialization_manifest = run.metadata.get(
                    "initialization_manifest"
                )
                if not isinstance(initialization_manifest, dict):
                    raise ProductError(
                        500,
                        "Incomplete run has no recoverable manifest",
                        code="run_initialization_invalid",
                    ) from None
                manifest = initialization_manifest
                await self.repositories.artifacts.create_json(
                    run.manifest_artifact_path,
                    manifest,
                )
            project = await self.context(run.project_id, require_available=True)
            sample_ids = _string_list(manifest.get("sample_ids"), "sample_ids")
            solution_ids = _string_list(manifest.get("solution_ids"), "solution_ids")
            execution_mode = _string(manifest.get("execution_mode"), "execution_mode")
            if execution_mode not in {"mock", "live"}:
                raise ProductError(
                    500,
                    "Run manifest has an invalid execution mode",
                    code="run_initialization_invalid",
                )
            samples_by_id = {sample.id: sample for sample in project.samples}
            try:
                selected_samples = [samples_by_id[sample_id] for sample_id in sample_ids]
            except KeyError as exc:
                raise ProductError(
                    500,
                    "Run manifest references an unavailable sample",
                    code="run_initialization_invalid",
                ) from exc
            selected_solutions: list[
                tuple[SolutionDefinition, CompatibilityContext]
            ] = []
            for solution_id in solution_ids:
                try:
                    solution = self.catalog.get(solution_id)
                    declaration = project.compatibility[solution_id]
                except KeyError as exc:
                    raise ProductError(
                        500,
                        "Run manifest references an unavailable solution",
                        code="run_initialization_invalid",
                    ) from exc
                selected_solutions.append((solution, declaration))
            await self._initialize_run(
                project=project,
                run=run,
                selected_samples=selected_samples,
                selected_solutions=selected_solutions,
                execution_mode=cast(Literal["mock", "live"], execution_mode),
            )

    async def create_run(
        self,
        project_id: str,
        request: RunCreateRequest,
        *,
        idempotency_key: str | None,
    ) -> RunAccepted:
        project = await self.context(project_id, require_available=True)
        await self._assert_custom_project_writable(project)
        if request.schema_version != project.schema_version:
            raise ProductError(
                409,
                "Schema version does not match the active project schema",
                code="schema_version_mismatch",
            )
        if request.prompt_version != project.prompt_version:
            raise ProductError(
                409,
                "Prompt version does not match the active project prompt",
                code="prompt_version_mismatch",
            )
        if request.execution_mode == "live" and not self.settings.allow_live_execution:
            raise ProductError(
                403,
                "Live execution is disabled for this runtime profile",
                code="live_execution_disabled",
            )
        if request.execution_mode != self.settings.default_execution_mode:
            raise ProductError(
                409,
                "Requested execution mode does not match the configured runtime",
                code="execution_mode_mismatch",
            )
        dispatcher = self._dispatcher
        if dispatcher is None:
            raise ProductError(
                503,
                "Durable task dispatch is unavailable",
                code="dispatch_unavailable",
            )
        samples_by_id = {item.id: item for item in project.samples}
        unknown_samples = sorted(set(request.sample_ids) - set(samples_by_id))
        if unknown_samples:
            raise ProductError(
                422,
                f"Unknown sample IDs: {', '.join(unknown_samples)}",
                code="unknown_sample",
            )
        prompt_examples = sorted(
            item.id for item in project.samples
            if item.scoring_role == "few_shot" and item.id in request.sample_ids
        )
        if prompt_examples:
            raise ProductError(
                422,
                f"Prompt examples cannot be scored: {', '.join(prompt_examples)}",
                code="prompt_example_selected",
            )
        excluded_samples = sorted(
            item.id
            for item in project.samples
            if item.scoring_role == "excluded" and item.id in request.sample_ids
        )
        if excluded_samples:
            raise ProductError(
                422,
                f"Excluded samples cannot be scored: {', '.join(excluded_samples)}",
                code="excluded_sample_selected",
            )
        selected_samples = [
            item for item in project.samples if item.id in request.sample_ids
        ]
        selected_solutions: list[tuple[SolutionDefinition, CompatibilityContext]] = []
        for solution_id in request.solution_ids:
            try:
                solution = self.catalog.get(solution_id)
            except KeyError as exc:
                raise ProductError(
                    422,
                    f"Unknown solution ID: {solution_id}",
                    code="unknown_solution",
                ) from exc
            declaration = project.compatibility.get(solution_id)
            if declaration is None or declaration.level == "unsupported":
                raise ProductError(
                    422,
                    f"Solution {solution_id} is unsupported for project {project.id}",
                    code="unsupported_solution",
                )
            selected_solutions.append((solution, declaration))
        selected_solutions.sort(key=lambda item: (item[0].order, item[0].id))
        solution_prompts: JsonObject = {
            solution.id: {
                "mode": (mode := _solution_prompt_mode(solution)),
                "version": project.prompt_versions[mode],
                "hash": project.prompt_hashes[mode],
            }
            for solution, _ in selected_solutions
        }

        image_availability = await asyncio.gather(
            *(
                self.repositories.artifacts.exists(sample.image_path)
                for sample in selected_samples
            )
        )
        missing_images = [
            sample.id
            for sample, available in zip(
                selected_samples,
                image_availability,
                strict=True,
            )
            if not available
        ]
        if missing_images:
            raise ProductError(
                409,
                f"Sample images are unavailable: {', '.join(missing_images)}",
                code="sample_image_unavailable",
            )
        canonical_request: JsonObject = {
            "project_id": project.id,
            "sample_ids": [item.id for item in selected_samples],
            "solution_ids": [item.id for item, _ in selected_solutions],
            "schema_version": request.schema_version,
            "schema_hash": project.schema_hash,
            "prompt_version": request.prompt_version,
            "prompt_hash": project.prompt_hash,
            "solution_prompts": solution_prompts,
            "execution_mode": request.execution_mode,
        }
        request_hash = content_sha256(strict_json_bytes(canonical_request))
        if idempotency_key is not None:
            key = idempotency_key.strip()
            if (
                not key
                or len(key.encode("utf-8")) > 200
                or any(ord(character) < 33 or ord(character) > 126 for character in key)
            ):
                raise ProductError(
                    400,
                    "Idempotency-Key must be 1-200 visible ASCII characters",
                    code="invalid_idempotency_key",
                )
            run_token = hashlib.sha256(
                f"{self.settings.owner_scope}\n{project.id}\n{key}".encode()
            ).hexdigest()[:24]
        else:
            run_token = secrets.token_hex(12)
        run_id = validate_id(f"run-{run_token}", field="run_id")
        await self.repositories.runs.resume_delete(run_id)
        total_pairs = len(selected_samples) * len(selected_solutions)
        now = self._now()
        manifest_path = run_manifest(run_id)
        run_record = RunRecord(
            project_id=project.id,
            run_id=run_id,
            manifest_artifact_path=manifest_path,
            total_tasks=total_pairs,
            pending_tasks=total_pairs,
            created_at=now,
            updated_at=now,
            metadata={
                "request_hash": request_hash,
                "execution_mode": request.execution_mode,
                "sample_count": len(selected_samples),
                "sample_ids": [item.id for item in selected_samples],
                "solution_count": len(selected_solutions),
                "reference_only_count": sum(
                    not declaration.comparable_quality
                    for _, declaration in selected_solutions
                ),
                "dispatch_complete": False,
            },
        )
        manifest_value: JsonObject = {
            "format_version": "2.0",
            "run_id": run_id,
            **canonical_request,
            "created_at": datetime_text(run_record.created_at),
            "request_hash": request_hash,
            "solutions": [
                {
                    "solution_id": solution.id,
                    "order": solution.order,
                    "compatibility": declaration.level,
                    "comparable_quality": declaration.comparable_quality,
                    "deterministic_mapping_id": declaration.deterministic_mapping_id,
                    "prompt": solution_prompts[solution.id],
                }
                for solution, declaration in selected_solutions
            ],
            "total_pairs": total_pairs,
        }
        run_record = replace(
            run_record,
            metadata={
                **run_record.metadata,
                "initialization_manifest": manifest_value,
            },
        )
        created_run = False
        try:
            await self._assert_custom_project_writable(project)
            stored_run = await self.repositories.runs.create(run_record)
            created_run = True
        except AlreadyExistsError:
            stored_run = await self.repositories.runs.get(project.id, run_id)
            if stored_run.value.metadata.get("request_hash") != request_hash:
                raise ProductError(
                    409,
                    "Idempotency-Key was already used for a different request",
                    code="idempotency_conflict",
                ) from None
            if stored_run.value.metadata.get("dispatch_complete") is True:
                return self._accepted(stored_run.value)
        if created_run:
            try:
                await self._assert_custom_project_writable(project)
            except ProductError:
                await self.repositories.artifacts.delete_prefix(f"runs/{run_id}/")
                await self.repositories.tasks.delete_for_run(run_id)
                await self.repositories.runs.delete_auxiliary(project.id, run_id)
                await self.repositories.runs.delete(project.id, run_id)
                raise
        initialization_manifest = stored_run.value.metadata.get(
            "initialization_manifest"
        )
        if isinstance(initialization_manifest, dict):
            durable_manifest = initialization_manifest
        else:
            try:
                durable_manifest = require_json_object(
                    await self.repositories.artifacts.get_json(manifest_path),
                    field="run manifest",
                )
            except NotFoundError:
                durable_manifest = {
                    **manifest_value,
                    "created_at": datetime_text(stored_run.value.created_at),
                }
        await self.repositories.artifacts.create_json(
            manifest_path,
            durable_manifest,
        )
        stored_run = await self._initialize_run(
            project=project,
            run=stored_run.value,
            selected_samples=selected_samples,
            selected_solutions=selected_solutions,
            execution_mode=request.execution_mode,
        )
        return self._accepted(stored_run.value)

    async def _mark_dispatch_complete(
        self,
        project_id: str,
        run_id: str,
    ) -> Versioned[RunRecord]:
        for _ in range(16):
            current = await self.repositories.runs.get(project_id, run_id)
            if current.value.metadata.get("dispatch_complete") is True:
                return current
            metadata = dict(current.value.metadata)
            metadata["dispatch_complete"] = True
            metadata.pop("initialization_manifest", None)
            updated = replace(current.value, metadata=metadata, updated_at=self._now())
            try:
                return await self.repositories.runs.replace(
                    updated,
                    expected_etag=current.etag,
                )
            except ConcurrencyError:
                continue
        raise ProductError(
            409,
            "Could not record durable queue dispatch",
            code="concurrent_update",
        )

    def _accepted(self, run: RunRecord) -> RunAccepted:
        status: Literal["queued", "running"] = (
            "queued" if run.status == "queued" else "running"
        )
        return RunAccepted(
            run_id=run.run_id,
            status=status,
            total_pairs=run.total_tasks,
            event_url=f"/api/runs/{run.run_id}/events",
            snapshot_url=f"/api/runs/{run.run_id}",
            results_url=f"/api/runs/{run.run_id}/results",
            download_url=f"/api/runs/{run.run_id}/download",
        )

    def _history_view(self, run: RunRecord) -> RunHistoryView:
        mode = run.metadata.get("execution_mode")
        execution_mode = cast(
            Literal["mock", "live"] | None,
            mode if mode in {"mock", "live"} else None,
        )

        def optional_count(name: str) -> int | None:
            value = run.metadata.get(name)
            return (
                value
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                else None
            )

        return RunHistoryView(
            run_id=run.run_id,
            project_id=run.project_id,
            status=_run_status(run.status),
            created_at=run.created_at,
            updated_at=run.updated_at,
            total_pairs=run.total_tasks,
            completed_pairs=run.total_tasks - run.pending_tasks,
            execution_mode=execution_mode,
            sample_count=optional_count("sample_count"),
            solution_count=optional_count("solution_count"),
            reference_only_count=optional_count("reference_only_count"),
            download_ready=run.finalizer_completed and run.download_artifact_path is not None,
        )

    async def list_runs(
        self,
        project_id: str,
        *,
        limit: int = 100,
    ) -> list[RunHistoryView]:
        project = await self.context(project_id, require_available=True)
        records = await self.repositories.runs.list(project.id, limit=limit)
        return [
            self._history_view(item.value)
            for item in records
            if item.value.metadata.get("dispatch_complete") is True
        ]

    async def find_run(self, run_id: str) -> Versioned[RunRecord]:
        try:
            validate_id(run_id, field="run_id")
        except ValueError as exc:
            raise ProductError(404, "Run not found", code="run_not_found") from exc
        try:
            stored = await self.repositories.runs.find(run_id)
        except NotFoundError as exc:
            raise ProductError(404, "Run not found", code="run_not_found") from exc
        project = await self.context(stored.value.project_id)
        if not project.available:
            raise ProductError(
                451,
                project.availability_reason or "Project is unavailable",
                code="rights_restricted",
            )
        if stored.value.metadata.get("dispatch_complete") is not True:
            raise ProductError(
                409,
                "Run initialization is incomplete",
                code="run_initializing",
            )
        return stored

    async def manifest_document(self, run: RunRecord) -> JsonObject:
        try:
            value = await self.repositories.artifacts.get_json(
                run.manifest_artifact_path
            )
        except NotFoundError as exc:
            raise ProductError(
                500,
                "Run manifest is unavailable",
                code="manifest_missing",
            ) from exc
        return require_json_object(value, field="run manifest")

    async def manifest_view(self, run: RunRecord) -> RunManifestView:
        manifest = await self.manifest_document(run)
        samples = _string_list(manifest.get("sample_ids"), "sample_ids")
        solutions = _string_list(manifest.get("solution_ids"), "solution_ids")
        mode = _string(manifest.get("execution_mode"), "execution_mode")
        reference_ids = [
            _string(item.get("solution_id"), "solution_id")
            for item in _object_list(manifest.get("solutions"), "solutions")
            if item.get("comparable_quality") is False
        ]
        return RunManifestView(
            id=run.run_id,
            project_id=run.project_id,
            sample_ids=samples,
            solution_ids=solutions,
            schema_version=_string(manifest.get("schema_version"), "schema_version"),
            prompt_version=_string(manifest.get("prompt_version"), "prompt_version"),
            status=_run_status(run.status),
            total_pairs=run.total_tasks,
            completed_pairs=run.total_tasks - run.pending_tasks,
            created_at=run.created_at,
            execution_mode=cast(Literal["mock", "live"], mode),
            reference_only_solution_ids=reference_ids,
        )

    async def tasks_for_run(self, run: RunRecord) -> list[TaskRecord]:
        tasks = [item.value for item in await self.repositories.tasks.list_for_run(run.run_id)]
        return sorted(
            tasks,
            key=lambda item: (
                _metadata_int(item.metadata.get("sample_order"), 2**31 - 1),
                _metadata_int(item.metadata.get("solution_order"), 2**31 - 1),
                item.pair_id,
            ),
        )

    async def task_view(
        self,
        task: TaskRecord,
        *,
        compact: bool = False,
        include_telemetry: bool | None = None,
    ) -> RunTaskView:
        load_telemetry = not compact if include_telemetry is None else include_telemetry
        telemetry = (
            await self._telemetry(task.telemetry_artifact_path)
            if load_telemetry
            else PipelineTelemetryView()
        )
        parsed = (
            await self._artifact_object(task.parsed_artifact_path)
            if not compact and task.parsed_artifact_path
            else None
        )
        status = _task_status(task)
        stage = _task_stage(task, status)
        progress = _task_progress(status, stage)
        elapsed = max(
            0,
            int((task.updated_at - task.created_at).total_seconds() * 1000),
        )
        if task.terminal and telemetry.latency_ms:
            elapsed = int(telemetry.latency_ms)
        return RunTaskView(
            pair_id=pair_id(task.sample_id, task.solution_id).replace("|", "::"),
            sample_id=task.sample_id,
            solution_id=task.solution_id,
            solution_order=_metadata_int(task.metadata.get("solution_order"), 2**31 - 1),
            status=status,
            stage=stage,
            progress=progress,
            retries=max(task.attempts - 1, 0),
            elapsed_ms=elapsed,
            estimated_cost_usd=telemetry.estimated_cost_usd,
            partial_output=parsed,
            error=task.error_message,
            compatibility=cast(
                Compatibility,
                task.metadata.get("compatibility", "unsupported"),
            ),
            comparable_quality=task.metadata.get("comparable_quality") is True,
        )

    async def result_view(
        self,
        task: TaskRecord,
        *,
        project: ProjectContext | None = None,
        sample: SampleContext | None = None,
        gold: JsonObject | None = None,
        compact: bool = False,
    ) -> RunResultView:
        if task.status not in _TERMINAL_TASKS:
            raise ProductError(409, "Result is not ready", code="result_not_ready")
        raw, parsed, comparison, telemetry = await asyncio.gather(
            self._artifact_json(task.raw_artifact_path),
            self._artifact_json(task.parsed_artifact_path),
            self._artifact_json(task.comparison_artifact_path),
            self._telemetry(task.telemetry_artifact_path),
        )
        parsed_object = parsed if isinstance(parsed, dict) and task.status == "succeeded" else None
        if project is None or sample is None:
            project, sample = await self.sample_context(task.project_id, task.sample_id)
            gold = await self._artifact_object(sample.gold_path)
        solution = self.catalog.get(task.solution_id)
        metric_family: Literal["structured", "ocr_text"] = (
            "ocr_text" if solution.kind == "ocr_text" else "structured"
        )
        ocr_metrics = (
            _ocr_text_metrics_view(comparison)
            if metric_family == "ocr_text"
            else None
        )
        comparisons = (
            [] if metric_family == "ocr_text" else _comparison_views(comparison)
        )
        if (
            metric_family == "structured"
            and not comparisons
            and gold is not None
            and parsed_object is not None
        ):
            comparisons = _compare_objects(gold, parsed_object)
        schema_valid: bool | None = (
            None
            if metric_family == "ocr_text"
            else _schema_valid(project.schema, parsed_object)
            if parsed_object is not None
            else False
        )
        if (
            metric_family == "structured"
            and isinstance(comparison, dict)
            and isinstance(comparison.get("schema_valid"), bool)
        ):
            schema_valid = cast(bool, comparison["schema_valid"])
        compatibility = cast(
            Compatibility,
            task.metadata.get("compatibility", "unsupported"),
        )
        comparable = task.metadata.get("comparable_quality") is True
        artifacts = {} if compact else await self._artifact_views(task)
        raw_response = (
            raw
            if isinstance(raw, str)
            else json.dumps(
                raw,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            )
            if raw is not None
            else ""
        )
        result_status = cast(Literal["succeeded", "failed", "invalid"], task.status)
        return RunResultView(
            run_id=task.run_id,
            project_id=task.project_id,
            sample_id=task.sample_id,
            solution_id=task.solution_id,
            solution_order=_metadata_int(task.metadata.get("solution_order"), 2**31 - 1),
            status=result_status,
            schema_valid=schema_valid,
            metric_family=metric_family,
            ocr_metrics=ocr_metrics,
            raw_response=raw_response,
            parsed_output=parsed_object,
            comparisons=comparisons,
            telemetry=telemetry,
            error=(
                ErrorView(
                    code=task.error_code or "execution_failed",
                    message=task.error_message or "The extraction did not succeed.",
                )
                if task.status != "succeeded"
                else None
            ),
            compatibility=compatibility,
            comparable_quality=comparable,
            quality_scored=(
                sample.scoring_role == "scored"
                and comparable
                and (
                    ocr_metrics is not None
                    if metric_family == "ocr_text"
                    else gold is not None
                )
            ),
            artifacts=artifacts,
        )

    async def _result_views(
        self,
        tasks: list[TaskRecord],
        *,
        compact: bool,
    ) -> list[RunResultView]:
        if not tasks:
            return []
        project = await self.context(tasks[0].project_id, require_available=True)
        samples_by_id = {sample.id: sample for sample in project.samples}
        selected_samples = {
            task.sample_id: samples_by_id[task.sample_id] for task in tasks
        }
        gold_values = await asyncio.gather(
            *(
                self._artifact_object(sample.gold_path)
                for sample in selected_samples.values()
            )
        )
        gold_by_id = {
            sample_id: gold
            for sample_id, gold in zip(
                selected_samples,
                gold_values,
                strict=True,
            )
        }
        return list(
            await asyncio.gather(
                *(
                    self.result_view(
                        task,
                        project=project,
                        sample=selected_samples[task.sample_id],
                        gold=gold_by_id[task.sample_id],
                        compact=compact,
                    )
                    for task in tasks
                )
            )
        )

    async def _artifact_json(self, path: str | None) -> JsonValue | None:
        if path is None:
            return None
        try:
            return await self.repositories.artifacts.get_json(path)
        except NotFoundError:
            return None

    async def _artifact_views(self, task: TaskRecord) -> dict[str, ArtifactView]:
        paths = {
            "raw": task.raw_artifact_path,
            "parsed": task.parsed_artifact_path,
            "comparison": task.comparison_artifact_path,
            "telemetry": task.telemetry_artifact_path,
        }
        result: dict[str, ArtifactView] = {}
        for kind, path in paths.items():
            if path is None:
                continue
            try:
                info = await self.repositories.artifacts.get_info(path)
            except NotFoundError:
                continue
            result[kind] = ArtifactView(
                kind=cast(Literal["raw", "parsed", "comparison", "telemetry"], kind),
                url=(
                    f"/api/runs/{task.run_id}/results/{task.sample_id}/"
                    f"{task.solution_id}/artifacts/{kind}"
                ),
                content_type=info.content_type,
                size_bytes=info.size,
                sha256=info.sha256,
            )
        return result

    async def _telemetry(self, path: str | None) -> PipelineTelemetryView:
        value = await self._artifact_json(path)
        return _telemetry_view(value)

    async def _persisted_task_views(
        self,
        run: RunRecord,
        manifest: RunManifestView,
    ) -> list[RunTaskView] | None:
        value: JsonValue | None = None
        for path in (
            run_tasks(run.run_id),
            run.aggregate_artifact_path,
        ):
            if path is None:
                continue
            try:
                candidate = await self.repositories.artifacts.get_json(path)
            except NotFoundError:
                continue
            if isinstance(candidate, dict) and isinstance(candidate.get("tasks"), list):
                value = candidate
                break
            if path == run.aggregate_artifact_path:
                value = candidate
        if not isinstance(value, dict):
            return None
        raw_tasks = value.get("tasks")
        if isinstance(raw_tasks, list):
            try:
                tasks = [RunTaskView.model_validate(item) for item in raw_tasks]
            except ValidationError:
                return None
        else:
            raw_results = value.get("results")
            if not isinstance(raw_results, list):
                return None
            statuses: dict[tuple[str, str], str] = {}
            errors: dict[tuple[str, str], str | None] = {}
            for item in raw_results:
                if not isinstance(item, dict):
                    return None
                sample_id = item.get("sample_id")
                solution_id = item.get("solution_id")
                status = item.get("status")
                if (
                    not isinstance(sample_id, str)
                    or not isinstance(solution_id, str)
                    or not isinstance(status, str)
                    or status not in _TERMINAL_TASKS
                ):
                    return None
                key = (sample_id, solution_id)
                statuses[key] = status
                error_code = item.get("error_code")
                errors[key] = error_code if isinstance(error_code, str) else None
            solution_order = {
                solution_id: index
                for index, solution_id in enumerate(manifest.solution_ids)
            }
            tasks = []
            for sample_id in manifest.sample_ids:
                for solution_id in manifest.solution_ids:
                    key = (sample_id, solution_id)
                    status = statuses.get(key)
                    if status is None:
                        return None
                    comparable = (
                        solution_id not in manifest.reference_only_solution_ids
                    )
                    tasks.append(
                        RunTaskView(
                            pair_id=pair_id(sample_id, solution_id).replace("|", "::"),
                            sample_id=sample_id,
                            solution_id=solution_id,
                            solution_order=solution_order[solution_id],
                            status=cast(
                                Literal["succeeded", "failed", "invalid"],
                                status,
                            ),
                            stage=(
                                "completed" if status == "succeeded" else "failed"
                            ),
                            progress=1.0,
                            retries=0,
                            elapsed_ms=0,
                            estimated_cost_usd=None,
                            partial_output=None,
                            error=errors[key],
                            compatibility=(
                                "full" if comparable else "partial_reference"
                            ),
                            comparable_quality=comparable,
                        )
                    )
        expected = {
            pair_id(sample_id, solution_id).replace("|", "::")
            for sample_id in manifest.sample_ids
            for solution_id in manifest.solution_ids
        }
        if {task.pair_id for task in tasks} != expected:
            return None
        return sorted(
            tasks,
            key=lambda task: (
                manifest.sample_ids.index(task.sample_id),
                task.solution_order,
                task.pair_id,
            ),
        )

    async def snapshot(
        self,
        run_id: str,
        *,
        compact: bool = False,
        result_sample_id: str | None = None,
    ) -> RunSnapshotView:
        event_cursor = await self.repositories.events.latest_sequence(run_id)
        run = (await self.find_run(run_id)).value
        manifest = await self.manifest_view(run)
        if result_sample_id is not None and result_sample_id not in manifest.sample_ids:
            raise ProductError(404, "Sample not found in run", code="sample_not_found")
        if compact and result_sample_id is None:
            result_sample_id = manifest.sample_ids[0] if manifest.sample_ids else None
        task_views = (
            await self._persisted_task_views(run, manifest)
            if compact and run.finalizer_completed
            else None
        )
        if task_views is None:
            tasks = await self.tasks_for_run(run)
            task_views = list(
                await asyncio.gather(
                    *(self.task_view(item, compact=compact) for item in tasks)
                )
            )
        else:
            tasks = []
        if result_sample_id is not None:
            tasks = [
                stored.value
                for stored in await asyncio.gather(
                    *(
                        self.repositories.tasks.get(
                            run.run_id,
                            pair_id(result_sample_id, solution_id),
                        )
                        for solution_id in manifest.solution_ids
                    )
                )
            ]
        terminal = [
            item
            for item in tasks
            if item.terminal
            and (result_sample_id is None or item.sample_id == result_sample_id)
        ]
        results = await self._result_views(terminal, compact=compact)
        return RunSnapshotView(
            manifest=manifest,
            tasks=task_views,
            results=results,
            event_cursor=str(event_cursor),
        )

    async def list_results(
        self,
        run_id: str,
        *,
        sample_id: str | None = None,
        compact: bool = False,
    ) -> list[RunResultView]:
        run = (await self.find_run(run_id)).value
        tasks = await self.tasks_for_run(run)
        if sample_id is not None and not any(item.sample_id == sample_id for item in tasks):
            raise ProductError(404, "Sample not found in run", code="sample_not_found")
        terminal = [
            item
            for item in tasks
            if item.terminal and (sample_id is None or item.sample_id == sample_id)
        ]
        return await self._result_views(terminal, compact=compact)

    async def result(
        self,
        run_id: str,
        sample_id: str,
        solution_id: str,
    ) -> RunResultView:
        run = (await self.find_run(run_id)).value
        try:
            task = (
                await self.repositories.tasks.get(
                    run.run_id,
                    pair_id(sample_id, solution_id),
                )
            ).value
        except (NotFoundError, ValueError) as exc:
            raise ProductError(404, "Result not found", code="result_not_found") from exc
        return await self.result_view(task)

    async def artifact(
        self,
        run_id: str,
        sample_id: str,
        solution_id: str,
        kind: Literal["raw", "parsed", "comparison", "telemetry"],
    ) -> tuple[bytes, ArtifactInfo]:
        run = (await self.find_run(run_id)).value
        try:
            task = (
                await self.repositories.tasks.get(
                    run.run_id,
                    pair_id(sample_id, solution_id),
                )
            ).value
        except (NotFoundError, ValueError) as exc:
            raise ProductError(404, "Result not found", code="result_not_found") from exc
        path = {
            "raw": task.raw_artifact_path,
            "parsed": task.parsed_artifact_path,
            "comparison": task.comparison_artifact_path,
            "telemetry": task.telemetry_artifact_path,
        }[kind]
        if path is None:
            raise ProductError(404, "Artifact not found", code="artifact_not_found")
        try:
            data, info = await asyncio.gather(
                self.repositories.artifacts.get_bytes(path),
                self.repositories.artifacts.get_info(path),
            )
        except NotFoundError as exc:
            raise ProductError(404, "Artifact not found", code="artifact_not_found") from exc
        return data, info

    async def download(self, run_id: str) -> tuple[bytes, ArtifactInfo]:
        run = (await self.find_run(run_id)).value
        if not run.finalizer_completed or run.download_artifact_path is None:
            raise ProductError(409, "Run download is not ready", code="download_not_ready")
        try:
            data, info = await asyncio.gather(
                self.repositories.artifacts.get_bytes(run.download_artifact_path),
                self.repositories.artifacts.get_info(run.download_artifact_path),
            )
        except NotFoundError as exc:
            raise ProductError(
                500,
                "Persisted run download is missing",
                code="download_missing",
            ) from exc
        return data, info

    async def _persisted_aggregate(self, run: RunRecord) -> ProjectAggregateView | None:
        paths = [
            path
            for path in (run.aggregate_artifact_path, run_aggregate(run.run_id))
            if path is not None
        ]
        for path in dict.fromkeys(paths):
            try:
                value = await self.repositories.artifacts.get_json(path)
            except NotFoundError:
                continue
            document = _aggregate_report_document(value)
            if document is None or document.get("format_version") != "2.0":
                continue
            try:
                aggregate = ProjectAggregateView.model_validate(document)
            except ValidationError:
                continue
            if aggregate.project_id == run.project_id and aggregate.run_id == run.run_id:
                return aggregate
        return None

    async def _compute_aggregate(
        self,
        project: ProjectContext,
        run: RunRecord,
        tasks: list[TaskRecord] | tuple[TaskRecord, ...],
    ) -> ProjectAggregateView:
        selected_ids = {item.sample_id for item in tasks}
        scored_sample_ids = {
            sample.id
            for sample in project.samples
            if sample.id in selected_ids and sample.scoring_role == "scored"
        }
        gold_samples = [
            sample
            for sample in project.samples
            if (
                sample.id in selected_ids
                and sample.scoring_role == "scored"
                and sample.gold_path is not None
            )
        ]
        gold_values = await asyncio.gather(
            *(self._artifact_object(sample.gold_path) for sample in gold_samples)
        )
        gold_by_id = {
            sample.id: gold
            for sample, gold in zip(gold_samples, gold_values, strict=True)
            if gold is not None
        }
        slots = asyncio.Semaphore(32)
        field_accumulators: dict[tuple[str, str], _FieldAggregateAccumulator] = {}

        async def result_inputs(
            task: TaskRecord,
            *,
            include_comparison: bool,
        ) -> tuple[PipelineTelemetryView | None, JsonValue | None]:
            async with slots:
                telemetry_value, comparison_value = await asyncio.gather(
                    self._artifact_json(task.telemetry_artifact_path),
                    (
                        self._artifact_json(task.comparison_artifact_path)
                        if include_comparison
                        else asyncio.sleep(0, result=None)
                    ),
                )
            telemetry = (
                _telemetry_view(telemetry_value)
                if isinstance(telemetry_value, dict)
                else None
            )
            return telemetry, comparison_value

        solution_rows: list[SolutionAggregateView] = []
        for solution in self.catalog.list():
            declaration = project.compatibility.get(solution.id)
            solution_tasks = [
                item for item in tasks if item.solution_id == solution.id
            ]
            if declaration is None or not solution_tasks:
                continue
            terminal_tasks = [item for item in solution_tasks if item.terminal]
            successes = [item for item in terminal_tasks if item.status == "succeeded"]
            expected_quality_tasks = [
                item
                for item in solution_tasks
                if item.terminal
                and declaration.comparable_quality
                and (
                    item.sample_id in scored_sample_ids
                    if solution.kind == "ocr_text"
                    else item.sample_id in gold_by_id
                )
            ]
            expected_pairs = {item.pair_id for item in expected_quality_tasks}
            loaded = await asyncio.gather(
                *(
                    result_inputs(
                        task,
                        include_comparison=task.pair_id in expected_pairs,
                    )
                    for task in solution_tasks
                )
            )
            inputs_by_pair = {
                task.pair_id: values
                for task, values in zip(solution_tasks, loaded, strict=True)
            }
            telemetry_rows = [
                (task, values[0])
                for task, values in zip(solution_tasks, loaded, strict=True)
                if values[0] is not None
            ]
            latencies = [telemetry.latency_ms for _, telemetry in telemetry_rows]
            latency_stats = LatencyStats.from_values(latencies)
            telemetry_by_pair = {
                task.pair_id: telemetry for task, telemetry in telemetry_rows
            }
            unknown_cost = any(
                telemetry.estimated_cost_usd is None
                for _, telemetry in telemetry_rows
            ) or any(
                task.terminal and task.pair_id not in telemetry_by_pair
                for task in solution_tasks
            )
            total_cost = (
                sum(
                    telemetry.estimated_cost_usd or 0.0
                    for _, telemetry in telemetry_rows
                )
                if not unknown_cost
                else None
            )
            telemetry_evidence = [
                _task_evidence(task) for task, _ in telemetry_rows
            ]
            cost_evidence = [
                _task_evidence(task)
                for task, telemetry in telemetry_rows
                if telemetry.estimated_cost_usd is not None
            ]
            input_token_rows = [
                (
                    task,
                    sum(
                        component.input_tokens
                        for component in telemetry.components
                        if component.input_tokens is not None
                    ),
                )
                for task, telemetry in telemetry_rows
                if any(
                    component.input_tokens is not None
                    for component in telemetry.components
                )
            ]
            output_token_rows = [
                (
                    task,
                    sum(
                        component.output_tokens
                        for component in telemetry.components
                        if component.output_tokens is not None
                    ),
                )
                for task, telemetry in telemetry_rows
                if any(
                    component.output_tokens is not None
                    for component in telemetry.components
                )
            ]
            total_token_rows = [
                (task, telemetry.total_tokens)
                for task, telemetry in telemetry_rows
                if telemetry.total_tokens is not None
            ]
            page_rows = [
                (task, telemetry.total_pages)
                for task, telemetry in telemetry_rows
                if telemetry.total_pages is not None
            ]
            attempt_rows = [
                (task, telemetry.attempts) for task, telemetry in telemetry_rows
            ]
            cost_scope: Literal["accrued", "complete"] = (
                "complete"
                if terminal_tasks and len(terminal_tasks) == len(solution_tasks)
                else "accrued"
            )

            if solution.kind == "ocr_text":
                metrics: list[tuple[TaskRecord, OcrTextMetricsView]] = []
                for task in expected_quality_tasks:
                    values = inputs_by_pair.get(task.pair_id)
                    metric = (
                        _ocr_text_metrics_view(values[1])
                        if values is not None
                        else None
                    )
                    if metric is None:
                        try:
                            reference = (
                                await self.repositories.artifacts.get_bytes(
                                    sample_ocr_text(project.id, task.sample_id)
                                )
                            ).decode("utf-8")
                        except (NotFoundError, UnicodeDecodeError):
                            continue
                        metric = OcrTextMetricsView.model_validate(
                            evaluate_ocr_text(
                                project_id=project.id,
                                reference=reference,
                                hypothesis="",
                            ).model_dump(mode="json")
                        )
                    metrics.append((task, metric))
                complete_ocr_evidence = len(metrics) == len(expected_quality_tasks)

                reference_tokens = sum(
                    metric.reference_tokens for _, metric in metrics
                )
                matched_tokens = sum(
                    metric.matched_reference_tokens for _, metric in metrics
                )
                character_metrics = [
                    metric
                    for _, metric in metrics
                    if metric.character_edits is not None
                    and metric.reference_characters is not None
                ]
                word_metrics = [
                    metric
                    for _, metric in metrics
                    if metric.word_edits is not None
                    and metric.reference_words is not None
                ]
                character_reference = sum(
                    metric.reference_characters or 0
                    for metric in character_metrics
                )
                word_reference = sum(
                    metric.reference_words or 0 for metric in word_metrics
                )
                evidence = [_task_evidence(item) for item in terminal_tasks]
                scored_evidence = [
                    _task_evidence(item) for item in expected_quality_tasks
                ]
                solution_rows.append(
                    SolutionAggregateView(
                        solution_id=solution.id,
                        metric_family="ocr_text",
                        normalized_field_f1=AggregateMeasure(
                            value=None,
                            unit="ratio",
                            evidence=[],
                        ),
                        whole_answer_exactness=AggregateMeasure(
                            value=None,
                            unit="ratio",
                            evidence=[],
                        ),
                        reference_token_recall=AggregateMeasure(
                            value=(
                                matched_tokens / reference_tokens
                                if complete_ocr_evidence and reference_tokens
                                else None
                            ),
                            unit="ratio",
                            evidence=scored_evidence,
                        ),
                        character_error_rate=AggregateMeasure(
                            value=(
                                sum(
                                    metric.character_edits or 0
                                    for metric in character_metrics
                                )
                                / character_reference
                                if complete_ocr_evidence and character_reference
                                else None
                            ),
                            unit="ratio",
                            polarity="lower_better",
                            evidence=scored_evidence,
                        ),
                        word_error_rate=AggregateMeasure(
                            value=(
                                sum(
                                    metric.word_edits or 0
                                    for metric in word_metrics
                                )
                                / word_reference
                                if complete_ocr_evidence and word_reference
                                else None
                            ),
                            unit="ratio",
                            polarity="lower_better",
                            evidence=scored_evidence,
                        ),
                        success_rate=AggregateMeasure(
                            value=(
                                len(successes) / len(terminal_tasks)
                                if terminal_tasks
                                else None
                            ),
                            unit="ratio",
                            evidence=evidence,
                        ),
                        median_latency=AggregateMeasure(
                            value=latency_stats.p50_ms,
                            unit="milliseconds",
                            polarity="lower_better",
                            evidence=telemetry_evidence,
                        ),
                        p95_latency=AggregateMeasure(
                            value=latency_stats.p95_ms,
                            unit="milliseconds",
                            polarity="lower_better",
                            evidence=telemetry_evidence,
                        ),
                        input_tokens=AggregateMeasure(
                            value=(
                                sum(value for _, value in input_token_rows)
                                if input_token_rows
                                else None
                            ),
                            unit="count",
                            polarity="lower_better",
                            evidence=[
                                _task_evidence(task) for task, _ in input_token_rows
                            ],
                        ),
                        output_tokens=AggregateMeasure(
                            value=(
                                sum(value for _, value in output_token_rows)
                                if output_token_rows
                                else None
                            ),
                            unit="count",
                            polarity="lower_better",
                            evidence=[
                                _task_evidence(task) for task, _ in output_token_rows
                            ],
                        ),
                        total_tokens=AggregateMeasure(
                            value=(
                                sum(value for _, value in total_token_rows)
                                if total_token_rows
                                else None
                            ),
                            unit="count",
                            polarity="lower_better",
                            evidence=[
                                _task_evidence(task) for task, _ in total_token_rows
                            ],
                        ),
                        total_pages=AggregateMeasure(
                            value=(
                                sum(value for _, value in page_rows)
                                if page_rows
                                else None
                            ),
                            unit="count",
                            polarity="lower_better",
                            evidence=[
                                _task_evidence(task) for task, _ in page_rows
                            ],
                        ),
                        total_attempts=AggregateMeasure(
                            value=(
                                sum(value for _, value in attempt_rows)
                                if attempt_rows
                                else None
                            ),
                            unit="count",
                            polarity="lower_better",
                            evidence=[
                                _task_evidence(task) for task, _ in attempt_rows
                            ],
                        ),
                        estimated_cost=AggregateMeasure(
                            value=total_cost,
                            unit="usd",
                            polarity="lower_better",
                            evidence=cost_evidence,
                        ),
                        cost_scope=cost_scope,
                        compatibility=declaration.level,
                        comparable_quality=declaration.comparable_quality,
                    )
                )
                continue

            matched = wrong = partial = missing = extra = exact_matches = 0
            for task in expected_quality_tasks:
                gold = gold_by_id[task.sample_id]
                values = inputs_by_pair.get(task.pair_id)
                comparisons = (
                    _comparison_views(values[1]) if values is not None else []
                )
                if task.status != "succeeded":
                    flattened_gold = _flatten(gold)
                    missing += len(flattened_gold)
                    for raw_path in flattened_gold:
                        path = _aggregate_field_path(raw_path)
                        key = (solution.id, path)
                        accumulator = field_accumulators.setdefault(
                            key,
                            _FieldAggregateAccumulator(
                                solution_id=solution.id,
                                path=path,
                                label=path,
                            ),
                        )
                        accumulator.record(state="missing", task=task)
                    continue
                if not comparisons and _flatten(gold):
                    parsed = await self._artifact_object(task.parsed_artifact_path)
                    if parsed is None:
                        missing += len(_flatten(gold))
                        continue
                    if project.kind == "built_in":
                        from receipt_bench.projects.evaluation import evaluate_answer

                        comparisons = [
                            FieldComparisonView(
                                path=item.path,
                                label=item.path,
                                state=item.status,
                                expected=item.expected,
                                actual=item.actual,
                            )
                            for item in evaluate_answer(
                                project=self.registry.get(project.id),
                                sample_id=task.sample_id,
                                solution_id=task.solution_id,
                                gold=gold,
                                actual=parsed,
                            ).fields
                        ]
                    else:
                        comparisons = _compare_objects(gold, parsed)
                matched += sum(item.state == "matched" for item in comparisons)
                wrong += sum(item.state == "wrong" for item in comparisons)
                partial += sum(item.state == "partial" for item in comparisons)
                missing += sum(item.state == "missing" for item in comparisons)
                extra += sum(item.state == "extra" for item in comparisons)
                for comparison in comparisons:
                    path = _aggregate_field_path(comparison.path)
                    key = (solution.id, path)
                    accumulator = field_accumulators.setdefault(
                        key,
                        _FieldAggregateAccumulator(
                            solution_id=solution.id,
                            path=path,
                            label=path,
                        ),
                    )
                    accumulator.record(state=comparison.state, task=task)
                exact_matches += bool(comparisons) and all(
                    item.state == "matched" for item in comparisons
                )

            denominator = 2 * matched + 2 * (wrong + partial) + missing + extra
            field_f1 = (
                (2 * matched / denominator)
                if expected_quality_tasks and denominator > 0
                else None
            )
            exact = (
                exact_matches / len(expected_quality_tasks)
                if expected_quality_tasks
                else None
            )
            coverage_denominator = matched + wrong + partial + missing
            coverage = (
                (matched + wrong + partial) / coverage_denominator
                if coverage_denominator
                else None
            )
            evidence = [_task_evidence(item) for item in terminal_tasks]
            scored_evidence = [
                _task_evidence(item) for item in expected_quality_tasks
            ]
            solution_rows.append(
                SolutionAggregateView(
                    solution_id=solution.id,
                    normalized_field_f1=AggregateMeasure(
                        value=field_f1,
                        unit="ratio",
                        evidence=scored_evidence,
                    ),
                    whole_answer_exactness=AggregateMeasure(
                        value=exact,
                        unit="ratio",
                        evidence=scored_evidence,
                    ),
                    labeled_field_coverage=AggregateMeasure(
                        value=coverage,
                        unit="ratio",
                        evidence=scored_evidence,
                    ),
                    success_rate=AggregateMeasure(
                        value=(
                            len(successes) / len(terminal_tasks)
                            if terminal_tasks
                            else None
                        ),
                        unit="ratio",
                        evidence=evidence,
                    ),
                    median_latency=AggregateMeasure(
                        value=latency_stats.p50_ms,
                        unit="milliseconds",
                        polarity="lower_better",
                        evidence=telemetry_evidence,
                    ),
                    p95_latency=AggregateMeasure(
                        value=latency_stats.p95_ms,
                        unit="milliseconds",
                        polarity="lower_better",
                        evidence=telemetry_evidence,
                    ),
                    input_tokens=AggregateMeasure(
                        value=(
                            sum(value for _, value in input_token_rows)
                            if input_token_rows
                            else None
                        ),
                        unit="count",
                        polarity="lower_better",
                        evidence=[
                            _task_evidence(task) for task, _ in input_token_rows
                        ],
                    ),
                    output_tokens=AggregateMeasure(
                        value=(
                            sum(value for _, value in output_token_rows)
                            if output_token_rows
                            else None
                        ),
                        unit="count",
                        polarity="lower_better",
                        evidence=[
                            _task_evidence(task) for task, _ in output_token_rows
                        ],
                    ),
                    total_tokens=AggregateMeasure(
                        value=(
                            sum(value for _, value in total_token_rows)
                            if total_token_rows
                            else None
                        ),
                        unit="count",
                        polarity="lower_better",
                        evidence=[
                            _task_evidence(task) for task, _ in total_token_rows
                        ],
                    ),
                    total_pages=AggregateMeasure(
                        value=(
                            sum(value for _, value in page_rows)
                            if page_rows
                            else None
                        ),
                        unit="count",
                        polarity="lower_better",
                        evidence=[_task_evidence(task) for task, _ in page_rows],
                    ),
                    total_attempts=AggregateMeasure(
                        value=(
                            sum(value for _, value in attempt_rows)
                            if attempt_rows
                            else None
                        ),
                        unit="count",
                        polarity="lower_better",
                        evidence=[
                            _task_evidence(task) for task, _ in attempt_rows
                        ],
                    ),
                    estimated_cost=AggregateMeasure(
                        value=total_cost,
                        unit="usd",
                        polarity="lower_better",
                        evidence=cost_evidence,
                    ),
                    cost_scope=cost_scope,
                    compatibility=declaration.level,
                    comparable_quality=declaration.comparable_quality,
                )
            )
        return ProjectAggregateView(
            project_id=project.id,
            run_id=run.run_id,
            has_gold=bool(gold_by_id),
            scored_sample_count=len(gold_by_id),
            excluded_prompt_examples=sum(
                item.scoring_role == "few_shot" for item in project.samples
            ),
            solutions=solution_rows,
            fields=[
                field_accumulators[key].view()
                for key in sorted(field_accumulators)
            ],
        )

    async def aggregate(self, project_id: str, run_id: str) -> ProjectAggregateView:
        project = await self.context(project_id, require_available=True)
        run = (await self.find_run(run_id)).value
        if run.project_id != project.id:
            raise ProductError(404, "Run not found for project", code="run_not_found")
        if run.finalizer_completed:
            persisted = await self._persisted_aggregate(run)
            if persisted is not None:
                return persisted
        aggregate = await self._compute_aggregate(
            project,
            run,
            await self.tasks_for_run(run),
        )
        if run.finalizer_completed:
            cache_path = run_aggregate(run.run_id)
            payload = cast(JsonObject, aggregate.model_dump(mode="json"))
            try:
                await self.repositories.artifacts.create_json(cache_path, payload)
            except ImmutableArtifactError:
                persisted = await self._persisted_aggregate(run)
                if persisted is not None:
                    return persisted
                existing = await self.repositories.artifacts.get_json(cache_path)
                document = _aggregate_report_document(existing)
                if document is not None and document.get("format_version") != "2.0":
                    return aggregate
                raise
        return aggregate

    async def aggregate_bytes(self, project_id: str, run_id: str) -> bytes:
        project = await self.context(project_id, require_available=True)
        run = (await self.find_run(run_id)).value
        if run.project_id != project.id:
            raise ProductError(404, "Run not found for project", code="run_not_found")
        cache_path = run_aggregate(run.run_id)
        if run.finalizer_completed:
            try:
                data = await self.repositories.artifacts.get_bytes(cache_path)
            except NotFoundError:
                pass
            else:
                value = strict_json_loads(data)
                document = _aggregate_report_document(value)
                if document is not None and document.get("format_version") == "2.0":
                    try:
                        persisted = ProjectAggregateView.model_validate(document)
                    except ValidationError as exc:
                        raise ProductError(
                            500,
                            "Persisted project aggregate is invalid",
                            code="aggregate_invalid",
                        ) from exc
                    if (
                        persisted.project_id == project.id
                        and persisted.run_id == run.run_id
                    ):
                        return data
                    raise ProductError(
                        500,
                        "Persisted project aggregate belongs to another run",
                        code="aggregate_invalid",
                    )
        aggregate = await self.aggregate(project_id, run_id)
        payload = cast(JsonObject, aggregate.model_dump(mode="json"))
        data = strict_json_bytes(payload, newline=True)
        return data

    async def finalize_run(
        self,
        run: RunRecord,
        tasks: tuple[TaskRecord, ...],
    ) -> FinalizationOutput:
        project = await self.context(run.project_id, require_available=True)
        aggregate = await self._compute_aggregate(project, run, tasks)
        aggregate_payload = cast(JsonObject, aggregate.model_dump(mode="json"))
        task_views = await asyncio.gather(
            *(
                self.task_view(
                    task,
                    compact=True,
                    include_telemetry=True,
                )
                for task in tasks
            )
        )
        task_payload: JsonObject = {
            "schema_version": "2.0",
            "tasks": [task.model_dump(mode="json") for task in task_views],
        }
        await asyncio.gather(
            self.repositories.artifacts.create_json(
                run_aggregate(run.run_id),
                aggregate_payload,
            ),
            self.repositories.artifacts.create_json(
                run_tasks(run.run_id),
                task_payload,
            ),
        )
        result_index: JsonObject = {
            "run_id": run.run_id,
            "project_id": run.project_id,
            "total_tasks": run.total_tasks,
            "succeeded_tasks": run.succeeded_tasks,
            "invalid_tasks": run.invalid_tasks,
            "failed_tasks": run.failed_tasks,
            "poisoned_tasks": run.poisoned_tasks,
            "results": [
                {
                    "sample_id": task.sample_id,
                    "solution_id": task.solution_id,
                    "status": task.status,
                    "raw_artifact_path": task.raw_artifact_path,
                    "parsed_artifact_path": task.parsed_artifact_path,
                    "comparison_artifact_path": task.comparison_artifact_path,
                    "telemetry_artifact_path": task.telemetry_artifact_path,
                    "error_code": task.error_code,
                    "poisoned": task.poisoned,
                }
                for task in sorted(tasks, key=lambda item: item.pair_id)
            ],
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(
            buffer,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for name, value in (
                ("aggregate.json", aggregate_payload),
                ("results.json", result_index),
            ):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, strict_json_bytes(value, newline=True))
        return FinalizationOutput(
            aggregate=aggregate_payload,
            download_bytes=buffer.getvalue(),
        )

    async def readiness(self) -> bool:
        try:
            await self.repositories.projects.list(self.settings.owner_scope)
            self.registry.list()
            return True
        except Exception:
            return False


def _run_status(status: str) -> Literal["queued", "running", "completed", "failed"]:
    if status == "queued":
        return "queued"
    if status == "completed":
        return "completed"
    if status == "failed":
        return "failed"
    return "running"


def _task_status(task: TaskRecord) -> Literal[
    "queued", "running", "retrying", "succeeded", "failed", "invalid"
]:
    if task.status == "processing":
        return "retrying" if task.attempts > 1 else "running"
    if task.status == "queued" and task.attempts > 0:
        return "retrying"
    return cast(
        Literal["queued", "running", "retrying", "succeeded", "failed", "invalid"],
        task.status,
    )


def _task_stage(
    task: TaskRecord,
    status: str,
) -> Literal[
    "queued",
    "preflight",
    "ocr",
    "normalizing",
    "extracting",
    "evaluating",
    "persisting",
    "completed",
    "failed",
]:
    if status in {"failed", "invalid"}:
        return "failed"
    if status == "succeeded":
        return "completed"
    mapping = {
        "queued": "queued",
        "executing": "extracting",
        "parsing": "evaluating",
        "persisting": "persisting",
        "ocr": "ocr",
        "normalizing": "normalizing",
        "preflight": "preflight",
    }
    return cast(
        Literal[
            "queued",
            "preflight",
            "ocr",
            "normalizing",
            "extracting",
            "evaluating",
            "persisting",
            "completed",
            "failed",
        ],
        mapping.get(task.stage, "preflight"),
    )


def _task_progress(status: str, stage: str) -> float:
    if status in _TERMINAL_TASKS:
        return 1.0
    return {
        "queued": 0.0,
        "preflight": 0.08,
        "ocr": 0.35,
        "normalizing": 0.6,
        "extracting": 0.45,
        "evaluating": 0.75,
        "persisting": 0.9,
    }.get(stage, 0.05)


def _metadata_int(value: JsonValue, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _string(value: JsonValue, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _string_list(value: JsonValue, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a string array")
    return cast(list[str], value)


def _object_list(value: JsonValue, field: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{field} must be an object array")
    return cast(list[JsonObject], value)


def _title_from_gold(gold: JsonObject | None, fallback: str) -> str:
    if gold is None:
        return fallback
    company = gold.get("company")
    if isinstance(company, str) and company.strip():
        return company.strip()[:80]
    store = gold.get("store")
    if isinstance(store, dict):
        name = store.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()[:80]
    return fallback


def _text(value: JsonValue) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _visual_from_gold(gold: JsonObject | None, filename: str) -> ReceiptVisual:
    if gold is None:
        return ReceiptVisual(
            merchant=filename.rsplit(".", maxsplit=1)[0].replace("_", " ").upper(),
            address=["RECEIPT IMAGE"],
            date="",
            receipt_number="",
            currency="",
            lines=[],
            subtotal="",
            tax="",
            total="",
        )
    store = gold.get("store")
    store_object = store if isinstance(store, dict) else {}
    merchant = _text(gold.get("company") or store_object.get("name"))
    address_value = gold.get("address") or store_object.get("addr")
    address = [line.strip() for line in _text(address_value).splitlines() if line.strip()]
    menu = gold.get("menu")
    lines = []
    if isinstance(menu, list):
        for item in menu[:12]:
            if isinstance(item, dict):
                lines.append(
                    ReceiptLine(
                        label=_text(item.get("nm")),
                        quantity=_text(item.get("cnt") or item.get("num")),
                        amount=_text(item.get("price") or item.get("unitprice")),
                    )
                )
    subtotal = gold.get("sub_total")
    subtotal_object = subtotal if isinstance(subtotal, dict) else {}
    total = gold.get("total")
    total_object = total if isinstance(total, dict) else {}
    return ReceiptVisual(
        merchant=merchant or filename.rsplit(".", maxsplit=1)[0],
        address=address,
        date=_text(gold.get("date")),
        receipt_number="",
        currency="",
        lines=lines,
        subtotal=_text(subtotal_object.get("subtotal_price")),
        tax=_text(subtotal_object.get("tax_price")),
        total=_text(total if not isinstance(total, dict) else total_object.get("total_price")),
    )


def _label_for_path(path: str) -> str:
    raw = path.removeprefix("$.").replace(".", " · ").replace("_", " ")
    return " ".join(word.capitalize() for word in raw.split())


def _flatten(value: JsonValue, path: str = "$") -> dict[str, JsonValue]:
    if isinstance(value, dict):
        if not value:
            return {path: value}
        result: dict[str, JsonValue] = {}
        for key, child in value.items():
            result.update(_flatten(child, f"{path}.{key}"))
        return result
    if isinstance(value, list):
        if not value:
            return {path: value}
        result = {}
        for index, child in enumerate(value):
            result.update(_flatten(child, f"{path}[{index}]"))
        return result
    return {path: value}


def _aggregate_field_path(path: str) -> str:
    normalized = re.sub(r"\[\d+\]", "[]", path)
    return normalized[2:] if normalized.startswith("$.") else normalized


def _aggregate_report_document(value: JsonValue) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    report = value.get("report")
    if isinstance(report, dict):
        return report
    return value


def _normalized(value: JsonValue) -> str:
    if isinstance(value, str):
        return " ".join(value.casefold().split())
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _compare_objects(expected: JsonObject, actual: JsonObject) -> list[FieldComparisonView]:
    expected_fields = _flatten(expected)
    actual_fields = _flatten(actual)
    result = []
    for path in sorted(set(expected_fields) | set(actual_fields)):
        has_expected = path in expected_fields
        has_actual = path in actual_fields
        expected_value = expected_fields.get(path)
        actual_value = actual_fields.get(path)
        if not has_expected:
            state = "extra"
        elif not has_actual:
            state = "missing"
        elif _normalized(expected_value) == _normalized(actual_value):
            state = "matched"
        else:
            state = "wrong"
        result.append(
            FieldComparisonView(
                path=path.removeprefix("$."),
                label=_label_for_path(path),
                state=cast(Literal["matched", "wrong", "missing", "extra"], state),
                expected=expected_value,
                actual=actual_value,
            )
        )
    return result


def _ocr_text_metrics_view(value: JsonValue | None) -> OcrTextMetricsView | None:
    if not isinstance(value, dict) or value.get("metric_family") != "ocr_text":
        return None
    try:
        return OcrTextMetricsView.model_validate(value)
    except ValidationError:
        return None


def _comparison_views(value: JsonValue | None) -> list[FieldComparisonView]:
    if isinstance(value, dict):
        raw_fields = value.get("fields") or value.get("comparisons")
    else:
        raw_fields = value
    if not isinstance(raw_fields, list):
        return []
    result = []
    for item in raw_fields:
        if not isinstance(item, dict):
            continue
        path = item.get("path") or item.get("field")
        state = item.get("state") or item.get("status")
        if not isinstance(path, str) or not isinstance(state, str):
            continue
        if state == "mismatched":
            state = "wrong"
        if state not in {"matched", "partial", "wrong", "missing", "extra"}:
            continue
        label_value = item.get("label")
        label = label_value if isinstance(label_value, str) else _label_for_path(path)
        result.append(
            FieldComparisonView(
                path=path.removeprefix("$."),
                label=label,
                state=cast(
                    Literal["matched", "partial", "wrong", "missing", "extra"],
                    state,
                ),
                expected=item.get("expected"),
                actual=item.get("actual"),
            )
        )
    return sorted(result, key=lambda item: item.path)


def _schema_valid(schema: JsonObject, value: JsonObject | None) -> bool:
    if value is None:
        return False
    try:
        validator_class = jsonschema.validators.validator_for(schema)
        return bool(
            validator_class(
                schema,
                format_checker=jsonschema.FormatChecker(),
            ).is_valid(value)
        )
    except (jsonschema.exceptions.SchemaError, jsonschema.exceptions.RefResolutionError):
        return False


def _number(value: JsonValue, default: float = 0.0) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return max(float(value), 0.0)
    return default


def _optional_int(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _optional_cost(value: JsonValue) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        numeric = float(value)
        return numeric if math.isfinite(numeric) and numeric >= 0 else None
    return None


def _optional_string(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def _telemetry_view(value: JsonValue | None) -> PipelineTelemetryView:
    document = value if isinstance(value, dict) else {}
    components_value = document.get("components", [])
    components: list[PipelineComponentView] = []
    if isinstance(components_value, list):
        for index, raw in enumerate(components_value):
            if not isinstance(raw, dict):
                continue
            usage = raw.get("usage")
            usage_object = usage if isinstance(usage, dict) else {}
            identifier = raw.get("id") or raw.get("component") or f"component-{index + 1}"
            status = raw.get("status", "succeeded")
            if status not in {"succeeded", "failed"}:
                status = "failed" if raw.get("error") else "succeeded"
            components.append(
                PipelineComponentView(
                    id=str(identifier),
                    label=str(raw.get("label") or identifier).replace("_", " ").title(),
                    status=cast(Literal["succeeded", "failed"], status),
                    latency_ms=_number(raw.get("latency_ms")),
                    input_tokens=_optional_int(
                        raw.get("input_tokens", usage_object.get("input_tokens"))
                    ),
                    output_tokens=_optional_int(
                        raw.get("output_tokens", usage_object.get("output_tokens"))
                    ),
                    pages=_optional_int(raw.get("pages", usage_object.get("pages"))),
                    estimated_cost_usd=_optional_cost(
                        raw.get("estimated_cost_usd", raw.get("estimated_cost"))
                    ),
                    attempt=max(_metadata_int(raw.get("attempt"), 1), 1),
                    input=_optional_string(raw.get("input")),
                    output=_optional_string(raw.get("output")),
                    error=_optional_string(raw.get("error")),
                )
            )
    total_tokens = _optional_int(document.get("total_tokens"))
    if total_tokens is None:
        token_values = [
            (item.input_tokens or 0) + (item.output_tokens or 0) for item in components
        ]
        total_tokens = sum(token_values) if any(token_values) else None
    total_pages = _optional_int(document.get("total_pages"))
    if total_pages is None:
        pages = [item.pages for item in components if item.pages is not None]
        total_pages = sum(pages) if pages else None
    errors_value = document.get("errors", [])
    errors = (
        [item for item in errors_value if isinstance(item, str)]
        if isinstance(errors_value, list)
        else []
    )
    total_cost = _optional_cost(document.get("estimated_cost_usd"))
    if total_cost is None and components and all(
        item.estimated_cost_usd is not None for item in components
    ):
        total_cost = sum(
            item.estimated_cost_usd or 0.0 for item in components
        )
    return PipelineTelemetryView(
        components=components,
        total_tokens=total_tokens,
        total_pages=total_pages,
        latency_ms=_number(
            document.get("latency_ms"),
            sum(item.latency_ms for item in components),
        ),
        estimated_cost_usd=total_cost,
        attempts=max(_metadata_int(document.get("attempts"), 1), 1),
        errors=errors,
    )


def _evidence(result: RunResultView) -> EvidenceRef:
    return EvidenceRef(
        sample_id=result.sample_id,
        solution_id=result.solution_id,
        result_url=(
            f"/api/runs/{result.run_id}/results/{result.sample_id}/{result.solution_id}"
        ),
    )


def _task_evidence(task: TaskRecord) -> EvidenceRef:
    return EvidenceRef(
        sample_id=task.sample_id,
        solution_id=task.solution_id,
        result_url=(
            f"/api/runs/{task.run_id}/results/{task.sample_id}/{task.solution_id}"
        ),
    )


__all__ = [
    "CompatibilityContext",
    "ProductError",
    "ProductService",
    "ProjectContext",
    "SampleContext",
    "validate_custom_schema",
    "validate_gold",
]
