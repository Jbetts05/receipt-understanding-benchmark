from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from receipt_bench.projects.models import JsonObject, JsonValue
from receipt_bench.storage.validation import validate_id

Compatibility = Literal["full", "partial_reference", "unsupported"]
ExecutionMode = Literal["mock", "live"]
ProjectKind = Literal["built_in", "custom"]
ResultStatus = Literal["succeeded", "failed", "invalid"]
RunStatus = Literal["queued", "running", "completed", "failed"]
TaskStatus = Literal["queued", "running", "retrying", "succeeded", "failed", "invalid"]
TaskStage = Literal[
    "queued",
    "preflight",
    "ocr",
    "normalizing",
    "extracting",
    "evaluating",
    "persisting",
    "completed",
    "failed",
]


class ApiModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        populate_by_name=True,
    )


class VersionRef(ApiModel):
    id: str
    version: str
    hash: str


class SchemaView(VersionRef):
    json_schema: JsonObject
    origin: Literal["dataset_derived", "user_supplied"]
    derivation: str


class PromptView(VersionRef):
    mode: Literal["direct-multimodal", "ocr-normalizer"] = "direct-multimodal"
    instructions: str
    user_template: str = ""
    example_ids: list[str]
    normalizer_model: str | None = None


class RightsNotice(ApiModel):
    state: Literal["cleared", "restricted", "owner_managed"]
    label: str
    detail: str


class LeakageNotice(ApiModel):
    state: Literal["known_risk", "test_split", "user_supplied"]
    label: str
    detail: str


class AttributionNotice(ApiModel):
    label: str
    citation: str
    license: str
    url: str | None


class ProjectCompatibilityView(ApiModel):
    solution_id: str
    compatibility: Compatibility
    comparable_quality: bool
    reason: str
    deterministic_mapping_id: str | None = None


class ProjectView(ApiModel):
    id: str
    name: str
    short_name: str
    kind: ProjectKind
    description: str
    sample_count: int = Field(ge=0)
    scored_sample_count: int = Field(ge=0)
    prompt_example_count: int = Field(ge=0)
    gold_sample_count: int = Field(ge=0)
    schema_: SchemaView = Field(alias="schema", serialization_alias="schema")
    prompt: PromptView
    prompt_contracts: list[PromptView] = Field(default_factory=list)
    rights: RightsNotice
    leakage: LeakageNotice
    attribution: AttributionNotice
    created_at: datetime
    available: bool = True
    availability_reason: str | None = None
    normalization_profile: str
    selected_solution_ids: list[str] = Field(default_factory=list)
    compatibility: list[ProjectCompatibilityView] = Field(default_factory=list)
    dataset_state: Literal["draft", "frozen"] = "draft"
    dataset_revision_hash: str | None = None
    import_mode: Literal["built_in", "manual", "labeled_package"] = "manual"
    has_ocr_references: bool = False


class ReceiptLine(ApiModel):
    label: str
    quantity: str
    amount: str


class ReceiptVisual(ApiModel):
    merchant: str
    address: list[str]
    date: str
    receipt_number: str
    currency: str
    lines: list[ReceiptLine]
    subtotal: str
    tax: str
    total: str


class ReceiptSampleView(ApiModel):
    id: str
    project_id: str
    title: str
    filename: str
    source_index: int | None
    prompt_example: bool
    gold_availability: Literal["available", "none"]
    prior_run_state: Literal["complete", "partial", "failed", "never"] = "never"
    gold: JsonObject | None
    visual: ReceiptVisual
    image_url: str | None
    scoring_role: Literal["scored", "few_shot", "excluded"]
    exclusion_reason: str | None = None
    image_available: bool


class SolutionView(ApiModel):
    id: str
    label: str
    short_label: str
    order: int = Field(ge=0)
    kind: Literal[
        "direct",
        "native",
        "native_pipeline",
        "ocr_pipeline",
        "ocr_text",
    ]
    provider: str
    lifecycle: Literal["ga", "preview"]
    model: str | None
    normalizer: str | None
    internal_models: list[str] = Field(default_factory=list)
    requires_project_analyzer: bool = False
    compatibility: dict[str, Compatibility]
    comparable_quality: dict[str, bool]


