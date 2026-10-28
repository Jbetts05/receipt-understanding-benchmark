import type { LiveRunState } from "../streaming";
import type { Project, ProjectAggregate, Solution } from "../types";
import { ChartIcon } from "./Icons";
import { BatchResults } from "./BatchResults";
import { ProjectHeader } from "./ProjectHeader";

export function ReportsWorkspace({
  project,
  solutions,
  runState,
  aggregate,
  aggregateError,
  onOpenRuns,
  onBuildRun,
  onOpenEvaluation,
  onOpenEvidence,
}: {
  project: Project;
  solutions: Solution[];
  runState: LiveRunState | null;
  aggregate: ProjectAggregate | null;
  aggregateError: string | null;
  onOpenRuns: () => void;
  onBuildRun: () => void;
  onOpenEvaluation: () => void;
  onOpenEvidence: (sampleId: string, solutionId: string) => void;
}) {
  if (aggregate !== null) {
    return (
      <BatchResults
        project={project}
        solutions={solutions}
        aggregate={aggregate}
        onOpenEvidence={onOpenEvidence}
      />
    );
  }

  const running =
    runState !== null &&
    runState.manifest.status !== "completed" &&
    runState.manifest.status !== "failed";
  const loadingAggregate =
    runState !== null && !running && aggregateError === null;
  const title =
    runState === null
      ? "No report selected"
      : running
        ? "Report pending"
        : loadingAggregate
          ? "Loading report"
          : "Report unavailable";
  const description =
    runState === null
      ? "Open a project run to view its aggregate quality, latency, reliability, and cost evidence."
      : running
        ? "Aggregate evidence will appear when the active run records results."
        : loadingAggregate
          ? "Loading the persisted aggregate evidence for this run."
          : aggregateError ?? "The active run has no aggregate evidence available.";

  return (
    <div className="view-shell reports-view">
      <ProjectHeader
        project={project}
        eyebrow="Reports"
        title={`${project.short_name} aggregate evidence`}
        description="Reports remain scoped to one dataset and one immutable run."
        compact
      />
      <section className="reports-empty" aria-live={running ? "polite" : undefined}>
        <ChartIcon />
        <div>
          <h2>{title}</h2>
          <p>{description}</p>
          {runState !== null && <code>{runState.manifest.id}</code>}
        </div>
        <div className="reports-empty-actions">
          {runState === null ? (
            <>
              <button className="secondary-button" type="button" onClick={onOpenRuns}>
                View runs
              </button>
              <button className="primary-button" type="button" onClick={onBuildRun}>
                Configure run
              </button>
            </>
          ) : (
            <>
              <button className="secondary-button" type="button" onClick={onOpenRuns}>
                Choose another run
              </button>
              <button className="primary-button" type="button" onClick={onOpenEvaluation}>
                Open evaluation
              </button>
            </>
          )}
        </div>
      </section>
    </div>
  );
}
