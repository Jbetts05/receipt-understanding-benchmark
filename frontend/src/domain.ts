import { encodePathSegment } from "./api";
import type {
  AggregateMeasure,
  Compatibility,
  FieldAggregate,
  FieldComparison,
  JsonObject,
  JsonValue,
  MetricFamily,
  Project,
  ProjectAggregate,
  Prompt,
  PromptMode,
  ReceiptSample,
  RunManifest,
  RunResult,
  Solution,
  SolutionAggregate,
} from "./types";

export interface RunSelection {
  sampleIds: string[];
  solutionIds: string[];
  schemaConfirmed: boolean;
  promptConfirmed: boolean;
}

export interface LaunchGuard {
  allowed: boolean;
  pairCount: number;
  reasons: string[];
}

export interface SynchronizedHighlight {
  path: string;
  gold: JsonValue | undefined;
  outputs: Map<string, FieldComparison | null>;
}

export function modelDisplayName(modelId: string): string {
  const parts = modelId.split("-");
  if (parts[0]?.toLocaleLowerCase("en-US") === "gpt" && parts.length >= 2) {
    const suffix = parts
      .slice(2)
      .map((part) => `${part.charAt(0).toLocaleUpperCase("en-US")}${part.slice(1)}`)
      .join(" ");
    return `GPT-${parts[1]}${suffix ? ` ${suffix}` : ""}`;
  }
  return parts
    .map((part) => `${part.charAt(0).toLocaleUpperCase("en-US")}${part.slice(1)}`)
    .join(" ");
}

export function solutionTypeLabel(solution: Solution): string {
  if (solution.kind === "direct") {
    return "Multimodal model";
  }
  if (solution.kind === "native") {
    return "Extraction API";
  }
  if (solution.kind === "native_pipeline") {
    return solution.normalizer === null
      ? "Extraction API"
      : `Extraction API + ${modelDisplayName(solution.normalizer)}`;
  }
  if (solution.kind === "ocr_text") {
    return "OCR text (standalone)";
  }
  return solution.normalizer === null
    ? "OCR pipeline"
    : `OCR + ${modelDisplayName(solution.normalizer)}`;
}

/**
 * Discloses exactly how a project's pinned schema originated, using the
 * API-supplied `schema.origin`/`schema.derivation` rather than inferring it
 * from `project.kind`. When the API supplies exact provenance text (e.g. the
 * precise SROIE/CORD ground-truth fields a built-in schema was derived from),
 * that text is shown verbatim; otherwise a generic note reflects `origin`.
 */
export function schemaOriginNote(schema: Project["schema"]): string {
  const provenance =
    schema.derivation && schema.derivation.trim().length > 0
      ? schema.derivation
      : schema.origin === "dataset_derived"
        ? "This schema is a repository-authored contract derived from this dataset's ground-truth labels."
        : "This schema was authored by the project owner when the project was created.";
  return `${provenance} Every receipt in the project is scored against the same pinned contract.`;
}

/** Explicit, human-readable label for a pinned prompt contract's solution mode. */
export function promptModeLabel(mode: PromptMode): string {
  return mode === "direct-multimodal" ? "Direct multimodal" : "OCR + normalizer";
}

/**
 * One sentence disclosing whether a pinned prompt contract is an exact,
 * repository-authored contract (built-in projects) or one generated from the
 * project owner's single authored prompt (custom projects), driven by
 * `project.kind` rather than assuming every contract's provenance.
 */
export function promptContractOriginNote(project: Project): string {
  return project.kind === "built_in"
    ? "Exact, repository-authored prompt contract for this mode."
    : "Generated for this mode from the prompt authored when this project was created.";
}

/** Sorts prompt contracts into a stable, mode-driven display order. */
export function orderedPromptContracts(project: Project): Prompt[] {
  const order: PromptMode[] = ["direct-multimodal", "ocr-normalizer"];
  return [...project.prompt_contracts].sort(
    (left, right) => order.indexOf(left.mode) - order.indexOf(right.mode),
  );
}