class CustomProjectCreateRequest(ApiModel):
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(default="", max_length=2_000)
    json_schema: JsonObject = Field(alias="schema", serialization_alias="schema")
    prompt: str = Field(min_length=1, max_length=20_000)
    selected_solution_ids: list[str] = Field(min_length=1, max_length=32)

    @field_validator("name", "prompt")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value cannot be blank")
        return stripped

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str) -> str:
        return value.strip()

    @field_validator("selected_solution_ids")
    @classmethod
    def validate_solution_ids(cls, value: list[str]) -> list[str]:
        return [
            validate_id(item, field="solution_id")
            for item in value
        ]

    @model_validator(mode="after")
    def require_unique_solutions(self) -> CustomProjectCreateRequest:
        if len(set(self.selected_solution_ids)) != len(self.selected_solution_ids):
            raise ValueError("selected_solution_ids must be unique")
        return self


class RunCreateRequest(ApiModel):
    sample_ids: list[str] = Field(min_length=1, max_length=100)
    solution_ids: list[str] = Field(min_length=1, max_length=32)
    schema_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    execution_mode: ExecutionMode

    @field_validator("sample_ids", "solution_ids")
    @classmethod
    def validate_identifiers(cls, value: list[str]) -> list[str]:
        return [validate_id(item, field="selection ID") for item in value]

    @model_validator(mode="after")
    def require_unique_pairs(self) -> RunCreateRequest:
        if len(set(self.sample_ids)) != len(self.sample_ids):
            raise ValueError("sample_ids must be unique")
        if len(set(self.solution_ids)) != len(self.solution_ids):
            raise ValueError("solution_ids must be unique")
        return self


class RunAccepted(ApiModel):
    run_id: str
    status: Literal["queued", "running"]
    total_pairs: int = Field(ge=1)
    event_url: str
    snapshot_url: str
    results_url: str
    download_url: str


class RunHistoryView(ApiModel):
    run_id: str
    project_id: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    total_pairs: int = Field(ge=1)
    completed_pairs: int = Field(ge=0)
    execution_mode: ExecutionMode | None = None
    sample_count: int | None = Field(default=None, ge=0)
    solution_count: int | None = Field(default=None, ge=0)
    reference_only_count: int | None = Field(default=None, ge=0)
    download_ready: bool


class RunManifestView(ApiModel):
    id: str
    project_id: str
    sample_ids: list[str]
    solution_ids: list[str]
    schema_version: str
    prompt_version: str
    status: RunStatus
    total_pairs: int = Field(ge=1)
    completed_pairs: int = Field(ge=0)
    created_at: datetime
    execution_mode: ExecutionMode
    reference_only_solution_ids: list[str] = Field(default_factory=list)


class RunTaskView(ApiModel):
    pair_id: str
    sample_id: str
    solution_id: str
    solution_order: int = Field(ge=0)
    status: TaskStatus
    stage: TaskStage
    progress: float = Field(ge=0.0, le=1.0)
    retries: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)
    estimated_cost_usd: float | None
    partial_output: JsonObject | None
    error: str | None
    compatibility: Compatibility
    comparable_quality: bool


class FieldComparisonView(ApiModel):
    path: str
    label: str
    state: Literal["matched", "partial", "wrong", "missing", "extra"]
    expected: JsonValue = None
    actual: JsonValue = None


class PipelineComponentView(ApiModel):
    id: str
    label: str
    status: Literal["succeeded", "failed"]
    latency_ms: float = Field(ge=0.0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    pages: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)
    attempt: int = Field(default=1, ge=1)
    input: str | None = None
    output: str | None = None
    error: str | None = None


class PipelineTelemetryView(ApiModel):
    components: list[PipelineComponentView] = Field(default_factory=list)
    total_tokens: int | None = Field(default=None, ge=0)
    total_pages: int | None = Field(default=None, ge=0)
    latency_ms: float = Field(default=0.0, ge=0.0)
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)
    attempts: int = Field(default=1, ge=1)
    errors: list[str] = Field(default_factory=list)


class ErrorView(ApiModel):
    code: str
    message: str


class ArtifactView(ApiModel):
    kind: Literal["raw", "parsed", "comparison", "telemetry"]
    url: str
    content_type: str
    size_bytes: int = Field(ge=0)
    sha256: str


