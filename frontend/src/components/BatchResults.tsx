import { useEffect, useMemo, useState } from "react";

import { qualityLanguage } from "../domain";
import type {
  AggregateMeasure,
  FieldAggregate,
  Project,
  ProjectAggregate,
  SolutionAggregate,
  Solution,
} from "../types";
import { ArrowIcon, ChartIcon, InfoIcon } from "./Icons";
import { ProjectHeader } from "./ProjectHeader";

type QualityMetricKey =
  | "normalized_field_f1"
  | "whole_answer_exactness"
  | "labeled_field_coverage"
  | "reference_token_recall"
  | "character_error_rate"
  | "word_error_rate"
  | "success_rate";
type UsageMetricKey =
  | "input_tokens"
  | "output_tokens"
  | "total_tokens"
  | "total_pages"
  | "total_attempts"
  | "median_latency"
  | "p95_latency"
  | "estimated_cost";
type QualitySortKey = "solution" | QualityMetricKey;
type UsageSortKey = "solution" | UsageMetricKey;
type FieldSortKey =
  | "path"
  | "matched"
  | "wrong"
  | "partial"
  | "missing"
  | "extra"
  | "coverage"
  | "exact_match_rate"
  | "normalized_field_f1";
type SortDirection = "asc" | "desc";

const qualityLabels: Record<QualityMetricKey, string> = {
  normalized_field_f1: "Normalized field F1",
  whole_answer_exactness: "Whole-answer exactness",
  labeled_field_coverage: "Labeled coverage",
  reference_token_recall: "Reference token recall",
  character_error_rate: "Character error rate",
  word_error_rate: "Word error rate",
  success_rate: "Success",
};

const usageLabels: Record<UsageMetricKey, string> = {
  input_tokens: "Input tokens",
  output_tokens: "Output tokens",
  total_tokens: "Total tokens",
  total_pages: "Pages",
  total_attempts: "Attempts",
  median_latency: "P50 latency",
  p95_latency: "P95 latency",
  estimated_cost: "Estimated cost",
};

function ReportSortHeader<T extends string>({
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
        <span aria-hidden="true">{active ? (direction === "asc" ? "▲" : "▼") : "↕"}</span>
      </button>
    </th>
  );
}

function formatMeasure(measure: AggregateMeasure): string {
  if (measure.value === null) {
    return "Not available";
  }
  if (measure.unit === "ratio") {
    return `${(measure.value * 100).toFixed(1)}%`;
  }
  if (measure.unit === "milliseconds") {
    return `${(measure.value / 1_000).toFixed(2)} s`;
  }
  if (measure.unit === "count") {
    return Math.round(measure.value).toLocaleString("en-US");
  }
  return `$${measure.value.toFixed(5)}`;
}