/**
 * The comparable-quality metric family a solution is scored under. `ocr_text`
 * solutions (the standalone, normalizer-free `-text` derivations of an OCR
 * pipeline) are scored against reference-token-recall/CER/WER; every other
 * kind is scored against structured Field F1/whole-answer exactness/schema
 * coverage. Mirrors the backend's identical `solution.kind == "ocr_text"`
 * branch.
 */
export function metricFamilyForSolution(solution: Solution): MetricFamily {
  return solution.kind === "ocr_text" ? "ocr_text" : "structured";
}

export function metricFamilyLabel(family: MetricFamily): string {
  return family === "ocr_text" ? "OCR text recall" : "Field extraction";
}

function labelForPath(path: string): string {
  return path
    .replaceAll(".", " · ")
    .replaceAll(/(?:^|_)([a-z])/g, (_, letter: string) => ` ${letter.toUpperCase()}`)
    .replaceAll(/\[(\d+)\]/g, " #$1")
    .trim();
}

function flattenJson(
  value: JsonValue,
  prefix = "",
  output = new Map<string, JsonValue>(),
): Map<string, JsonValue> {
  if (Array.isArray(value)) {
    if (value.length === 0 && prefix) {
      output.set(prefix, value);
    } else {
      value.forEach((item, index) => {
        flattenJson(item, `${prefix}[${index}]`, output);
      });
    }
    return output;
  }
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value);
    if (entries.length === 0 && prefix) {
      output.set(prefix, value);
    } else {
      entries.forEach(([key, child]) => {
        flattenJson(child, prefix ? `${prefix}.${key}` : key, output);
      });
    }
    return output;
  }
  if (prefix) {
    output.set(prefix, value);
  }
  return output;
}

function normalizeScalar(value: JsonValue): string {
  if (typeof value === "string") {
    return value.trim().replace(/\s+/g, " ").toLocaleLowerCase("en-US");
  }
  if (typeof value === "number") {
    return Number.isFinite(value) ? String(Math.round(value * 100) / 100) : String(value);
  }
  return JSON.stringify(value);
}

export function compareJson(
  gold: JsonObject | null,
  actual: JsonObject | null,
): FieldComparison[] {
  if (gold === null) {
    return [];
  }
  const expected = flattenJson(gold);
  const received = actual === null ? new Map<string, JsonValue>() : flattenJson(actual);
  const paths = new Set([...expected.keys(), ...received.keys()]);
  return [...paths]
    .sort((left, right) => left.localeCompare(right))
    .map((path) => {
      const hasExpected = expected.has(path);
      const hasActual = received.has(path);
      const expectedValue = expected.get(path);
      const actualValue = received.get(path);
      let state: FieldComparison["state"];
      if (!hasExpected) {
        state = "extra";
      } else if (!hasActual) {
        state = "missing";
      } else if (
        expectedValue !== undefined &&
        actualValue !== undefined &&
        normalizeScalar(expectedValue) === normalizeScalar(actualValue)
      ) {
        state = "matched";
      } else {
        state = "wrong";
      }
      return {
        path,
        label: labelForPath(path),
        state,
        expected: expectedValue,
        actual: actualValue,
      };
    });
}

function stableJson(value: JsonValue): string {
  if (Array.isArray(value)) {
    return `[${value.map(stableJson).sort().join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, child]) => `${JSON.stringify(key)}:${stableJson(child)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function normalizeOrderInsensitiveArrays(value: JsonValue): JsonValue {
  if (Array.isArray(value)) {
    return value
      .map(normalizeOrderInsensitiveArrays)
      .sort((left, right) => stableJson(left).localeCompare(stableJson(right)));
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, child]) => [
        key,
        normalizeOrderInsensitiveArrays(child),
      ]),
    );
  }
  return value;
}

