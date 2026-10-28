import { useEffect, useMemo, useState, type KeyboardEvent } from "react";

import type { ProductClient } from "../api";
import {
  getCompatibility,
  isTerminalRunStatus,
  modelDisplayName,
  solutionTypeLabel,
} from "../domain";
import type {
  Project,
  RunHistoryItem,
  Solution,
} from "../types";
import { GridIcon, PlayIcon, PulseIcon, TrashIcon } from "./Icons";

export type AdminSection = "datasets" | "runs" | "services";
type SortDirection = "asc" | "desc";
type DatasetSortKey =
  | "name"
  | "kind"
  | "state"
  | "receipts"
  | "gold"
  | "runs"
  | "created";
type RunSortKey =
  | "status"
  | "dataset"
  | "run"
  | "started"
  | "progress"
  | "receipts"
  | "services"
  | "mode";
type ServiceSortKey =
  | "service"
  | "provider"
  | "type"
  | "lifecycle"
  | "model"
  | "assistance"
  | "role";

export function getAdminTabDestination(
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

export interface AdminRunInventory extends RunHistoryItem {
  project_name: string;
  project_short_name: string;
}

function SortHeader<T extends string>({
  label,
  sortKey,
  activeKey,
  direction,
  onSort,
}: {
  label: string;
  sortKey: T;
  activeKey: T;
  direction: SortDirection;
  onSort: (key: T) => void;
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
        <span aria-hidden="true">
          {active ? (direction === "asc" ? "▲" : "▼") : "↕"}
        </span>
      </button>
    </th>
  );
}

function compareValues(
  left: string | number | null,
  right: string | number | null,
  direction: SortDirection,
): number {
  if (left === null && right === null) return 0;
  if (left === null) return 1;
  if (right === null) return -1;
  const comparison =
    typeof left === "number" && typeof right === "number"
      ? left - right
      : String(left).localeCompare(String(right), undefined, { numeric: true });
  return direction === "asc" ? comparison : -comparison;
}

function formatDate(value: string): string {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.valueOf())) {
    return "Unknown";
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(timestamp);
}

function serviceAssistance(solution: Solution): string {
  const parts = [
    solution.normalizer === null
      ? null
      : `Normalizer: ${modelDisplayName(solution.normalizer)}`,
    solution.internal_models.length === 0
      ? null
      : `Internal: ${solution.internal_models.map(modelDisplayName).join(", ")}`,
  ].filter((item): item is string => item !== null);
  return parts.length === 0 ? "None" : parts.join(" · ");
}

