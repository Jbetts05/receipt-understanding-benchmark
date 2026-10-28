import { useEffect, useRef, useState, type ReactNode } from "react";

import type { LiveRunState } from "../streaming";
import { isTerminalRunStatus } from "../domain";
import type {
  HealthStatus,
  Project,
  ReceiptSample,
  RunHistoryItem,
  WorkspaceView,
} from "../types";
import {
  AdminIcon,
  ChartIcon,
  CloseIcon,
  CompareIcon,
  FlaskIcon,
  GridIcon,
  HomeIcon,
  MenuIcon,
  MoonIcon,
  PlayIcon,
  PlusIcon,
  PulseIcon,
  SunIcon,
  TrashIcon,
} from "./Icons";
import { RecentRuns } from "./RecentRuns";

function ThemeButton() {
  const [theme, setTheme] = useState<"light" | "dark">(
    typeof document !== "undefined" &&
      document.documentElement.getAttribute("data-theme") === "dark"
      ? "dark"
      : "light",
  );
  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    setTheme(next);
  }
  return (
    <button
      className="icon-button"
      type="button"
      onClick={toggleTheme}
      aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
    >
      {theme === "dark" ? <SunIcon /> : <MoonIcon />}
    </button>
  );
}

const mobileNavigationQuery = "(max-width: 900px)";
const viewContextLabels: Record<WorkspaceView, string> = {
  home: "Home",
  evaluation: "Evaluation",
  dataset: "Datasets",
  builder: "Configure run",
  catalog: "Models & services",
  runs: "Runs",
  reports: "Reports",
  admin: "Admin Center",
  new_project: "Create dataset",
};

function formatShellTime(value: string): string {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.valueOf())) {
    return "Unknown";
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(timestamp);
}

function isMobileNavigation(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia(mobileNavigationQuery).matches
  );
}

function focusableDrawerItems(drawer: HTMLElement): HTMLElement[] {
  return Array.from(
    drawer.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => !element.hasAttribute("inert"));
}

function NavButton({
  active,
  icon,
  label,
  onClick,
}: {
  active: boolean;
  icon: ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={active ? "active" : ""}
      aria-current={active ? "page" : undefined}
      onClick={onClick}
    >
      {icon}
      <span>{label}</span>
    </button>
  );
}

export const PRIMARY_NAVIGATION = [
  {
    id: "overview-nav-heading",
    label: "Overview",
    items: [{ view: "home", label: "Home", icon: <HomeIcon /> }],
  },
  {
    id: "data-nav-heading",
    label: "Data",
    items: [
      { view: "dataset", label: "Datasets", icon: <GridIcon /> },
      { view: "new_project", label: "Create dataset", icon: <PlusIcon /> },
    ],
  },
  {
    id: "extractors-nav-heading",
    label: "Extractors",
    items: [
      { view: "catalog", label: "Models & services", icon: <PulseIcon /> },
      { view: "builder", label: "Configure run", icon: <PlusIcon /> },
    ],
  },
  {
    id: "evaluations-nav-heading",
    label: "Evaluations",
    items: [
      { view: "evaluation", label: "Evaluation detail", icon: <CompareIcon /> },
      { view: "runs", label: "Runs", icon: <PlayIcon /> },
    ],
  },
  {
    id: "insights-nav-heading",
    label: "Insights",
    items: [{ view: "reports", label: "Reports", icon: <ChartIcon /> }],
  },
  {
    id: "administration-nav-heading",
    label: "Administration",
    items: [{ view: "admin", label: "Admin Center", icon: <AdminIcon /> }],
  },
] as const satisfies ReadonlyArray<{
  id: string;
  label: string;
  items: ReadonlyArray<{
    view: WorkspaceView;
    label: string;
    icon: ReactNode;
  }>;
}>;

