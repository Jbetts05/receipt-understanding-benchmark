import { useEffect, useState, type CSSProperties } from "react";

import { resultPairKey } from "../domain";
import type {
  LiveRunState as LiveRunStateType,
} from "../streaming";
import type { Project, ReceiptSample, RunTask, Solution } from "../types";
import { ArrowIcon, CheckIcon, PlayIcon, PulseIcon, RetryIcon } from "./Icons";
import { ProjectHeader } from "./ProjectHeader";

const stageLabels: Record<RunTask["stage"], string> = {
  queued: "Queued",
  preflight: "Image preflight",
  ocr: "Reading document",
  normalizing: "Normalizing OCR",
  extracting: "Extracting fields",
  evaluating: "Comparing output",
  persisting: "Persisting artifacts",
  completed: "Complete",
  failed: "Terminal failure",
};

function formatElapsed(milliseconds: number): string {
  return milliseconds >= 1_000
    ? `${(milliseconds / 1_000).toFixed(1)}s`
    : `${Math.round(milliseconds)}ms`;
}

function formatCost(value: number | null): string {
  return value === null ? "—" : `$${value.toFixed(5)}`;
}

function partialSummary(task: RunTask): string | null {
  if (task.partial_output === null) {
    return null;
  }
  const first = Object.entries(task.partial_output)[0];
  if (first === undefined) {
    return null;
  }
  const value = typeof first[1] === "object" ? JSON.stringify(first[1]) : String(first[1]);
  return `${first[0]}: ${value}`;
}

export function visibleRunTasks(
  runState: LiveRunStateType,
  visiblePairKeys?: ReadonlySet<string>,
): RunTask[] {
  return runState.positionKeys
    .filter((key) => visiblePairKeys === undefined || visiblePairKeys.has(key))
    .map((key) => runState.tasks[key])
    .filter((task): task is RunTask => task !== undefined);
}

function TaskCell({
  task,
  onOpen,
}: {
  task: RunTask;
  onOpen: () => void;
}) {
  const terminal =
    task.status === "succeeded" ||
    task.status === "failed" ||
    task.status === "invalid";
  const summary = partialSummary(task);
  return (
    <button
      className={`task-cell task-${task.status}`}
      type="button"
      disabled={!terminal}
      onClick={onOpen}
      aria-label={`${stageLabels[task.stage]}, ${Math.round(task.progress * 100)} percent`}
    >
      <span className="task-cell-topline">
        <span className="task-state">
          {task.status === "succeeded" ? (
            <CheckIcon />
          ) : task.status === "retrying" ? (
            <RetryIcon />
          ) : (
            <span className="task-dot" aria-hidden="true" />
          )}
          {stageLabels[task.stage]}
        </span>
        <strong>{Math.round(task.progress * 100)}%</strong>
      </span>
      <span
        className="task-progress"
        style={{ "--progress-scale": task.progress } as CSSProperties}
        aria-hidden="true"
      >
        <span />
      </span>
      <span className="task-cell-meta">
        <span>{formatElapsed(task.elapsed_ms)}</span>
        <span>{task.retries} retries</span>
        <span>{formatCost(task.estimated_cost_usd)}</span>
      </span>
      {summary !== null && <code>{summary}</code>}
      {task.error !== null && <small>{task.error}</small>}
    </button>
  );
}

