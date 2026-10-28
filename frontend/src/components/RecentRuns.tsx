import type { RunHistoryItem } from "../types";

function formatRunTime(value: string): string {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.valueOf())) {
    return "Unknown time";
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(timestamp);
}

export function RecentRuns({
  runs,
  activeRunId,
  error,
  loading,
  restoringRunId,
  variant,
  onSelect,
  onRefresh,
}: {
  runs: RunHistoryItem[];
  activeRunId: string | null;
  error: string | null;
  loading: boolean;
  restoringRunId: string | null;
  variant: "rail" | "navigation";
  onSelect: (run: RunHistoryItem) => void;
  onRefresh: () => void;
}) {
  const headingId = `recent-runs-${variant}-heading`;
  return (
    <section
      className={`recent-runs recent-runs-${variant}`}
      aria-labelledby={headingId}
      aria-busy={loading}
    >
      <header>
        <span className="nav-kicker" id={headingId}>
          Recent runs
        </span>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          aria-label="Refresh recent runs"
        >
          Refresh
        </button>
      </header>
      {error !== null && (
        <p className="recent-runs-error" role="alert">
          {error}
        </p>
      )}
      {loading && runs.length === 0 ? (
        <p className="recent-runs-empty" aria-live="polite">
          Loading runs…
        </p>
      ) : runs.length === 0 ? (
        <p className="recent-runs-empty">No runs for this project yet.</p>
      ) : (
        <ol>
          {runs.map((run) => {
            const active = activeRunId === run.run_id;
            const restoring = restoringRunId === run.run_id;
            return (
              <li key={run.run_id}>
                <button
                  type="button"
                  className={active ? "active" : ""}
                  aria-current={active ? "true" : undefined}
                  aria-label={`Restore run ${run.run_id}, ${run.status}, ${run.completed_pairs} of ${run.total_pairs} pairs`}
                  disabled={restoring}
                  onClick={() => onSelect(run)}
                >
                  <span className={`recent-run-status status-${run.status}`}>
                    <span aria-hidden="true" />
                    {restoring ? "Restoring" : run.status}
                  </span>
                  <code title={run.run_id}>{run.run_id}</code>
                  <span className="recent-run-detail">
                    <strong>
                      {run.completed_pairs}/{run.total_pairs}
                    </strong>
                    <span>pairs</span>
                    <time dateTime={run.created_at}>{formatRunTime(run.created_at)}</time>
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