export function AppShell({
  projects,
  activeProject,
  view,
  health,
  source,
  runState,
  runHistory,
  runHistoryError,
  runHistoryLoading,
  restoringRunId,
  onProjectChange,
  onViewChange,
  onRunSelect,
  onRunHistoryRefresh,
  onDeleteProjectRequest,
  onDeleteRunRequest,
  children,
}: {
  projects: Project[];
  activeProject: Project;
  view: WorkspaceView;
  health: HealthStatus;
  source: "server" | "offline";
  runState: LiveRunState | null;
  runHistory: RunHistoryItem[];
  runHistoryError: string | null;
  runHistoryLoading: boolean;
  restoringRunId: string | null;
  samples: ReceiptSample[];
  selectedSampleId: string;
  onProjectChange: (projectId: string) => void;
  onViewChange: (view: WorkspaceView) => void;
  onSampleChange: (sampleId: string) => void;
  onRunSelect: (run: RunHistoryItem) => void;
  onRunHistoryRefresh: () => void;
  onDeleteProjectRequest: () => void;
  onDeleteRunRequest: () => void;
  children: ReactNode;
}) {
  const [navigationOpen, setNavigationOpen] = useState(false);
  const [mobileNavigation, setMobileNavigation] = useState(isMobileNavigation);
  const navigationRef = useRef<HTMLElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const query = window.matchMedia(mobileNavigationQuery);
    const update = () => {
      setMobileNavigation(query.matches);
      if (!query.matches) {
        setNavigationOpen(false);
      }
    };
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    if (!mobileNavigation || !navigationOpen) {
      return;
    }
    restoreFocusRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : menuButtonRef.current;
    const frame = window.requestAnimationFrame(() => navigationRef.current?.focus());
    const handleKeyDown = (event: KeyboardEvent) => {
      const drawer = navigationRef.current;
      if (drawer === null) {
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        setNavigationOpen(false);
        return;
      }
      if (event.key !== "Tab") {
        return;
      }
      const items = focusableDrawerItems(drawer);
      if (items.length === 0) {
        event.preventDefault();
        drawer.focus();
        return;
      }
      const first = items[0]!;
      const last = items[items.length - 1]!;
      const active = document.activeElement;
      if (
        event.shiftKey &&
        (active === first || active === drawer || !drawer.contains(active))
      ) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !drawer.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", handleKeyDown);
      const target = restoreFocusRef.current ?? menuButtonRef.current;
      restoreFocusRef.current = null;
      target?.focus();
    };
  }, [mobileNavigation, navigationOpen]);

  function selectView(next: WorkspaceView) {
    onViewChange(next);
    setNavigationOpen(false);
  }

  function selectProject(projectId: string) {
    onProjectChange(projectId);
    setNavigationOpen(false);
  }

  function selectRun(run: RunHistoryItem) {
    onRunSelect(run);
    setNavigationOpen(false);
  }

  return (
    <div className="app-frame">
      <header className="topbar">
        <button
          ref={menuButtonRef}
          className="icon-button mobile-menu"
          type="button"
          aria-label={navigationOpen ? "Close navigation" : "Open navigation"}
          aria-expanded={navigationOpen}
          aria-controls="primary-navigation"
          onClick={() => setNavigationOpen((current) => !current)}
        >
          {navigationOpen ? <CloseIcon /> : <MenuIcon />}
        </button>
        <button
          className="brand"
          type="button"
          onClick={() => selectView("home")}
          aria-label="Go to Receipt Bench home"
        >
          <span className="brand-symbol">
            <FlaskIcon />
          </span>
          <strong>Receipt Bench</strong>
        </button>
        <div className="topbar-context" aria-label="Current context">
          <span>{viewContextLabels[view]}</span>
          <span aria-hidden="true">/</span>
          <strong>{activeProject.name}</strong>
          <span className="topbar-mobile-project">{activeProject.short_name}</span>
          {runState !== null && (
            <span className={`topbar-run-state status-${runState.manifest.status}`}>
              {runState.manifest.status}
              {runState.manifest.status === "completed" ? " ✓" : ""}
            </span>
          )}
        </div>
        <div className="topbar-actions">
          <span className={`runtime-status runtime-${health.execution_mode}`}>
            <span aria-hidden="true" />
            {source === "offline"
              ? "Explicit offline adapter"
              : `${health.profile} · ${health.execution_mode}`}
          </span>
          <ThemeButton />
        </div>
      </header>

      <aside
        ref={navigationRef}
        id="primary-navigation"
        className={`navigation ${navigationOpen ? "is-open" : ""}`}
        role={mobileNavigation ? "dialog" : undefined}
        aria-modal={mobileNavigation && navigationOpen ? true : undefined}
        aria-labelledby={mobileNavigation ? "mobile-navigation-title" : undefined}
        aria-hidden={mobileNavigation && !navigationOpen ? true : undefined}
        inert={mobileNavigation && !navigationOpen ? true : undefined}
        tabIndex={mobileNavigation ? -1 : undefined}
      >
        <h2 className="sr-only" id="mobile-navigation-title">
          Primary navigation
        </h2>
        <div className="navigation-scroll">
          {PRIMARY_NAVIGATION.map((group) => (
            <section
              className="nav-group"
              aria-labelledby={group.id}
              key={group.id}
            >
              <span className="nav-kicker" id={group.id}>
                {group.label}
              </span>
              {group.items.map((item) => (
                <NavButton
                  active={view === item.view}
                  icon={item.icon}
                  label={item.label}
                  onClick={() => selectView(item.view)}
                  key={item.view}
                />
              ))}
            </section>
          ))}
          <RecentRuns
            variant="navigation"
            runs={runHistory}
            activeRunId={runState?.manifest.id ?? null}
            error={runHistoryError}
            loading={runHistoryLoading}
            restoringRunId={restoringRunId}
            onSelect={selectRun}
            onRefresh={onRunHistoryRefresh}
          />
          <section className="nav-group navigation-meta">
            <span>Private storage</span>
            <strong>{health.execution_mode === "live" ? "Azure connected" : "Local workspace"}</strong>
          </section>
        </div>
      </aside>

      <aside
        className="context-rail"
        aria-label="Dataset, run, and run history context"
      >
        <section className="context-card dataset-context-card">
          <h2>Dataset</h2>
          <label className="context-project-select">
            <span className="sr-only">Active dataset</span>
            <select
              value={activeProject.id}
              onChange={(event) => selectProject(event.target.value)}
            >
              {projects.map((project) => (
                <option value={project.id} key={project.id}>
                  {project.short_name}
                </option>
              ))}
            </select>
            <small>v{activeProject.schema.version}</small>
          </label>
          <dl className="context-stats">
            <div>
              <dd>{activeProject.sample_count}</dd>
              <dt>Receipts</dt>
            </div>
            <div>
              <dd>{activeProject.gold_sample_count}</dd>
              <dt>Golden answers</dt>
            </div>
            <div>
              <dd>{activeProject.scored_sample_count}</dd>
              <dt>Scored receipts</dt>
            </div>
          </dl>
          <button
            className="context-action"
            type="button"
            onClick={() => selectView("dataset")}
          >
            View dataset
            <span aria-hidden="true">↗</span>
          </button>
          {activeProject.kind === "custom" && (
            <button
              className="context-action danger-text"
              type="button"
              onClick={onDeleteProjectRequest}
            >
              <TrashIcon />
              Delete dataset
            </button>
          )}
        </section>
        <section className="context-card run-context-card">
          <h2>Run details</h2>
          {runState === null ? (
            <>
              <p className="rail-empty">No run is active for this dataset.</p>
              <button
                className="context-action"
                type="button"
                onClick={() => selectView("builder")}
              >
                Configure run
              </button>
            </>
          ) : (
            <>
              <span className="context-label">Run name</span>
              <strong>{activeProject.short_name} evaluation</strong>
              <code className="rail-run-id">{runState.manifest.id}</code>
              <div className={`rail-run-status status-${runState.manifest.status}`}>
                <span aria-hidden="true" />
                {runState.manifest.status}
              </div>
              <dl className="run-detail-list">
                <div>
                  <dt>Started</dt>
                  <dd>{formatShellTime(runState.manifest.created_at)}</dd>
                </div>
                <div>
                  <dt>Extractors</dt>
                  <dd>{runState.manifest.solution_ids.length}</dd>
                </div>
                <div>
                  <dt>Receipts</dt>
                  <dd>{runState.manifest.sample_ids.length}</dd>
                </div>
                <div>
                  <dt>Total requests</dt>
                  <dd>{runState.manifest.total_pairs}</dd>
                </div>
              </dl>
              <button
                className="context-action"
                type="button"
                onClick={() => selectView("builder")}
              >
                Run configuration
              </button>
              <button
                className="context-action danger-text"
                type="button"
                disabled={!isTerminalRunStatus(runState.manifest.status)}
                aria-disabled={!isTerminalRunStatus(runState.manifest.status)}
                title={
                  isTerminalRunStatus(runState.manifest.status)
                    ? undefined
                    : "Only a completed or failed run can be deleted."
                }
                onClick={onDeleteRunRequest}
              >
                <TrashIcon />
                Delete run
              </button>
            </>
          )}
        </section>
        <RecentRuns
          variant="rail"
          runs={runHistory}
          activeRunId={runState?.manifest.id ?? null}
          error={runHistoryError}
          loading={runHistoryLoading}
          restoringRunId={restoringRunId}
          onSelect={selectRun}
          onRefresh={onRunHistoryRefresh}
        />
      </aside>

      {navigationOpen && (
        <button
          type="button"
          className="nav-scrim"
          onClick={() => setNavigationOpen(false)}
          aria-label="Close navigation"
        />
      )}
      <main className="workspace">{children}</main>
    </div>
  );
}
