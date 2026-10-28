from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import ValidationError

from receipt_bench.models import CanonicalReceipt, ComponentMeasurement, ensure_finite_numbers
from receipt_bench.projects.compatibility import (
    CORD_NATIVE_MAPPING_IDS,
    resolve_native_mapping,
)
from receipt_bench.projects.models import (
    JsonObject,
    Project,
    PromptVersion,
    SolutionCompatibility,
    ensure_json_object,
)
from receipt_bench.projects.normalization import (
    normalize_cord_content_understanding_answer,
)
from receipt_bench.projects.prompting import render_user_prompt
from receipt_bench.prompting import image_data_uri, strip_provider_nulls
from receipt_bench.providers._openai_chat import (
    ReasoningEffort,
    TokenLimitParam,
    chat_completion_text,
    json_object_response_format,
    strict_json_schema_response_format,
)
from receipt_bench.providers._openai_messages import (
    assistant_message,
    system_message,
    user_image_message,
    user_text_message,
)
from receipt_bench.providers.base import (
    InvalidSolutionOutput,
    ProviderResponseError,
    SolutionAdapter,
)
from receipt_bench.providers.content_understanding import (
    ContentUnderstandingClient,
    extract_content_understanding_fields,
    map_expense_receipt_fields,
)
from receipt_bench.providers.content_understanding_schema import (
    content_understanding_analyzer_definition,
)
from receipt_bench.providers.ocr_pipeline import OcrTextExtractor


@dataclass(frozen=True)
class RuntimePromptExample:
    sample_id: str
    answer: JsonObject
    image_bytes: bytes | None = None
    media_type: str | None = None
    ocr_text: str | None = None

    def __post_init__(self) -> None:
        ensure_finite_numbers(self.answer)
        if self.image_bytes is not None and not self.image_bytes:
            raise ValueError("Example image bytes cannot be empty")


@dataclass(frozen=True)
class ProjectExecutionContext:
    project: Project
    compatibility: SolutionCompatibility
    prompt: PromptVersion
    examples: tuple[RuntimePromptExample, ...]

    def __post_init__(self) -> None:
        if self.prompt.project_id != self.project.project_id:
            raise ValueError("Prompt and project IDs do not match")
        if self.compatibility.solution_id not in {
            declaration.solution_id for declaration in self.project.compatibility
        }:
            raise ValueError("Compatibility declaration does not belong to project")
        if tuple(example.sample_id for example in self.examples) != self.prompt.example_sample_ids:
            raise ValueError("Runtime examples must match the prompt example IDs in order")


@dataclass(frozen=True)
class ProjectRawResponse:
    raw: JsonObject
    components: tuple[ComponentMeasurement, ...]

    def __post_init__(self) -> None:
        ensure_finite_numbers(self.raw)
        ensure_finite_numbers(self.components)

    def telemetry(self) -> JsonObject:
        return {
            "components": [
                component.model_dump(mode="json") for component in self.components
            ]
        }


def parse_project_answer(
    text: str,
    schema: dict[str, Any],
    *,
    strip_synthetic_nulls: bool,
) -> JsonObject:
    if not text.strip():
        raise ValueError("Model returned an empty response")
    payload = json.loads(text)
    cleaned = (
        strip_provider_nulls(payload, schema)
        if strip_synthetic_nulls
        else payload
    )
    return ensure_json_object(cleaned, context="Project model response")


