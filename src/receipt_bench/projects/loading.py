from __future__ import annotations

import json
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import Field, field_validator, model_validator

from receipt_bench.projects.compatibility import REGISTERED_DETERMINISTIC_MAPPING_IDS
from receipt_bench.projects.models import (
    ArtifactKind,
    ArtifactReference,
    CompatibilityLevel,
    ContractModel,
    DatasetSource,
    Identifier,
    NormalizationProfile,
    Project,
    ProjectSchema,
    PromptVersion,
    Sample,
    ScoringRole,
    SemanticVersion,
    Sha256,
    SolutionCompatibility,
    SolutionKind,
    canonical_sha256,
    ensure_json_object,
)

_BUILTIN_DEFINITION_FILES = {
    "sroie": "sroie.project.json",
    "cord-v2": "cord.project.json",
}


class SchemaFileSpec(ContractModel):
    schema_id: Identifier
    version: SemanticVersion
    path: str
    content_hash: Sha256

    @field_validator("path")
    @classmethod
    def validate_schema_path(cls, value: str) -> str:
        return ArtifactReference(kind="schema", path=value).path


class PromptFileSpec(ContractModel):
    path: str

    @field_validator("path")
    @classmethod
    def validate_prompt_path(cls, value: str) -> str:
        return ArtifactReference(kind="prompt", path=value).path


class ArtifactTemplateSpec(ContractModel):
    kind: ArtifactKind
    path_template: str
    media_type: str
    selector: str | None = None


class SampleSetSpec(ContractModel):
    prefix: Identifier
    indices: tuple[int, ...]
    id_width: int = Field(ge=1, le=12)
    split: str
    language: str
    image: ArtifactTemplateSpec
    gold: ArtifactTemplateSpec
    ocr_reference: ArtifactTemplateSpec

    @model_validator(mode="after")
    def validate_indices(self) -> SampleSetSpec:
        if not self.indices or len(set(self.indices)) != len(self.indices):
            raise ValueError("Built-in sample indices must be non-empty and unique")
        if tuple(sorted(self.indices)) != self.indices:
            raise ValueError("Built-in sample indices must be ordered")
        if any(index < 0 for index in self.indices):
            raise ValueError("Built-in sample indices cannot be negative")
        return self


class FewShotSpec(ContractModel):
    sample_id: Identifier
    reason: str = Field(min_length=1)
    image_sha256: Sha256
    gold_sha256: Sha256
    ocr_sha256: Sha256


class ExcludedSampleSpec(ContractModel):
    sample_id: Identifier
    reason: str = Field(min_length=1)
    image_sha256: Sha256
    gold_sha256: Sha256
    ocr_sha256: Sha256


class CompatibilityGroupSpec(ContractModel):
    solution_ids: tuple[Identifier, ...]
    solution_kind: SolutionKind
    level: CompatibilityLevel
    comparable_quality: bool
    reason: str
    deterministic_mapping_id: str | None = None


class BuiltInProjectSpec(ContractModel):
    project_id: Identifier
    name: str
    description: str
    source: DatasetSource
    normalization_profile: NormalizationProfile
    schema_file: SchemaFileSpec
    prompt_files: tuple[PromptFileSpec, ...]
    sample_set: SampleSetSpec
    few_shot_selection_method: str
    few_shots: tuple[FewShotSpec, ...]
    excluded_samples: tuple[ExcludedSampleSpec, ...] = ()
    compatibility_groups: tuple[CompatibilityGroupSpec, ...]
    expected_sample_count: int = Field(ge=1)
    expected_scored_sample_count: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_fixed_shape(self) -> BuiltInProjectSpec:
        if len(self.sample_set.indices) != self.expected_sample_count:
            raise ValueError("Sample index count does not match expected fixed project size")
        if len(self.few_shots) != 2:
            raise ValueError("Built-in projects require exactly two few-shot examples")
        few_shot_ids = tuple(item.sample_id for item in self.few_shots)
        excluded_ids = tuple(item.sample_id for item in self.excluded_samples)
        if len(few_shot_ids) != len(set(few_shot_ids)):
            raise ValueError("Few-shot sample IDs must be unique")
        if len(excluded_ids) != len(set(excluded_ids)):
            raise ValueError("Excluded sample IDs must be unique")
        if set(few_shot_ids) & set(excluded_ids):
            raise ValueError("Few-shot and excluded sample IDs cannot overlap")
        if (
            self.expected_scored_sample_count
            != self.expected_sample_count
            - len(self.few_shots)
            - len(self.excluded_samples)
        ):
            raise ValueError("Scored count must exclude every non-scored sample")
        unregistered_mappings = sorted(
            group.deterministic_mapping_id
            for group in self.compatibility_groups
            if group.deterministic_mapping_id is not None
            and group.deterministic_mapping_id
            not in REGISTERED_DETERMINISTIC_MAPPING_IDS
        )
        if unregistered_mappings:
            raise ValueError(
                "Unregistered deterministic mapping IDs: "
                + ", ".join(unregistered_mappings)
            )
        return self


