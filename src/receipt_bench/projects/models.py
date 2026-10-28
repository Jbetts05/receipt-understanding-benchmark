from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from receipt_bench.models import ensure_finite_numbers

type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]*$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SemanticVersion = Annotated[
    str,
    StringConstraints(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"),
]

PromptMode = Literal["direct-multimodal", "ocr-normalizer"]
NormalizationProfile = Literal["exact-v1", "sroie-v1", "cord-v2-v1"]
CompatibilityLevel = Literal["full", "partial-reference", "unsupported"]
SolutionKind = Literal[
    "direct-multimodal",
    "native-normalizer",
    "ocr-normalizer",
    "ocr-text",
    "native-custom",
    "native-prebuilt",
]
ScoringRole = Literal["scored", "few-shot", "excluded"]

_HASH_KEYS = frozenset({"content_hash"})


def canonical_sha256(value: JsonValue) -> str:
    """Hash JSON by value, independent of source-file whitespace or key order."""

    ensure_finite_numbers(value)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def content_hash(payload: Mapping[str, Any]) -> str:
    semantic_payload = {
        key: value for key, value in payload.items() if key not in _HASH_KEYS
    }
    return canonical_sha256(semantic_payload)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def reject_nested_non_finite_numbers(self) -> Self:
        ensure_finite_numbers(self)
        return self


ArtifactKind = Literal[
    "image",
    "gold",
    "ocr-reference",
    "schema",
    "prompt",
    "raw-response",
    "parsed-answer",
    "comparison",
    "telemetry",
]


class ArtifactReference(ContractModel):
    kind: ArtifactKind
    path: str = Field(min_length=1)
    sha256: Sha256 | None = None
    media_type: str | None = None
    selector: str | None = None

    @field_validator("path")
    @classmethod
    def require_portable_relative_path(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("Artifact paths must use portable '/' separators")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Artifact paths must be repository-relative and cannot contain '..'")
        if "." in path.parts:
            raise ValueError("Artifact paths cannot contain '.' segments")
        return value


class DatasetSource(ContractModel):
    dataset_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    split: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    license: str = Field(min_length=1)
    attribution: str = Field(min_length=1)
    redistribution: Literal["open", "gated-no-redistribution"]


class Sample(ContractModel):
    project_id: Identifier
    sample_id: Identifier
    source_id: str = Field(min_length=1)
    source_index: int = Field(ge=0)
    ordinal: int = Field(ge=0)
    split: str = Field(min_length=1)
    language: str = Field(min_length=2)
    image: ArtifactReference
    gold: ArtifactReference
    ocr_reference: ArtifactReference | None = None
    scoring_role: ScoringRole = "scored"
    few_shot_reason: str | None = None
    exclusion_reason: str | None = None

    @model_validator(mode="after")
    def validate_role_metadata(self) -> Self:
        if self.image.kind != "image" or self.gold.kind != "gold":
            raise ValueError("Sample image/gold references must use their matching artifact kinds")
        if self.ocr_reference is not None and self.ocr_reference.kind != "ocr-reference":
            raise ValueError("Sample OCR reference must use kind 'ocr-reference'")
        if self.scoring_role == "few-shot":
            if not self.few_shot_reason:
                raise ValueError("Few-shot samples require a selection reason")
            if self.exclusion_reason is not None:
                raise ValueError("Few-shot samples cannot carry an exclusion reason")
        elif self.scoring_role == "excluded":
            if not self.exclusion_reason:
                raise ValueError("Excluded samples require an exclusion reason")
            if self.few_shot_reason is not None:
                raise ValueError("Excluded samples cannot carry a few-shot selection reason")
        elif self.few_shot_reason is not None or self.exclusion_reason is not None:
            raise ValueError("Scored samples cannot carry a non-scoring reason")

        if self.scoring_role != "scored":
            references = (self.image, self.gold, self.ocr_reference)
            if any(reference is None or reference.sha256 is None for reference in references):
                raise ValueError(
                    "Non-scored image, gold, and OCR references require SHA-256 checksums"
                )
        return self


class ProjectSchema(ContractModel):
    schema_id: Identifier
    version: SemanticVersion
    content_hash: Sha256
    source: ArtifactReference
    document: JsonObject

    @model_validator(mode="after")
    def verify_content_hash(self) -> Self:
        if self.source.kind != "schema":
            raise ValueError("Project schema source must use kind 'schema'")
        calculated = canonical_sha256(self.document)
        if self.content_hash != calculated:
            raise ValueError(
                f"Schema hash mismatch: declared {self.content_hash}, calculated {calculated}"
            )
        return self


class PromptVersion(ContractModel):
    prompt_id: Identifier
    project_id: Identifier
    schema_id: Identifier
    mode: PromptMode
    version: SemanticVersion
    system_instruction: str = Field(min_length=1)
    user_template: str = Field(min_length=1)
    example_sample_ids: tuple[Identifier, ...]
    content_hash: Sha256

    @model_validator(mode="after")
    def verify_prompt_contract(self) -> Self:
        if len(set(self.example_sample_ids)) != len(self.example_sample_ids):
            raise ValueError("Prompt example sample IDs must be unique")
        if "{schema_json}" not in self.user_template:
            raise ValueError("Prompt user template must contain {schema_json}")
        has_ocr_slot = "{ocr_text}" in self.user_template
        if has_ocr_slot != (self.mode == "ocr-normalizer"):
            raise ValueError("Only OCR-normalizer templates must contain {ocr_text}")
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        calculated = canonical_sha256(payload)
        if self.content_hash != calculated:
            raise ValueError(
                f"Prompt hash mismatch: declared {self.content_hash}, calculated {calculated}"
            )
        return self


class SolutionCompatibility(ContractModel):
    solution_id: Identifier
    solution_kind: SolutionKind
    level: CompatibilityLevel
    comparable_quality: bool
    reason: str = Field(min_length=1)
    deterministic_mapping_id: str | None = None

    @model_validator(mode="after")
    def validate_comparability(self) -> Self:
        if self.comparable_quality != (self.level == "full"):
            raise ValueError("Only full compatibility can be included in comparable quality")
        if (
            self.solution_kind in {"native-custom", "native-prebuilt"}
            and self.level == "full"
            and not self.deterministic_mapping_id
        ):
            raise ValueError(
                "Fully compatible native solutions require a deterministic mapping"
            )
        if self.level != "full" and self.deterministic_mapping_id is not None:
            raise ValueError("Reference/unsupported declarations cannot claim a complete mapping")
        return self


class Project(ContractModel):
    project_id: Identifier
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    built_in: bool
    source: DatasetSource
    normalization_profile: NormalizationProfile
    project_schema: ProjectSchema
    prompts: tuple[PromptVersion, ...]
    samples: tuple[Sample, ...]
    compatibility: tuple[SolutionCompatibility, ...]
    few_shot_selection_method: str | None = None
    expected_sample_count: int | None = Field(default=None, ge=1)
    expected_scored_sample_count: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_project_contract(self) -> Self:
        sample_ids = [sample.sample_id for sample in self.samples]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("Project sample IDs must be unique")
        if [sample.ordinal for sample in self.samples] != list(range(len(self.samples))):
            raise ValueError("Project sample ordinals must be contiguous and ordered")
        if any(sample.project_id != self.project_id for sample in self.samples):
            raise ValueError("Every sample must belong to its containing project")

        if (
            self.expected_sample_count is not None
            and len(self.samples) != self.expected_sample_count
        ):
            raise ValueError("Project sample count does not match its fixed contract")
        scored = tuple(sample for sample in self.samples if sample.scoring_role == "scored")
        if (
            self.expected_scored_sample_count is not None
            and len(scored) != self.expected_scored_sample_count
        ):
            raise ValueError("Project scored-sample count does not match its fixed contract")

        few_shot_ids = tuple(
            sample.sample_id for sample in self.samples if sample.scoring_role == "few-shot"
        )
        if self.built_in and (
            len(few_shot_ids) != 2 or not self.few_shot_selection_method
        ):
            raise ValueError(
                "Built-in projects require two examples and a recorded selection method"
            )
        modes = [prompt.mode for prompt in self.prompts]
        if len(modes) != len(set(modes)):
            raise ValueError("A project cannot contain duplicate prompt modes")
        for prompt in self.prompts:
            if prompt.project_id != self.project_id:
                raise ValueError("Every prompt must belong to its containing project")
            if prompt.schema_id != self.project_schema.schema_id:
                raise ValueError("Every prompt must reference the active project schema")
            if prompt.example_sample_ids != few_shot_ids:
                raise ValueError("Prompt examples must exactly match ordered few-shot samples")

        solution_ids = [declaration.solution_id for declaration in self.compatibility]
        if len(solution_ids) != len(set(solution_ids)):
            raise ValueError("Solution compatibility declarations must be unique")
        return self

    @property
    def few_shot_samples(self) -> tuple[Sample, ...]:
        return tuple(sample for sample in self.samples if sample.scoring_role == "few-shot")

    @property
    def excluded_samples(self) -> tuple[Sample, ...]:
        return tuple(sample for sample in self.samples if sample.scoring_role == "excluded")

    @property
    def excluded_sample_ids(self) -> tuple[str, ...]:
        return tuple(sample.sample_id for sample in self.excluded_samples)

    @property
    def scored_samples(self) -> tuple[Sample, ...]:
        return tuple(sample for sample in self.samples if sample.scoring_role == "scored")

    @property
    def scored_sample_ids(self) -> tuple[str, ...]:
        return tuple(sample.sample_id for sample in self.scored_samples)

    def sample(self, sample_id: str) -> Sample:
        for sample in self.samples:
            if sample.sample_id == sample_id:
                return sample
        raise KeyError(f"Unknown sample {sample_id!r} for project {self.project_id!r}")

    def prompt(self, mode: PromptMode) -> PromptVersion:
        for prompt in self.prompts:
            if prompt.mode == mode:
                return prompt
        raise KeyError(f"Project {self.project_id!r} has no {mode!r} prompt")

    def solution_compatibility(self, solution_id: str) -> SolutionCompatibility:
        for declaration in self.compatibility:
            if declaration.solution_id == solution_id:
                return declaration
        raise KeyError(
            f"Project {self.project_id!r} has no compatibility declaration for {solution_id!r}"
        )


class VersionReference(ContractModel):
    identifier: Identifier
    version: SemanticVersion
    content_hash: Sha256


class RunSolution(ContractModel):
    solution_id: Identifier
    compatibility: CompatibilityLevel
    comparable_quality: bool
    deterministic_mapping_id: str | None = None


class RunManifest(ContractModel):
    run_id: Identifier
    project_id: Identifier
    created_at: datetime
    schema_ref: VersionReference
    prompts: tuple[VersionReference, ...]
    sample_ids: tuple[Identifier, ...]
    solutions: tuple[RunSolution, ...]

    @field_validator("created_at")
    @classmethod
    def require_aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Run timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def require_unique_nonempty_pairs(self) -> Self:
        if not self.sample_ids or len(set(self.sample_ids)) != len(self.sample_ids):
            raise ValueError("Run sample IDs must be non-empty and unique")
        solution_ids = [solution.solution_id for solution in self.solutions]
        if not solution_ids or len(set(solution_ids)) != len(solution_ids):
            raise ValueError("Run solution IDs must be non-empty and unique")
        if len({prompt.identifier for prompt in self.prompts}) != len(self.prompts):
            raise ValueError("Run prompt references must be unique")
        return self

    @classmethod
    def for_project(
        cls,
        *,
        run_id: str,
        project: Project,
        created_at: datetime,
        sample_ids: Sequence[str],
        solution_ids: Sequence[str],
    ) -> RunManifest:
        requested_samples = tuple(sample_ids)
        for sample_id in requested_samples:
            role = project.sample(sample_id).scoring_role
            if role != "scored":
                raise ValueError(
                    f"Non-scored sample {sample_id!r} ({role}) cannot be scored in a run"
                )

        solutions: list[RunSolution] = []
        for solution_id in solution_ids:
            declaration = project.solution_compatibility(solution_id)
            if declaration.level == "unsupported":
                raise ValueError(
                    f"Solution {solution_id!r} is unsupported for project {project.project_id!r}"
                )
            solutions.append(
                RunSolution(
                    solution_id=solution_id,
                    compatibility=declaration.level,
                    comparable_quality=declaration.comparable_quality,
                    deterministic_mapping_id=declaration.deterministic_mapping_id,
                )
            )

        return cls(
            run_id=run_id,
            project_id=project.project_id,
            created_at=created_at,
            schema_ref=VersionReference(
                identifier=project.project_schema.schema_id,
                version=project.project_schema.version,
                content_hash=project.project_schema.content_hash,
            ),
            prompts=tuple(
                VersionReference(
                    identifier=prompt.prompt_id,
                    version=prompt.version,
                    content_hash=prompt.content_hash,
                )
                for prompt in project.prompts
            ),
            sample_ids=requested_samples,
            solutions=tuple(solutions),
        )


TaskStatus = Literal["queued", "leased", "running", "succeeded", "failed"]
TaskStage = Literal["pending", "provider", "ocr", "normalizer", "evaluation", "terminal"]


class Task(ContractModel):
    task_id: str = Field(min_length=3)
    run_id: Identifier
    project_id: Identifier
    sample_id: Identifier
    solution_id: Identifier
    status: TaskStatus
    stage: TaskStage
    attempts: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    lease_id: str | None = None
    error_type: str | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Task timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_task_state(self) -> Self:
        expected_id = f"{self.sample_id}|{self.solution_id}"
        if self.task_id != expected_id:
            raise ValueError(f"Task ID must be the stable pair key {expected_id!r}")
        if self.updated_at < self.created_at:
            raise ValueError("Task update cannot precede its creation")
        terminal = self.status in {"succeeded", "failed"}
        if terminal != (self.stage == "terminal"):
            raise ValueError("Only terminal tasks can use the terminal stage")
        if self.status in {"leased", "running"} and not self.lease_id:
            raise ValueError("Leased/running tasks require a lease ID")
        return self


class ResultArtifacts(ContractModel):
    raw: ArtifactReference | None = None
    parsed: ArtifactReference | None = None
    comparison: ArtifactReference | None = None
    telemetry: ArtifactReference

    @model_validator(mode="after")
    def validate_artifact_kinds(self) -> Self:
        expected = {
            "raw": "raw-response",
            "parsed": "parsed-answer",
            "comparison": "comparison",
            "telemetry": "telemetry",
        }
        for name, kind in expected.items():
            reference = getattr(self, name)
            if reference is not None and reference.kind != kind:
                raise ValueError(f"Result artifact {name!r} must use kind {kind!r}")
        return self


ResultStatus = Literal["succeeded", "failed", "unparseable"]


class Result(ContractModel):
    run_id: Identifier
    project_id: Identifier
    sample_id: Identifier
    solution_id: Identifier
    status: ResultStatus
    attempts: int = Field(ge=1)
    started_at: datetime
    completed_at: datetime
    artifacts: ResultArtifacts
    error_type: str | None = None
    error_message: str | None = None

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Result timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_result_state(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("Result completion cannot precede its start")
        if self.status == "succeeded":
            if self.artifacts.parsed is None or self.artifacts.comparison is None:
                raise ValueError("Successful results require parsed and comparison artifacts")
            if self.error_type is not None or self.error_message is not None:
                raise ValueError("Successful results cannot carry an error")
        elif not self.error_type:
            raise ValueError("Failed/unparseable results require an error type")
        return self


def ensure_json_object(value: Any, *, context: str) -> JsonObject:
    """Validate a runtime value as finite JSON and return an explicitly typed object."""

    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"{context} must be a JSON object")

    def validate(item: Any, path: str) -> JsonValue:
        if item is None or isinstance(item, str | bool | int):
            return item
        if isinstance(item, float):
            ensure_finite_numbers(item)
            return item
        if isinstance(item, list):
            return [validate(child, f"{path}[{index}]") for index, child in enumerate(item)]
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            return {key: validate(child, f"{path}.{key}") for key, child in item.items()}
        raise TypeError(f"{context} contains a non-JSON value at {path}")

    return {key: validate(child, f"$.{key}") for key, child in value.items()}


__all__ = [
    "ArtifactReference",
    "CompatibilityLevel",
    "ContractModel",
    "DatasetSource",
    "JsonObject",
    "JsonPrimitive",
    "JsonValue",
    "NormalizationProfile",
    "Project",
    "ProjectSchema",
    "PromptMode",
    "PromptVersion",
    "Result",
    "ResultArtifacts",
    "RunManifest",
    "Sample",
    "ScoringRole",
    "SolutionCompatibility",
    "SolutionKind",
    "Task",
    "VersionReference",
    "canonical_sha256",
    "content_hash",
    "ensure_json_object",
]
