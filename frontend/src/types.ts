export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];
export interface JsonObject {
  [key: string]: JsonValue;
}

export type ProjectKind = "built_in" | "custom";
export type RightsState = "cleared" | "restricted" | "owner_managed";
export type LeakageState = "known_risk" | "test_split" | "user_supplied";
export type GoldAvailability = "available" | "none";
export type PriorRunState = "complete" | "partial" | "failed" | "never";
export type Compatibility = "full" | "partial_reference" | "unsupported";
export type SolutionKind = "direct" | "native" | "ocr_pipeline" | "ocr_text" | "native_pipeline";
/**
 * Provenance of a project's pinned schema. `dataset_derived` schemas are
 * repository-authored contracts derived from a benchmark dataset's own
 * ground-truth labels (e.g. SROIE/CORD entity or line-item fields);
 * `user_supplied` schemas are authored directly by a custom-project owner.
 */
export type SchemaOrigin = "dataset_derived" | "user_supplied";
/**
 * Comparable-quality metric families. `structured` rows are scored with
 * field-level Field F1/whole-answer exactness/labeled-field coverage; `ocr_text`
 * rows are scored against a reference-token-recall (and, for full-page OCR
 * references only, character/word error rate) family instead. The two
 * families must never be numerically compared against one another.
 */
export type MetricFamily = "structured" | "ocr_text";
export type ExecutionMode = "mock" | "live";
export type ResultStatus =
  | "queued"
  | "running"
  | "retrying"
  | "succeeded"
  | "failed"
  | "invalid";
export type TaskStage =
  | "queued"
  | "preflight"
  | "ocr"
  | "normalizing"
  | "extracting"
  | "evaluating"
  | "persisting"
  | "completed"
  | "failed";
export type ComparisonState = "matched" | "partial" | "wrong" | "missing" | "extra";
export type WorkspaceView =
  | "home"
  | "evaluation"
  | "dataset"
  | "builder"
  | "catalog"
  | "runs"
  | "reports"
  | "admin"
  | "new_project";

export interface VersionRef {
  id: string;
  version: string;
  hash: string;
}

/** Solution-mode a pinned prompt contract applies to. */
export type PromptMode = "direct-multimodal" | "ocr-normalizer";

/**
 * A single pinned prompt contract for one solution mode. A project pins one
 * `Prompt` per mode it supports (`direct-multimodal` for direct/native/
 * native_pipeline extraction, `ocr-normalizer` for the post-OCR
 * normalization stage), so the exact instructions and normalizer identity
 * behind each mode are always inspectable rather than merged into one
 * generic "prompt".
 */
export interface Prompt extends VersionRef {
  mode: PromptMode;
  instructions: string;
  example_ids: string[];
  /**
   * The user-editable template this contract was compiled from, when the
   * API distinguishes it from the final `instructions` (e.g. before schema
   * injection). `null` when no separate template is disclosed.
   */
  user_template: string | null;
  /**
   * External normalizer model backing this mode's post-extraction
   * normalization stage. `null` for `direct-multimodal` (no normalizer
   * stage) or when this contract discloses no normalizer.
   */
  normalizer_model: string | null;
}

export interface RightsNotice {
  state: RightsState;
  label: string;
  detail: string;
}

export interface LeakageNotice {
  state: LeakageState;
  label: string;
  detail: string;
}

export interface AttributionNotice {
  label: string;
  citation: string;
  license: string;
  url: string | null;
}

export interface ProjectCompatibility {
  solution_id: string;
  compatibility: Compatibility;
  comparable_quality: boolean;
  reason: string;
  deterministic_mapping_id: string | null;
}

export interface Project {
  id: string;
  name: string;
  short_name: string;
  kind: ProjectKind;
  description: string;
  sample_count: number;
  scored_sample_count: number;
  prompt_example_count: number;
  gold_sample_count: number;
  schema: VersionRef & {
    json_schema: JsonObject;
    origin: SchemaOrigin;
    /**
     * Exact, API-supplied provenance text (e.g. precise SROIE/CORD
     * ground-truth field derivation). `null` when no specific derivation
     * text is available; presentation falls back to a generic origin note.
     */
    derivation: string | null;
  };

  prompt: VersionRef & { instructions: string; example_ids: string[] };
  /**
   * Every pinned prompt contract for this project, one per solution mode.
   * Built-in projects return exact, repository-authored contracts per mode;
   * custom projects return contracts generated from the owner's single
   * authored prompt. The contract inspector renders every entry so
   * mode-specific instructions and normalizer identity are never hidden
   * behind the single generic `prompt` field above.
   */
  prompt_contracts: Prompt[];
  rights: RightsNotice;
  leakage: LeakageNotice;
  attribution: AttributionNotice;
  created_at: string;
  available: boolean;
  availability_reason: string | null;
  normalization_profile: string;
  selected_solution_ids: string[];
  compatibility: ProjectCompatibility[];
  dataset_state: "draft" | "frozen";
  dataset_revision_hash: string | null;
  import_mode: "built_in" | "manual" | "labeled_package";
  has_ocr_references: boolean;
}