export function compareProjectJson(
  project: Project,
  gold: JsonObject | null,
  actual: JsonObject | null,
): FieldComparison[] {
  const orderInsensitive =
    project.normalization_profile.toLocaleLowerCase("en-US").includes("cord") ||
    project.id.toLocaleLowerCase("en-US").includes("cord");
  return compareJson(
    orderInsensitive && gold !== null
      ? (normalizeOrderInsensitiveArrays(gold) as JsonObject)
      : gold,
    orderInsensitive && actual !== null
      ? (normalizeOrderInsensitiveArrays(actual) as JsonObject)
      : actual,
  );
}

export function getSynchronizedHighlight(
  path: string,
  gold: JsonObject | null,
  results: RunResult[],
): SynchronizedHighlight {
  const goldValue =
    gold === null ? undefined : flattenJson(gold).get(path);
  return {
    path,
    gold: goldValue,
    outputs: new Map(
      results.map((result) => [
        result.solution_id,
        result.comparisons.find((comparison) => comparison.path === path) ?? null,
      ]),
    ),
  };
}

export function getCompatibility(
  project: Project,
  solution: Solution,
): Compatibility {
  const projectDeclaration = project.compatibility.find(
    (item) => item.solution_id === solution.id,
  );
  if (projectDeclaration !== undefined) {
    return projectDeclaration.compatibility;
  }
  const declared = solution.compatibility[project.id];
  return declared ?? "unsupported";
}

export function isComparableQuality(
  project: Project,
  solution: Solution,
): boolean {
  const projectDeclaration = project.compatibility.find(
    (item) => item.solution_id === solution.id,
  );
  if (projectDeclaration !== undefined) {
    return projectDeclaration.comparable_quality;
  }
  return solution.comparable_quality[project.id] === true;
}

export interface ConfigurationPresentation {
  /** Short, explicit configuration label driven by API metadata. */
  label: string;
  /** One sentence of supporting detail; never implies poor quality. */
  detail: string;
}

/**
 * Whether a solution requires a project-specific custom-analyzer
 * registration to run at all, as opposed to being an always-available
 * prebuilt/native extractor. Reads the API-supplied
 * `solution.requires_project_analyzer` field directly — true only for the
 * Content Understanding schema-configured analyzer, false otherwise — never inferred
 * from solution identity or compatibility state.
 */
export function requiresCustomAnalyzerRegistration(solution: Solution): boolean {
  return solution.requires_project_analyzer;
}

/**
 * Shared presentation logic for `configurationPresentation` (project-bound)
 * and the pre-creation custom-project wizard, which has no `Project` object
 * yet. Never implies poor output quality for reference-only or unsupported
 * paths; states plainly how the solution is configured instead.
 */
export function presentationForCompatibility(
  compatibility: Compatibility,
  solution: Solution,
  reason: string | null,
): ConfigurationPresentation {
  if (compatibility === "unsupported") {
    return solution.kind === "ocr_text"
      ? {
          label: "OCR transcription unavailable · not configured",
          detail:
            reason ??
            "This project has no OCR ground-truth transcription for this solution to be scored against.",
        }
      : {
          label: "Custom analyzer unavailable · not configured",
          detail:
            reason ??
            "This project has no registration or deterministic schema mapping for this solution.",
        };
  }
  const assisted = solution.normalizer !== null;
  const assistedServiceNoun = solution.kind === "native_pipeline" ? "Extraction API" : "OCR";
  const modeLabel = assisted
    ? `Assisted · ${assistedServiceNoun} + ${modelDisplayName(solution.normalizer!)}`
    : solution.kind === "ocr_text"
      ? "Standalone OCR text output"
      : "Standalone native output";
  if (compatibility === "partial_reference") {
    return {
      label: modeLabel,
      detail:
        reason ??
        "Retained as reference evidence; its output has no deterministic mapping to this project's schema.",
    };
  }
  return {
    label: modeLabel,
    detail:
      reason ??
      "Schema-mapped output; included in comparable quality aggregates.",
  };
}