def _read_json_object(path: Traversable) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Project contract file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in project contract {path}: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"Project contract root must be an object: {path}")
    return payload


def find_repository_root(explicit_root: Path | None = None) -> Path:
    if explicit_root is not None:
        root = explicit_root.resolve()
        if not (root / "schemas").is_dir() or not (root / "prompts").is_dir():
            raise FileNotFoundError(f"Not a receipt-benchmark repository root: {root}")
        return root

    candidates = (Path.cwd(), *Path(__file__).resolve().parents)
    for candidate in candidates:
        if (
            (candidate / "schemas").is_dir()
            and (candidate / "prompts").is_dir()
            and (candidate / "src" / "receipt_bench" / "projects").is_dir()
        ):
            return candidate
    raise FileNotFoundError(
        "Could not locate repository schemas/prompts; pass repository_root explicitly"
    )


def _artifact_from_template(
    template: ArtifactTemplateSpec,
    *,
    source_id: str,
    sample_id: str,
    source_index: int,
    sha256: str | None,
) -> ArtifactReference:
    return ArtifactReference(
        kind=template.kind,
        path=template.path_template.format(
            source_id=source_id,
            sample_id=sample_id,
            source_index=source_index,
        ),
        sha256=sha256,
        media_type=template.media_type,
        selector=template.selector,
    )


def _load_prompt(path: Traversable) -> PromptVersion:
    return PromptVersion.model_validate(_read_json_object(path))


def _resource_path(root: Traversable, portable_path: str) -> Traversable:
    return root.joinpath(*PurePosixPath(portable_path).parts)


