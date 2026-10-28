import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";

import type { ProductClient } from "../api";
import {
  configurationPresentation,
  metricFamilyForSolution,
  resultPairKey,
  solutionTypeLabel,
} from "../domain";
import type { LiveRunState } from "../streaming";
import type {
  AggregateMeasure,
  Project,
  ProjectAggregate,
  ReceiptSample,
  Solution,
} from "../types";
import {
  ChartIcon,
  CheckIcon,
  FileIcon,
  PlayIcon,
} from "./Icons";
import { ComparisonWorkspace } from "./ComparisonWorkspace";
import { BatchResults } from "./BatchResults";
import { LiveWorkspace } from "./LiveWorkspace";
import { PipelineInspector } from "./PipelineInspector";

type EvidenceView = "live" | "pipeline" | "batch";
type CopyLinkStatus = "idle" | "copying" | "copied" | "error";
type CopyLinkResult = Extract<CopyLinkStatus, "copied" | "error"> | "stale";
interface CopyLinkRequestGuard {
  generation: number;
  isCurrent: (generation: number, runId: string) => boolean;
}
type SortKey =
  | "extractor"
  | "type"
  | "progress"
  | "field_f1"
  | "coverage"
  | "exactness"
  | "token_recall"
  | "cer"
  | "wer"
  | "success"
  | "latency"
  | "cost";
type SortDirection = "asc" | "desc";

const evidenceViews: readonly EvidenceView[] = ["live", "pipeline", "batch"];
const evidenceViewLabels: Record<EvidenceView, string> = {
  live: "Live tasks",
  pipeline: "Pipeline inspector",
  batch: "Batch evidence",
};
const evidenceTabIds: Record<EvidenceView, string> = {
  live: "run-evidence-tab-live",
  pipeline: "run-evidence-tab-pipeline",
  batch: "run-evidence-tab-batch",
};
const evidencePanelIds: Record<EvidenceView, string> = {
  live: "run-evidence-panel-live",
  pipeline: "run-evidence-panel-pipeline",
  batch: "run-evidence-panel-batch",
};

export function resolveEvidenceView(
  requestedView: EvidenceView,
  aggregateAvailable: boolean,
): EvidenceView {
  return requestedView === "batch" && !aggregateAvailable ? "live" : requestedView;
}

export function evidenceViewForKey(
  currentView: EvidenceView,
  key: string,
  aggregateAvailable: boolean,
): EvidenceView | null {
  const availableViews = aggregateAvailable
    ? evidenceViews
    : evidenceViews.filter((view) => view !== "batch");
  if (key === "Home") {
    return availableViews[0] ?? null;
  }
  if (key === "End") {
    return availableViews[availableViews.length - 1] ?? null;
  }
  if (key !== "ArrowLeft" && key !== "ArrowRight") {
    return null;
  }
  const currentIndex = availableViews.indexOf(currentView);
  const offset = key === "ArrowRight" ? 1 : -1;
  const nextIndex =
    (Math.max(currentIndex, 0) + offset + availableViews.length) %
    availableViews.length;
  return availableViews[nextIndex] ?? null;
}

export async function copyRunLinkToClipboard(
  runId: string,
  href: string,
  clipboard: Pick<Clipboard, "writeText"> | null | undefined,
  guard?: CopyLinkRequestGuard,
): Promise<CopyLinkResult> {
  if (clipboard === null || clipboard === undefined) {
    return "error";
  }
  try {
    const url = new URL(href);
    url.searchParams.set("run", runId);
    await clipboard.writeText(url.toString());
  } catch {
    if (
      guard !== undefined &&
      !guard.isCurrent(guard.generation, runId)
    ) {
      return "stale";
    }
    return "error";
  }
  return guard !== undefined && !guard.isCurrent(guard.generation, runId)
    ? "stale"
    : "copied";
}

export function failurePairKeysForRun(
  runState: LiveRunState,
): ReadonlySet<string> {
  return new Set(
    runState.positionKeys.filter((key) => {
      const task = runState.tasks[key];
      return task?.status === "failed" || task?.status === "invalid";
    }),
  );
}

interface PerformanceRow {
  solution: Solution;
  row: ProjectAggregate["solutions"][number] | undefined;
  terminal: number;
  totalTasks: number;
  referenceOnly: boolean;
}