class OcrTextMetricsView(ApiModel):
    metric_family: Literal["ocr_text"] = "ocr_text"
    normalization_id: str
    reference_kind: Literal["full_page", "labeled_subset"]
    reference_token_recall: float = Field(ge=0.0, le=1.0)
    matched_reference_tokens: int = Field(ge=0)
    reference_tokens: int = Field(ge=1)
    hypothesis_tokens: int = Field(ge=0)
    character_error_rate: float | None = Field(default=None, ge=0.0)
    character_edits: int | None = Field(default=None, ge=0)
    reference_characters: int | None = Field(default=None, ge=1)
    word_error_rate: float | None = Field(default=None, ge=0.0)
    word_edits: int | None = Field(default=None, ge=0)
    reference_words: int | None = Field(default=None, ge=1)


class RunResultView(ApiModel):
    run_id: str
    project_id: str
    sample_id: str
    solution_id: str
    solution_order: int = Field(ge=0)
    status: ResultStatus
    schema_valid: bool | None
    metric_family: Literal["structured", "ocr_text"] = "structured"
    ocr_metrics: OcrTextMetricsView | None = None
    raw_response: str
    parsed_output: JsonObject | None
    comparisons: list[FieldComparisonView]
    telemetry: PipelineTelemetryView
    error: ErrorView | None
    compatibility: Compatibility
    comparable_quality: bool
    quality_scored: bool
    artifacts: dict[str, ArtifactView]


class RunSnapshotView(ApiModel):
    manifest: RunManifestView
    tasks: list[RunTaskView]
    results: list[RunResultView]
    event_cursor: str


class EvidenceRef(ApiModel):
    sample_id: str
    solution_id: str
    result_url: str


class AggregateMeasure(ApiModel):
    value: float | None
    unit: Literal["ratio", "milliseconds", "usd", "count"]
    polarity: Literal["higher_better", "lower_better"] = "higher_better"
    evidence: list[EvidenceRef]


class FieldAggregateView(ApiModel):
    solution_id: str
    path: str
    label: str
    matched: int = Field(ge=0)
    wrong: int = Field(ge=0)
    partial: int = Field(ge=0)
    missing: int = Field(ge=0)
    extra: int = Field(ge=0)
    coverage: AggregateMeasure
    exact_match_rate: AggregateMeasure
    normalized_field_f1: AggregateMeasure


class SolutionAggregateView(ApiModel):
    solution_id: str
    metric_family: Literal["structured", "ocr_text"] = "structured"
    normalized_field_f1: AggregateMeasure
    whole_answer_exactness: AggregateMeasure
    labeled_field_coverage: AggregateMeasure | None = None
    reference_token_recall: AggregateMeasure | None = None
    character_error_rate: AggregateMeasure | None = None
    word_error_rate: AggregateMeasure | None = None
    success_rate: AggregateMeasure
    median_latency: AggregateMeasure
    p95_latency: AggregateMeasure
    input_tokens: AggregateMeasure
    output_tokens: AggregateMeasure
    total_tokens: AggregateMeasure
    total_pages: AggregateMeasure
    total_attempts: AggregateMeasure
    estimated_cost: AggregateMeasure
    cost_scope: Literal["accrued", "complete"]
    compatibility: Compatibility
    comparable_quality: bool


class ProjectAggregateView(ApiModel):
    format_version: Literal["2.0"] = "2.0"
    project_id: str
    run_id: str
    has_gold: bool
    scored_sample_count: int = Field(ge=0)
    excluded_prompt_examples: int = Field(ge=0)
    solutions: list[SolutionAggregateView]
    fields: list[FieldAggregateView]


class HealthView(ApiModel):
    status: Literal["ok", "ready", "not_ready"]
    profile: Literal["local", "hosted"]
    execution_mode: ExecutionMode


__all__ = [
    "AggregateMeasure",
    "ApiModel",
    "ArtifactView",
    "AttributionNotice",
    "Compatibility",
    "CustomProjectCreateRequest",
    "ErrorView",
    "EvidenceRef",
    "ExecutionMode",
    "FieldAggregateView",
    "FieldComparisonView",
    "HealthView",
    "LeakageNotice",
    "OcrTextMetricsView",
    "PipelineComponentView",
    "PipelineTelemetryView",
    "ProjectAggregateView",
    "ProjectCompatibilityView",
    "ProjectView",
    "PromptView",
    "ReceiptSampleView",
    "ReceiptVisual",
    "RightsNotice",
    "RunAccepted",
    "RunCreateRequest",
    "RunHistoryView",
    "RunManifestView",
    "RunResultView",
    "RunSnapshotView",
    "RunTaskView",
    "SchemaView",
    "SolutionAggregateView",
    "SolutionView",
    "VersionRef",
]