function qualityMeasure(
  row: SolutionAggregate,
  key: QualityMetricKey,
): AggregateMeasure | null {
  return row[key];
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

export function BatchResults({
  project,
  solutions,
  aggregate,
  onOpenEvidence,
  embedded = false,
}: {
  project: Project;
  solutions: Solution[];
  aggregate: ProjectAggregate;
  onOpenEvidence: (sampleId: string, solutionId: string) => void;
  embedded?: boolean;
}) {
  const [activeEvidence, setActiveEvidence] = useState<{
    id: string;
    solutionId: string;
    title: string;
    display: string;
    measure: AggregateMeasure;
  } | null>(null);
  const [qualitySortKey, setQualitySortKey] =
    useState<QualitySortKey>("solution");
  const [qualitySortDirection, setQualitySortDirection] =
    useState<SortDirection>("asc");
  const [usageSortKey, setUsageSortKey] = useState<UsageSortKey>("solution");
  const [usageSortDirection, setUsageSortDirection] =
    useState<SortDirection>("asc");
  const [fieldSortKey, setFieldSortKey] = useState<FieldSortKey>("path");
  const [fieldSortDirection, setFieldSortDirection] =
    useState<SortDirection>("asc");
  const [fieldSolutionId, setFieldSolutionId] = useState(
    aggregate.fields[0]?.solution_id ?? "",
  );
  const solutionById = useMemo(
    () => new Map(solutions.map((solution) => [solution.id, solution])),
    [solutions],
  );
  const qualityRows = useMemo(
    () =>
      [...aggregate.solutions].sort((left, right) => {
        const leftSolution = solutionById.get(left.solution_id);
        const rightSolution = solutionById.get(right.solution_id);
        const leftValue =
         qualitySortKey === "solution"
           ? leftSolution?.label ?? left.solution_id
           : qualityMeasure(left, qualitySortKey)?.value ?? null;
        const rightValue =
         qualitySortKey === "solution"
           ? rightSolution?.label ?? right.solution_id
           : qualityMeasure(right, qualitySortKey)?.value ?? null;
        return (
         compareValues(leftValue, rightValue, qualitySortDirection) ||
         left.solution_id.localeCompare(right.solution_id)
        );
      }),
    [
      aggregate.solutions,
      qualitySortDirection,
      qualitySortKey,
      solutionById,
    ],
  );
  const usageRows = useMemo(
    () =>
      [...aggregate.solutions].sort((left, right) => {
        const leftValue =
         usageSortKey === "solution"
           ? solutionById.get(left.solution_id)?.label ?? left.solution_id
           : left[usageSortKey].value;
        const rightValue =
         usageSortKey === "solution"
           ? solutionById.get(right.solution_id)?.label ?? right.solution_id
           : right[usageSortKey].value;
        return (
         compareValues(leftValue, rightValue, usageSortDirection) ||
         left.solution_id.localeCompare(right.solution_id)
        );
      }),
    [aggregate.solutions, solutionById, usageSortDirection, usageSortKey],
  );
  const fieldSolutionIds = useMemo(
    () =>
      solutions
        .map((solution) => solution.id)
        .filter((solutionId) =>
         aggregate.fields.some((field) => field.solution_id === solutionId),
        ),
    [aggregate.fields, solutions],
  );
  const fieldRows = useMemo(
    () =>
      aggregate.fields
        .filter((field) => field.solution_id === fieldSolutionId)
        .sort((left, right) => {
         const value = (field: FieldAggregate): string | number | null => {
           if (fieldSortKey === "path") return field.label;
           if (
             fieldSortKey === "coverage" ||
             fieldSortKey === "exact_match_rate" ||
             fieldSortKey === "normalized_field_f1"
           ) {
             return field[fieldSortKey].value;
           }
           return field[fieldSortKey];
         };
         return (
           compareValues(value(left), value(right), fieldSortDirection) ||
           left.path.localeCompare(right.path, undefined, { numeric: true })
         );
        }),
    [
      aggregate.fields,
      fieldSolutionId,
      fieldSortDirection,
      fieldSortKey,
    ],
  );

  function sortQuality(key: QualitySortKey) {
    if (key === qualitySortKey) {
      setQualitySortDirection((current) =>
        current === "asc" ? "desc" : "asc",
      );
      return;
    }
    setQualitySortKey(key);
    setQualitySortDirection(key === "solution" ? "asc" : "desc");
  }

  function sortUsage(key: UsageSortKey) {
    if (key === usageSortKey) {
      setUsageSortDirection((current) =>
        current === "asc" ? "desc" : "asc",
      );
      return;
    }
    setUsageSortKey(key);
    setUsageSortDirection("asc");
  }

  function sortFields(key: FieldSortKey) {
    if (key === fieldSortKey) {
      setFieldSortDirection((current) =>
        current === "asc" ? "desc" : "asc",
      );
      return;
    }
    setFieldSortKey(key);
    setFieldSortDirection(
      key === "path" ||
        ["wrong", "partial", "missing", "extra"].includes(key)
        ? "asc"
        : "desc",
    );
  }

  useEffect(() => {
    setActiveEvidence(null);
    setQualitySortKey("solution");
    setQualitySortDirection("asc");
    setUsageSortKey("solution");
    setUsageSortDirection("asc");
    setFieldSortKey("path");
    setFieldSortDirection("asc");
    setFieldSolutionId(aggregate.fields[0]?.solution_id ?? "");
  }, [aggregate.project_id, aggregate.run_id]);

  function showEvidence(
    id: string,
    solutionId: string,
    title: string,
    display: string,
    measure: AggregateMeasure,
  ) {
    setActiveEvidence({ id, solutionId, title, display, measure });
  }

  return (
    <div className={embedded ? "embedded-workspace results-view" : "view-shell results-view"}>
      {!embedded && (
        <ProjectHeader
          project={project}
          eyebrow="Batch results"
          title={`${project.short_name} run evidence`}
          description="Quality, reliability, latency, and cost stay separate. Every aggregate opens its contributing results."
          compact
        />
      )}

      <section className="aggregate-scope" aria-label="Aggregate scope">
        <div>
          <span className="eyebrow">Evaluation scope</span>
          <strong>{qualityLanguage(aggregate.has_gold)}</strong>
        </div>
        <dl>
          <div>
            <dt>Project</dt>
            <dd>{project.short_name} only</dd>
          </div>
          <div>
            <dt>Scored receipts</dt>
            <dd>{aggregate.scored_sample_count}</dd>
          </div>
          <div>
            <dt>Prompt examples excluded</dt>
            <dd>{aggregate.excluded_prompt_examples}</dd>
          </div>
          <div>
            <dt>Run</dt>
            <dd>{aggregate.run_id}</dd>
          </div>
        </dl>
      </section>

      <div className="scope-note">
        <InfoIcon />
        <p>
          No SROIE and CORD values are combined. There is no cross-dataset score and no
          universal composite metric.
        </p>
      </div>

      <section className="aggregate-table-section" aria-labelledby="aggregate-table-heading">
        <div className="section-title-row">
          <div>
            <h2 id="aggregate-table-heading">Solution quality</h2>
            <p>Select any value to inspect all contributing receipt/result pairs.</p>
          </div>
          <span className="stable-label">Sortable evidence · not a composite ranking</span>
        </div>
        <div className="aggregate-table-scroll">
          <table className="aggregate-table">
            <thead>
              <tr>
                <ReportSortHeader label="Solution" sortKey="solution" activeKey={qualitySortKey} direction={qualitySortDirection} onSort={sortQuality} />
                {(Object.keys(qualityLabels) as QualityMetricKey[]).map((key) => (
                  <ReportSortHeader
                    key={key}
                    label={qualityLabels[key]}
                    sortKey={key}
                    activeKey={qualitySortKey}
                    direction={qualitySortDirection}
                    onSort={sortQuality}
                  />
                ))}
              </tr>
            </thead>
            <tbody>
              {qualityRows.map((row) => {
                const solution = solutionById.get(row.solution_id);
                if (solution === undefined) {
                  return null;
                }
                return (
                  <tr key={row.solution_id}>
                    <th scope="row">
                      <span className="solution-index">
                        {String(solution.order + 1).padStart(2, "0")}
                      </span>
                      <span>
                        <strong>{solution.label}</strong>
                        <small>{solution.kind.replace("_", " ")}</small>
                      </span>
                    </th>
                    {(Object.keys(qualityLabels) as QualityMetricKey[]).map((metricKey) => {
                      const measure = qualityMeasure(row, metricKey);
                      const cellId = `quality:${row.solution_id}:${metricKey}`;
                      const active = activeEvidence?.id === cellId;
                      return (
                        <td key={metricKey}>
                          <button
                            type="button"
                            className={active ? "active" : ""}
                            disabled={measure === null || measure.evidence.length === 0}
                            onClick={() => {
                              if (measure !== null) {
                                showEvidence(
                                  cellId,
                                  row.solution_id,
                                  qualityLabels[metricKey],
                                  formatMeasure(measure),
                                  measure,
                                );
                              }
                            }}
                            aria-label={`${solution.label} ${qualityLabels[metricKey]} ${
                              measure === null
                                ? "Not applicable"
                                : formatMeasure(measure)
                            }; ${measure?.evidence.length ?? 0} evidence links`}
                          >
                            <strong>
                              {measure === null ? "Not applicable" : formatMeasure(measure)}
                            </strong>
                            <small>{measure?.evidence.length ?? 0} evidence links</small>
                          </button>
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <section className="aggregate-table-section" aria-labelledby="usage-table-heading">
        <div className="section-title-row">
          <div>
            <h2 id="usage-table-heading">Usage and performance</h2>
            <p>Recorded totals and latency percentiles for this run only.</p>
          </div>
          <span className="stable-label">Lower is better · cost is estimated</span>
        </div>
        <div className="aggregate-table-scroll">
          <table className="aggregate-table">
            <thead>
              <tr>
                <ReportSortHeader label="Solution" sortKey="solution" activeKey={usageSortKey} direction={usageSortDirection} onSort={sortUsage} />
                {(Object.keys(usageLabels) as UsageMetricKey[]).map((key) => (
                  <ReportSortHeader
                    key={key}
                    label={usageLabels[key]}
                    sortKey={key}
                    activeKey={usageSortKey}
                    direction={usageSortDirection}
                    onSort={sortUsage}
                  />
                ))}
              </tr>
            </thead>
            <tbody>
              {usageRows.map((row) => {
                const solution = solutionById.get(row.solution_id);
                if (solution === undefined) return null;
                return (
                  <tr key={row.solution_id}>
                    <th scope="row">
                      <span className="solution-index">
                        {String(solution.order + 1).padStart(2, "0")}
                      </span>
                      <span>
                        <strong>{solution.label}</strong>
                        <small>{solution.kind.replace("_", " ")}</small>
                      </span>
                    </th>
                    {(Object.keys(usageLabels) as UsageMetricKey[]).map((metricKey) => {
                      const measure = row[metricKey];
                      const cellId = `usage:${row.solution_id}:${metricKey}`;
                      return (
                        <td key={metricKey}>
                          <button
                            type="button"
                            className={activeEvidence?.id === cellId ? "active" : ""}
                            disabled={measure.evidence.length === 0}
                            onClick={() =>
                              showEvidence(
                                cellId,
                                row.solution_id,
                                usageLabels[metricKey],
                                formatMeasure(measure),
                                measure,
                              )
                            }
                          >
                            <strong>{formatMeasure(measure)}</strong>
                            <small>
                              {metricKey === "estimated_cost"
                                ? `${row.cost_scope} estimate`
                                : `${measure.evidence.length} evidence links`}
                            </small>
                          </button>
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <section className="aggregate-table-section" aria-labelledby="field-table-heading">
        <div className="section-title-row">
          <div>
            <h2 id="field-table-heading">Field quality detail</h2>
            <p>Matched, wrong, partial, missing, and extra outputs by schema field.</p>
          </div>
          {fieldSolutionIds.length > 0 && (
            <label className="select-field compact-select">
              <span>Solution</span>
              <select
                value={fieldSolutionId}
                onChange={(event) => setFieldSolutionId(event.target.value)}
              >
                {fieldSolutionIds.map((solutionId) => (
                  <option key={solutionId} value={solutionId}>
                    {solutionById.get(solutionId)?.label ?? solutionId}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
        {fieldRows.length === 0 ? (
          <div className="scope-note">
            <InfoIcon />
            <p>No comparable structured field evidence is available for this run.</p>
          </div>
        ) : (
          <div className="aggregate-table-scroll">
            <table className="aggregate-table field-aggregate-table">
              <thead>
                <tr>
                  {(
                    [
                      ["Field", "path"],
                      ["Matched", "matched"],
                      ["Wrong", "wrong"],
                      ["Partial", "partial"],
                      ["Missing", "missing"],
                      ["Extra", "extra"],
                      ["Coverage", "coverage"],
                      ["Exact rate", "exact_match_rate"],
                      ["Field F1", "normalized_field_f1"],
                    ] as const
                  ).map(([label, key]) => (
                    <ReportSortHeader
                      key={key}
                      label={label}
                      sortKey={key}
                      activeKey={fieldSortKey}
                      direction={fieldSortDirection}
                      onSort={sortFields}
                    />
                  ))}
                </tr>
              </thead>
              <tbody>
                {fieldRows.map((row) => (
                  <tr key={`${row.solution_id}:${row.path}`}>
                    <th scope="row">
                      <span>
                        <strong>{row.label}</strong>
                        <small>{row.path}</small>
                      </span>
                    </th>
                    {(
                      ["matched", "wrong", "partial", "missing", "extra"] as const
                    ).map((key) => {
                      const cellId = `field:${row.solution_id}:${row.path}:${key}`;
                      return (
                        <td key={key}>
                          <button
                            type="button"
                            className={activeEvidence?.id === cellId ? "active" : ""}
                            onClick={() =>
                              showEvidence(
                                cellId,
                                row.solution_id,
                                `${row.label} · ${key}`,
                                String(row[key]),
                                row.normalized_field_f1,
                              )
                            }
                          >
                            <strong>{row[key]}</strong>
                            <small>{row.normalized_field_f1.evidence.length} evidence links</small>
                          </button>
                        </td>
                      );
                    })}
                    {(
                      ["coverage", "exact_match_rate", "normalized_field_f1"] as const
                    ).map((key) => {
                      const measure = row[key];
                      const cellId = `field:${row.solution_id}:${row.path}:${key}`;
                      return (
                        <td key={key}>
                          <button
                            type="button"
                            className={activeEvidence?.id === cellId ? "active" : ""}
                            onClick={() =>
                              showEvidence(
                                cellId,
                                row.solution_id,
                                `${row.label} · ${key.replaceAll("_", " ")}`,
                                formatMeasure(measure),
                                measure,
                              )
                            }
                          >
                            <strong>{formatMeasure(measure)}</strong>
                            <small>{measure.evidence.length} evidence links</small>
                          </button>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {activeEvidence !== null && (
        <section className="evidence-drawer" aria-labelledby="evidence-heading">
          <div className="evidence-drawer-heading">
            <div>
              <span className="eyebrow">Contributing evidence</span>
              <h2 id="evidence-heading">
                {solutionById.get(activeEvidence.solutionId)?.label} ·{" "}
                {activeEvidence.title}
              </h2>
            </div>
            <strong>{activeEvidence.display}</strong>
          </div>
          <div className="evidence-link-list">
            {activeEvidence.measure.evidence.map((evidence) => (
              <button
                type="button"
                onClick={() =>
                  onOpenEvidence(evidence.sample_id, evidence.solution_id)
                }
                key={`${evidence.sample_id}-${evidence.solution_id}`}
              >
                <span className="evidence-icon">
                  <ChartIcon />
                </span>
                <span>
                  <strong>{evidence.sample_id}</strong>
                  <small>{evidence.solution_id}</small>
                </span>
                <code>{evidence.result_url}</code>
                <ArrowIcon />
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