export function AdminCenter({
  client,
  projects,
  project,
  solutions,
  refreshToken,
  onDeleteDataset,
  onDeleteRun,
  initialSection = "datasets",
  initialRuns,
}: {
  client: Pick<ProductClient, "listProjectRuns">;
  projects: Project[];
  project: Project;
  solutions: Solution[];
  refreshToken: number;
  onDeleteDataset: (projectId: string, projectName: string) => void;
  onDeleteRun: (runId: string) => void;
  initialSection?: AdminSection;
  initialRuns?: AdminRunInventory[];
}) {
  const [section, setSection] = useState<AdminSection>(initialSection);
  const [query, setQuery] = useState("");
  const [runs, setRuns] = useState<AdminRunInventory[]>(initialRuns ?? []);
  const [loading, setLoading] = useState(initialRuns === undefined);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [datasetSort, setDatasetSort] = useState<DatasetSortKey>("created");
  const [datasetDirection, setDatasetDirection] =
    useState<SortDirection>("desc");
  const [runSort, setRunSort] = useState<RunSortKey>("started");
  const [runDirection, setRunDirection] = useState<SortDirection>("desc");
  const [serviceSort, setServiceSort] =
    useState<ServiceSortKey>("service");
  const [serviceDirection, setServiceDirection] =
    useState<SortDirection>("asc");

  useEffect(() => {
    if (initialRuns !== undefined) {
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void Promise.all(
      projects.map(async (item) => {
        const history = await client.listProjectRuns(
          item.id,
          100,
          controller.signal,
        );
        return history.map((run) => ({
          ...run,
          project_name: item.name,
          project_short_name: item.short_name,
        }));
      }),
    ).then(
      (groups) => {
        if (!controller.signal.aborted) {
          setRuns(groups.flat());
          setLoading(false);
        }
      },
      (reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(
            reason instanceof Error
              ? reason.message
              : "Run inventory could not be loaded.",
          );
          setLoading(false);
        }
      },
    );
    return () => controller.abort();
  }, [client, initialRuns, projects, refreshToken, reloadToken]);

  const normalizedQuery = query.trim().toLocaleLowerCase("en-US");
  const visibleRuns = useMemo(
    () =>
      runs.filter((run) =>
        normalizedQuery.length === 0
          ? true
          : [
              run.run_id,
              run.project_id,
              run.project_name,
              run.status,
              run.execution_mode ?? "",
            ].some((value) =>
              value.toLocaleLowerCase("en-US").includes(normalizedQuery),
            ),
      ),
    [normalizedQuery, runs],
  );
  const visibleProjects = useMemo(
    () =>
      projects.filter((item) =>
        normalizedQuery.length === 0
          ? true
          : [
              item.id,
              item.name,
              item.short_name,
              item.kind,
              item.dataset_state,
            ].some((value) =>
              value.toLocaleLowerCase("en-US").includes(normalizedQuery),
            ),
      ),
    [normalizedQuery, projects],
  );
  const visibleSolutions = useMemo(
    () =>
      solutions.filter((solution) =>
        normalizedQuery.length === 0
          ? true
          : [
              solution.id,
              solution.label,
              solution.provider,
              solution.kind,
              solution.model ?? "",
              solution.normalizer ?? "",
            ].some((value) =>
              value.toLocaleLowerCase("en-US").includes(normalizedQuery),
            ),
      ),
    [normalizedQuery, solutions],
  );
  const runCounts = useMemo(() => {
    const counts = new Map<string, number>();
    runs.forEach((run) =>
      counts.set(run.project_id, (counts.get(run.project_id) ?? 0) + 1),
    );
    return counts;
  }, [runs]);
  const activeRunCounts = useMemo(() => {
    const counts = new Map<string, number>();
    runs
      .filter((run) => !isTerminalRunStatus(run.status))
      .forEach((run) =>
        counts.set(run.project_id, (counts.get(run.project_id) ?? 0) + 1),
      );
    return counts;
  }, [runs]);

  const sortedProjects = useMemo(
    () =>
      [...visibleProjects].sort((left, right) => {
        const values: Record<
          DatasetSortKey,
          [string | number | null, string | number | null]
        > = {
          name: [left.name, right.name],
          kind: [left.kind, right.kind],
          state: [left.dataset_state, right.dataset_state],
          receipts: [left.sample_count, right.sample_count],
          gold: [left.gold_sample_count, right.gold_sample_count],
          runs: [
            runCounts.get(left.id) ?? 0,
            runCounts.get(right.id) ?? 0,
          ],
          created: [Date.parse(left.created_at), Date.parse(right.created_at)],
        };
        const [leftValue, rightValue] = values[datasetSort];
        return (
          compareValues(leftValue, rightValue, datasetDirection) ||
          left.id.localeCompare(right.id)
        );
      }),
    [datasetDirection, datasetSort, runCounts, visibleProjects],
  );
  const sortedRuns = useMemo(
    () =>
      [...visibleRuns].sort((left, right) => {
        const values: Record<
          RunSortKey,
          [string | number | null, string | number | null]
        > = {
          status: [left.status, right.status],
          dataset: [left.project_name, right.project_name],
          run: [left.run_id, right.run_id],
          started: [Date.parse(left.created_at), Date.parse(right.created_at)],
          progress: [
            left.completed_pairs / left.total_pairs,
            right.completed_pairs / right.total_pairs,
          ],
          receipts: [left.sample_count, right.sample_count],
          services: [left.solution_count, right.solution_count],
          mode: [left.execution_mode, right.execution_mode],
        };
        const [leftValue, rightValue] = values[runSort];
        return (
          compareValues(leftValue, rightValue, runDirection) ||
          left.run_id.localeCompare(right.run_id)
        );
      }),
    [runDirection, runSort, visibleRuns],
  );
  const sortedSolutions = useMemo(
    () =>
      [...visibleSolutions].sort((left, right) => {
        const values: Record<
          ServiceSortKey,
          [string | number | null, string | number | null]
        > = {
          service: [left.label, right.label],
          provider: [left.provider, right.provider],
          type: [solutionTypeLabel(left), solutionTypeLabel(right)],
          lifecycle: [left.lifecycle, right.lifecycle],
          model: [
            left.model === null ? null : modelDisplayName(left.model),
            right.model === null ? null : modelDisplayName(right.model),
          ],
          assistance: [serviceAssistance(left), serviceAssistance(right)],
          role: [
            getCompatibility(project, left),
            getCompatibility(project, right),
          ],
        };
        const [leftValue, rightValue] = values[serviceSort];
        return (
          compareValues(leftValue, rightValue, serviceDirection) ||
          left.id.localeCompare(right.id)
        );
      }),
    [project, serviceDirection, serviceSort, visibleSolutions],
  );

  function sortDatasets(key: DatasetSortKey) {
    if (key === datasetSort) {
      setDatasetDirection((current) => (current === "asc" ? "desc" : "asc"));
      return;
    }
    setDatasetSort(key);
    setDatasetDirection(key === "name" || key === "kind" || key === "state" ? "asc" : "desc");
  }

  function sortRuns(key: RunSortKey) {
    if (key === runSort) {
      setRunDirection((current) => (current === "asc" ? "desc" : "asc"));
      return;
    }
    setRunSort(key);
    setRunDirection(
      key === "started" || key === "progress" || key === "receipts" || key === "services"
        ? "desc"
        : "asc",
    );
  }

  function sortServices(key: ServiceSortKey) {
    if (key === serviceSort) {
      setServiceDirection((current) => (current === "asc" ? "desc" : "asc"));
      return;
    }
    setServiceSort(key);
    setServiceDirection("asc");
  }

  function handleTabKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    const tablist = event.currentTarget.parentElement;
    const tabs =
      tablist === null
        ? []
        : Array.from(
            tablist.querySelectorAll<HTMLButtonElement>(
              '[role="tab"]:not(:disabled)',
            ),
          );
    const destination = getAdminTabDestination(
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

  const activeRuns = runs.filter((run) => !isTerminalRunStatus(run.status)).length;

  return (
    <div className="view-shell admin-view">
      <header className="admin-header">
        <div>
          <h1>Admin Center</h1>
          <p>
            Review datasets, runs, and registered services across this deployment.
            Deletion uses the site&apos;s existing access model and durable backend guards.
          </p>
        </div>
        <span className="stable-label">Same access as this site</span>
      </header>

      <section className="admin-summary" aria-label="Administrative inventory summary">
        <div>
          <GridIcon />
          <span>Datasets</span>
          <strong>{projects.length}</strong>
        </div>
        <div>
          <PlayIcon />
          <span>Recorded runs</span>
          <strong>{runs.length}</strong>
        </div>
        <div>
          <PlayIcon />
          <span>Active runs</span>
          <strong>{activeRuns}</strong>
        </div>
        <div>
          <PulseIcon />
          <span>Services</span>
          <strong>{solutions.length}</strong>
        </div>
      </section>

      <section className="admin-inventory" aria-labelledby="admin-inventory-heading">
        <div className="admin-toolbar">
          <div>
            <h2 id="admin-inventory-heading">Deployment inventory</h2>
            <p>Up to 100 recent runs are loaded for each dataset.</p>
          </div>
          <label className="admin-search">
            <span className="sr-only">Filter administrative inventory</span>
            <input
              type="search"
              value={query}
              placeholder="Filter this inventory"
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
        </div>

        <div className="admin-tabs" role="tablist" aria-label="Admin Center sections">
          {(
            [
              ["datasets", "Datasets", projects.length],
              ["runs", "Runs", runs.length],
              ["services", "Services", solutions.length],
            ] as const
          ).map(([value, label, count]) => (
            <button
              type="button"
              role="tab"
              id={`admin-center-tab-${value}`}
              aria-controls={`admin-center-panel-${value}`}
              aria-selected={section === value}
              tabIndex={section === value ? 0 : -1}
              className={section === value ? "active" : ""}
              onClick={() => setSection(value)}
              onKeyDown={handleTabKeyDown}
              key={value}
            >
              {label}
              <span>{count}</span>
            </button>
          ))}
        </div>

        {error !== null && (
          <div className="admin-error" role="alert">
            <span>{error}</span>
            <button
              className="text-button"
              type="button"
              onClick={() => setReloadToken((current) => current + 1)}
            >
              Retry
            </button>
          </div>
        )}

        {section === "datasets" && (
          <div
            className="admin-table-scroll"
            role="tabpanel"
            id="admin-center-panel-datasets"
            aria-labelledby="admin-center-tab-datasets"
          >
            <table className="admin-table">
              <thead>
                <tr>
                  <SortHeader label="Dataset" sortKey="name" activeKey={datasetSort} direction={datasetDirection} onSort={sortDatasets} />
                  <SortHeader label="Type" sortKey="kind" activeKey={datasetSort} direction={datasetDirection} onSort={sortDatasets} />
                  <SortHeader label="State" sortKey="state" activeKey={datasetSort} direction={datasetDirection} onSort={sortDatasets} />
                  <SortHeader label="Receipts" sortKey="receipts" activeKey={datasetSort} direction={datasetDirection} onSort={sortDatasets} />
                  <SortHeader label="Gold" sortKey="gold" activeKey={datasetSort} direction={datasetDirection} onSort={sortDatasets} />
                  <SortHeader label="Runs" sortKey="runs" activeKey={datasetSort} direction={datasetDirection} onSort={sortDatasets} />
                  <SortHeader label="Created" sortKey="created" activeKey={datasetSort} direction={datasetDirection} onSort={sortDatasets} />
                  <th scope="col">Action</th>
                </tr>
              </thead>
              <tbody>
                {sortedProjects.map((item) => {
                  const activeForDataset = activeRunCounts.get(item.id) ?? 0;
                  const inventoryReady = !loading && error === null;
                  const deletable =
                    item.kind === "custom" &&
                    inventoryReady &&
                    activeForDataset === 0;
                  const deleteTitle = deletable
                    ? `Delete ${item.name}`
                    : loading
                      ? "Wait for the run inventory to finish loading"
                      : error !== null
                        ? "Run inventory must be available before deletion"
                        : `${activeForDataset} active run(s) must finish first`;
                  return (
                    <tr key={item.id}>
                      <th scope="row">
                        <strong>{item.name}</strong>
                        <code>{item.id}</code>
                      </th>
                      <td>{item.kind === "custom" ? "Custom" : "Built-in"}</td>
                      <td>{item.dataset_state}</td>
                      <td>{item.sample_count}</td>
                      <td>{item.gold_sample_count}</td>
                      <td>
                        {loading ? "Loading…" : runCounts.get(item.id) ?? 0}
                      </td>
                      <td>
                        <time dateTime={item.created_at}>{formatDate(item.created_at)}</time>
                      </td>
                      <td>
                        {item.kind !== "custom" ? (
                          <span className="admin-protected">Protected</span>
                        ) : (
                          <button
                            className="admin-delete"
                            type="button"
                            disabled={!deletable}
                            title={deleteTitle}
                            onClick={() => onDeleteDataset(item.id, item.name)}
                          >
                            <TrashIcon />
                            Delete
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {section === "runs" && (
          <div
            className="admin-table-scroll"
            role="tabpanel"
            id="admin-center-panel-runs"
            aria-labelledby="admin-center-tab-runs"
          >
            {loading && runs.length === 0 ? (
              <div className="admin-empty" aria-live="polite">
                Loading run inventory…
              </div>
            ) : sortedRuns.length === 0 ? (
              <div className="admin-empty">No runs match this filter.</div>
            ) : (
              <table className="admin-table admin-runs-table">
                <thead>
                  <tr>
                    <SortHeader label="Status" sortKey="status" activeKey={runSort} direction={runDirection} onSort={sortRuns} />
                    <SortHeader label="Dataset" sortKey="dataset" activeKey={runSort} direction={runDirection} onSort={sortRuns} />
                    <SortHeader label="Run" sortKey="run" activeKey={runSort} direction={runDirection} onSort={sortRuns} />
                    <SortHeader label="Started" sortKey="started" activeKey={runSort} direction={runDirection} onSort={sortRuns} />
                    <SortHeader label="Progress" sortKey="progress" activeKey={runSort} direction={runDirection} onSort={sortRuns} />
                    <SortHeader label="Receipts" sortKey="receipts" activeKey={runSort} direction={runDirection} onSort={sortRuns} />
                    <SortHeader label="Services" sortKey="services" activeKey={runSort} direction={runDirection} onSort={sortRuns} />
                    <SortHeader label="Mode" sortKey="mode" activeKey={runSort} direction={runDirection} onSort={sortRuns} />
                    <th scope="col">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedRuns.map((run) => {
                    const terminal = isTerminalRunStatus(run.status);
                    return (
                      <tr key={run.run_id}>
                        <td>
                          <span className={`runs-status status-${run.status}`}>
                            <span aria-hidden="true" />
                            {run.status}
                          </span>
                        </td>
                        <td>
                          <strong>{run.project_short_name}</strong>
                          <small>{run.project_name}</small>
                        </td>
                        <th scope="row">
                          <code title={run.run_id}>{run.run_id}</code>
                        </th>
                        <td>
                          <time dateTime={run.created_at}>{formatDate(run.created_at)}</time>
                        </td>
                        <td>
                          {run.completed_pairs}/{run.total_pairs}
                        </td>
                        <td>{run.sample_count ?? "—"}</td>
                        <td>{run.solution_count ?? "—"}</td>
                        <td>{run.execution_mode ?? "—"}</td>
                        <td>
                          <button
                            className="admin-delete"
                            type="button"
                            disabled={!terminal}
                            title={
                              terminal
                                ? `Delete ${run.run_id}`
                                : "Only completed or failed runs can be deleted"
                            }
                            onClick={() => onDeleteRun(run.run_id)}
                          >
                            <TrashIcon />
                            Delete
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        )}

        {section === "services" && (
          <div
            role="tabpanel"
            id="admin-center-panel-services"
            aria-labelledby="admin-center-tab-services"
          >
            <div className="admin-service-note">
              Services are read-only here. Their Azure resources and catalog
              registration remain managed through configuration and infrastructure.
            </div>
            <div className="admin-table-scroll">
              <table className="admin-table admin-services-table">
                <thead>
                  <tr>
                    <SortHeader label="Service" sortKey="service" activeKey={serviceSort} direction={serviceDirection} onSort={sortServices} />
                    <SortHeader label="Provider" sortKey="provider" activeKey={serviceSort} direction={serviceDirection} onSort={sortServices} />
                    <SortHeader label="Type" sortKey="type" activeKey={serviceSort} direction={serviceDirection} onSort={sortServices} />
                    <SortHeader label="Lifecycle" sortKey="lifecycle" activeKey={serviceSort} direction={serviceDirection} onSort={sortServices} />
                    <SortHeader label="Model" sortKey="model" activeKey={serviceSort} direction={serviceDirection} onSort={sortServices} />
                    <SortHeader label="Assistance" sortKey="assistance" activeKey={serviceSort} direction={serviceDirection} onSort={sortServices} />
                    <SortHeader label={`${project.short_name} role`} sortKey="role" activeKey={serviceSort} direction={serviceDirection} onSort={sortServices} />
                  </tr>
                </thead>
                <tbody>
                  {sortedSolutions.map((solution) => (
                    <tr key={solution.id}>
                      <th scope="row">
                        <strong>{solution.label}</strong>
                        <code>{solution.id}</code>
                      </th>
                      <td>{solution.provider}</td>
                      <td>{solutionTypeLabel(solution)}</td>
                      <td>{solution.lifecycle === "ga" ? "GA" : "Preview"}</td>
                      <td>
                        {solution.model === null
                          ? "—"
                          : modelDisplayName(solution.model)}
                      </td>
                      <td>{serviceAssistance(solution)}</td>
                      <td>{getCompatibility(project, solution).replace("_", " ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
