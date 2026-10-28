from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from receipt_bench.models import CanonicalReceipt, StrictModel, Usage

ComparisonMode = Literal["guided", "upload"]
ExecutionMode = Literal["mock", "azure"]
PresetName = Literal["full", "fast", "custom"]
TerminalStatus = Literal["succeeded", "failed", "invalid"]


class DemoSolution(StrictModel):
    id: str
    label: str
    order: int
    kind: str
    api: str
    lifecycle: str
    model: str | None = None
    normalizer: str | None = None


class DemoPreset(StrictModel):
    id: PresetName
    label: str
    description: str
    solution_ids: list[str]


class GoldField(StrictModel):
    key: str
    field: str
    label: str
    expected: Any


class DemoReceiptSummary(StrictModel):
    id: str
    sample_id: str
    title: str
    description: str
    selection_reason: str
    image_url: str
    width: int
    height: int
    source_row_index: int
    labeled_fields: list[str]
    gold_fields: list[GoldField]


class DatasetSummary(StrictModel):
    name: str
    id: str
    revision: str
    split: str
    license: str
    license_url: str
    citation: str
    annotation_scope: str


class DemoConfigResponse(StrictModel):
    execution_mode: ExecutionMode
    is_mock: bool
    live_execution_enabled: bool
    receipts: list[DemoReceiptSummary]
    solutions: list[DemoSolution]
    presets: list[DemoPreset]
    dataset: DatasetSummary
    upload_max_bytes: int
    notices: list[str]


class RunCreateRequest(StrictModel):
    comparison_mode: ComparisonMode
    preset: PresetName = "fast"
    receipt_ids: list[str] = Field(default_factory=list, max_length=3)
    upload_tokens: list[str] = Field(default_factory=list, max_length=5)
    solution_ids: list[str] = Field(default_factory=list, max_length=15)


class RunAccepted(StrictModel):
    run_id: str
    status: Literal["running", "completed", "failed"]
    total_pairs: int
    event_url: str
    snapshot_url: str
    manifest_url: str
    results_url: str


class UploadAccepted(StrictModel):
    token: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    expires_in_seconds: int


class ErrorView(StrictModel):
    code: str
    message: str


class ComponentView(StrictModel):
    component: str
    latency_ms: float
    usage: Usage
    estimated_cost_usd: float | None = None


class FieldComparison(StrictModel):
    key: str
    field: str
    label: str
    state: Literal["matched", "missing", "mismatched", "extra"]
    expected: Any = None
    actual: Any = None
    similarity: float | None = None


class SolutionResult(StrictModel):
    receipt_id: str
    sample_id: str
    solution_id: str
    solution_order: int
    status: TerminalStatus
    mock: bool
    schema_valid: bool
    arithmetic_consistent: bool | None = None
    latency_ms: float
    estimated_cost_usd: float | None = None
    canonical_result: CanonicalReceipt | None = None
    components: list[ComponentView] = Field(default_factory=list)
    field_comparisons: list[FieldComparison] = Field(default_factory=list)
    error: ErrorView | None = None


class RunSnapshot(StrictModel):
    run_id: str
    status: Literal["running", "completed", "failed"]
    comparison_mode: ComparisonMode
    execution_mode: ExecutionMode
    mock: bool
    total_pairs: int
    completed_pairs: int
    receipt_order: list[str]
    solution_order: list[str]
    results: list[SolutionResult]
    error: ErrorView | None = None


__all__ = [
    "ComparisonMode",
    "ComponentView",
    "DatasetSummary",
    "DemoConfigResponse",
    "DemoPreset",
    "DemoReceiptSummary",
    "DemoSolution",
    "ErrorView",
    "ExecutionMode",
    "FieldComparison",
    "GoldField",
    "PresetName",
    "RunAccepted",
    "RunCreateRequest",
    "RunSnapshot",
    "SolutionResult",
    "TerminalStatus",
    "UploadAccepted",
]