/**
 * Replaces vague "Reference only"/"Unsupported" compatibility copy with an
 * explicit description of how a solution is configured for a project:
 * a standalone native output, an assisted OCR + named-normalizer service, or
 * a custom analyzer that is unavailable/not configured for this project.
 * Driven entirely by API-supplied compatibility metadata and solution
 * fields; never hard-codes a solution or project identity.
 */
export function configurationPresentation(
  project: Project,
  solution: Solution,
): ConfigurationPresentation {
  const compatibility = getCompatibility(project, solution);
  const declaration = project.compatibility.find(
    (item) => item.solution_id === solution.id,
  );
  return presentationForCompatibility(
    compatibility,
    solution,
    declaration?.reason ?? null,
  );
}

export function runLaunchGuard(
  selection: RunSelection,
  project: Project,
  solutions: Solution[],
): LaunchGuard {
  const reasons: string[] = [];
  const selectedSolutions = solutions.filter((solution) =>
    selection.solutionIds.includes(solution.id),
  );
  if (selection.sampleIds.length === 0) {
    reasons.push("Choose at least one receipt.");
  }
  if (selectedSolutions.length === 0) {
    reasons.push("Choose at least one solution.");
  }
  if (
    selectedSolutions.some(
      (solution) => getCompatibility(project, solution) === "unsupported",
    )
  ) {
    reasons.push("Remove unsupported solutions before launching.");
  }
  if (!selection.schemaConfirmed) {
    reasons.push("Confirm the pinned schema version.");
  }
  if (!selection.promptConfirmed) {
    reasons.push("Confirm the pinned prompt version.");
  }
  return {
    allowed: reasons.length === 0,
    pairCount: selection.sampleIds.length * selectedSolutions.length,
    reasons,
  };
}

function ratio(
  value: number | null,
  evidence: AggregateMeasure["evidence"],
  polarity: AggregateMeasure["polarity"] = "higher_better",
): AggregateMeasure {
  return { value, unit: "ratio", polarity, evidence };
}

function metric(
  value: number | null,
  unit: AggregateMeasure["unit"],
  evidence: AggregateMeasure["evidence"],
  polarity: AggregateMeasure["polarity"] = "higher_better",
): AggregateMeasure {
  return { value, unit, polarity, evidence };
}

/** Weighted-sum aggregation of a per-result rate (matches/total), mirroring how the backend aggregates OCR-text token/character/word rates across scored results. */
function weightedRate(
  pairs: Array<{ numerator: number; denominator: number }>,
): number | null {
  const denominator = pairs.reduce((sum, pair) => sum + pair.denominator, 0);
  if (denominator === 0) {
    return null;
  }
  const numerator = pairs.reduce((sum, pair) => sum + pair.numerator, 0);
  return numerator / denominator;
}

function median(values: number[]): number | null {
  if (values.length === 0) {
    return null;
  }
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 0) {
    return ((sorted[middle - 1] ?? 0) + (sorted[middle] ?? 0)) / 2;
  }
  return sorted[middle] ?? null;
}

function percentile(values: number[], percentage: number): number | null {
  if (values.length === 0) {
    return null;
  }
  const sorted = [...values].sort((left, right) => left - right);
  if (sorted.length === 1) {
    return sorted[0] ?? null;
  }
  const rank = (percentage / 100) * (sorted.length - 1);
  const lower = Math.floor(rank);
  const upper = Math.min(lower + 1, sorted.length - 1);
  const fraction = rank - lower;
  return (
    (sorted[lower] ?? 0) +
    ((sorted[upper] ?? 0) - (sorted[lower] ?? 0)) * fraction
  );
}