/** Ascending-default keys: string columns sort A→Z, and lower-is-better OCR diagnostics sort best-first. */
function defaultDirectionForKey(key: SortKey): SortDirection {
  return key === "extractor" ||
    key === "type" ||
    key === "cer" ||
    key === "wer" ||
    key === "latency" ||
    key === "cost"
    ? "asc"
    : "desc";
}

const columnTooltips: Partial<Record<SortKey, string>> = {
  progress: "Terminal (succeeded, failed, or invalid) requests out of the total pinned for this extractor.",
  field_f1: "Field F1 balances precision and recall of extracted fields against the golden answer. Structured extractors only; not comparable to OCR text metrics.",
  coverage: "Labeled-field coverage: share of fields present in the golden answers that this extractor emitted (matched, wrong, or partial) rather than leaving missing.",
  exactness: "Exact match: share of receipts whose entire parsed answer matched the golden answer exactly.",
  token_recall: "Reference token recall: share of ground-truth OCR tokens covered by this solution's text output. Standalone OCR text solutions only; not comparable to Field F1.",
  cer: "Character error rate: character-level edit distance to the reference text divided by reference length (lower is better). Available for SROIE and imported full-page transcripts; unavailable for CORD's labeled-subset reference.",
  wer: "Word error rate: word-level edit distance to the reference text divided by reference word count (lower is better). Available for SROIE and imported full-page transcripts; unavailable for CORD's labeled-subset reference.",
  success: "Share of requests that completed without a terminal error.",
  latency: "Median wall-clock latency across this extractor's completed requests.",
  cost: "Accrued estimated provider cost across completed requests. It increases while a run is active and becomes the full run estimate at terminal completion.",
};

function sortValue(item: PerformanceRow, key: SortKey): number | string | null {
  const isOcrText =
    (item.row?.metric_family ?? metricFamilyForSolution(item.solution)) === "ocr_text";
  switch (key) {
    case "extractor":
      return item.solution.label;
    case "type":
      return solutionTypeLabel(item.solution);
    case "progress":
      return item.totalTasks === 0 ? 0 : item.terminal / item.totalTasks;
    case "field_f1":
      return item.referenceOnly || isOcrText ? null : item.row?.normalized_field_f1.value ?? null;
    case "coverage":
      return item.referenceOnly || isOcrText
        ? null
        : item.row?.labeled_field_coverage?.value ?? null;
    case "exactness":
      return item.referenceOnly || isOcrText ? null : item.row?.whole_answer_exactness.value ?? null;
    case "token_recall":
      return item.referenceOnly || !isOcrText ? null : item.row?.reference_token_recall?.value ?? null;
    case "cer":
      return item.referenceOnly || !isOcrText ? null : item.row?.character_error_rate?.value ?? null;
    case "wer":
      return item.referenceOnly || !isOcrText ? null : item.row?.word_error_rate?.value ?? null;
    case "success":
      return item.row?.success_rate.value ?? null;
    case "latency":
      return item.row?.median_latency.value ?? null;
    case "cost":
      return item.row?.estimated_cost.value ?? null;
    default:
      return null;
  }
}

function compareRows(
  left: PerformanceRow,
  right: PerformanceRow,
  key: SortKey,
  direction: SortDirection,
): number {
  const leftValue = sortValue(left, key);
  const rightValue = sortValue(right, key);
  if (leftValue === null && rightValue === null) {
    return 0;
  }
  if (leftValue === null) {
    return 1;
  }
  if (rightValue === null) {
    return -1;
  }
  if (typeof leftValue === "string" && typeof rightValue === "string") {
    const comparison = leftValue.localeCompare(rightValue);
    return direction === "asc" ? comparison : -comparison;
  }
  const comparison = (leftValue as number) - (rightValue as number);
  return direction === "asc" ? comparison : -comparison;
}

function SortableColumnHeader({
  label,
  sortKeyName,
  activeKey,
  direction,
  onSort,
}: {
  label: string;
  sortKeyName: SortKey;
  activeKey: SortKey;
  direction: SortDirection;
  onSort: (key: SortKey) => void;
}) {
  const active = activeKey === sortKeyName;
  const tooltip = columnTooltips[sortKeyName];
  const tooltipId = tooltip !== undefined ? `performance-tooltip-${sortKeyName}` : undefined;
  return (
    <th
      scope="col"
      className="sortable-column"
      aria-sort={active ? (direction === "asc" ? "ascending" : "descending") : undefined}
    >
      <button
        type="button"
        className="sortable-column-header"
        onClick={() => onSort(sortKeyName)}
        aria-describedby={tooltipId}
      >
        <span>{label}</span>
        <span className="sort-indicator" aria-hidden="true">
          {active ? (direction === "asc" ? "▲" : "▼") : "↕"}
        </span>
      </button>
      {tooltip !== undefined && (
        <span className="th-tooltip" role="tooltip" id={tooltipId}>
          {tooltip}
        </span>
      )}
    </th>
  );
}