export interface ReceiptLine {
  label: string;
  quantity: string;
  amount: string;
}

export interface ReceiptVisual {
  merchant: string;
  address: string[];
  date: string;
  receipt_number: string;
  currency: string;
  lines: ReceiptLine[];
  subtotal: string;
  tax: string;
  total: string;
}

export interface ReceiptSample {
  id: string;
  project_id: string;
  title: string;
  filename: string;
  source_index: number | null;
  prompt_example: boolean;
  gold_availability: GoldAvailability;
  prior_run_state: PriorRunState;
  gold: JsonObject | null;
  visual: ReceiptVisual;
  image_url: string | null;
  scoring_role: "scored" | "few_shot" | "excluded";
  exclusion_reason?: string | null;
  image_available: boolean;
}

export interface Solution {
  id: string;
  label: string;
  short_label: string;
  order: number;
  kind: SolutionKind;
  provider: string;
  lifecycle: "ga" | "preview";
  model: string | null;
  normalizer: string | null;
  /**
   * Model(s) the service itself invokes internally (e.g. a GA-constrained
   * variant, or an embedding model used for retrieval) that the API surfaces
   * for transparency. Distinct from `normalizer`: these are not a separate,
   * swappable external assistance step — they are intrinsic to how this
   * service produces its output, and are disclosed so that assistance is
   * never hidden even when there is no external normalizer.
   */
  internal_models: string[];
  /**
   * Whether this solution requires a per-project custom-analyzer
   * registration keyed to the project's pinned schema before it can run at
   * all (true only for the Content Understanding schema-configured analyzer; false for
   * every other solution, including always-available prebuilt/native
   * extractors). Supplied directly by the API; never inferred from solution
   * identity or compatibility state.
   */
  requires_project_analyzer: boolean;
  compatibility: Record<string, Compatibility>;
  comparable_quality: Record<string, boolean>;
}

export interface FieldComparison {
  path: string;
  label: string;
  state: ComparisonState;
  expected: JsonValue | undefined;
  actual: JsonValue | undefined;
}

export interface PipelineComponent {
  id: string;
  label: string;
  status: "succeeded" | "failed";
  latency_ms: number;
  input_tokens: number | null;
  output_tokens: number | null;
  pages: number | null;
  estimated_cost_usd: number | null;
  attempt: number;
  input: string | null;
  output: string | null;
  error: string | null;
}

export interface PipelineTelemetry {
  components: PipelineComponent[];
  total_tokens: number | null;
  total_pages: number | null;
  latency_ms: number;
  estimated_cost_usd: number | null;
  attempts: number;
  errors: string[];
}

/**
 * Reference-token-recall (and, for full-page OCR references only,
 * character/word error rate) metrics for a single `ocr_text` result. Mirrors
 * the backend's `OcrTextMetricsView`. `reference_kind` distinguishes a
 * full-page OCR reference (SROIE or an imported transcript) from a labeled semantic subset (CORD's
 * `valid_line`, which cannot support character/word error rate diagnostics).
 */
export interface OcrTextMetrics {
  metric_family: "ocr_text";
  normalization_id: string;
  reference_kind: "full_page" | "labeled_subset";
  reference_token_recall: number;
  matched_reference_tokens: number;
  reference_tokens: number;
  hypothesis_tokens: number;
  character_error_rate: number | null;
  character_edits: number | null;
  reference_characters: number | null;
  word_error_rate: number | null;
  word_edits: number | null;
  reference_words: number | null;
}

export interface RunResult {
  run_id: string;
  project_id: string;
  sample_id: string;
  solution_id: string;
  solution_order: number;
  status: Extract<ResultStatus, "succeeded" | "failed" | "invalid">;
  /** Null when schema validation does not apply, e.g. a standalone `ocr_text` result with no JSON schema to validate against. */
  schema_valid: boolean | null;
  metric_family: MetricFamily;
  ocr_metrics: OcrTextMetrics | null;
  raw_response: string;
  parsed_output: JsonObject | null;
  comparisons: FieldComparison[];
  telemetry: PipelineTelemetry;
  error: { code: string; message: string } | null;
  compatibility: Compatibility;
  comparable_quality: boolean;
  quality_scored: boolean;
  artifacts: Record<string, ArtifactRef>;
}

export interface ArtifactRef {
  kind: "raw" | "parsed" | "comparison" | "telemetry";
  url: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
}

export interface RunTask {
  pair_id: string;
  sample_id: string;
  solution_id: string;
  solution_order: number;
  status: ResultStatus;
  stage: TaskStage;
  progress: number;
  retries: number;
  elapsed_ms: number;
  estimated_cost_usd: number | null;
  partial_output: JsonObject | null;
  error: string | null;
  compatibility: Compatibility;
  comparable_quality: boolean;
}

