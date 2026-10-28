import { useEffect, useMemo, useState, type KeyboardEvent } from "react";

import {
  configurationPresentation,
  getSynchronizedHighlight,
  modelDisplayName,
  resultPairKey,
} from "../domain";
import type { LiveRunState } from "../streaming";
import type {
  FieldComparison,
  JsonValue,
  Project,
  ReceiptSample,
  RunResult,
  RunTask,
  Solution,
} from "../types";
import { AlertIcon, CheckIcon, FileIcon, PipelineIcon } from "./Icons";
import { ReceiptPreview } from "./ReceiptPreview";

export type DetailTab = "parsed" | "raw" | "pipeline";
type ComparisonMode = "matrix" | "side" | "extractor";
type SortDirection = "asc" | "desc";
export type ComparisonViewState =
  | FieldComparison["state"]
  | "unscored"
  | "not_applicable"
  | "pending"
  | "failed"
  | "invalid";

export function comparisonColumnLabel(solution: Solution): string {
  if (solution.normalizer === null) {
    return solution.short_label;
  }
  const normalizer = modelDisplayName(solution.normalizer);
  const shortNormalizer = normalizer.split(" ").at(-1) ?? normalizer;
  const suffixes = [` + ${normalizer}`, ` + ${shortNormalizer}`];
  const matchingSuffix = suffixes.find((suffix) =>
    solution.label.toLocaleLowerCase("en-US").endsWith(
      suffix.toLocaleLowerCase("en-US"),
    ),
  );
  const extractorLabel =
    matchingSuffix === undefined
      ? solution.label
      : solution.label.slice(0, -matchingSuffix.length);
  return `${extractorLabel} + ${normalizer}`;
}

export function getSolutionDetailTabDestination(
  currentIndex: number,
  tabCount: number,
  key: string,
): number | null {
  if (currentIndex < 0 || currentIndex >= tabCount || tabCount === 0) {
    return null;
  }
  if (key === "ArrowLeft") {
    return (currentIndex - 1 + tabCount) % tabCount;
  }
  if (key === "ArrowRight") {
    return (currentIndex + 1) % tabCount;
  }
  if (key === "Home") {
    return 0;
  }
  if (key === "End") {
    return tabCount - 1;
  }
  return null;
}

