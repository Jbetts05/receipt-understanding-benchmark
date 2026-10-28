from receipt_bench.projects.compatibility import (
    default_custom_compatibility,
    map_canonical_receipt_to_sroie,
    resolve_sroie_native_mapping,
)
from receipt_bench.projects.evaluation import (
    ComparisonCounts,
    FieldComparison,
    ProjectAggregate,
    ProjectEvaluation,
    aggregate_evaluations,
    aggregate_project,
    comparable_quality_summary,
    evaluate_answer,
    evaluate_json_text,
)
from receipt_bench.projects.examples import (
    ResolvedPromptExample,
    load_gold_answer,
    load_ocr_example_text,
    resolve_prompt_examples,
)
from receipt_bench.projects.loading import (
    load_builtin_project,
    load_builtin_projects,
    load_project_definition,
)
from receipt_bench.projects.models import (
    ArtifactReference,
    DatasetSource,
    Project,
    ProjectSchema,
    PromptVersion,
    Result,
    ResultArtifacts,
    RunManifest,
    Sample,
    SolutionCompatibility,
    Task,
    canonical_sha256,
)
from receipt_bench.projects.prompting import render_user_prompt
from receipt_bench.projects.registry import ProjectRegistry

SampleMetadata = Sample

__all__ = [
    "ArtifactReference",
    "ComparisonCounts",
    "DatasetSource",
    "FieldComparison",
    "Project",
    "ProjectAggregate",
    "ProjectEvaluation",
    "ProjectRegistry",
    "ProjectSchema",
    "PromptVersion",
    "ResolvedPromptExample",
    "Result",
    "ResultArtifacts",
    "RunManifest",
    "Sample",
    "SampleMetadata",
    "SolutionCompatibility",
    "Task",
    "aggregate_evaluations",
    "aggregate_project",
    "canonical_sha256",
    "comparable_quality_summary",
    "default_custom_compatibility",
    "evaluate_answer",
    "evaluate_json_text",
    "load_builtin_project",
    "load_builtin_projects",
    "load_gold_answer",
    "load_ocr_example_text",
    "load_project_definition",
    "map_canonical_receipt_to_sroie",
    "render_user_prompt",
    "resolve_prompt_examples",
    "resolve_sroie_native_mapping",
]