export interface RunManifest {
  id: string;
  project_id: string;
  sample_ids: string[];
  solution_ids: string[];
  schema_version: string;
  prompt_version: string;
  status: "queued" | "running" | "completed" | "failed";
  total_pairs: number;
  completed_pairs: number;
  created_at: string;
  execution_mode: ExecutionMode;
  reference_only_solution_ids: string[];
}

export interface RunHistoryItem {
  run_id: string;
  project_id: string;
  status: RunManifest["status"];
  created_at: string;
  updated_at: string;
  total_pairs: number;
  completed_pairs: number;
  execution_mode: ExecutionMode | null;
  sample_count: number | null;
  solution_count: number | null;
  reference_only_count: number | null;
  download_ready: boolean;
}

export interface RunCreateRequest {
  sample_ids: string[];
  solution_ids: string[];
  schema_version: string;
  prompt_version: string;
  execution_mode: ExecutionMode;
}

export interface RunAccepted {
  run_id: string;
  status: "queued" | "running";
  total_pairs: number;
  event_url: string;
  snapshot_url: string;
  results_url: string;
  download_url: string;
}

export interface RunSnapshot {
  manifest: RunManifest;
  tasks: RunTask[];
  results: RunResult[];
  /**
   * Inclusive cursor for the newest event represented by this snapshot.
   * The server emits monotonically increasing unsigned decimal SSE IDs.
   */
  event_cursor?: string | null;
}

export type RunEvent =
  | { type: "run_started"; run: RunManifest }
  | { type: "task_started"; task: RunTask }
  | { type: "task_progress"; task: RunTask }
  | { type: "task_completed"; task: RunTask; result?: RunResult }
  | { type: "run_completed"; run: RunManifest }
  | { type: "run_failed"; run: RunManifest; message: string };

export interface EvidenceRef {
  sample_id: string;
  solution_id: string;
  result_url: string;
}

export interface AggregateMeasure {
  value: number | null;
  unit: "ratio" | "milliseconds" | "usd" | "count";
  /** Whether a higher or lower value is the better outcome. Drives default sort direction. */
  polarity: "higher_better" | "lower_better";
  evidence: EvidenceRef[];
}

export interface FieldAggregate {
  solution_id: string;
  path: string;
  label: string;
  matched: number;
  wrong: number;
  partial: number;
  missing: number;
  extra: number;
  coverage: AggregateMeasure;
  exact_match_rate: AggregateMeasure;
  normalized_field_f1: AggregateMeasure;
}

export interface SolutionAggregate {
  solution_id: string;
  metric_family: MetricFamily;
  normalized_field_f1: AggregateMeasure;
  whole_answer_exactness: AggregateMeasure;
  /** Share of gold-labeled fields this extractor emitted, regardless of value match. Only present for the `structured` family. */
  labeled_field_coverage: AggregateMeasure | null;
  /** Reference-token recall against ground-truth OCR text. Only present for the `ocr_text` family. */
  reference_token_recall: AggregateMeasure | null;
  /** Full-page OCR diagnostic. Present for SROIE and imported full-page transcripts, never CORD's labeled subset. */
  character_error_rate: AggregateMeasure | null;
  /** Full-page OCR diagnostic. Present for SROIE and imported full-page transcripts, never CORD's labeled subset. */
  word_error_rate: AggregateMeasure | null;
  success_rate: AggregateMeasure;
  median_latency: AggregateMeasure;
  p95_latency: AggregateMeasure;
  input_tokens: AggregateMeasure;
  output_tokens: AggregateMeasure;
  total_tokens: AggregateMeasure;
  total_pages: AggregateMeasure;
  total_attempts: AggregateMeasure;
  estimated_cost: AggregateMeasure;
  cost_scope: "accrued" | "complete";
  compatibility: Compatibility;
  comparable_quality: boolean;
}

export interface ProjectAggregate {
  format_version: "2.0";
  project_id: string;
  run_id: string;
  has_gold: boolean;
  scored_sample_count: number;
  excluded_prompt_examples: number;
  solutions: SolutionAggregate[];
  fields: FieldAggregate[];
}

export interface CustomImageDraft {
  id: string;
  filename: string;
  media_type: string;
  size_bytes: number;
  file: File | null;
  preview_url: string | null;
  gold_text: string;
}

export interface CustomProjectDraft {
  name: string;
  description: string;
  images: CustomImageDraft[];
  schema_text: string;
  prompt: string;
  selected_solution_ids: string[];
}

export interface CustomProjectCreatePayload {
  name: string;
  description: string;
  schema: JsonObject;
  prompt: string;
  selected_solution_ids: string[];
}

export interface HealthStatus {
  status: "ok" | "ready" | "not_ready";
  profile: "local" | "hosted";
  execution_mode: ExecutionMode;
}

export interface ServerValidationIssue {
  field: ValidationIssue["field"] | "server";
  message: string;
  image_id?: string;
}

export interface ValidationIssue {
  field: "name" | "images" | "schema" | "prompt" | "gold" | "solutions";
  message: string;
  image_id?: string;
}
