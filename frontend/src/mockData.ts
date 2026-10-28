import { compareProjectJson, metricFamilyForSolution } from "./domain";
import type {
  JsonObject,
  OcrTextMetrics,
  PipelineComponent,
  Project,
  ReceiptSample,
  RunResult,
  Solution,
} from "./types";

export const MOCK_RUN_ID = "offline-run-001";

const demoSchema: JsonObject = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  type: "object",
  additionalProperties: false,
  required: ["vendor", "date", "total"],
  properties: {
    vendor: { type: "string" },
    date: { type: "string" },
    total: { type: "number" },
  },
};

const compatibility = [
  {
    solution_id: "offline-direct",
    compatibility: "full" as const,
    comparable_quality: true,
    reason: "The offline fixture emits the project schema.",
    deterministic_mapping_id: null,
  },
  {
    solution_id: "offline-ocr",
    compatibility: "full" as const,
    comparable_quality: true,
    reason: "The offline OCR fixture is normalized to the project schema.",
    deterministic_mapping_id: null,
  },
  {
    solution_id: "offline-reference",
    compatibility: "partial_reference" as const,
    comparable_quality: false,
    reason: "The native response has no deterministic schema mapping.",
    deterministic_mapping_id: null,
  },
  {
    solution_id: "offline-unsupported",
    compatibility: "unsupported" as const,
    comparable_quality: false,
    reason: "This analyzer requires a project-specific registration.",
    deterministic_mapping_id: null,
  },
  {
    solution_id: "offline-ocr-text",
    compatibility: "full" as const,
    comparable_quality: true,
    reason: "Standalone OCR text output, scored for reference-token recall.",
    deterministic_mapping_id: null,
  },
  {
    solution_id: "offline-native-measurable",
    compatibility: "full" as const,
    comparable_quality: true,
    reason: "This prebuilt analyzer's output is deterministically mapped to the project schema; unmapped fields count as misses.",
    deterministic_mapping_id: "offline-native-measurable-mapping",
  },
];

export const mockProjects: Project[] = [
  {
    id: "offline-demo",
    name: "Offline receipt demo",
    short_name: "DEMO",
    kind: "built_in",
    description: "Synthetic receipts available only in explicit offline mode.",
    sample_count: 2,
    scored_sample_count: 1,
    prompt_example_count: 0,
    gold_sample_count: 1,
    schema: {
      id: "offline-schema",
      version: "1.0.0",
      hash: "sha256:offline-schema",
      json_schema: demoSchema,
      origin: "dataset_derived",
      derivation:
        "Derived from this offline fixture's ground-truth labels for vendor, date, and total, standing in for the SROIE/CORD-style entity labels a real built-in dataset schema is derived from.",
    },
    prompt: {
      id: "offline-prompt",
      version: "1.0.0",
      hash: "sha256:offline-prompt",
      instructions: "Extract the synthetic vendor, date, and total.",
      example_ids: [],
    },
    prompt_contracts: [
      {
        id: "offline-prompt-direct",
        version: "1.0.0",
        hash: "sha256:offline-prompt-direct",
        mode: "direct-multimodal",
        instructions:
          "Extract the synthetic vendor, date, and total directly from the receipt image and emit the pinned JSON Schema.",
        example_ids: [],
        user_template: "Extract vendor, date, and total from this receipt image.",
        normalizer_model: null,
      },
      {
        id: "offline-prompt-ocr-normalizer",
        version: "1.0.0",
        hash: "sha256:offline-prompt-ocr-normalizer",
        mode: "ocr-normalizer",
        instructions:
          "Given the raw OCR transcription of a synthetic receipt, normalize the vendor, date, and total into the pinned JSON Schema.",
        example_ids: [],
        user_template: "Normalize this OCR transcription into vendor, date, and total.",
        normalizer_model: "gpt-5.6-luna",
      },
    ],
    rights: {
      state: "owner_managed",
      label: "Synthetic fixture",
      detail: "No third-party receipt data is embedded in the frontend.",
    },
    leakage: {
      state: "user_supplied",
      label: "Offline fixture",
      detail: "This fixture is not a benchmark.",
    },
    attribution: {
      label: "Receipt laboratory",
      citation: "Synthetic offline fixture",
      license: "Internal demo",
      url: null,
    },
    created_at: "2026-08-30T00:00:00Z",
    available: true,
    availability_reason: null,
    normalization_profile: "custom",
    selected_solution_ids: [
      "offline-direct",
      "offline-ocr",
      "offline-reference",
    ],
    compatibility,
    dataset_state: "frozen",
    dataset_revision_hash: "offline-demo-revision",
    import_mode: "built_in",
    has_ocr_references: true,
  },
];