interface FieldAccumulator {
  solutionId: string;
  path: string;
  matched: number;
  wrong: number;
  partial: number;
  missing: number;
  extra: number;
  evidence: Map<string, AggregateMeasure["evidence"][number]>;
}

function aggregateFieldPath(path: string): string {
  return path.replace(/\[\d+\]/g, "[]");
}

function resultEvidence(result: RunResult) {
  return {
    sample_id: result.sample_id,
    solution_id: result.solution_id,
    result_url: `/api/runs/${encodePathSegment(result.run_id)}/results/${encodePathSegment(
      result.sample_id,
    )}/${encodePathSegment(result.solution_id)}`,
  };
}

export function buildProjectAggregate(
  project: Project,
  runId: string,
  samples: ReceiptSample[],
  solutions: Solution[],
  allResults: RunResult[],
): ProjectAggregate {
  const projectSamples = samples.filter((sample) => sample.project_id === project.id);
  const projectResults = allResults.filter(
    (result) => result.project_id === project.id && result.run_id === runId,
  );
  const selectedIds = new Set(projectResults.map((result) => result.sample_id));
  const scoredIds = new Set(
    projectSamples
      .filter(
        (sample) =>
          selectedIds.has(sample.id) &&
          sample.scoring_role === "scored" &&
          sample.gold !== null,
      )
      .map((sample) => sample.id),
  );
  const sampleById = new Map(projectSamples.map((sample) => [sample.id, sample]));
  const fieldAccumulators = new Map<string, FieldAccumulator>();
  const recordField = (
    result: RunResult,
    rawPath: string,
    state: FieldComparison["state"],
  ) => {
    const path = aggregateFieldPath(rawPath);
    const key = `${result.solution_id}\u0000${path}`;
    const accumulator = fieldAccumulators.get(key) ?? {
      solutionId: result.solution_id,
      path,
      matched: 0,
      wrong: 0,
      partial: 0,
      missing: 0,
      extra: 0,
      evidence: new Map(),
    };
    accumulator[state] += 1;
    accumulator.evidence.set(
      `${result.sample_id}\u0000${result.solution_id}`,
      resultEvidence(result),
    );
    fieldAccumulators.set(key, accumulator);
  };
  const rows: SolutionAggregate[] = solutions.map((solution) => {
    const results = projectResults.filter(
      (result) => result.solution_id === solution.id,
    );
    const successful = results.filter((result) => result.status === "succeeded");
    const scored = results.filter(
      (result) =>
        scoredIds.has(result.sample_id) &&
        result.comparable_quality &&
        isComparableQuality(project, solution),
    );
    const evidence = results.map(resultEvidence);
    const scoredEvidence = scored.map(resultEvidence);
    const success = results.length === 0 ? null : successful.length / results.length;
    const latency = median(results.map((result) => result.telemetry.latency_ms));
    const p95Latency = percentile(
      results.map((result) => result.telemetry.latency_ms),
      95,
    );
    const cost =
      results.length > 0 &&
      results.every((result) => result.telemetry.estimated_cost_usd !== null)
        ? results.reduce(
            (sum, result) => sum + (result.telemetry.estimated_cost_usd ?? 0),
            0,
          )
        : null;
    const shared = {
      solution_id: solution.id,
      success_rate: ratio(success, evidence),
      median_latency: metric(latency, "milliseconds", evidence, "lower_better"),
      p95_latency: metric(p95Latency, "milliseconds", evidence, "lower_better"),
      input_tokens: metric(
        (() => {
          const values = results.flatMap((result) => {
            const tokens = result.telemetry.components.flatMap((component) =>
              component.input_tokens === null ? [] : [component.input_tokens],
            );
            return tokens.length === 0
              ? []
              : [tokens.reduce((sum, value) => sum + value, 0)];
          });
          return values.length === 0
            ? null
            : values.reduce((sum, value) => sum + value, 0);
        })(),
        "count",
        results
          .filter((result) =>
            result.telemetry.components.some(
              (component) => component.input_tokens !== null,
            ),
          )
          .map(resultEvidence),
        "lower_better",
      ),
      output_tokens: metric(
        (() => {
          const values = results.flatMap((result) => {
            const tokens = result.telemetry.components.flatMap((component) =>
              component.output_tokens === null ? [] : [component.output_tokens],
            );
            return tokens.length === 0
              ? []
              : [tokens.reduce((sum, value) => sum + value, 0)];
          });
          return values.length === 0
            ? null
            : values.reduce((sum, value) => sum + value, 0);
        })(),
        "count",
        results
          .filter((result) =>
            result.telemetry.components.some(
              (component) => component.output_tokens !== null,
            ),
          )
          .map(resultEvidence),
        "lower_better",
      ),
      total_tokens: metric(
        (() => {
          const values = results.flatMap((result) =>
            result.telemetry.total_tokens === null
              ? []
              : [result.telemetry.total_tokens],
          );
          return values.length === 0
            ? null
            : values.reduce((sum, value) => sum + value, 0);
        })(),
        "count",
        results
          .filter((result) => result.telemetry.total_tokens !== null)
          .map(resultEvidence),
        "lower_better",
      ),
      total_pages: metric(
        (() => {
          const values = results.flatMap((result) =>
            result.telemetry.total_pages === null
              ? []
              : [result.telemetry.total_pages],
          );
          return values.length === 0
            ? null
            : values.reduce((sum, value) => sum + value, 0);
        })(),
        "count",
        results
          .filter((result) => result.telemetry.total_pages !== null)
          .map(resultEvidence),
        "lower_better",
      ),
      total_attempts: metric(
        results.length === 0
          ? null
          : results.reduce(
              (sum, result) => sum + result.telemetry.attempts,
              0,
            ),
        "count",
        evidence,
        "lower_better",
      ),
      estimated_cost: metric(cost, "usd", evidence, "lower_better"),
      cost_scope: "complete" as const,
      compatibility: getCompatibility(project, solution),
      comparable_quality: isComparableQuality(project, solution),
    };
    if (metricFamilyForSolution(solution) === "ocr_text") {
      // The ocr_text family is scored against reference-token-recall (and,
      // only for full-page OCR references such as SROIE or imported transcripts, character/word
      // error rate) instead of structured field comparisons. Field F1 and
      // whole-answer exactness are not applicable and stay null so they can
      // never be numerically compared against a structured row's values.
      const ocrScored = scored.flatMap((result) =>
        result.status === "succeeded" && result.ocr_metrics !== null
          ? [{ result, metrics: result.ocr_metrics }]
          : [],
      );
      const completeOcrEvidence = ocrScored.length === scored.length && scored.length > 0;
      const tokenRecall = completeOcrEvidence
        ? weightedRate(
            ocrScored.map(({ metrics }) => ({
              numerator: metrics.matched_reference_tokens,
              denominator: metrics.reference_tokens,
            })),
          )
        : null;
      const characterScored = ocrScored.filter(
        ({ metrics }) => metrics.character_edits !== null && metrics.reference_characters !== null,
      );
      const cer =
        completeOcrEvidence && characterScored.length === scored.length
          ? weightedRate(
              characterScored.map(({ metrics }) => ({
                numerator: metrics.character_edits ?? 0,
                denominator: metrics.reference_characters ?? 0,
              })),
            )
          : null;
      const wordScored = ocrScored.filter(
        ({ metrics }) => metrics.word_edits !== null && metrics.reference_words !== null,
      );
      const wer =
        completeOcrEvidence && wordScored.length === scored.length
          ? weightedRate(
              wordScored.map(({ metrics }) => ({
                numerator: metrics.word_edits ?? 0,
                denominator: metrics.reference_words ?? 0,
              })),
            )
          : null;
      const ocrEvidence = ocrScored.map(({ result }) => resultEvidence(result));
      return {
        ...shared,
        metric_family: "ocr_text",
        normalized_field_f1: ratio(null, []),
        whole_answer_exactness: ratio(null, []),
        labeled_field_coverage: null,
        reference_token_recall: ratio(tokenRecall, ocrEvidence),
        character_error_rate: ratio(
          cer,
          characterScored.map(({ result }) => resultEvidence(result)),
          "lower_better",
        ),
        word_error_rate: ratio(
          wer,
          wordScored.map(({ result }) => resultEvidence(result)),
          "lower_better",
        ),
      };
    }
    let matchedCount = 0;
    let wrongCount = 0;
    let partialCount = 0;
    let missingCount = 0;
    let extraCount = 0;
    let exactMatches = 0;
    scored.forEach((result) => {
      if (result.status !== "succeeded") {
        const gold = sampleById.get(result.sample_id)?.gold ?? null;
        const goldFields = gold === null ? new Map<string, JsonValue>() : flattenJson(gold);
        missingCount += goldFields.size;
        goldFields.forEach((_value, path) =>
          recordField(result, path, "missing"),
        );
        return;
      }
      result.comparisons.forEach((comparison) => {
        recordField(result, comparison.path, comparison.state);
        if (comparison.state === "matched") {
          matchedCount += 1;
        } else if (comparison.state === "wrong") {
          wrongCount += 1;
        } else if (comparison.state === "partial") {
          partialCount += 1;
        } else if (comparison.state === "missing") {
          missingCount += 1;
        } else {
          extraCount += 1;
        }
      });
      if (
        result.comparisons.length > 0 &&
        result.comparisons.every((comparison) => comparison.state === "matched")
      ) {
        exactMatches += 1;
      }
    });
    const truePositive = matchedCount;
    const falsePositive = wrongCount + partialCount + extraCount;
    const falseNegative = wrongCount + partialCount + missingCount;
    const denominator = 2 * truePositive + falsePositive + falseNegative;
    const f1 = scored.length > 0 && denominator > 0 ? (2 * truePositive) / denominator : null;
    const exact = scored.length === 0 ? null : exactMatches / scored.length;
    const coverageDenominator = matchedCount + wrongCount + partialCount + missingCount;
    const coverage =
      coverageDenominator === 0
        ? null
        : (matchedCount + wrongCount + partialCount) / coverageDenominator;
    return {
      ...shared,
      metric_family: "structured",
      normalized_field_f1: ratio(f1, scoredEvidence),
      whole_answer_exactness: ratio(exact, scoredEvidence),
      labeled_field_coverage: ratio(coverage, scoredEvidence),
      reference_token_recall: null,
      character_error_rate: null,
      word_error_rate: null,
    };
  });
  const fields: FieldAggregate[] = [...fieldAccumulators.values()]
    .sort(
      (left, right) =>
        left.solutionId.localeCompare(right.solutionId) ||
        left.path.localeCompare(right.path, undefined, { numeric: true }),
    )
    .map((item) => {
      const labeled = item.matched + item.wrong + item.partial + item.missing;
      const denominator =
        2 * item.matched +
        2 * (item.wrong + item.partial) +
        item.missing +
        item.extra;
      const evidence = [...item.evidence.values()];
      return {
        solution_id: item.solutionId,
        path: item.path,
        label: item.path,
        matched: item.matched,
        wrong: item.wrong,
        partial: item.partial,
        missing: item.missing,
        extra: item.extra,
        coverage: ratio(
          labeled === 0
            ? null
            : (item.matched + item.wrong + item.partial) / labeled,
          evidence,
        ),
        exact_match_rate: ratio(
          labeled === 0 ? null : item.matched / labeled,
          evidence,
        ),
        normalized_field_f1: ratio(
          denominator === 0 ? null : (2 * item.matched) / denominator,
          evidence,
        ),
      };
    });
  return {
    format_version: "2.0",
    project_id: project.id,
    run_id: runId,
    has_gold: scoredIds.size > 0,
    scored_sample_count: scoredIds.size,
    excluded_prompt_examples: projectSamples.filter(
      (sample) => sample.scoring_role === "few_shot",
    ).length,
    solutions: rows,
    fields,
  };
}

