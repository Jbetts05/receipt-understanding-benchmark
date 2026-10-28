import { useMemo, useState } from "react";

import { isTerminalRunStatus } from "../domain";
import type { Project, RunHistoryItem } from "../types";
import { PlayIcon, TrashIcon } from "./Icons";
import { ProjectHeader } from "./ProjectHeader";

type RunSortKey =
  | "status"
  | "run"
  | "started"
  | "progress"
  | "receipts"
  | "extractors"
  | "mode";
type SortDirection = "asc" | "desc";

function RunSortHeader({
  label,
  sortKey,
  activeKey,
  direction,
  onSort,
}: {
  label: string;
  sortKey: RunSortKey;
  activeKey: RunSortKey;
  direction: SortDirection;
  onSort: (key: RunSortKey) => void;
}) {
  const active = sortKey === activeKey;
  return (
    <th
      scope="col"
      aria-sort={
        active ? (direction === "asc" ? "ascending" : "descending") : undefined
      }
    >
      <button
        className="table-sort-button"
        type="button"
        onClick={() => onSort(sortKey)}
      >
        <span>{label}</span>
        <span aria-hidden="true">{active ? (direction === "asc" ? "▲" : "▼") : "↕"}</span>
      </button>
    </th>
  );
}

function formatRunTime(value: string): string {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.valueOf())) {
    return "Unknown";
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(timestamp);
}