export const mockSolutions: Solution[] = [
  {
    id: "offline-direct",
    label: "Offline direct",
    short_label: "Direct",
    order: 0,
    kind: "direct",
    provider: "Offline fixture",
    lifecycle: "ga",
    model: "synthetic-direct",
    normalizer: null,
    internal_models: [],
    requires_project_analyzer: false,
    compatibility: { "offline-demo": "full" },
    comparable_quality: { "offline-demo": true },
  },
  {
    id: "offline-ocr",
    label: "Offline OCR pipeline + GPT-5.6 Luna",
    short_label: "OCR + Luna",
    order: 1,
    kind: "ocr_pipeline",
    provider: "Offline fixture",
    lifecycle: "ga",
    model: "synthetic-ocr",
    normalizer: "gpt-5.6-luna",
    internal_models: [],
    requires_project_analyzer: false,
    compatibility: { "offline-demo": "full" },
    comparable_quality: { "offline-demo": true },
  },
  {
    id: "offline-reference",
    label: "Offline native reference",
    short_label: "Reference",
    order: 2,
    kind: "native",
    provider: "Offline fixture",
    lifecycle: "preview",
    model: null,
    normalizer: null,
    internal_models: ["gpt-5.4-mini"],
    requires_project_analyzer: false,
    compatibility: { "offline-demo": "partial_reference" },
    comparable_quality: { "offline-demo": false },
  },
  {
    id: "offline-unsupported",
    label: "Unavailable custom analyzer",
    short_label: "Unavailable",
    order: 3,
    kind: "native",
    provider: "Offline fixture",
    lifecycle: "preview",
    model: null,
    normalizer: null,
    internal_models: [],
    requires_project_analyzer: true,
    compatibility: { "offline-demo": "unsupported" },
    comparable_quality: { "offline-demo": false },
  },
  {
    id: "offline-ocr-text",
    label: "Offline OCR text",
    short_label: "OCR text",
    order: 4,
    kind: "ocr_text",
    provider: "Offline fixture",
    lifecycle: "ga",
    model: "synthetic-ocr",
    normalizer: null,
    internal_models: [],
    requires_project_analyzer: false,
    compatibility: { "offline-demo": "full" },
    comparable_quality: { "offline-demo": true },
  },
  {
    id: "offline-native-measurable",
    label: "Offline native prebuilt",
    short_label: "Prebuilt",
    order: 5,
    kind: "native",
    provider: "Offline fixture",
    lifecycle: "ga",
    model: "synthetic-prebuilt-receipt",
    normalizer: null,
    internal_models: ["gpt-5.2", "text-embedding-3-large"],
    requires_project_analyzer: false,
    compatibility: { "offline-demo": "full" },
    comparable_quality: { "offline-demo": true },
  },
  {
    id: "offline-native-pipeline",
    label: "Offline native pipeline + GPT-5.6 Luna",
    short_label: "Native + Luna",
    order: 6,
    kind: "native_pipeline",
    provider: "Offline fixture",
    lifecycle: "ga",
    model: "synthetic-native-pipeline",
    normalizer: "gpt-5.6-luna",
    internal_models: [],
    requires_project_analyzer: false,
    compatibility: { "offline-demo": "full" },
    comparable_quality: { "offline-demo": true },
  },
];

export const mockSamples: ReceiptSample[] = [
  {
    id: "offline-001",
    project_id: "offline-demo",
    title: "North Market",
    filename: "synthetic-north-market.svg",
    source_index: null,
    prompt_example: false,
    gold_availability: "available",
    prior_run_state: "complete",
    gold: { vendor: "North Market", date: "2026-08-30", total: 24.8 },
    visual: {
      merchant: "NORTH MARKET",
      address: ["SYNTHETIC RECEIPT"],
      date: "2026-08-30",
      receipt_number: "DEMO-001",
      currency: "USD",
      lines: [{ label: "LUNCH", quantity: "1", amount: "24.80" }],
      subtotal: "22.96",
      tax: "1.84",
      total: "24.80",
    },
    image_url: "/offline-receipt.svg",
    scoring_role: "scored",
    image_available: true,
  },
  {
    id: "offline-002",
    project_id: "offline-demo",
    title: "Unscored upload",
    filename: "synthetic-unscored.svg",
    source_index: null,
    prompt_example: false,
    gold_availability: "none",
    prior_run_state: "never",
    gold: null,
    visual: {
      merchant: "UNSCORED",
      address: ["SYNTHETIC RECEIPT"],
      date: "",
      receipt_number: "DEMO-002",
      currency: "USD",
      lines: [],
      subtotal: "",
      tax: "",
      total: "",
    },
    image_url: "/offline-receipt.svg",
    scoring_role: "scored",
    image_available: true,
  },
];

function component(
  id: string,
  label: string,
  latencyMs: number,
  output: JsonObject,
): PipelineComponent {
  return {
    id,
    label,
    status: "succeeded",
    latency_ms: latencyMs,
    input_tokens: id === "ocr" ? null : 420,
    output_tokens: id === "ocr" ? null : 84,
    pages: 1,
    estimated_cost_usd: id === "ocr" ? 0.0004 : 0.0012,
    attempt: 1,
    input: id === "ocr" ? "Synthetic receipt image" : "Synthetic OCR text",
    output: JSON.stringify(output, null, 2),
    error: null,
  };
}