export function qualityLanguage(hasGold: boolean): string {
  return hasGold
    ? "Gold-scored quality"
    : "No gold supplied · schema and agreement only";
}

export function scopeProjectAggregateToManifest(
  aggregate: ProjectAggregate,
  manifest: RunManifest,
): ProjectAggregate | null {
  if (
    aggregate.project_id !== manifest.project_id ||
    aggregate.run_id !== manifest.id
  ) {
    return null;
  }
  const sampleIds = new Set(manifest.sample_ids);
  const solutionIds = new Set(manifest.solution_ids);
  const scopeMeasure = (
    measure: AggregateMeasure,
    solutionId: string,
  ): AggregateMeasure => ({
    ...measure,
    evidence: measure.evidence.filter(
      (item) =>
        sampleIds.has(item.sample_id) &&
        item.solution_id === solutionId &&
        solutionIds.has(item.solution_id),
    ),
  });
  const scopeNullableMeasure = (
    measure: AggregateMeasure | null,
    solutionId: string,
  ): AggregateMeasure | null =>
    measure === null ? null : scopeMeasure(measure, solutionId);
  return {
    ...aggregate,
    solutions: aggregate.solutions
      .filter((row) => solutionIds.has(row.solution_id))
      .map((row) => ({
        ...row,
        normalized_field_f1: scopeMeasure(
          row.normalized_field_f1,
          row.solution_id,
        ),
        whole_answer_exactness: scopeMeasure(
          row.whole_answer_exactness,
          row.solution_id,
        ),
        labeled_field_coverage: scopeNullableMeasure(
          row.labeled_field_coverage,
          row.solution_id,
        ),
        reference_token_recall: scopeNullableMeasure(
          row.reference_token_recall,
          row.solution_id,
        ),
        character_error_rate: scopeNullableMeasure(
          row.character_error_rate,
          row.solution_id,
        ),
        word_error_rate: scopeNullableMeasure(row.word_error_rate, row.solution_id),
        success_rate: scopeMeasure(row.success_rate, row.solution_id),
        median_latency: scopeMeasure(row.median_latency, row.solution_id),
        p95_latency: scopeMeasure(row.p95_latency, row.solution_id),
        input_tokens: scopeMeasure(row.input_tokens, row.solution_id),
        output_tokens: scopeMeasure(row.output_tokens, row.solution_id),
        total_tokens: scopeMeasure(row.total_tokens, row.solution_id),
        total_pages: scopeMeasure(row.total_pages, row.solution_id),
        total_attempts: scopeMeasure(row.total_attempts, row.solution_id),
        estimated_cost: scopeMeasure(row.estimated_cost, row.solution_id),
      })),
    fields: aggregate.fields
      .filter((row) => solutionIds.has(row.solution_id))
      .map((row) => ({
        ...row,
        coverage: scopeMeasure(row.coverage, row.solution_id),
        exact_match_rate: scopeMeasure(row.exact_match_rate, row.solution_id),
        normalized_field_f1: scopeMeasure(
          row.normalized_field_f1,
          row.solution_id,
        ),
      })),
  };
}

export function resultPairKey(sampleId: string, solutionId: string): string {
  return `${sampleId}::${solutionId}`;
}

export function isTerminalStatus(status: RunResult["status"] | string): boolean {
  return status === "succeeded" || status === "failed" || status === "invalid";
}

/** Runs can only be deleted once they have reached a terminal manifest state. */
export function isTerminalRunStatus(status: RunManifest["status"]): boolean {
  return status === "completed" || status === "failed";
}