function formatMeasure(measure: AggregateMeasure | null | undefined): string {
  if (measure?.value === null || measure?.value === undefined) {
    return "—";
  }
  if (measure.unit === "ratio") {
    return `${(measure.value * 100).toFixed(1)}%`;
  }
  if (measure.unit === "milliseconds") {
    return `${(measure.value / 1_000).toFixed(2)} s`;
  }
  return `$${measure.value.toFixed(5)}`;
}

function lifecycleCopy(runState: LiveRunState | null): string {
  if (runState === null) {
    return "No run";
  }
  if (runState.manifest.status === "completed") {
    return "Completed aggregate";
  }
  if (runState.manifest.status === "failed") {
    return "Failed · partial aggregate";
  }
  return runState.manifest.completed_pairs === 0
    ? "Pending aggregate"
    : `Partial aggregate · ${runState.manifest.completed_pairs}/${runState.manifest.total_pairs}`;
}

export function EvaluationStudio({
  client,
  project,
  samples,
  solutions,
  runState,
  aggregate,
  aggregateError,
  resultLoadStatus = "idle",
  selectedSampleId,
  selectedSolutionId,
  onSampleChange,
  onSolutionChange,
  onBuildRun,
}: {
  client: ProductClient;
  project: Project;
  samples: ReceiptSample[];
  solutions: Solution[];
  runState: LiveRunState | null;
  aggregate: ProjectAggregate | null;
  aggregateError: string | null;
  resultLoadStatus?: "idle" | "loading" | "error";
  selectedSampleId: string;
  selectedSolutionId: string;
  onSampleChange: (sampleId: string) => void;
  onSolutionChange: (solutionId: string) => void;
  onBuildRun: () => void;
}) {
  const [copyLinkStatus, setCopyLinkStatus] =
    useState<CopyLinkStatus>("idle");
  const [evidenceView, setEvidenceView] = useState<EvidenceView>("live");
  const [failureOnly, setFailureOnly] = useState(false);
  const [showAllSolutions, setShowAllSolutions] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey>("field_f1");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const copyResetTimerRef = useRef<number | null>(null);
  const copyRequestGenerationRef = useRef(0);
  const activeCopyRunIdRef = useRef(runState?.manifest.id ?? null);
  activeCopyRunIdRef.current = runState?.manifest.id ?? null;
  const evidenceTabRefs = useRef<
    Record<EvidenceView, HTMLButtonElement | null>
  >({
    live: null,
    pipeline: null,
    batch: null,
  });
  const selectedSolutions = useMemo(() => {
    if (runState === null) {
      return [];
    }
    return runState.manifest.solution_ids
      .map((id) => solutions.find((solution) => solution.id === id))
      .filter((solution): solution is Solution => solution !== undefined)
      .sort((left, right) => left.order - right.order);
  }, [runState, solutions]);
  const comparableAggregates =
    aggregate?.solutions.filter(
      (row) => row.comparable_quality && row.normalized_field_f1.value !== null,
    ) ?? [];
  const bestAggregate =
    [...comparableAggregates].sort(
      (left, right) =>
        (right.normalized_field_f1.value ?? 0) -
        (left.normalized_field_f1.value ?? 0),
    )[0] ?? null;
  const bestSolution =
    bestAggregate === null
      ? null
      : solutions.find((solution) => solution.id === bestAggregate.solution_id) ?? null;
  const averageFieldF1 =
    comparableAggregates.length === 0
      ? null
      : comparableAggregates.reduce(
          (sum, row) => sum + (row.normalized_field_f1.value ?? 0),
          0,
        ) / comparableAggregates.length;
  const exactValues = comparableAggregates.flatMap((row) =>
    row.whole_answer_exactness.value === null
      ? []
      : [row.whole_answer_exactness.value],
  );
  const averageExactness =
    exactValues.length === 0
      ? null
      : exactValues.reduce((sum, value) => sum + value, 0) / exactValues.length;
  const aggregateRows = aggregate?.solutions ?? [];
  const totalCost =
    aggregateRows.length > 0 &&
    aggregateRows.every((row) => row.estimated_cost.value !== null)
      ? aggregateRows.reduce(
          (sum, row) => sum + (row.estimated_cost.value ?? 0),
          0,
        )
      : null;
  const manifestSamples = useMemo(
    () =>
      runState === null || runState.manifest.project_id !== project.id
        ? []
        : runState.manifest.sample_ids
            .map((id) =>
              samples.find(
                (sample) =>
                  sample.id === id &&
                  sample.project_id === runState.manifest.project_id,
              ),
            )
            .filter((sample): sample is ReceiptSample => sample !== undefined),
    [project.id, runState, samples],
  );
  const manifestResults =
    runState === null
      ? []
      : Object.values(runState.results).filter(
          (result) =>
            result.run_id === runState.manifest.id &&
            result.project_id === runState.manifest.project_id &&
            runState.manifest.sample_ids.includes(result.sample_id) &&
            runState.manifest.solution_ids.includes(result.solution_id),
        );
  const failurePairKeys = useMemo(
    () =>
      runState === null ? new Set<string>() : failurePairKeysForRun(runState),
    [runState],
  );
  const failures = failurePairKeys.size;
  const activeEvidenceView = resolveEvidenceView(
    evidenceView,
    aggregate !== null,
  );
  const tableRows = useMemo<PerformanceRow[]>(() => {
    if (runState === null) {
      return [];
    }
    return selectedSolutions.map((solution) => {
      const row = aggregate?.solutions.find(
        (item) => item.solution_id === solution.id,
      );
      const solutionTasks = runState.manifest.sample_ids.map(
        (sampleId) => runState.tasks[resultPairKey(sampleId, solution.id)],
      );
      const terminal = solutionTasks.filter(
        (task) =>
          task?.status === "succeeded" ||
          task?.status === "failed" ||
          task?.status === "invalid",
      ).length;
      const referenceOnly =
        runState.manifest.reference_only_solution_ids.includes(solution.id) ||
        row?.comparable_quality === false;
      return { solution, row, terminal, totalTasks: solutionTasks.length, referenceOnly };
    });
  }, [runState, selectedSolutions, aggregate]);
  const sortedRows = useMemo(
    () => [...tableRows].sort((left, right) => compareRows(left, right, sortKey, sortDirection)),
    [tableRows, sortKey, sortDirection],
  );
  const hasCoverage = tableRows.some(
    (item) =>
      item.row?.labeled_field_coverage?.value !== null &&
      item.row?.labeled_field_coverage?.value !== undefined,
  );
  const hasTokenRecall = tableRows.some(
    (item) =>
      item.row?.reference_token_recall?.value !== null &&
      item.row?.reference_token_recall?.value !== undefined,
  );
  const hasCharacterErrorRate = tableRows.some(
    (item) =>
      item.row?.character_error_rate?.value !== null &&
      item.row?.character_error_rate?.value !== undefined,
  );
  const hasWordErrorRate = tableRows.some(
    (item) =>
      item.row?.word_error_rate?.value !== null && item.row?.word_error_rate?.value !== undefined,
  );
  const visibleRows = showAllSolutions ? sortedRows : sortedRows.slice(0, 5);

  function handleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDirection((current) => (current === "asc" ? "desc" : "asc"));
      return;
    }
    setSortKey(key);
    setSortDirection(defaultDirectionForKey(key));
  }

  useEffect(() => {
    copyRequestGenerationRef.current += 1;
    setEvidenceView("live");
    setFailureOnly(false);
    setCopyLinkStatus("idle");
    setShowAllSolutions(false);
    setSortKey("field_f1");
    setSortDirection("desc");
    if (copyResetTimerRef.current !== null) {
      window.clearTimeout(copyResetTimerRef.current);
      copyResetTimerRef.current = null;
    }
  }, [runState?.manifest.id]);

  useEffect(() => {
    if (aggregate === null && evidenceView === "batch") {
      setEvidenceView("live");
    }
  }, [aggregate, evidenceView]);

  useEffect(() => {
    if (failures === 0) {
      setFailureOnly(false);
    }
  }, [failures]);

  useEffect(
    () => () => {
      copyRequestGenerationRef.current += 1;
      activeCopyRunIdRef.current = null;
      if (copyResetTimerRef.current !== null) {
        window.clearTimeout(copyResetTimerRef.current);
      }
    },
    [],
  );

  async function copyRunLink() {
    if (runState === null || typeof window === "undefined") {
      return;
    }
    if (copyResetTimerRef.current !== null) {
      window.clearTimeout(copyResetTimerRef.current);
      copyResetTimerRef.current = null;
    }
    const runId = runState.manifest.id;
    const generation = ++copyRequestGenerationRef.current;
    const guard: CopyLinkRequestGuard = {
      generation,
      isCurrent: (requestGeneration, requestRunId) =>
        copyRequestGenerationRef.current === requestGeneration &&
        activeCopyRunIdRef.current === requestRunId,
    };
    setCopyLinkStatus("copying");
    const status = await copyRunLinkToClipboard(
      runId,
      window.location.href,
      typeof navigator === "undefined" ? null : navigator.clipboard,
      guard,
    );
    if (status === "stale") {
      return;
    }
    setCopyLinkStatus(status);
    if (status === "copied") {
      copyResetTimerRef.current = window.setTimeout(() => {
        if (!guard.isCurrent(generation, runId)) {
          return;
        }
        setCopyLinkStatus("idle");
        copyResetTimerRef.current = null;
      }, 1_500);
    }
  }

  function selectEvidenceView(view: EvidenceView) {
    setFailureOnly(false);
    setEvidenceView(resolveEvidenceView(view, aggregate !== null));
  }

  function handleEvidenceTabKeyDown(
    event: ReactKeyboardEvent<HTMLButtonElement>,
    currentView: EvidenceView,
  ) {
    const nextView = evidenceViewForKey(
      currentView,
      event.key,
      aggregate !== null,
    );
    if (nextView === null) {
      return;
    }
    event.preventDefault();
    selectEvidenceView(nextView);
    evidenceTabRefs.current[nextView]?.focus();
  }

  return (
    <div className="evaluation-studio">
      <h1 className="sr-only">{project.name} evaluation workspace</h1>
      <header className="studio-toolbar">
        <nav className="studio-tabs" aria-label="Evaluation sections">
          <button className="active" type="button">
            Overview
          </button>
          <button
            type="button"
            disabled={runState === null}
            aria-disabled={runState === null}
            title={
              runState === null
                ? "Build a run before inspecting a receipt."
                : "Jump to the receipt inspector"
            }
            onClick={() => {
              if (runState === null) {
                return;
              }
              if (!runState.manifest.sample_ids.includes(selectedSampleId)) {
                const fallbackId = runState.manifest.sample_ids[0];
                if (fallbackId !== undefined) {
                  onSampleChange(fallbackId);
                }
              }
              requestAnimationFrame(() => {
                const heading = document.getElementById("inspector-heading");
                heading?.scrollIntoView({ behavior: "smooth", block: "start" });
                heading?.focus();
              });
            }}
          >
            Receipt inspector
          </button>
          <button
            type="button"
            disabled={failures === 0}
            aria-pressed={failureOnly}
            aria-controls={evidencePanelIds.live}
            className={failureOnly ? "active" : ""}
            title={
              failureOnly
                ? "Show all run tasks"
                : "Show failed and invalid run tasks"
            }
            onClick={() => {
              setEvidenceView("live");
              setFailureOnly((current) => !current);
              requestAnimationFrame(() =>
                document
                  .getElementById("evidence-surfaces-heading")
                  ?.scrollIntoView({ behavior: "smooth", block: "start" }),
              );
            }}
          >
            Failures {failures > 0 ? `(${failures})` : ""}
          </button>
          <button type="button" onClick={onBuildRun}>
            Configure run
          </button>
          <button
            type="button"
            onClick={() =>
              document.getElementById("performance-heading")?.scrollIntoView({
                behavior: "smooth",
                block: "start",
              })
            }
          >
            Metrics
          </button>
          <button
            type="button"
            disabled={aggregate === null}
            aria-disabled={aggregate === null}
            aria-pressed={activeEvidenceView === "batch"}
            aria-controls={evidencePanelIds.batch}
            title={
              aggregate === null
                ? "Cost and usage become available when an aggregate is ready."
                : "Show aggregate cost and usage evidence"
            }
            onClick={() => {
              if (aggregate === null) {
                return;
              }
              selectEvidenceView("batch");
              document.getElementById("evidence-surfaces-heading")?.scrollIntoView({
                behavior: "smooth",
                block: "start",
              });
            }}
          >
            Cost & usage
          </button>
        </nav>
        <div className="evaluation-actions">
          <button className="secondary-button" type="button" onClick={onBuildRun}>
            <PlayIcon />
            New run
          </button>
          <button
            className="secondary-button"
            type="button"
            disabled={runState === null || copyLinkStatus === "copying"}
            onClick={() => void copyRunLink()}
          >
            {copyLinkStatus === "copied" ? <CheckIcon /> : <FileIcon />}
            {copyLinkStatus === "copied"
              ? "Link copied"
              : copyLinkStatus === "copying"
                ? "Copying link"
                : copyLinkStatus === "error"
                  ? "Retry copy link"
                  : "Copy link"}
          </button>
          <span
            className={copyLinkStatus === "error" ? "stable-label" : "sr-only"}
            role="status"
            aria-live="polite"
          >
            {copyLinkStatus === "copying"
              ? "Copying the run link."
              : copyLinkStatus === "copied"
                ? "Run link copied."
                : copyLinkStatus === "error"
                  ? "Unable to copy the run link. Try again."
                  : ""}
          </span>
          {runState === null || runState.manifest.status !== "completed" ? (
            <button className="primary-button" type="button" disabled>
              Download
            </button>
          ) : (
            <a
              className="primary-button"
              href={client.runDownloadUrl(runState.manifest.id)}
              download
            >
              Export report
            </a>
          )}
        </div>
      </header>

      <section className="kpi-strip" aria-label="Evaluation key measures">
        <div className="best-extractor-kpi">
          <span>Best extractor</span>
          <strong className="kpi-name">{bestSolution?.label ?? "Pending"}</strong>
          <b>
            {bestAggregate === null
              ? "—"
              : formatMeasure(bestAggregate.normalized_field_f1)}
          </b>
          <small>Normalized field F1</small>
        </div>
        <div>
          <span>Average field F1</span>
          <strong>
            {averageFieldF1 === null
              ? "—"
              : `${(averageFieldF1 * 100).toFixed(1)}%`}
          </strong>
          <small>Across comparable extractors</small>
        </div>
        <div>
          <span>Exact match</span>
          <strong>
            {averageExactness === null
              ? "—"
              : `${(averageExactness * 100).toFixed(1)}%`}
          </strong>
          <small>{aggregate?.scored_sample_count ?? 0} golden answers</small>
        </div>
        <div>
          <span>Failed extractions</span>
          <strong>
            {runState === null ? "—" : `${failures} / ${runState.manifest.total_pairs}`}
          </strong>
          <small>
            {runState === null || runState.manifest.total_pairs === 0
              ? "No active run"
              : `${((failures / runState.manifest.total_pairs) * 100).toFixed(2)}%`}
          </small>
        </div>
        <div>
          <span>Total cost</span>
          <strong>{totalCost === null ? "—" : `$${totalCost.toFixed(5)}`}</strong>
          <small>{aggregateError ?? lifecycleCopy(runState)}</small>
        </div>
      </section>

      <section className="performance-section" aria-labelledby="performance-heading">
        <header className="section-title-row">
          <div>
            <h2 id="performance-heading">Extractor performance</h2>
          </div>
          {runState !== null && (
            <span className="stable-label">
              {runState.manifest.completed_pairs}/{runState.manifest.total_pairs} terminal
            </span>
          )}
        </header>
        {runState === null ? (
          <div className="performance-empty">No immutable manifest yet.</div>
        ) : (
          <>
            <div className="performance-table-scroll">
              <table className="performance-table">
                <thead>
                  <tr>
                    <SortableColumnHeader
                      label="Extractor"
                      sortKeyName="extractor"
                      activeKey={sortKey}
                      direction={sortDirection}
                      onSort={handleSort}
                    />
                    <SortableColumnHeader
                      label="Type"
                      sortKeyName="type"
                      activeKey={sortKey}
                      direction={sortDirection}
                      onSort={handleSort}
                    />
                    <SortableColumnHeader
                      label="Progress"
                      sortKeyName="progress"
                      activeKey={sortKey}
                      direction={sortDirection}
                      onSort={handleSort}
                    />
                    <SortableColumnHeader
                      label="Field F1"
                      sortKeyName="field_f1"
                      activeKey={sortKey}
                      direction={sortDirection}
                      onSort={handleSort}
                    />
                    {hasCoverage && (
                      <SortableColumnHeader
                        label="Labeled coverage"
                        sortKeyName="coverage"
                        activeKey={sortKey}
                        direction={sortDirection}
                        onSort={handleSort}
                      />
                    )}
                    <SortableColumnHeader
                      label="Exactness"
                      sortKeyName="exactness"
                      activeKey={sortKey}
                      direction={sortDirection}
                      onSort={handleSort}
                    />
                    {hasTokenRecall && (
                      <SortableColumnHeader
                        label="Token recall"
                        sortKeyName="token_recall"
                        activeKey={sortKey}
                        direction={sortDirection}
                        onSort={handleSort}
                      />
                    )}
                    {hasCharacterErrorRate && (
                      <SortableColumnHeader
                        label="CER"
                        sortKeyName="cer"
                        activeKey={sortKey}
                        direction={sortDirection}
                        onSort={handleSort}
                      />
                    )}
                    {hasWordErrorRate && (
                      <SortableColumnHeader
                        label="WER"
                        sortKeyName="wer"
                        activeKey={sortKey}
                        direction={sortDirection}
                        onSort={handleSort}
                      />
                    )}
                    <SortableColumnHeader
                      label="Success"
                      sortKeyName="success"
                      activeKey={sortKey}
                      direction={sortDirection}
                      onSort={handleSort}
                    />
                    <SortableColumnHeader
                      label="Median latency"
                      sortKeyName="latency"
                      activeKey={sortKey}
                      direction={sortDirection}
                      onSort={handleSort}
                    />
                    <SortableColumnHeader
                      label="Cost"
                      sortKeyName="cost"
                      activeKey={sortKey}
                      direction={sortDirection}
                      onSort={handleSort}
                    />
                  </tr>
                </thead>
                <tbody>
                  {visibleRows.map(({ solution, row, terminal, totalTasks, referenceOnly }, index) => {
                    const presentation = configurationPresentation(project, solution);
                    const isOcrText =
                      (row?.metric_family ?? metricFamilyForSolution(solution)) === "ocr_text";
                    return (
                      <tr
                        className={
                          selectedSolutionId === solution.id ? "selected" : ""
                        }
                        key={solution.id}
                      >
                        <th scope="row">
                          <button
                            type="button"
                            onClick={() => onSolutionChange(solution.id)}
                          >
                            <span className="solution-index">
                              {index + 1}
                            </span>
                            <span>
                              <strong>{solution.label}</strong>
                              <small title={presentation.detail}>
                                {presentation.label}
                              </small>
                            </span>
                          </button>
                        </th>
                        <td>
                          <span className="solution-type">
                            {solutionTypeLabel(solution)}
                          </span>
                        </td>
                        <td>
                          <strong>
                            {terminal}/{totalTasks}
                          </strong>
                          <small>terminal</small>
                        </td>
                        <td>
                          <div className="accuracy-cell">
                            <strong>
                              {referenceOnly || isOcrText
                                ? "—"
                                : formatMeasure(row?.normalized_field_f1)}
                            </strong>
                            {!referenceOnly && !isOcrText && (
                              <span
                                className="accuracy-bar"
                                style={{
                                  "--accuracy-scale":
                                    row?.normalized_field_f1.value ?? 0,
                                } as CSSProperties}
                                aria-hidden="true"
                              >
                                <span />
                              </span>
                            )}
                          </div>
                        </td>
                        {hasCoverage && (
                          <td>
                            {referenceOnly || isOcrText
                              ? "—"
                              : formatMeasure(row?.labeled_field_coverage)}
                          </td>
                        )}
                        <td>
                          {referenceOnly || isOcrText
                            ? "—"
                            : formatMeasure(row?.whole_answer_exactness)}
                        </td>
                        {hasTokenRecall && (
                          <td>
                            {referenceOnly || !isOcrText
                              ? "—"
                              : formatMeasure(row?.reference_token_recall)}
                          </td>
                        )}
                        {hasCharacterErrorRate && (
                          <td>
                            {referenceOnly || !isOcrText
                              ? "—"
                              : formatMeasure(row?.character_error_rate)}
                          </td>
                        )}
                        {hasWordErrorRate && (
                          <td>
                            {referenceOnly || !isOcrText
                              ? "—"
                              : formatMeasure(row?.word_error_rate)}
                          </td>
                        )}
                        <td>{formatMeasure(row?.success_rate)}</td>
                        <td>{formatMeasure(row?.median_latency)}</td>
                        <td>{formatMeasure(row?.estimated_cost)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {tableRows.length > 5 && (
              <button
                className="show-extractors"
                type="button"
                onClick={() => setShowAllSolutions((current) => !current)}
              >
                {showAllSolutions
                  ? "Show top five"
                  : `Show all ${tableRows.length} extractors`}
              </button>
            )}
          </>
        )}
      </section>

      <ComparisonWorkspace
        project={project}
        samples={samples}
        solutions={solutions}
        runState={runState}
        resultLoadStatus={resultLoadStatus}
        selectedSampleId={selectedSampleId}
        selectedSolutionId={selectedSolutionId}
        onSampleChange={onSampleChange}
        onSolutionChange={onSolutionChange}
        onBuildRun={onBuildRun}
      />

      {runState !== null && (
        <section className="evaluation-evidence" aria-labelledby="evidence-surfaces-heading">
          <header className="evaluation-evidence-heading">
            <div>
              <span className="eyebrow">Run evidence</span>
              <h2 id="evidence-surfaces-heading">Operational and artifact detail</h2>
              <p aria-live="polite">
                {failureOnly
                  ? `Showing ${failures} failed or invalid ${failures === 1 ? "task" : "tasks"}.`
                  : "Comparison remains above; inspect execution, pipeline, or aggregate provenance here."}
              </p>
            </div>
            <div className="evidence-tabs" role="tablist" aria-label="Run evidence">
              {evidenceViews.map((view) => {
                const disabled = view === "batch" && aggregate === null;
                const selected = activeEvidenceView === view;
                return (
                  <button
                    key={view}
                    ref={(button) => {
                      evidenceTabRefs.current[view] = button;
                    }}
                    id={evidenceTabIds[view]}
                    type="button"
                    role="tab"
                    tabIndex={selected ? 0 : -1}
                    aria-selected={selected}
                    aria-controls={evidencePanelIds[view]}
                    aria-disabled={disabled}
                    className={selected ? "active" : ""}
                    disabled={disabled}
                    onClick={() => selectEvidenceView(view)}
                    onKeyDown={(event) =>
                      handleEvidenceTabKeyDown(event, view)
                    }
                  >
                    {evidenceViewLabels[view]}
                  </button>
                );
              })}
            </div>
          </header>
          <div
            id={evidencePanelIds.live}
            className="evaluation-evidence-panel"
            role="tabpanel"
            aria-labelledby={evidenceTabIds.live}
            hidden={activeEvidenceView !== "live"}
          >
            {activeEvidenceView === "live" && (
              <LiveWorkspace
                embedded
                project={project}
                runState={runState}
                visiblePairKeys={failureOnly ? failurePairKeys : undefined}
                samples={manifestSamples}
                solutions={selectedSolutions}
                onBuildRun={onBuildRun}
                onOpenComparison={(sampleId, solutionId) => {
                  onSampleChange(sampleId);
                  onSolutionChange(solutionId);
                  selectEvidenceView("pipeline");
                }}
              />
            )}
          </div>
          <div
            id={evidencePanelIds.pipeline}
            className="evaluation-evidence-panel"
            role="tabpanel"
            aria-labelledby={evidenceTabIds.pipeline}
            hidden={activeEvidenceView !== "pipeline"}
          >
            {activeEvidenceView === "pipeline" && (
              <PipelineInspector
                embedded
                project={project}
                samples={manifestSamples}
                solutions={selectedSolutions}
                results={manifestResults}
                resultLoadStatus={resultLoadStatus}
                selectedSampleId={selectedSampleId}
                selectedSolutionId={selectedSolutionId}
                onSampleChange={onSampleChange}
                onSolutionChange={onSolutionChange}
              />
            )}
          </div>
          <div
            id={evidencePanelIds.batch}
            className="evaluation-evidence-panel"
            role="tabpanel"
            aria-labelledby={evidenceTabIds.batch}
            hidden={activeEvidenceView !== "batch"}
          >
            {activeEvidenceView === "batch" && aggregate !== null && (
              <BatchResults
                embedded
                project={project}
                solutions={selectedSolutions}
                aggregate={aggregate}
                onOpenEvidence={(sampleId, solutionId) => {
                  onSampleChange(sampleId);
                  onSolutionChange(solutionId);
                  selectEvidenceView("pipeline");
                }}
              />
            )}
          </div>
        </section>
      )}

      {aggregate !== null && (
        <footer className="evaluation-footnote">
          <ChartIcon />
          <span>
            Aggregate {aggregate.run_id} is scoped to {aggregate.project_id};{" "}
            {aggregate.excluded_prompt_examples} prompt examples excluded.
          </span>
        </footer>
      )}
    </div>
  );
}