function displayValue(value: JsonValue | undefined): string {
  if (value === undefined) {
    return "Not returned";
  }
  if (value === null) {
    return "null";
  }
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function flattenForView(
  value: JsonValue,
  prefix = "",
  output: Array<{ path: string; value: JsonValue }> = [],
) {
  if (Array.isArray(value)) {
    if (value.length === 0) {
      output.push({ path: prefix || "$", value });
    }
    value.forEach((item, index) =>
      flattenForView(item, `${prefix}[${index}]`, output),
    );
  } else if (value !== null && typeof value === "object") {
    const entries = Object.entries(value);
    if (entries.length === 0) {
      output.push({ path: prefix || "$", value });
    }
    entries.forEach(([key, child]) =>
      flattenForView(child, prefix ? `${prefix}.${key}` : key, output),
    );
  } else {
    output.push({ path: prefix || "$", value });
  }
  return output;
}

function stateLabel(state: ComparisonViewState): string {
  if (state === "matched") {
    return "Exact";
  }
  if (state === "extra") {
    return "Extra output";
  }
  if (state === "wrong") {
    return "Wrong";
  }
  if (state === "partial") {
    return "Partial";
  }
  if (state === "missing") {
    return "Missing";
  }
  if (state === "not_applicable") {
    return "Not present";
  }
  if (state === "pending") {
    return "Pending";
  }
  if (state === "failed") {
    return "Run failed";
  }
  if (state === "invalid") {
    return "Invalid";
  }
  return "Unscored";
}

export interface ComparisonOutput {
  value: JsonValue | undefined;
  state: ComparisonViewState;
}

export interface ComparisonRow {
  path: string;
  gold: JsonValue | undefined;
  goldPresent: boolean;
  outputs: Map<string, ComparisonOutput>;
}

export function buildComparisonRows(
  sample: ReceiptSample,
  solutions: Solution[],
  results: RunResult[],
): ComparisonRow[] {
  const gold =
    sample.gold === null
      ? new Map<string, JsonValue>()
      : new Map(
          flattenForView(sample.gold).map((item) => [item.path, item.value]),
        );
  const paths = new Set(gold.keys());
  const resultBySolution = new Map(results.map((result) => [result.solution_id, result]));
  const parsedBySolution = new Map(
    results.map((result) => [
      result.solution_id,
      result.parsed_output === null
        ? new Map<string, JsonValue>()
        : new Map(
            flattenForView(result.parsed_output).map((field) => [
              field.path,
              field.value,
            ]),
          ),
    ]),
  );
  results.forEach((result) => {
    result.comparisons.forEach((comparison) => paths.add(comparison.path));
    if (result.parsed_output !== null) {
      flattenForView(result.parsed_output).forEach((field) => paths.add(field.path));
    }
  });
  return [...paths]
    .sort((left, right) => left.localeCompare(right))
    .map((path) => ({
      path,
      gold: gold.get(path),
      goldPresent: gold.has(path),
      outputs: new Map<string, ComparisonOutput>(
        solutions.map((solution) => {
          const result = resultBySolution.get(solution.id);
          if (result === undefined) {
            return [
              solution.id,
              { value: undefined, state: "pending" as const },
            ];
          }
          const comparison = result?.comparisons.find((item) => item.path === path);
          const parsed = parsedBySolution.get(solution.id)!;
          const parsedPresent = parsed.has(path);
          const parsedValue = parsed.get(path);
          if (result.status === "failed" || result.status === "invalid") {
            return [
              solution.id,
              { value: parsedValue, state: result.status },
            ];
          }
          const comparable =
            result.quality_scored &&
            result.comparable_quality &&
            result.compatibility === "full";
          let state: ComparisonViewState;
          if (!parsedPresent) {
            state = gold.has(path)
              ? comparable
                ? "missing"
                : "unscored"
              : "not_applicable";
          } else if (!comparable) {
            state = "unscored";
          } else {
            state = comparison?.state ?? (gold.has(path) ? "unscored" : "extra");
          }
          return [
            solution.id,
            {
              value: parsedPresent ? parsedValue : comparison?.actual,
              state,
            },
          ];
        }),
      ),
    }));
}

function displayOutput(output: ComparisonOutput | undefined): string {
  if (output === undefined || output.state === "pending") {
    return "Pending";
  }
  if (output.state === "failed" && output.value === undefined) {
    return "Run failed";
  }
  if (output.state === "invalid" && output.value === undefined) {
    return "Invalid output";
  }
  if (output.state === "not_applicable") {
    return "Not present";
  }
  return displayValue(output.value);
}

function absentResultStatus(
  task: RunTask | undefined,
  loadStatus: "idle" | "loading" | "error",
): { primary: string; secondary: string } {
  if (loadStatus === "loading") {
    return { primary: "Loading result", secondary: "Loading" };
  }
  if (
    task === undefined ||
    !["succeeded", "failed", "invalid"].includes(task.status)
  ) {
    return {
      primary: task?.stage === "queued" ? "Pending" : (task?.stage ?? "Pending"),
      secondary: `${Math.round((task?.progress ?? 0) * 100)}%`,
    };
  }
  return loadStatus === "error"
    ? { primary: "Result unavailable", secondary: "Load failed" }
    : { primary: "Result unavailable", secondary: "Not loaded" };
}

function resultStatus(
  result: RunResult | undefined,
  task: RunTask | undefined,
  loadStatus: "idle" | "loading" | "error",
): string {
  if (result === undefined) {
    return absentResultStatus(task, loadStatus).secondary;
  }
  if (result.status === "invalid") {
    return "Invalid";
  }
  return result.status === "failed" ? "Failed" : "Complete";
}

function selectedQualityLabel(
  project: Project,
  solution: Solution,
  result: RunResult,
): string {
  if (!result.comparable_quality) {
    return configurationPresentation(project, solution).label;
  }
  if (result.metric_family === "ocr_text") {
    if (!result.quality_scored || result.ocr_metrics === null) {
      return "No OCR reference";
    }
    return `${(result.ocr_metrics.reference_token_recall * 100).toFixed(1)}% token recall`;
  }
  if (!result.quality_scored || result.comparisons.length === 0) {
    return "No gold";
  }
  const exact = result.comparisons.filter(
    (comparison) => comparison.state === "matched",
  ).length;
  return `${exact}/${result.comparisons.length} fields exact`;
}

function reviewTaskStatus(task: RunTask | undefined): string {
  switch (task?.status) {
    case "succeeded":
      return "Complete";
    case "failed":
      return "Failed";
    case "invalid":
      return "Invalid";
    case "running":
      return "Running";
    case "retrying":
      return "Retrying";
    case "queued":
      return "Queued";
    default:
      return "Not scheduled";
  }
}

export function ComparisonWorkspace({
  project,
  samples,
  solutions,
  runState,
  resultLoadStatus = "idle",
  selectedSampleId,
  selectedSolutionId,
  onSampleChange,
  onSolutionChange,
  onBuildRun,
}: {
  project: Project;
  samples: ReceiptSample[];
  solutions: Solution[];
  runState: LiveRunState | null;
  resultLoadStatus?: "idle" | "loading" | "error";
  selectedSampleId: string;
  selectedSolutionId: string;
  onSampleChange: (sampleId: string) => void;
  onSolutionChange: (solutionId: string) => void;
  onBuildRun: () => void;
}) {
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [detailTab, setDetailTab] = useState<DetailTab>("parsed");
  const [mode, setMode] = useState<ComparisonMode>("matrix");
  const [sortKey, setSortKey] = useState("field");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const manifestSamples = useMemo(
    () =>
      runState === null || runState.manifest.project_id !== project.id
        ? []
        : runState.manifest.sample_ids
            .map((id) =>
              samples.find(
                (item) =>
                  item.id === id &&
                  item.project_id === runState.manifest.project_id,
              ),
            )
            .filter((item): item is ReceiptSample => item !== undefined),
    [project.id, runState, samples],
  );
  const sample =
    manifestSamples.find((item) => item.id === selectedSampleId) ??
    manifestSamples[0] ??
    null;
  const sampleIndex =
    sample === null ? -1 : manifestSamples.findIndex((item) => item.id === sample.id);
  const orderedSolutions = useMemo(() => {
    if (runState === null) {
      return [];
    }
    return runState.manifest.solution_ids
      .map((id) => solutions.find((solution) => solution.id === id))
      .filter((solution): solution is Solution => solution !== undefined)
      .sort((left, right) => left.order - right.order);
  }, [runState, solutions]);
  const selectedSolution =
    orderedSolutions.find((solution) => solution.id === selectedSolutionId) ??
    orderedSolutions[0] ??
    null;
  const sampleResults = useMemo(
    () =>
      runState === null || sample === null
        ? []
        : Object.values(runState.results)
            .filter(
              (result) =>
                result.run_id === runState.manifest.id &&
                result.project_id === project.id &&
                result.sample_id === sample.id &&
                runState.manifest.solution_ids.includes(result.solution_id),
            )
            .sort((left, right) => left.solution_order - right.solution_order),
    [project.id, runState, sample],
  );
  const resultBySolution = useMemo(
    () => new Map(sampleResults.map((result) => [result.solution_id, result])),
    [sampleResults],
  );
  const displayedSolutions = useMemo(
    () =>
      mode === "extractor" && selectedSolution !== null
        ? [selectedSolution]
        : orderedSolutions,
    [mode, orderedSolutions, selectedSolution],
  );
  const displayedResults = useMemo(
    () => {
      const displayedSolutionIds = new Set(
        displayedSolutions.map((solution) => solution.id),
      );
      return sampleResults.filter((result) =>
        displayedSolutionIds.has(result.solution_id),
      );
    },
    [displayedSolutions, sampleResults],
  );
  const rows = useMemo(
    () =>
      sample === null
        ? []
        : buildComparisonRows(sample, displayedSolutions, displayedResults),
    [displayedResults, displayedSolutions, sample],
  );
  const sortedRows = useMemo(
    () =>
      [...rows].sort((left, right) => {
        const valueFor = (row: ComparisonRow): string | null => {
          if (sortKey === "field") return row.path;
          if (sortKey === "gold") {
            return row.goldPresent ? displayValue(row.gold) : null;
          }
          const output = row.outputs.get(sortKey);
          if (output === undefined || output.state === "pending") return null;
          return output.value === undefined
            ? stateLabel(output.state)
            : displayValue(output.value);
        };
        const leftValue = valueFor(left);
        const rightValue = valueFor(right);
        if (leftValue === null && rightValue === null) {
          return left.path.localeCompare(right.path);
        }
        if (leftValue === null) return 1;
        if (rightValue === null) return -1;
        const comparison = leftValue.localeCompare(rightValue, undefined, {
          numeric: true,
        });
        const directed = sortDirection === "asc" ? comparison : -comparison;
        return directed || left.path.localeCompare(right.path);
      }),
    [rows, sortDirection, sortKey],
  );
  const selectedResult =
    selectedSolution === null
      ? null
      : resultBySolution.get(selectedSolution.id) ?? null;
  const highlight =
    sample === null || selectedPath === null
      ? null
      : getSynchronizedHighlight(selectedPath, sample.gold, displayedResults);

  useEffect(() => {
    setSelectedPath((current) => {
      if (current !== null && rows.some((row) => row.path === current)) {
        return current;
      }
      return (
        sampleResults
          .flatMap((result) => result.comparisons)
          .find((comparison) => comparison.state !== "matched")?.path ??
        rows[0]?.path ??
        null
      );
    });
  }, [rows, sampleResults]);

  useEffect(() => {
    setDetailTab("parsed");
  }, [selectedSolution?.id, sample?.id]);

  useEffect(() => {
    setSortKey("field");
    setSortDirection("asc");
  }, [runState?.manifest.id]);

  useEffect(() => {
    if (mode === "extractor") {
      setSortKey("field");
      setSortDirection("asc");
    }
  }, [mode, selectedSolution?.id]);

  function sortMatrix(key: string) {
    if (key === sortKey) {
      setSortDirection((current) => (current === "asc" ? "desc" : "asc"));
      return;
    }
    setSortKey(key);
    setSortDirection("asc");
  }

  function handleDetailTabKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    const tablist = event.currentTarget.parentElement;
    const tabs =
      tablist === null
        ? []
        : Array.from(
            tablist.querySelectorAll<HTMLButtonElement>(
              '[role="tab"]:not(:disabled)',
            ),
          );
    const destination = getSolutionDetailTabDestination(
      tabs.indexOf(event.currentTarget),
      tabs.length,
      event.key,
    );
    const target = destination === null ? undefined : tabs[destination];
    if (target === undefined) {
      return;
    }
    event.preventDefault();
    target.focus();
    target.click();
  }

  if (runState === null) {
    return (
      <section className="evaluation-empty" aria-labelledby="no-run-heading">
        <FileIcon />
        <div>
          <h2 id="no-run-heading">No evaluation selected</h2>
          <p>Configure a run to create an immutable receipt × solution manifest.</p>
        </div>
        <button className="primary-button" type="button" onClick={onBuildRun}>
          Build evaluation
        </button>
      </section>
    );
  }

  if (sample === null) {
    return (
      <section className="evaluation-empty">
        <FileIcon />
        <div>
          <h2>Manifest evidence unavailable</h2>
          <p>The selected run references no authorized receipt.</p>
        </div>
      </section>
    );
  }

  return (
    <section className="receipt-inspector" aria-labelledby="inspector-heading">
      <header className="inspector-toolbar">
        <div>
          <h2 id="inspector-heading" tabIndex={-1}>Receipt inspector</h2>
          <span className="receipt-position">
            {sampleIndex + 1} / {manifestSamples.length}
          </span>
        </div>
        <label className="select-field compact-select">
          <span>Receipt</span>
          <select
            value={sample.id}
            onChange={(event) => onSampleChange(event.target.value)}
            aria-label="Select receipt"
          >
            {manifestSamples.map((item) => {
              return (
                <option value={item.id} key={item.id}>
                  {item.id} · {item.title}
                </option>
              );
            })}
          </select>
        </label>
        <div className="segmented-control" aria-label="Comparison layout">
          <button
            type="button"
            className={mode === "matrix" ? "active" : ""}
            aria-pressed={mode === "matrix"}
            onClick={() => setMode("matrix")}
          >
            Side by side
          </button>
          <button
            type="button"
            className={mode === "side" ? "active" : ""}
            aria-pressed={mode === "side"}
            onClick={() => setMode("side")}
          >
            Field focus
          </button>
          <button
            type="button"
            className={mode === "extractor" ? "active" : ""}
            aria-pressed={mode === "extractor"}
            disabled={runState.manifest.status !== "completed"}
            title={
              runState.manifest.status === "completed"
                ? "Review every receipt for one extractor"
                : "Extractor review is available when the run completes."
            }
            onClick={() => setMode("extractor")}
          >
            By extractor
          </button>
        </div>
        <div className="inspector-navigation">
          <button
            type="button"
            disabled={sampleIndex <= 0}
            onClick={() => onSampleChange(manifestSamples[sampleIndex - 1]!.id)}
          >
            Previous
          </button>
          <button
            type="button"
            disabled={sampleIndex < 0 || sampleIndex >= manifestSamples.length - 1}
            onClick={() => onSampleChange(manifestSamples[sampleIndex + 1]!.id)}
          >
            Next
          </button>
        </div>
      </header>

      <div className="inspector-grid">
        <aside className="receipt-source" aria-label="Source receipt">
          <div className="panel-heading">
            <span>Receipt</span>
            <code>{sample.id}</code>
          </div>
          <ReceiptPreview sample={sample} selectedPath={selectedPath} />
          <footer>
            <span>{sample.filename}</span>
            <span>
              {sample.source_index === null
                ? "Owner upload"
                : `Pinned index ${sample.source_index}`}
            </span>
          </footer>
        </aside>

        <div className="comparison-evidence">
          {sample.scoring_role === "few_shot" && (
            <div className="inline-note note-accent">
              Few-shot example · visible for audit and excluded from quality scores.
            </div>
          )}
          {sample.scoring_role === "excluded" && (
            <div className="inline-note note-excluded">
              <strong>Excluded from evaluation</strong>
              <span>
                {sample.exclusion_reason ?? "Excluded by the project contract."}
              </span>
            </div>
          )}
          {sample.gold === null && (
            <div className="no-gold-note">
              <AlertIcon />
              <div>
                <strong>No golden answer</strong>
                <p>Evidence and telemetry remain available; quality is not reported.</p>
              </div>
            </div>
          )}
          {resultLoadStatus === "loading" && (
            <div className="inline-note" role="status">
              Loading persisted results for this receipt…
            </div>
          )}
          {resultLoadStatus === "error" && (
            <div className="inline-note note-excluded" role="alert">
              Persisted results for this receipt could not be loaded.
            </div>
          )}
          {sample.gold !== null && rows.some((row) => !row.goldPresent) && (
            <div className="inline-note output-only-note">
              Fields marked “Not in gold” were returned by at least one extractor but are
              absent from this receipt’s dataset annotation.
            </div>
          )}

          {mode === "extractor" && selectedSolution !== null && (
            <section
              className="extractor-review-controls"
              aria-labelledby="extractor-review-heading"
            >
              <div className="extractor-review-heading">
                <div>
                  <span>Extractor review</span>
                  <strong id="extractor-review-heading">
                    {selectedSolution.label}
                  </strong>
                </div>
                <label className="select-field">
                  <span>Extractor</span>
                  <select
                    value={selectedSolution.id}
                    onChange={(event) => onSolutionChange(event.target.value)}
                  >
                    {orderedSolutions.map((solution) => (
                      <option value={solution.id} key={solution.id}>
                        {solution.label}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <div
                className="extractor-receipt-list"
                role="list"
                aria-label={`${selectedSolution.label} receipts`}
              >
                {manifestSamples.map((item, index) => {
                  const task =
                    runState.tasks[resultPairKey(item.id, selectedSolution.id)];
                  return (
                    <button
                      type="button"
                      role="listitem"
                      className={item.id === sample.id ? "active" : ""}
                      aria-current={item.id === sample.id ? "true" : undefined}
                      onClick={() => onSampleChange(item.id)}
                      key={item.id}
                    >
                      <span>{index + 1}</span>
                      <strong>{item.title}</strong>
                      <small>
                        {item.id} · {reviewTaskStatus(task)}
                      </small>
                    </button>
                  );
                })}
              </div>
            </section>
          )}

          {mode !== "side" ? (
            <div className="comparison-matrix-scroll">
              <table className="comparison-matrix">
                <thead>
                  <tr>
                    <th
                      scope="col"
                      aria-sort={
                        sortKey === "field"
                          ? sortDirection === "asc"
                            ? "ascending"
                            : "descending"
                          : undefined
                      }
                    >
                      <button
                        className="table-sort-button"
                        type="button"
                        onClick={() => sortMatrix("field")}
                      >
                        Field
                        <span aria-hidden="true">
                          {sortKey === "field" ? (sortDirection === "asc" ? "▲" : "▼") : "↕"}
                        </span>
                      </button>
                    </th>
                    <th
                      scope="col"
                      className="gold-cell"
                      aria-sort={
                        sortKey === "gold"
                          ? sortDirection === "asc"
                            ? "ascending"
                            : "descending"
                          : undefined
                      }
                    >
                      <button
                        className="table-sort-button"
                        type="button"
                        onClick={() => sortMatrix("gold")}
                      >
                        Golden answer
                        <span aria-hidden="true">
                          {sortKey === "gold" ? (sortDirection === "asc" ? "▲" : "▼") : "↕"}
                        </span>
                      </button>
                    </th>
                    {displayedSolutions.map((solution) => {
                      const result = resultBySolution.get(solution.id);
                      const task =
                        runState.tasks[resultPairKey(sample.id, solution.id)];
                      const columnLabel = comparisonColumnLabel(solution);
                      return (
                        <th
                          scope="col"
                          key={solution.id}
                          aria-sort={
                            sortKey === solution.id
                              ? sortDirection === "asc"
                                ? "ascending"
                                : "descending"
                              : undefined
                          }
                        >
                          <button
                            type="button"
                            className={
                              selectedSolution?.id === solution.id ? "selected" : ""
                            }
                            disabled={result === undefined}
                            onClick={() => {
                              onSolutionChange(solution.id);
                              sortMatrix(solution.id);
                            }}
                            aria-label={`Select ${columnLabel} and sort fields by its output`}
                          >
                            <span>{columnLabel}</span>
                            <small>
                              {runState.manifest.reference_only_solution_ids.includes(
                                solution.id,
                              )
                                ? configurationPresentation(project, solution).label
                                : resultStatus(result, task, resultLoadStatus)}{" "}
                              {sortKey === solution.id
                                ? sortDirection === "asc"
                                  ? "▲"
                                  : "▼"
                                : "↕"}
                            </small>
                          </button>
                        </th>
                      );
                    })}
                  </tr>
                </thead>
                <tbody>
                  {sortedRows.map((row) => (
                    <tr
                      className={`${selectedPath === row.path ? "selected" : ""}${
                        row.goldPresent ? "" : " output-only-row"
                      }`}
                      key={row.path}
                    >
                      <th scope="row">
                        <button type="button" onClick={() => setSelectedPath(row.path)}>
                          {row.path}
                        </button>
                      </th>
                      <td
                        className={`gold-cell${row.goldPresent ? "" : " gold-absent"}`}
                        title={
                          row.goldPresent
                            ? undefined
                            : sample.gold === null
                              ? "This receipt has no golden answer."
                              : "At least one extractor returned this field, but it is absent from the dataset gold for this receipt."
                        }
                      >
                        <strong>
                          {row.goldPresent
                            ? displayValue(row.gold)
                            : sample.gold === null
                              ? "No gold"
                              : "Not in gold"}
                        </strong>
                        {!row.goldPresent && sample.gold !== null && (
                          <small>Output-only field</small>
                        )}
                      </td>
                      {displayedSolutions.map((solution) => {
                        const result = resultBySolution.get(solution.id);
                        const task =
                          runState.tasks[resultPairKey(sample.id, solution.id)];
                        const output = row.outputs.get(solution.id);
                        const pending = output?.state === "pending";
                        const absent = absentResultStatus(task, resultLoadStatus);
                        return (
                          <td key={solution.id}>
                            <button
                              type="button"
                              className={`matrix-value state-${
                                output?.state ?? "pending"
                              }`}
                              disabled={pending}
                              onClick={() => {
                                setSelectedPath(row.path);
                                onSolutionChange(solution.id);
                              }}
                            >
                              <strong>
                                {pending
                                  ? absent.primary
                                  : displayOutput(output)}
                              </strong>
                              <small>
                                {pending
                                  ? absent.secondary
                                  : stateLabel(output?.state ?? "unscored")}
                              </small>
                            </button>
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                  {rows.length === 0 && (
                    <tr>
                      <td colSpan={displayedSolutions.length + 2}>
                        <div className="matrix-pending">
                          Results will populate in these fixed solution positions.
                        </div>
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="side-by-side-grid">
              <article className="output-card gold-output">
                <span className="eyebrow">Golden answer</span>
                <strong>
                  {sample.gold === null
                    ? "No gold"
                    : highlight?.gold === undefined
                      ? "Not in gold"
                      : displayValue(highlight.gold)}
                </strong>
                <code>{selectedPath ?? "Select a field"}</code>
              </article>
              {orderedSolutions.map((solution) => {
                const result = resultBySolution.get(solution.id);
                const selectedRow =
                  selectedPath === null
                    ? null
                    : rows.find((row) => row.path === selectedPath) ?? null;
                const output = selectedRow?.outputs.get(solution.id);
                const task = runState.tasks[resultPairKey(sample.id, solution.id)];
                const absent = absentResultStatus(task, resultLoadStatus);
                return (
                  <button
                    type="button"
                    className={`output-card${
                      selectedSolution?.id === solution.id ? " selected" : ""
                    } state-${output?.state ?? "pending"}`}
                    disabled={result === undefined}
                    onClick={() => onSolutionChange(solution.id)}
                    key={solution.id}
                  >
                    <span className="eyebrow">{solution.short_label}</span>
                    <strong>
                      {result === undefined
                        ? absent.primary
                        : displayOutput(output)}
                    </strong>
                    <small>
                      {result === undefined
                        ? absent.secondary
                        : stateLabel(output?.state ?? "unscored")}
                    </small>
                  </button>
                );
              })}
            </div>
          )}

          <details className="solution-detail">
            <summary>
              <span>View JSON and pipeline details</span>
              <small>{selectedSolution?.label ?? "Select an extractor"}</small>
            </summary>
            {selectedResult === null || selectedSolution === null ? (
              <div className="matrix-pending">
                Select a completed output to inspect parsed, raw, and pipeline evidence.
              </div>
            ) : (
              <>
                <header className="solution-detail-heading">
                  <div>
                    <span className="eyebrow">Selected solution</span>
                    <h3 id="solution-detail-heading">{selectedSolution.label}</h3>
                  </div>
                  <strong>
                    {selectedQualityLabel(project, selectedSolution, selectedResult)}
                  </strong>
                  <div className="tab-list" role="tablist" aria-label="Solution detail">
                    {(["parsed", "raw", "pipeline"] as const).map((tab) => (
                      <button
                        type="button"
                        role="tab"
                        id={`solution-detail-tab-${tab}`}
                        aria-controls={`solution-detail-panel-${tab}`}
                        aria-selected={detailTab === tab}
                        tabIndex={detailTab === tab ? 0 : -1}
                        className={detailTab === tab ? "active" : ""}
                        onClick={() => setDetailTab(tab)}
                        onKeyDown={handleDetailTabKeyDown}
                        key={tab}
                      >
                        {tab === "pipeline" && <PipelineIcon />}
                        {tab === "raw" ? "Raw response" : tab}
                      </button>
                    ))}
                  </div>
                </header>
                <div
                  role="tabpanel"
                  id={`solution-detail-panel-${detailTab}`}
                  aria-labelledby={`solution-detail-tab-${detailTab}`}
                  tabIndex={0}
                >
                  {selectedResult.error !== null && (
                    <div className="output-error">
                      <AlertIcon />
                      <span>{selectedResult.error.message}</span>
                    </div>
                  )}
                  {detailTab === "raw" && (
                    <pre className="json-block detail-json">
                      <code>
                        {selectedResult.raw_response || "No raw response was persisted."}
                      </code>
                    </pre>
                  )}
                  {detailTab === "parsed" && (
                    <pre className="json-block detail-json">
                      <code>
                        {selectedResult.parsed_output === null
                          ? "No parsed output reached a terminal success state."
                          : JSON.stringify(selectedResult.parsed_output, null, 2)}
                      </code>
                    </pre>
                  )}
                  {detailTab === "pipeline" && (
                    <div className="pipeline-inline">
                      <dl className="component-metrics">
                        <div>
                          <dt>Latency</dt>
                          <dd>{(selectedResult.telemetry.latency_ms / 1_000).toFixed(2)} s</dd>
                        </div>
                        <div>
                          <dt>Tokens</dt>
                          <dd>{selectedResult.telemetry.total_tokens ?? "—"}</dd>
                        </div>
                        <div>
                          <dt>Pages</dt>
                          <dd>{selectedResult.telemetry.total_pages ?? "—"}</dd>
                        </div>
                        <div>
                          <dt>Attempts</dt>
                          <dd>{selectedResult.telemetry.attempts}</dd>
                        </div>
                        <div>
                          <dt>Cost</dt>
                          <dd>
                            {selectedResult.telemetry.estimated_cost_usd === null
                              ? "—"
                              : `$${selectedResult.telemetry.estimated_cost_usd.toFixed(5)}`}
                          </dd>
                        </div>
                      </dl>
                      <ol className="pipeline-mini-list">
                        {selectedResult.telemetry.components.map((component, index) => (
                          <li key={`${component.id}-${index}`}>
                            {component.status === "succeeded" ? (
                              <CheckIcon />
                            ) : (
                              <AlertIcon />
                            )}
                            <span>
                              <strong>{component.label}</strong>
                              <small>
                                Attempt {component.attempt} ·{" "}
                                {Math.round(component.latency_ms)} ms
                              </small>
                            </span>
                            <details>
                              <summary>Evidence</summary>
                              <pre className="json-block">
                                <code>{component.output ?? component.error ?? "No output"}</code>
                              </pre>
                            </details>
                          </li>
                        ))}
                      </ol>
                    </div>
                  )}
                </div>
              </>
            )}
          </details>
        </div>
      </div>
      <footer className="inspector-legend" aria-label="Field state legend">
        <strong>Field legend</strong>
        {(["matched", "partial", "wrong", "missing", "extra"] as const).map(
          (state) => (
            <span className={`field-state state-${state}`} key={state}>
              <span aria-hidden="true" />
              {stateLabel(state)}
            </span>
          ),
        )}
      </footer>
    </section>
  );
}