export function RunsWorkspace({
  project,
  runs,
  activeRunId,
  loading,
  error,
  restoringRunId,
  onRefresh,
  onOpen,
  onDelete,
  onBuildRun,
}: {
  project: Project;
  runs: RunHistoryItem[];
  activeRunId: string | null;
  loading: boolean;
  error: string | null;
  restoringRunId: string | null;
  onRefresh: () => void;
  onOpen: (run: RunHistoryItem) => void;
  onDelete: (runId: string) => void;
  onBuildRun: () => void;
}) {
  const [sortKey, setSortKey] = useState<RunSortKey>("started");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const terminalRuns = runs.filter((run) => isTerminalRunStatus(run.status)).length;
  const completedPairs = runs.reduce((sum, run) => sum + run.completed_pairs, 0);
  const totalPairs = runs.reduce((sum, run) => sum + run.total_pairs, 0);
  const sortedRuns = useMemo(
    () =>
      [...runs].sort((left, right) => {
        const values: Record<RunSortKey, [string | number | null, string | number | null]> = {
          status: [left.status, right.status],
          run: [left.run_id, right.run_id],
          started: [Date.parse(left.created_at), Date.parse(right.created_at)],
          progress: [
            left.total_pairs === 0 ? 0 : left.completed_pairs / left.total_pairs,
            right.total_pairs === 0 ? 0 : right.completed_pairs / right.total_pairs,
          ],
          receipts: [left.sample_count, right.sample_count],
          extractors: [left.solution_count, right.solution_count],
          mode: [left.execution_mode, right.execution_mode],
        };
        const [leftValue, rightValue] = values[sortKey];
        if (leftValue === null && rightValue === null) {
          return left.run_id.localeCompare(right.run_id);
        }
        if (leftValue === null) return 1;
        if (rightValue === null) return -1;
        const comparison =
          typeof leftValue === "number" && typeof rightValue === "number"
            ? leftValue - rightValue
            : String(leftValue).localeCompare(String(rightValue), undefined, {
                numeric: true,
              });
        const directed = sortDirection === "asc" ? comparison : -comparison;
        return directed || left.run_id.localeCompare(right.run_id);
      }),
    [runs, sortDirection, sortKey],
  );

  function sortBy(key: RunSortKey) {
    if (key === sortKey) {
      setSortDirection((current) => (current === "asc" ? "desc" : "asc"));
      return;
    }
    setSortKey(key);
    setSortDirection(
      key === "started" || key === "progress" || key === "receipts" || key === "extractors"
        ? "desc"
        : "asc",
    );
  }

  return (
    <div className="view-shell runs-view">
      <ProjectHeader
        project={project}
        eyebrow="Runs"
        title={`${project.short_name} run history`}
        description="Restore, inspect, or remove durable experiment runs for this dataset."
        compact
        actions={
          <div className="runs-header-actions">
            <button
              className="secondary-button"
              type="button"
              disabled={loading}
              onClick={onRefresh}
            >
              {loading ? "Refreshing…" : "Refresh"}
            </button>
            <button className="primary-button" type="button" onClick={onBuildRun}>
              <PlayIcon />
              Configure run
            </button>
          </div>
        }
      />

      <section className="runs-summary" aria-label="Run history summary">
        <div>
          <span>Recorded runs</span>
          <strong>{runs.length}</strong>
        </div>
        <div>
          <span>Terminal runs</span>
          <strong>{terminalRuns}</strong>
        </div>
        <div>
          <span>Completed pairs</span>
          <strong>
            {completedPairs}/{totalPairs}
          </strong>
        </div>
      </section>

      {error !== null && (
        <p className="runs-error" role="alert">
          {error}
        </p>
      )}

      <section className="runs-table-section" aria-labelledby="runs-table-heading">
        <div className="section-title-row">
          <div>
            <h2 id="runs-table-heading">Project runs</h2>
            <p>Newest first. Opening a run restores its receipt and solution evidence.</p>
          </div>
          <span className="stable-label">{loading ? "Refreshing" : `${runs.length} runs`}</span>
        </div>
        {loading && runs.length === 0 ? (
          <div className="runs-empty" aria-live="polite">
            Loading run history…
          </div>
        ) : runs.length === 0 ? (
          <div className="runs-empty">
            <PlayIcon />
            <div>
              <h2>No runs yet</h2>
              <p>Configure the first comparison run for this dataset.</p>
            </div>
            <button className="primary-button" type="button" onClick={onBuildRun}>
              Configure run
            </button>
          </div>
        ) : (
          <div className="runs-table-scroll">
            <table className="runs-table">
              <thead>
                <tr>
                  <RunSortHeader label="Status" sortKey="status" activeKey={sortKey} direction={sortDirection} onSort={sortBy} />
                  <RunSortHeader label="Run" sortKey="run" activeKey={sortKey} direction={sortDirection} onSort={sortBy} />
                  <RunSortHeader label="Started" sortKey="started" activeKey={sortKey} direction={sortDirection} onSort={sortBy} />
                  <RunSortHeader label="Progress" sortKey="progress" activeKey={sortKey} direction={sortDirection} onSort={sortBy} />
                  <RunSortHeader label="Receipts" sortKey="receipts" activeKey={sortKey} direction={sortDirection} onSort={sortBy} />
                  <RunSortHeader label="Extractors" sortKey="extractors" activeKey={sortKey} direction={sortDirection} onSort={sortBy} />
                  <RunSortHeader label="Mode" sortKey="mode" activeKey={sortKey} direction={sortDirection} onSort={sortBy} />
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {sortedRuns.map((run) => {
                  const active = run.run_id === activeRunId;
                  const restoring = run.run_id === restoringRunId;
                  const terminal = isTerminalRunStatus(run.status);
                  return (
                    <tr className={active ? "active" : ""} key={run.run_id}>
                      <td>
                        <span className={`runs-status status-${run.status}`}>
                          <span aria-hidden="true" />
                          {run.status}
                        </span>
                      </td>
                      <th scope="row">
                        <code title={run.run_id}>{run.run_id}</code>
                        {active && <small>Active</small>}
                      </th>
                      <td>
                        <time dateTime={run.created_at}>{formatRunTime(run.created_at)}</time>
                      </td>
                      <td>
                        <strong>
                          {run.completed_pairs}/{run.total_pairs}
                        </strong>
                        <small>pairs</small>
                      </td>
                      <td>{run.sample_count ?? "—"}</td>
                      <td>{run.solution_count ?? "—"}</td>
                      <td>{run.execution_mode ?? "—"}</td>
                      <td>
                        <div className="runs-row-actions">
                          <button
                            className="text-button"
                            type="button"
                            disabled={restoring}
                            onClick={() => onOpen(run)}
                          >
                            {restoring ? "Restoring…" : "Open"}
                          </button>
                          <button
                            className="text-button danger-text"
                            type="button"
                            disabled={!terminal}
                            title={
                              terminal
                                ? `Delete ${run.run_id}`
                                : "Only completed or failed runs can be deleted."
                            }
                            onClick={() => onDelete(run.run_id)}
                          >
                            <TrashIcon />
                            Delete
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