def _load_project_contract(
    definition_path: Traversable,
    *,
    contract_root: Traversable,
) -> Project:
    spec = BuiltInProjectSpec.model_validate(_read_json_object(definition_path))

    schema_path = _resource_path(contract_root, spec.schema_file.path)
    schema_document = ensure_json_object(
        _read_json_object(schema_path), context=f"Schema {schema_path}"
    )
    calculated_schema_hash = canonical_sha256(schema_document)
    if calculated_schema_hash != spec.schema_file.content_hash:
        raise ValueError(
            f"Schema hash mismatch for {schema_path}: "
            f"declared {spec.schema_file.content_hash}, calculated {calculated_schema_hash}"
        )
    project_schema = ProjectSchema(
        schema_id=spec.schema_file.schema_id,
        version=spec.schema_file.version,
        content_hash=spec.schema_file.content_hash,
        source=ArtifactReference(
            kind="schema",
            path=spec.schema_file.path,
            media_type="application/schema+json",
        ),
        document=schema_document,
    )

    prompts = tuple(
        _load_prompt(_resource_path(contract_root, item.path))
        for item in spec.prompt_files
    )
    few_shots = {item.sample_id: item for item in spec.few_shots}
    excluded_samples = {item.sample_id: item for item in spec.excluded_samples}
    samples: list[Sample] = []
    for ordinal, source_index in enumerate(spec.sample_set.indices):
        source_id = str(source_index).zfill(spec.sample_set.id_width)
        sample_id = f"{spec.sample_set.prefix}-{source_id}"
        selected = few_shots.get(sample_id)
        excluded = excluded_samples.get(sample_id)
        metadata = selected if selected is not None else excluded
        scoring_role: ScoringRole
        if selected is not None:
            scoring_role = "few-shot"
        elif excluded is not None:
            scoring_role = "excluded"
        else:
            scoring_role = "scored"
        samples.append(
            Sample(
                project_id=spec.project_id,
                sample_id=sample_id,
                source_id=source_id,
                source_index=source_index,
                ordinal=ordinal,
                split=spec.sample_set.split,
                language=spec.sample_set.language,
                image=_artifact_from_template(
                    spec.sample_set.image,
                    source_id=source_id,
                    sample_id=sample_id,
                    source_index=source_index,
                    sha256=metadata.image_sha256 if metadata is not None else None,
                ),
                gold=_artifact_from_template(
                    spec.sample_set.gold,
                    source_id=source_id,
                    sample_id=sample_id,
                    source_index=source_index,
                    sha256=metadata.gold_sha256 if metadata is not None else None,
                ),
                ocr_reference=_artifact_from_template(
                    spec.sample_set.ocr_reference,
                    source_id=source_id,
                    sample_id=sample_id,
                    source_index=source_index,
                    sha256=metadata.ocr_sha256 if metadata is not None else None,
                ),
                scoring_role=scoring_role,
                few_shot_reason=selected.reason if selected is not None else None,
                exclusion_reason=excluded.reason if excluded is not None else None,
            )
        )
    if set(few_shots) != {
        sample.sample_id for sample in samples if sample.scoring_role == "few-shot"
    }:
        raise ValueError("Few-shot selection references a sample outside the fixed set")
    if set(excluded_samples) != {
        sample.sample_id for sample in samples if sample.scoring_role == "excluded"
    }:
        raise ValueError("Excluded-sample selection references a sample outside the fixed set")

    compatibility = tuple(
        SolutionCompatibility(
            solution_id=solution_id,
            solution_kind=group.solution_kind,
            level=group.level,
            comparable_quality=group.comparable_quality,
            reason=group.reason,
            deterministic_mapping_id=group.deterministic_mapping_id,
        )
        for group in spec.compatibility_groups
        for solution_id in group.solution_ids
    )
    return Project(
        project_id=spec.project_id,
        name=spec.name,
        description=spec.description,
        built_in=True,
        source=spec.source,
        normalization_profile=spec.normalization_profile,
        project_schema=project_schema,
        prompts=prompts,
        samples=tuple(samples),
        compatibility=compatibility,
        few_shot_selection_method=spec.few_shot_selection_method,
        expected_sample_count=spec.expected_sample_count,
        expected_scored_sample_count=spec.expected_scored_sample_count,
    )


def load_project_definition(
    definition_path: Path,
    *,
    repository_root: Path | None = None,
) -> Project:
    """Load and fully validate a project from explicit filesystem contracts."""

    root = find_repository_root(repository_root)
    return _load_project_contract(definition_path, contract_root=root)


def load_builtin_project(
    project_id: str,
    *,
    repository_root: Path | None = None,
    definitions_root: Path | None = None,
) -> Project:
    """Load a built-in contract exclusively from installed package resources.

    The optional filesystem arguments remain accepted for API compatibility but
    cannot override packaged built-in contracts.
    """

    try:
        filename = _BUILTIN_DEFINITION_FILES[project_id]
    except KeyError as exc:
        raise KeyError(f"Unknown built-in project: {project_id!r}") from exc
    _ = repository_root, definitions_root
    resource_root = files("receipt_bench.projects").joinpath("resources")
    return _load_project_contract(
        resource_root.joinpath("definitions", filename),
        contract_root=resource_root,
    )


def load_builtin_projects(
    *,
    repository_root: Path | None = None,
    definitions_root: Path | None = None,
) -> tuple[Project, ...]:
    return tuple(
        load_builtin_project(
            project_id,
            repository_root=repository_root,
            definitions_root=definitions_root,
        )
        for project_id in ("sroie", "cord-v2")
    )


__all__ = [
    "BuiltInProjectSpec",
    "find_repository_root",
    "load_builtin_project",
    "load_builtin_projects",
    "load_project_definition",
]