/**
 * Synthetic reference-token-recall (and, on a full-page reference such as
 * SROIE, character/word error rate) metrics for the standalone `ocr_text`
 * fixture solution. CORD's `valid_line` reference is a labeled semantic
 * subset rather than full-page OCR, so a `labeled_subset` result omits the
 * character/word diagnostics entirely (`null`), matching the backend's
 * `evaluate_ocr_text` behavior.
 */
function ocrTextMetrics(sampleId: string): OcrTextMetrics {
  const referenceTokens = 50;
  const matchedTokens = sampleId === "offline-001" ? 46 : 41;
  if (sampleId !== "offline-001") {
    return {
      metric_family: "ocr_text",
      normalization_id: "receipt-ocr-text-v1",
      reference_kind: "labeled_subset",
      reference_token_recall: matchedTokens / referenceTokens,
      matched_reference_tokens: matchedTokens,
      reference_tokens: referenceTokens,
      hypothesis_tokens: 48,
      character_error_rate: null,
      character_edits: null,
      reference_characters: null,
      word_error_rate: null,
      word_edits: null,
      reference_words: null,
    };
  }
  return {
    metric_family: "ocr_text",
    normalization_id: "receipt-ocr-text-v1",
    reference_kind: "full_page",
    reference_token_recall: matchedTokens / referenceTokens,
    matched_reference_tokens: matchedTokens,
    reference_tokens: referenceTokens,
    hypothesis_tokens: 48,
    character_error_rate: 0.04,
    character_edits: 12,
    reference_characters: 300,
    word_error_rate: 0.06,
    word_edits: 3,
    reference_words: referenceTokens,
  };
}

export function makeMockRunResult(
  project: Project,
  sample: ReceiptSample,
  solution: Solution,
  runId = MOCK_RUN_ID,
): RunResult {
  const metricFamily = metricFamilyForSolution(solution);
  const isOcrText = metricFamily === "ocr_text";
  // The native-prebuilt fixture demonstrates a CORD-like prebuilt analyzer
  // that omits an expected field; the missing `total` counts as a miss in
  // Field F1 and schema coverage rather than being excluded.
  const dropsTotal = solution.id === "offline-native-measurable" && sample.id === "offline-001";
  const parsed =
    sample.id === "offline-001"
      ? ({
          vendor: "North Market",
          date: "2026-08-30",
          ...(dropsTotal ? {} : { total: solution.id === "offline-ocr" ? 24.08 : 24.8 }),
        } satisfies JsonObject)
      : ({
          vendor: "Unscored",
          date: "2026-08-30",
          total: 8.5,
        } satisfies JsonObject);
  const comparable = solution.comparable_quality[project.id] === true;
  const components =
    solution.kind === "ocr_pipeline"
      ? [
          component("ocr", "Document OCR", 310, parsed),
          component("normalizer", "Schema normalizer", 680, parsed),
        ]
      : solution.kind === "native_pipeline"
        ? [
            component("extractor", "Native extraction", 310, parsed),
            component("normalizer", "Schema normalizer", 680, parsed),
          ]
        : [component("extractor", "Multimodal extraction", 790, parsed)];
  return {
    run_id: runId,
    project_id: project.id,
    sample_id: sample.id,
    solution_id: solution.id,
    solution_order: solution.order,
    status: "succeeded",
    schema_valid: isOcrText ? null : true,
    metric_family: metricFamily,
    ocr_metrics: isOcrText ? ocrTextMetrics(sample.id) : null,
    raw_response: isOcrText ? "Synthetic OCR text output" : JSON.stringify(parsed),
    parsed_output: isOcrText ? null : parsed,
    comparisons:
      isOcrText || sample.gold === null || !comparable
        ? []
        : compareProjectJson(project, sample.gold, parsed),
    telemetry: {
      components,
      total_tokens: 504,
      total_pages: 1,
      latency_ms: components.reduce((sum, item) => sum + item.latency_ms, 0),
      estimated_cost_usd: components.reduce(
        (sum, item) => sum + (item.estimated_cost_usd ?? 0),
        0,
      ),
      attempts: 1,
      errors: [],
    },
    error: null,
    compatibility: solution.compatibility[project.id] ?? "unsupported",
    comparable_quality: comparable,
    quality_scored: sample.gold !== null && comparable,
    artifacts: {},
  };
}

export function mockResultsFor(
  project: Project,
  samples: ReceiptSample[],
  runId = MOCK_RUN_ID,
): RunResult[] {
  return samples.flatMap((sample) =>
    mockSolutions
      .filter(
        (solution) =>
          solution.compatibility[project.id] !== "unsupported" &&
          solution.compatibility[project.id] !== undefined,
      )
      .map((solution) => makeMockRunResult(project, sample, solution, runId)),
  );
}
