import type { LiveRunState } from "../streaming";
import type { Project, RunHistoryItem, Solution } from "../types";
import { ChartIcon, GridIcon, PlayIcon, PlusIcon } from "./Icons";

export function HomeWorkspace({
  projects,
  project,
  solutions,
  runState,
  runHistory,
  onOpenDataset,
  onCreateDataset,
  onBuildRun,
  onOpenEvaluation,
  onOpenRuns,
  onOpenReports,
}: {
  projects: Project[];
  project: Project;
  solutions: Solution[];
  runState: LiveRunState | null;
  runHistory: RunHistoryItem[];
  onOpenDataset: () => void;
  onCreateDataset: () => void;
  onBuildRun: () => void;
  onOpenEvaluation: () => void;
  onOpenRuns: () => void;
  onOpenReports: () => void;
}) {
  const comparableSolutions = project.compatibility.filter(
    (item) => item.comparable_quality,
  ).length;
  const completedPairs =
    runState === null ? 0 : runState.manifest.completed_pairs;
  const totalPairs = runState === null ? 0 : runState.manifest.total_pairs;

  return (
    <div className="view-shell home-view">
      <header className="home-header">
        <div>
          <h1>Receipt Bench</h1>
          <p>
            Compare receipt-extraction models and services against known answers,
            with quality, latency, usage, and estimated cost kept as separate evidence.
          </p>
        </div>
        <div className="home-header-actions">
          <button className="secondary-button" type="button" onClick={onOpenDataset}>
            <GridIcon />
            Explore datasets
          </button>
          <button className="primary-button" type="button" onClick={onBuildRun}>
            <PlayIcon />
            Configure run
          </button>
        </div>
      </header>

      <section className="home-summary" aria-label="Workspace overview">
        <div>
          <span>Datasets</span>
          <strong>{projects.length}</strong>
          <small>Built-in and custom</small>
        </div>
        <div>
          <span>Current receipts</span>
          <strong>{project.sample_count}</strong>
          <small>{project.gold_sample_count} with golden answers</small>
        </div>
        <div>
          <span>Quality-comparable</span>
          <strong>{comparableSolutions}</strong>
          <small>{solutions.length} runnable configurations</small>
        </div>
        <div>
          <span>Recorded runs</span>
          <strong>{runHistory.length}</strong>
          <small>For {project.short_name}</small>
        </div>
      </section>

      <div className="home-grid">
        <section className="home-panel" aria-labelledby="home-dataset-heading">
          <div className="home-panel-heading">
            <div>
              <h2 id="home-dataset-heading">Current dataset</h2>
              <p>All runs remain isolated to this schema, prompt, and receipt set.</p>
            </div>
            <span className="stable-label">
              {project.kind === "custom" ? "Custom dataset" : "Built-in benchmark"}
            </span>
          </div>
          <h3>{project.name}</h3>
          <p className="home-panel-description">{project.description}</p>
          <dl className="home-facts">
            <div>
              <dt>Schema</dt>
              <dd>v{project.schema.version}</dd>
            </div>
            <div>
              <dt>Prompt</dt>
              <dd>v{project.prompt.version}</dd>
            </div>
            <div>
              <dt>Scored receipts</dt>
              <dd>{project.scored_sample_count}</dd>
            </div>
            <div>
              <dt>Dataset state</dt>
              <dd>{project.dataset_state}</dd>
            </div>
          </dl>
          <div className="home-panel-actions">
            <button className="secondary-button" type="button" onClick={onOpenDataset}>
              View dataset
            </button>
            <button className="text-button" type="button" onClick={onCreateDataset}>
              <PlusIcon />
              Create dataset
            </button>
          </div>
        </section>

        <section className="home-panel" aria-labelledby="home-run-heading">
          <div className="home-panel-heading">
            <div>
              <h2 id="home-run-heading">
                {runState === null ? "Start a comparison" : "Continue current run"}
              </h2>
              <p>
                {runState === null
                  ? "Choose receipts and configurations, then pin the experiment contract."
                  : "Open the live or completed evidence without rebuilding the run."}
              </p>
            </div>
            {runState !== null && (
              <span className={`runs-status status-${runState.manifest.status}`}>
                <span aria-hidden="true" />
                {runState.manifest.status}
              </span>
            )}
          </div>
          {runState === null ? (
            <div className="home-run-empty">
              <PlayIcon />
              <strong>No active run for {project.short_name}</strong>
              <p>Existing runs remain available in run history.</p>
            </div>
          ) : (
            <dl className="home-facts home-run-facts">
              <div>
                <dt>Progress</dt>
                <dd>
                  {completedPairs}/{totalPairs} pairs
                </dd>
              </div>
              <div>
                <dt>Receipts</dt>
                <dd>{runState.manifest.sample_ids.length}</dd>
              </div>
              <div>
                <dt>Configurations</dt>
                <dd>{runState.manifest.solution_ids.length}</dd>
              </div>
              <div>
                <dt>Mode</dt>
                <dd>{runState.manifest.execution_mode}</dd>
              </div>
            </dl>
          )}
          <div className="home-panel-actions">
            {runState === null ? (
              <button className="primary-button" type="button" onClick={onBuildRun}>
                <PlayIcon />
                Configure run
              </button>
            ) : (
              <>
                <button
                  className="primary-button"
                  type="button"
                  onClick={onOpenEvaluation}
                >
                  Open evaluation
                </button>
                <button
                  className="secondary-button"
                  type="button"
                  onClick={onOpenReports}
                >
                  <ChartIcon />
                  View report
                </button>
              </>
            )}
            <button className="text-button" type="button" onClick={onOpenRuns}>
              View run history
            </button>
          </div>
        </section>
      </div>
    </div>
  );
}