export function LiveWorkspace({
  project,
  runState,
  samples,
  solutions,
  onBuildRun,
  onOpenComparison,
  embedded = false,
  visiblePairKeys,
}: {
  project: Project;
  runState: LiveRunStateType | null;
  samples: ReceiptSample[];
  solutions: Solution[];
  onBuildRun: () => void;
  onOpenComparison: (sampleId: string, solutionId: string) => void;
  embedded?: boolean;
  visiblePairKeys?: ReadonlySet<string>;
}) {
  const [sortKey, setSortKey] = useState("solution");
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("asc");

  useEffect(() => {
    setSortKey("solution");
    setSortDirection("asc");
  }, [runState?.manifest.id, visiblePairKeys]);

  if (runState === null || runState.manifest.project_id !== project.id) {
    return (
      <div className={embedded ? "embedded-workspace" : "view-shell"}>
        {!embedded && (
          <ProjectHeader
            project={project}
            eyebrow="Live run"
            title="No active run"
            description="Choose receipts and compatible solution paths to begin a durable parallel run."
            compact
          />
        )}
        <div className="large-empty-state">
          <PulseIcon />
          <h2>The task grid will stay in configured order</h2>
          <p>Stages, retries, elapsed time, partial output, and cost appear as events arrive.</p>
          <button className="primary-button" type="button" onClick={onBuildRun}>
            <PlayIcon />
            Build a run
          </button>
        </div>
      </div>
    );
  }

  const tasks = visibleRunTasks(runState, visiblePairKeys);
  const visibleSampleIds = new Set(tasks.map((task) => task.sample_id));
  const visibleSolutionIds = new Set(tasks.map((task) => task.solution_id));
  const selectedSolutions = runState.manifest.solution_ids
    .filter(
      (id) => visiblePairKeys === undefined || visibleSolutionIds.has(id),
    )
    .map((id) => solutions.find((solution) => solution.id === id))
    .filter((solution): solution is Solution => solution !== undefined);
  const selectedSamples = runState.manifest.sample_ids
    .filter(
      (id) => visiblePairKeys === undefined || visibleSampleIds.has(id),
    )
    .map((id) =>
      samples.find(
        (sample) =>
          sample.id === id && sample.project_id === runState.manifest.project_id,
      ),
    )
    .filter((sample): sample is ReceiptSample => sample !== undefined);
  const visibleTaskByPairId = new Map(
    tasks.map((task) => [task.pair_id, task]),
  );
  const succeeded = tasks.filter((task) => task.status === "succeeded").length;
  const failed = tasks.filter(
    (task) => task.status === "failed" || task.status === "invalid",
  ).length;
  const retrying = tasks.filter((task) => task.status === "retrying").length;
  const terminal = tasks.filter(
    (task) =>
      task.status === "succeeded" ||
      task.status === "failed" ||
      task.status === "invalid",
  ).length;
  const totalPairs =
    visiblePairKeys === undefined ? runState.manifest.total_pairs : tasks.length;
  const terminalPairs =
    visiblePairKeys === undefined
      ? runState.manifest.completed_pairs
      : terminal;
  const progress =
    totalPairs === 0
      ? 0
      : terminalPairs / totalPairs;
  const sortedSolutions = [...selectedSolutions].sort((left, right) => {
    let comparison: number;
    if (sortKey === "solution") {
      comparison = left.label.localeCompare(right.label, undefined, {
        numeric: true,
      });
    } else {
      const leftTask = visibleTaskByPairId.get(resultPairKey(sortKey, left.id));
      const rightTask = visibleTaskByPairId.get(resultPairKey(sortKey, right.id));
      const leftValue = leftTask?.progress ?? -1;
      const rightValue = rightTask?.progress ?? -1;
      comparison = leftValue - rightValue;
      if (comparison === 0) {
        comparison = (leftTask?.elapsed_ms ?? -1) - (rightTask?.elapsed_ms ?? -1);
      }
    }
    const directed = sortDirection === "asc" ? comparison : -comparison;
    return directed || left.order - right.order;
  });

  function sortTasks(key: string) {
    if (key === sortKey) {
      setSortDirection((current) => (current === "asc" ? "desc" : "asc"));
      return;
    }
    setSortKey(key);
    setSortDirection(key === "solution" ? "asc" : "desc");
  }

  return (
    <div className={embedded ? "embedded-workspace live-view" : "view-shell live-view"}>
      {!embedded && (
        <ProjectHeader
          project={project}
          eyebrow="Live run"
          title={
            runState.manifest.status === "completed"
              ? "Run complete"
              : "Parallel extraction in progress"
          }
          description={`Run ${runState.manifest.id} · immutable schema and prompt versions`}
          actions={
            <button className="secondary-button" type="button" onClick={onBuildRun}>
              New run
            </button>
          }
          compact
        />
      )}

      <section className="run-telemetry" aria-label="Run progress">
        <div className="run-progress-copy">
          <strong>
            {terminalPairs} / {totalPairs}
          </strong>
          <span>
            {visiblePairKeys === undefined
              ? "terminal pairs"
              : "visible terminal pairs"}
          </span>
        </div>
        <div
          className="overall-progress"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={totalPairs}
          aria-valuenow={terminalPairs}
          style={{ "--progress-scale": progress } as CSSProperties}
        >
          <span />
        </div>
        <dl>
          <div>
            <dt>Succeeded</dt>
            <dd>{succeeded}</dd>
          </div>
          <div>
            <dt>Retrying</dt>
            <dd>{retrying}</dd>
          </div>
          <div>
            <dt>Failed / invalid</dt>
            <dd>{failed}</dd>
          </div>
          <div>
            <dt>Execution</dt>
            <dd>{runState.manifest.execution_mode}</dd>
          </div>
        </dl>
      </section>

      <section className="task-matrix-section" aria-labelledby="task-grid-heading">
        <div className="section-title-row">
          <div>
            <h2 id="task-grid-heading">
              {visiblePairKeys === undefined
                ? "Receipt × solution grid"
                : "Filtered task pairs"}
            </h2>
            <p>
              {visiblePairKeys === undefined
                ? "Sort by solution or by progress for any receipt column."
                : "Only the exact receipt and solution pairs in this filtered view are shown."}
            </p>
          </div>
          <span className="stable-label">
            <span aria-hidden="true" />
            Durable event order
          </span>
        </div>
        <div className="task-matrix-scroll">
          {visiblePairKeys === undefined ? (
            <table className="task-matrix">
              <thead>
                <tr>
                  <th
                    scope="col"
                    aria-sort={
                      sortKey === "solution"
                        ? sortDirection === "asc"
                          ? "ascending"
                          : "descending"
                        : undefined
                    }
                  >
                    <button
                      className="table-sort-button"
                      type="button"
                      onClick={() => sortTasks("solution")}
                    >
                      Solution
                      <span aria-hidden="true">
                        {sortKey === "solution"
                          ? sortDirection === "asc"
                            ? "▲"
                            : "▼"
                          : "↕"}
                      </span>
                    </button>
                  </th>
                  {selectedSamples.map((sample) => (
                    <th
                      scope="col"
                      key={sample.id}
                      aria-sort={
                        sortKey === sample.id
                          ? sortDirection === "asc"
                            ? "ascending"
                            : "descending"
                          : undefined
                      }
                    >
                      <button
                        className="table-sort-button"
                        type="button"
                        onClick={() => sortTasks(sample.id)}
                      >
                        <span>{sample.title}</span>
                        <code>{sample.id}</code>
                        <span aria-hidden="true">
                          {sortKey === sample.id
                            ? sortDirection === "asc"
                              ? "▲"
                              : "▼"
                            : "↕"}
                        </span>
                      </button>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sortedSolutions.map((solution) => (
                  <tr key={solution.id}>
                    <th scope="row">
                      <span className="solution-index">
                        {String(solution.order + 1).padStart(2, "0")}
                      </span>
                      <span>
                        <strong>{solution.label}</strong>
                        <small>{solution.kind.replace("_", " ")}</small>
                      </span>
                    </th>
                    {selectedSamples.map((sample) => {
                      const key = resultPairKey(sample.id, solution.id);
                      const task = visibleTaskByPairId.get(key);
                      return (
                        <td key={key}>
                          {task !== undefined && (
                            <TaskCell
                              task={task}
                              onOpen={() =>
                                onOpenComparison(task.sample_id, task.solution_id)
                              }
                            />
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <table className="task-matrix">
              <thead>
                <tr>
                  <th scope="col">Receipt</th>
                  <th scope="col">Solution</th>
                  <th scope="col">Task evidence</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => {
                  const sample = samples.find(
                    (item) => item.id === task.sample_id,
                  );
                  const solution = solutions.find(
                    (item) => item.id === task.solution_id,
                  );
                  if (sample === undefined || solution === undefined) {
                    return null;
                  }
                  return (
                    <tr key={task.pair_id}>
                      <th scope="row">
                        <strong>{sample.title}</strong>
                        <small>{sample.id}</small>
                      </th>
                      <td>
                        <strong>{solution.label}</strong>
                        <small>{solution.kind.replace("_", " ")}</small>
                      </td>
                      <td>
                        <TaskCell
                          task={task}
                          onOpen={() =>
                            onOpenComparison(task.sample_id, task.solution_id)
                          }
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </section>

      {runState.manifest.status === "completed" && (
        <div className="terminal-banner" role="status">
          <CheckIcon />
          <span>
            <strong>
              {visiblePairKeys === undefined
                ? "All pairs reached a terminal state."
                : "All visible pairs reached a terminal state."}
            </strong>
            Results remain linked to raw responses, parsed output, and pipeline evidence.
          </span>
          <button
            className="secondary-button"
            type="button"
            disabled={tasks[0] === undefined}
            onClick={() => {
              const firstVisibleTask = tasks[0];
              if (firstVisibleTask !== undefined) {
                onOpenComparison(
                  firstVisibleTask.sample_id,
                  firstVisibleTask.solution_id,
                );
              }
            }}
          >
            Compare outputs
            <ArrowIcon />
          </button>
        </div>
      )}
    </div>
  );
}