def _answer_json(answer: JsonObject) -> str:
    return json.dumps(
        answer,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _direct_messages(
    context: ProjectExecutionContext,
    image_bytes: bytes,
    media_type: str,
) -> list[ChatCompletionMessageParam]:
    messages: list[ChatCompletionMessageParam] = [
        system_message(context.prompt.system_instruction)
    ]
    for example in context.examples:
        if example.image_bytes is None or example.media_type is None:
            raise ValueError(f"Direct example {example.sample_id!r} has no image")
        messages.extend(
            [
                user_image_message(
                    "Example receipt image. Return its demonstrated project-native answer.",
                    image_data_uri(example.image_bytes, example.media_type),
                ),
                assistant_message(_answer_json(example.answer)),
            ]
        )
    messages.append(
        user_image_message(
            render_user_prompt(context.prompt, context.project.project_schema),
            image_data_uri(image_bytes, media_type),
        )
    )
    return messages


def _normalizer_messages(
    context: ProjectExecutionContext,
    ocr_text: str,
) -> list[ChatCompletionMessageParam]:
    messages: list[ChatCompletionMessageParam] = [
        system_message(context.prompt.system_instruction)
    ]
    for example in context.examples:
        if example.ocr_text is None:
            raise ValueError(f"OCR example {example.sample_id!r} has no OCR text")
        messages.extend(
            [
                user_text_message(
                    "Example OCR input:\n"
                    + example.ocr_text
                    + "\nReturn its demonstrated project-native answer."
                ),
                assistant_message(_answer_json(example.answer)),
            ]
        )
    messages.append(
        user_text_message(
            render_user_prompt(
                context.prompt,
                context.project.project_schema,
                ocr_text=ocr_text,
            )
        )
    )
    return messages


class ProjectAdapter(ABC):
    def __init__(self, solution_id: str) -> None:
        self.solution_id = solution_id

    @abstractmethod
    def execute_raw(
        self,
        context: ProjectExecutionContext,
        image_bytes: bytes,
        media_type: str,
    ) -> ProjectRawResponse: ...

    @abstractmethod
    def parse_raw(
        self,
        context: ProjectExecutionContext,
        raw: JsonObject,
    ) -> JsonObject: ...


class OpenAIProjectAdapter(ProjectAdapter):
    def __init__(
        self,
        solution_id: str,
        *,
        client: OpenAI,
        deployment: str,
        strict_schema: bool,
        token_limit_param: TokenLimitParam,
        reasoning_effort: ReasoningEffort | None,
    ) -> None:
        super().__init__(solution_id)
        self._client = client
        self._deployment = deployment
        self._strict_schema = strict_schema
        self._token_limit_param = token_limit_param
        self._reasoning_effort = reasoning_effort

    def execute_raw(
        self,
        context: ProjectExecutionContext,
        image_bytes: bytes,
        media_type: str,
    ) -> ProjectRawResponse:
        response_format = (
            strict_json_schema_response_format(context.project.project_schema.document)
            if self._strict_schema
            else json_object_response_format()
        )
        text, provider_response, measurement = chat_completion_text(
            self._client,
            deployment=self._deployment,
            messages=_direct_messages(context, image_bytes, media_type),
            response_format=response_format,
            reasoning_effort=self._reasoning_effort,
            token_limit_param=self._token_limit_param,
            component="direct_extraction",
        )
        return ProjectRawResponse(
            raw={
                "kind": "direct",
                "answer_text": text,
                "provider_response": provider_response,
            },
            components=(measurement,),
        )

    def parse_raw(
        self,
        context: ProjectExecutionContext,
        raw: JsonObject,
    ) -> JsonObject:
        text = raw.get("answer_text")
        if not isinstance(text, str):
            raise ValueError("Direct provider artifact contains no answer text")
        return parse_project_answer(
            text,
            context.project.project_schema.document,
            strip_synthetic_nulls=self._strict_schema,
        )


class OcrProjectAdapter(ProjectAdapter):
    def __init__(
        self,
        solution_id: str,
        *,
        extractor: OcrTextExtractor,
        normalizer_client: OpenAI,
        normalizer_deployment: str,
        reasoning_effort: ReasoningEffort | None,
    ) -> None:
        super().__init__(solution_id)
        self._extractor = extractor
        self._normalizer_client = normalizer_client
        self._normalizer_deployment = normalizer_deployment
        self._reasoning_effort = reasoning_effort

    def execute_raw(
        self,
        context: ProjectExecutionContext,
        image_bytes: bytes,
        media_type: str,
    ) -> ProjectRawResponse:
        extraction = self._extractor.extract_text(image_bytes, media_type)
        try:
            text, normalizer_response, normalizer_measurement = chat_completion_text(
                self._normalizer_client,
                deployment=self._normalizer_deployment,
                messages=_normalizer_messages(context, extraction.text),
                response_format=strict_json_schema_response_format(
                    context.project.project_schema.document
                ),
                reasoning_effort=self._reasoning_effort,
                token_limit_param="max_completion_tokens",
                component="normalize",
            )
        except InvalidSolutionOutput as exc:
            raise InvalidSolutionOutput(
                str(exc),
                raw_response={
                    "kind": "ocr-pipeline",
                    "ocr_text": extraction.text,
                    "ocr_response": extraction.raw_response,
                    "normalization_response": exc.raw_response,
                },
                components=[extraction.measurement, *exc.components],
            ) from exc
        except ProviderResponseError as exc:
            raise ProviderResponseError(
                f"normalize: call failed after successful OCR: {exc}",
                raw_response={
                    "kind": "ocr-pipeline",
                    "ocr_text": extraction.text,
                    "ocr_response": extraction.raw_response,
                    "normalization_response": exc.raw_response,
                },
                components=[extraction.measurement, *exc.components],
            ) from exc
        return ProjectRawResponse(
            raw={
                "kind": "ocr-pipeline",
                "ocr_text": extraction.text,
                "ocr_response": extraction.raw_response,
                "answer_text": text,
                "normalization_response": normalizer_response,
            },
            components=(extraction.measurement, normalizer_measurement),
        )

    def parse_raw(
        self,
        context: ProjectExecutionContext,
        raw: JsonObject,
    ) -> JsonObject:
        text = raw.get("answer_text")
        if not isinstance(text, str):
            raise ValueError("Normalizer artifact contains no answer text")
        return parse_project_answer(
            text,
            context.project.project_schema.document,
            strip_synthetic_nulls=True,
        )


class OcrTextProjectAdapter(ProjectAdapter):
    """Expose one OCR service without a downstream schema normalizer."""

    def __init__(
        self,
        solution_id: str,
        *,
        extractor: OcrTextExtractor,
    ) -> None:
        super().__init__(solution_id)
        self._extractor = extractor

    def execute_raw(
        self,
        context: ProjectExecutionContext,
        image_bytes: bytes,
        media_type: str,
    ) -> ProjectRawResponse:
        del context
        extraction = self._extractor.extract_text(image_bytes, media_type)
        return ProjectRawResponse(
            raw={
                "kind": "ocr-text",
                "ocr_text": extraction.text,
                "ocr_response": extraction.raw_response,
            },
            components=(extraction.measurement,),
        )

    def parse_raw(
        self,
        context: ProjectExecutionContext,
        raw: JsonObject,
    ) -> JsonObject:
        del context
        text = raw.get("ocr_text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("OCR artifact contains no text")
        return {"text": text}


class NativeNormalizerProjectAdapter(ProjectAdapter):
    """Normalize one native receipt service's canonical output into the project schema."""

    def __init__(
        self,
        solution_id: str,
        *,
        adapter: SolutionAdapter,
        normalizer_client: OpenAI,
        normalizer_deployment: str,
        reasoning_effort: ReasoningEffort | None,
    ) -> None:
        super().__init__(solution_id)
        self._adapter = adapter
        self._normalizer_client = normalizer_client
        self._normalizer_deployment = normalizer_deployment
        self._reasoning_effort = reasoning_effort

    def execute_raw(
        self,
        context: ProjectExecutionContext,
        image_bytes: bytes,
        media_type: str,
    ) -> ProjectRawResponse:
        native = self._adapter.execute(image_bytes, media_type)
        native_text = _answer_json(
            ensure_json_object(
                native.receipt.model_dump(mode="json"),
                context="Native receipt output",
            )
        )
        try:
            text, normalizer_response, normalizer_measurement = chat_completion_text(
                self._normalizer_client,
                deployment=self._normalizer_deployment,
                messages=_normalizer_messages(context, native_text),
                response_format=strict_json_schema_response_format(
                    context.project.project_schema.document
                ),
                reasoning_effort=self._reasoning_effort,
                token_limit_param="max_completion_tokens",
                component="normalize",
            )
        except InvalidSolutionOutput as exc:
            raise InvalidSolutionOutput(
                str(exc),
                raw_response={
                    "kind": "native-pipeline",
                    "native_candidate": json.loads(native_text),
                    "native_response": native.raw_response,
                    "normalization_response": exc.raw_response,
                },
                components=[*native.components, *exc.components],
            ) from exc
        except ProviderResponseError as exc:
            raise ProviderResponseError(
                f"normalize: call failed after successful native extraction: {exc}",
                raw_response={
                    "kind": "native-pipeline",
                    "native_candidate": json.loads(native_text),
                    "native_response": native.raw_response,
                    "normalization_response": exc.raw_response,
                },
                components=[*native.components, *exc.components],
            ) from exc
        return ProjectRawResponse(
            raw={
                "kind": "native-pipeline",
                "native_candidate": json.loads(native_text),
                "native_response": native.raw_response,
                "answer_text": text,
                "normalization_response": normalizer_response,
            },
            components=tuple([*native.components, normalizer_measurement]),
        )

    def parse_raw(
        self,
        context: ProjectExecutionContext,
        raw: JsonObject,
    ) -> JsonObject:
        text = raw.get("answer_text")
        if not isinstance(text, str):
            raise ValueError("Native normalizer artifact contains no answer text")
        return parse_project_answer(
            text,
            context.project.project_schema.document,
            strip_synthetic_nulls=True,
        )


class ContentUnderstandingCustomProjectAdapter(ProjectAdapter):
    """Route each built-in project to its exact-schema custom analyzer."""

    def __init__(
        self,
        solution_id: str,
        *,
        client: ContentUnderstandingClient,
        analyzer_ids: Mapping[str, str],
    ) -> None:
        super().__init__(solution_id)
        self._client = client
        self._analyzer_ids = dict(analyzer_ids)
        self._ensured_analyzers: set[str] = set()
        self._analyzer_lock = threading.Lock()
        if not self._analyzer_ids or any(
            not project_id or not analyzer_id
            for project_id, analyzer_id in self._analyzer_ids.items()
        ):
            raise ValueError("Custom analyzer routing requires non-empty project IDs")

    def execute_raw(
        self,
        context: ProjectExecutionContext,
        image_bytes: bytes,
        media_type: str,
    ) -> ProjectRawResponse:
        analyzer_id = self._analyzer_ids.get(context.project.project_id)
        if analyzer_id is None:
            if context.project.built_in:
                raise ValueError(
                    "No Content Understanding custom analyzer is configured for "
                    f"project {context.project.project_id!r}"
                )
            schema_hash = context.project.project_schema.content_hash
            analyzer_id = f"custom_{schema_hash[:24]}"
            with self._analyzer_lock:
                if analyzer_id not in self._ensured_analyzers:
                    self._client.ensure_analyzer(
                        analyzer_id,
                        content_understanding_analyzer_definition(
                            context.project.project_schema.document,
                            schema_hash=schema_hash,
                        ),
                        schema_hash=schema_hash,
                    )
                    self._ensured_analyzers.add(analyzer_id)

        fields, provider_response, components = extract_content_understanding_fields(
            self._client,
            analyzer_id,
            image_bytes,
            media_type,
        )
        try:
            if context.project.project_id == "sroie":
                receipt = CanonicalReceipt.model_validate(
                    map_expense_receipt_fields(fields)
                )
                mapping_id = context.compatibility.deterministic_mapping_id
                if mapping_id is None:
                    raise ValueError("SROIE custom extraction requires its native mapping")
                candidate = resolve_native_mapping(mapping_id)(receipt)
            else:
                candidate = ensure_json_object(
                    strip_provider_nulls(
                        fields,
                        context.project.project_schema.document,
                    ),
                    context="Content Understanding project fields",
                )
                if (
                    context.compatibility.deterministic_mapping_id
                    in CORD_NATIVE_MAPPING_IDS
                ):
                    candidate = normalize_cord_content_understanding_answer(candidate)
        except (ProviderResponseError, TypeError, ValueError, ValidationError) as exc:
            raise InvalidSolutionOutput(
                "native_extraction: custom analyzer fields do not satisfy the "
                f"{context.project.project_id} contract: {exc}",
                raw_response=provider_response,
                components=list(components),
            ) from exc
        return ProjectRawResponse(
            raw={
                "kind": "native-custom",
                "analyzer_id": analyzer_id,
                "provider_response": provider_response,
                "project_candidate": candidate,
            },
            components=components,
        )

    def delete_generated_analyzer(self, analyzer_id: str) -> None:
        if analyzer_id in self._analyzer_ids.values():
            raise ValueError("Configured built-in analyzers cannot be deleted")
        with self._analyzer_lock:
            self._client.delete_analyzer(analyzer_id)
            self._ensured_analyzers.discard(analyzer_id)

    def parse_raw(
        self,
        context: ProjectExecutionContext,
        raw: JsonObject,
    ) -> JsonObject:
        del context
        return ensure_json_object(
            raw.get("project_candidate"),
            context="Content Understanding project candidate",
        )


class NativeProjectAdapter(ProjectAdapter):
    def __init__(self, solution_id: str, *, adapter: SolutionAdapter) -> None:
        super().__init__(solution_id)
        self._adapter = adapter

    def execute_raw(
        self,
        context: ProjectExecutionContext,
        image_bytes: bytes,
        media_type: str,
    ) -> ProjectRawResponse:
        del context
        response = self._adapter.execute(image_bytes, media_type)
        return ProjectRawResponse(
            raw={
                "kind": "native",
                "provider_response": response.raw_response,
                "canonical_candidate": response.receipt.model_dump(mode="json"),
            },
            components=tuple(response.components),
        )

    def parse_raw(
        self,
        context: ProjectExecutionContext,
        raw: JsonObject,
    ) -> JsonObject:
        candidate = raw.get("canonical_candidate")
        receipt = CanonicalReceipt.model_validate(candidate)
        mapping_id = context.compatibility.deterministic_mapping_id
        if mapping_id is not None:
            return resolve_native_mapping(mapping_id)(receipt)
        return ensure_json_object(
            receipt.model_dump(mode="json"),
            context="Native reference output",
        )


def invalid_project_output(
    *,
    component: str,
    error: Exception,
    raw_response: JsonObject,
    measurements: tuple[ComponentMeasurement, ...],
) -> InvalidSolutionOutput:
    return InvalidSolutionOutput(
        f"{component}: response could not be parsed into the active project schema: {error}",
        raw_response=raw_response,
        components=list(measurements),
    )


__all__ = [
    "ContentUnderstandingCustomProjectAdapter",
    "NativeNormalizerProjectAdapter",
    "NativeProjectAdapter",
    "OcrProjectAdapter",
    "OcrTextProjectAdapter",
    "OpenAIProjectAdapter",
    "ProjectAdapter",
    "ProjectExecutionContext",
    "ProjectRawResponse",
    "RuntimePromptExample",
    "invalid_project_output",
    "parse_project_answer",
]
