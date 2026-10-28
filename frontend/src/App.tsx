import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  ApiError,
  HttpProductClient,
  type ProductClient,
} from "./api";
import { AggregateRefreshController } from "./aggregateRefresh";
import { bootstrapProduct, type BootstrapResult } from "./bootstrap";
import {
  getCompatibility,
  isComparableQuality,
  scopeProjectAggregateToManifest,
} from "./domain";
import {
  RunGenerationGuard,
  RunLaunchLock,
  ProjectRequestGuard,
  runDraftFingerprint,
  snapshotMatchesHistoryRun,
  type LaunchAttemptOutcome,
} from "./runGuard";
import {
  applyRunSnapshot,
  createInitialRunState,
  reduceRunEvent,
  replaceRunResults,
  setRunConnection,
  type LiveRunState,
} from "./streaming";
import type {
  CustomProjectDraft,
  Project,
  ProjectAggregate,
  ReceiptSample,
  RunManifest,
  RunCreateRequest,
  RunHistoryItem,
  RunSnapshot,
  Solution,
  WorkspaceView,
} from "./types";
import {
  buildCustomProjectPayload,
  parseOptionalGold,
  validateCustomProjectDraft,
} from "./wizard";
import { AppShell } from "./components/AppShell";
import { AdminCenter } from "./components/AdminCenter";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { CustomProjectWizard } from "./components/CustomProjectWizard";
import { DatasetBrowser } from "./components/DatasetBrowser";
import { EvaluationStudio } from "./components/EvaluationStudio";
import { HomeWorkspace } from "./components/HomeWorkspace";
import { ModelsCatalog } from "./components/ModelsCatalog";
import { RunBuilder } from "./components/RunBuilder";
import { RunsWorkspace } from "./components/RunsWorkspace";
import { ReportsWorkspace } from "./components/ReportsWorkspace";

export const FULL_RUN_HISTORY_LIMIT = 100;
export const RECENT_RUN_HISTORY_LIMIT = 8;

const WORKSPACE_VIEWS: readonly WorkspaceView[] = [
  "home",
  "evaluation",
  "dataset",
  "builder",
  "catalog",
  "runs",
  "reports",
  "admin",
  "new_project",
];

export interface WorkspaceUrlState {
  view: WorkspaceView;
  runId: string | null;
}

export interface WorkspaceNavigationRequest {
  generation: number;
  location: WorkspaceUrlState;
}

export interface PendingWorkspaceSnapshot {
  snapshot: RunSnapshot;
  navigation: WorkspaceNavigationRequest;
  sourceProjectId: string;
}

export type WorkspaceHistoryMode = "push" | "replace";

interface WorkspaceHistoryTarget {
  location: { href: string };
  history: {
    pushState(data: unknown, unused: string, url?: string | URL | null): void;
    replaceState(data: unknown, unused: string, url?: string | URL | null): void;
  };
}

interface WorkspacePopstateTarget {
  location: { href: string };
  addEventListener(type: "popstate", listener: () => void): void;
  removeEventListener(type: "popstate", listener: () => void): void;
}

function isWorkspaceView(value: string | null): value is WorkspaceView {
  return value !== null && WORKSPACE_VIEWS.some((view) => view === value);
}

export function parseWorkspaceUrl(value: string | URL): WorkspaceUrlState {
  const url = value instanceof URL ? value : new URL(value);
  const rawRunId = url.searchParams.get("run");
  const runId =
    rawRunId === null || rawRunId.trim() === "" ? null : rawRunId.trim();
  const requestedView = url.searchParams.get("view");
  return {
    view: isWorkspaceView(requestedView)
      ? requestedView
      : runId === null
        ? "home"
        : "evaluation",
    runId,
  };
}

export function createWorkspaceUrl(
  currentHref: string,
  state: WorkspaceUrlState,
): URL {
  const url = new URL(currentHref);
  url.searchParams.set("view", state.view);
  if (state.runId === null) {
    url.searchParams.delete("run");
  } else {
    url.searchParams.set("run", state.runId);
  }
  return url;
}

export function updateWorkspaceUrl(
  target: WorkspaceHistoryTarget,
  state: WorkspaceUrlState,
  mode: WorkspaceHistoryMode,
): void {
  const nextUrl = createWorkspaceUrl(target.location.href, state);
  if (nextUrl.href === new URL(target.location.href).href) {
    return;
  }
  if (mode === "push") {
    target.history.pushState(null, "", nextUrl);
  } else {
    target.history.replaceState(null, "", nextUrl);
  }
}

export function subscribeToWorkspacePopstate(
  target: WorkspacePopstateTarget,
  onChange: (state: WorkspaceUrlState) => void,
): () => void {
  const handlePopstate = () => {
    onChange(parseWorkspaceUrl(target.location.href));
  };
  target.addEventListener("popstate", handlePopstate);
  return () => target.removeEventListener("popstate", handlePopstate);
}

/**
 * Versions the run target in the URL. View-only changes keep the same
 * generation because they do not change which snapshot may be restored.
 */
export class WorkspaceNavigationGuard {
  private generation = 0;
  private runId: string | null = null;

  capture(location: WorkspaceUrlState): WorkspaceNavigationRequest {
    if (location.runId !== this.runId) {
      this.generation += 1;
      this.runId = location.runId;
    }
    return { generation: this.generation, location };
  }

  isCurrent(
    request: WorkspaceNavigationRequest,
    location: WorkspaceUrlState,
  ): boolean {
    return (
      request.generation === this.generation &&
      request.location.runId === location.runId &&
      location.runId === this.runId
    );
  }

  get currentRunId(): string | null {
    return this.runId;
  }
}

export function canApplyWorkspaceNavigation(
  guard: WorkspaceNavigationGuard,
  request: WorkspaceNavigationRequest,
  currentHref: string,
): boolean {
  return guard.isCurrent(request, parseWorkspaceUrl(currentHref));
}

export function pendingWorkspaceSnapshotForProject(
  pending: PendingWorkspaceSnapshot | null,
  projectId: string,
  guard: WorkspaceNavigationGuard,
  currentHref: string,
): RunSnapshot | null {
  return pending?.snapshot.manifest.project_id === projectId &&
    canApplyWorkspaceNavigation(
      guard,
      pending.navigation,
      currentHref,
    )
    ? pending.snapshot
    : null;
}

export function runHistoryForSurfaces(history: RunHistoryItem[]): {
  full: RunHistoryItem[];
  recent: RunHistoryItem[];
} {
  return {
    full: history,
    recent: history.slice(0, RECENT_RUN_HISTORY_LIMIT),
  };
}

export function fetchFullProjectRunHistory(
  client: Pick<ProductClient, "listProjectRuns">,
  projectId: string,
  signal: AbortSignal,
): Promise<RunHistoryItem[]> {
  return client.listProjectRuns(projectId, FULL_RUN_HISTORY_LIMIT, signal);
}

function toggleSet(current: Set<string>, value: string): Set<string> {
  const next = new Set(current);
  if (next.has(value)) {
    next.delete(value);
  } else {
    next.add(value);
  }
  return next;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const prefix = error.code === null ? "" : `${error.code}: `;
    const details = error.issues.length === 0 ? "" : ` · ${error.issues.join(" · ")}`;
    return `${prefix}${error.message}${details}`;
  }

  return error instanceof Error ? error.message : "The request failed.";
}

export async function refreshRunSnapshot({
  client,
  runId,
  signal,
  resultSampleId,
  isCurrent,
  applySnapshot,
  setError,
}: {
  client: Pick<ProductClient, "getRun">;
  runId: string;
  signal: AbortSignal;
  resultSampleId?: string;
  isCurrent: () => boolean;
  applySnapshot: (snapshot: RunSnapshot) => void;
  setError: (message: string | null) => void;
}): Promise<void> {
  try {
    const snapshot = await client.getRun(runId, signal, resultSampleId);
    if (signal.aborted || !isCurrent()) {
      return;
    }
    applySnapshot(snapshot);
    setError(null);
  } catch (error) {
    if (
      !signal.aborted &&
      isCurrent() &&
      !(error instanceof DOMException && error.name === "AbortError")
    ) {
      setError(errorMessage(error));
    }
  }
}

function launchFailureOutcome(error: unknown): LaunchAttemptOutcome {
  if (!(error instanceof ApiError)) {
    return "retryable";
  }
  return error.status >= 500 ||
    error.status === 408 ||
    error.status === 409 ||
    error.status === 425 ||
    error.status === 429
    ? "retryable"
    : "definitive_failure";
}

function normalizeSamples(
  client: ProductClient,
  projectId: string,
  samples: ReceiptSample[],
): ReceiptSample[] {
  return samples.map((sample) => ({
    ...sample,
    image_url:
      sample.image_url ??
      (sample.image_available
        ? client.sampleImageUrl(projectId, sample.id)
        : null),
  }));
}

function initialSolutionIds(project: Project, solutions: Solution[]): string[] {
  const preferred = project.selected_solution_ids.filter((id) => {
    const solution = solutions.find((item) => item.id === id);
    return (
      solution !== undefined &&
      getCompatibility(project, solution) !== "unsupported"
    );
  });
  if (preferred.length > 0) {
    return preferred;
  }
  return solutions
    .filter((solution) => getCompatibility(project, solution) !== "unsupported")
    .slice(0, 5)
    .map((solution) => solution.id);
}

export interface AppProps {
  client?: ProductClient;
  explicitMock?: boolean;
  allowOfflineFallback?: boolean;
}

export function App({
  client: providedClient,
  explicitMock = import.meta.env.VITE_PRODUCT_CLIENT === "mock",
  allowOfflineFallback = import.meta.env.VITE_OFFLINE_FALLBACK === "true",
}: AppProps = {}) {
  const httpClientRef = useRef<ProductClient>(
    providedClient ?? new HttpProductClient(),
  );
  const [bootRevision, setBootRevision] = useState(0);
  const [projectLoadRevision, setProjectLoadRevision] = useState(0);
  const [bootstrap, setBootstrap] = useState<BootstrapResult | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [activeProjectId, setActiveProjectId] = useState("");
  const [activeProject, setActiveProject] = useState<Project | null>(null);
  const [samples, setSamples] = useState<ReceiptSample[]>([]);
  const [solutions, setSolutions] = useState<Solution[]>([]);
  const [view, setView] = useState<WorkspaceView>(() =>
    typeof window === "undefined"
      ? "home"
      : parseWorkspaceUrl(window.location.href).view,
  );
  const [browserSampleId, setBrowserSampleId] = useState("");
  const [selectedRunSampleId, setSelectedRunSampleId] = useState("");
  const [selectedSolutionId, setSelectedSolutionId] = useState("");
  const [selectedSampleIds, setSelectedSampleIds] = useState<Set<string>>(
    new Set(),
  );
  const [selectedSolutionIds, setSelectedSolutionIds] = useState<Set<string>>(
    new Set(),
  );
  const [schemaConfirmed, setSchemaConfirmed] = useState(false);
  const [promptConfirmed, setPromptConfirmed] = useState(false);
  const [liveRun, setLiveRun] = useState<LiveRunState | null>(null);
  const [aggregate, setAggregate] = useState<ProjectAggregate | null>(null);
  const [aggregateError, setAggregateError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [resultLoad, setResultLoad] = useState<{
    runId: string;
    sampleId: string;
    status: "loading" | "error";
  } | null>(null);
  const [runHistory, setRunHistory] = useState<RunHistoryItem[]>([]);
  const [runHistoryError, setRunHistoryError] = useState<string | null>(null);
  const [runHistoryLoading, setRunHistoryLoading] = useState(false);
  const [restoringRunId, setRestoringRunId] = useState<string | null>(null);
  const [launchPending, setLaunchPending] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<
    | { type: "run"; id: string }
    | { type: "project"; id: string; name: string }
    | null
  >(null);
  const [deletePending, setDeletePending] = useState(false);
  const [adminRefreshToken, setAdminRefreshToken] = useState(0);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const snapshotAbortRef = useRef<AbortController | null>(null);
  const resultsAbortRef = useRef<AbortController | null>(null);
  const historyAbortRef = useRef<AbortController | null>(null);
  const restoreAbortRef = useRef<AbortController | null>(null);
  const runGuardRef = useRef(new RunGenerationGuard());
  const launchLockRef = useRef(new RunLaunchLock());
  const aggregateRefreshRef = useRef(new AggregateRefreshController());
  const runGenerationRef = useRef<number | null>(null);
  const projectGenerationRef = useRef(0);
  const sampleRefreshGenerationRef = useRef(0);
  const historyGuardRef = useRef(new ProjectRequestGuard());
  const restoreGuardRef = useRef(new ProjectRequestGuard());
  const transitionGuardRef = useRef(new ProjectRequestGuard());
  const workspaceNavigationGuardRef = useRef(new WorkspaceNavigationGuard());
  const pendingUrlSnapshotRef =
    useRef<PendingWorkspaceSnapshot | null>(null);
  const workspacePopstateHandlerRef = useRef<
    (location: WorkspaceUrlState) => void
  >(() => undefined);
  const stableProjectIdRef = useRef("");
  const activeProjectIdRef = useRef("");
  const selectedRunSampleIdRef = useRef("");
  const viewRef = useRef<WorkspaceView>(view);
  activeProjectIdRef.current = activeProjectId;
  selectedRunSampleIdRef.current = selectedRunSampleId;
  viewRef.current = view;

  const client = bootstrap?.client ?? httpClientRef.current;
  const projectSamples = useMemo(
    () =>
      activeProject === null
        ? []
        : samples.filter((sample) => sample.project_id === activeProject.id),
    [activeProject, samples],
  );
  const historySurfaces = useMemo(
    () => runHistoryForSurfaces(runHistory),
    [runHistory],
  );

  const refreshRunHistory = useCallback(
    async (projectId: string, showLoading = true) => {
      if (bootstrap === null || projectId !== activeProjectIdRef.current) {
        return;
      }
      const generation = historyGuardRef.current.begin(projectId);
      historyAbortRef.current?.abort();
      const controller = new AbortController();
      historyAbortRef.current = controller;
      if (showLoading) {
        setRunHistoryLoading(true);
      }
      setRunHistoryError(null);
      try {
        const history = await fetchFullProjectRunHistory(
          bootstrap.client,
          projectId,
          controller.signal,
        );
        if (
          controller.signal.aborted ||
          !historyGuardRef.current.isCurrent(generation, projectId) ||
          projectId !== activeProjectIdRef.current
        ) {
          return;
        }
        if (history.some((run) => run.project_id !== projectId)) {
          setRunHistory([]);
          setRunHistoryError("Run history returned data for a different project.");
          return;
        }
        setRunHistory(history);
      } catch (error) {
        if (
          !controller.signal.aborted &&
          historyGuardRef.current.isCurrent(generation, projectId) &&
          projectId === activeProjectIdRef.current
        ) {
          setRunHistoryError(errorMessage(error));
        }
      } finally {
        if (
          historyAbortRef.current === controller &&
          historyGuardRef.current.isCurrent(generation, projectId)
        ) {
          historyAbortRef.current = null;
          setRunHistoryLoading(false);
        }
      }
    },
    [bootstrap],
  );

  const refreshProjectSamples = useCallback(
    async (projectId: string) => {
      if (bootstrap === null || projectId !== activeProjectIdRef.current) {
        return;
      }
      const generation = ++sampleRefreshGenerationRef.current;
      try {
        const incoming = await bootstrap.client.listSamples(projectId);
        if (
          generation === sampleRefreshGenerationRef.current &&
          projectId === activeProjectIdRef.current
        ) {
          setSamples(normalizeSamples(bootstrap.client, projectId, incoming));
        }
      } catch (error) {
        if (
          generation === sampleRefreshGenerationRef.current &&
          projectId === activeProjectIdRef.current
        ) {
          setRunError(errorMessage(error));
        }
      }
    },
    [bootstrap],
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    void bootstrapProduct({
      explicitMock,
      allowOfflineFallback,
      httpClient: httpClientRef.current,
    })
      .then(async (result) => {
        if (cancelled) {
          return;
        }
        let resolvedLocation =
          typeof window === "undefined"
            ? { view: "home" as const, runId: null }
            : parseWorkspaceUrl(window.location.href);
        let requestedSnapshot: RunSnapshot | null = null;
        let requestedNavigation =
          workspaceNavigationGuardRef.current.capture(resolvedLocation);
        while (resolvedLocation.runId !== null) {
          try {
            const snapshot = await result.client.getRun(resolvedLocation.runId);
            if (cancelled) {
              return;
            }
            if (!isCurrentWorkspaceNavigation(requestedNavigation)) {
              resolvedLocation =
                typeof window === "undefined"
                  ? { view: "home", runId: null }
                  : parseWorkspaceUrl(window.location.href);
              requestedNavigation =
                workspaceNavigationGuardRef.current.capture(resolvedLocation);
              continue;
            }
            if (typeof window !== "undefined") {
              resolvedLocation = parseWorkspaceUrl(window.location.href);
              requestedNavigation =
                workspaceNavigationGuardRef.current.capture(resolvedLocation);
            }
            if (
              result.projects.some(
                (project) => project.id === snapshot.manifest.project_id,
              )
            ) {
              requestedSnapshot = snapshot;
              break;
            }
          } catch {
            if (!isCurrentWorkspaceNavigation(requestedNavigation)) {
              resolvedLocation =
                typeof window === "undefined"
                  ? { view: "home", runId: null }
                  : parseWorkspaceUrl(window.location.href);
              requestedNavigation =
                workspaceNavigationGuardRef.current.capture(resolvedLocation);
              continue;
            }
            if (typeof window !== "undefined") {
              resolvedLocation = parseWorkspaceUrl(window.location.href);
              requestedNavigation =
                workspaceNavigationGuardRef.current.capture(resolvedLocation);
            }
          }
          resolvedLocation = { ...resolvedLocation, runId: null };
          requestedNavigation =
            workspaceNavigationGuardRef.current.capture(resolvedLocation);
          if (typeof window !== "undefined") {
            updateWorkspaceUrl(window, resolvedLocation, "replace");
          }
          break;
        }
        if (cancelled) {
          return;
        }
        const selectedProjectId =
          requestedSnapshot?.manifest.project_id ?? result.projects[0]?.id ?? "";
        activeProjectIdRef.current = selectedProjectId;
        stableProjectIdRef.current = result.projects[0]?.id ?? "";
        setBootstrap(result);
        setProjects(result.projects);
        pendingUrlSnapshotRef.current =
          requestedSnapshot === null
            ? null
            : {
                snapshot: requestedSnapshot,
                navigation: requestedNavigation,
                sourceProjectId: stableProjectIdRef.current,
              };
        setWorkspaceView(resolvedLocation.view);
        if (typeof window !== "undefined") {
          updateWorkspaceUrl(window, resolvedLocation, "replace");
        }
        setActiveProjectId(selectedProjectId);
        setLoading(false);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(errorMessage(error));
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [allowOfflineFallback, bootRevision, explicitMock]);

  useEffect(() => {
    if (bootstrap === null || activeProjectId === "") {
      return;
    }
    setRunHistory([]);
    setRunHistoryError(null);
    void refreshRunHistory(activeProjectId);
    return () => {
      historyGuardRef.current.invalidate();
      historyAbortRef.current?.abort();
      historyAbortRef.current = null;
    };
  }, [activeProjectId, bootstrap, refreshRunHistory]);

  useEffect(() => {
    if (bootstrap === null || activeProjectId === "") {
      return;
    }
    const generation = ++projectGenerationRef.current;
    setLoading(true);
    setLoadError(null);
    void Promise.all([
      bootstrap.client.getProject(activeProjectId),
      bootstrap.client.listSamples(activeProjectId),
      bootstrap.client.listSolutions(activeProjectId),
    ])
      .then(([project, incomingSamples, incomingSolutions]) => {
        if (
          generation !== projectGenerationRef.current ||
          project.id !== activeProjectId
        ) {
          return;
        }
        if (!project.available) {
          throw new ApiError(
            403,
            project.availability_reason ?? "Project data is not authorized.",
            { code: "project_unavailable" },
          );
        }
        const orderedSolutions = [...incomingSolutions].sort(
          (left, right) => left.order - right.order,
        );
        const normalizedSamples = normalizeSamples(
          bootstrap.client,
          project.id,
          incomingSamples,
        );
        const pendingSnapshot = pendingUrlSnapshotRef.current;
        const snapshot =
          typeof window === "undefined"
            ? pendingSnapshot?.snapshot.manifest.project_id === project.id &&
              workspaceNavigationGuardRef.current.isCurrent(
                pendingSnapshot.navigation,
                pendingSnapshot.navigation.location,
              )
              ? pendingSnapshot.snapshot
              : null
            : pendingWorkspaceSnapshotForProject(
                pendingSnapshot,
                project.id,
                workspaceNavigationGuardRef.current,
                window.location.href,
              );
        if (
          pendingSnapshot?.snapshot.manifest.project_id === project.id &&
          snapshot === null
        ) {
          return;
        }
        setActiveProject(project);
        setSamples(normalizedSamples);
        setSolutions(orderedSolutions);
        if (snapshot !== null) {
          const generationId = runGuardRef.current.begin();
          runGuardRef.current.bind(generationId, snapshot.manifest.id);
          runGenerationRef.current = generationId;
          setLiveRun(applyRunSnapshot(null, snapshot, orderedSolutions));
          setSelectedSampleIds(new Set(snapshot.manifest.sample_ids));
          setSelectedSolutionIds(new Set(snapshot.manifest.solution_ids));
          setSelectedRunSampleId(snapshot.manifest.sample_ids[0] ?? "");
          setBrowserSampleId(snapshot.manifest.sample_ids[0] ?? "");
          setSelectedSolutionId(snapshot.manifest.solution_ids[0] ?? "");
          if (pendingUrlSnapshotRef.current === pendingSnapshot) {
            pendingUrlSnapshotRef.current = null;
          }
        } else {
          const firstSample =
            normalizedSamples.find((sample) => sample.scoring_role === "scored") ??
            normalizedSamples[0];
          const solutionIds = initialSolutionIds(project, orderedSolutions);
          setBrowserSampleId(firstSample?.id ?? "");
          setSelectedRunSampleId("");
          setSelectedSampleIds(
            new Set(
              firstSample?.scoring_role === "scored" ? [firstSample.id] : [],
            ),
          );
          setSelectedSolutionIds(new Set(solutionIds));
          setSelectedSolutionId(solutionIds[0] ?? "");
        }
        stableProjectIdRef.current = project.id;
        setSchemaConfirmed(false);
        setPromptConfirmed(false);
        setLoading(false);
      })
      .catch((error: unknown) => {
        if (generation === projectGenerationRef.current) {
          setLoadError(errorMessage(error));
          setLoading(false);
        }
      });
  }, [activeProjectId, bootstrap, projectLoadRevision]);

  useEffect(() => {
    if (
      liveRun === null ||
      activeProject === null ||
      liveRun.manifest.project_id !== activeProject.id
    ) {
      setSelectedRunSampleId("");
      return;
    }
    const manifestSamples = liveRun.manifest.sample_ids.filter((sampleId) =>
      projectSamples.some((sample) => sample.id === sampleId),
    );
    const manifestSolutions = liveRun.manifest.solution_ids.filter((solutionId) =>
      solutions.some((solution) => solution.id === solutionId),
    );
    setSelectedRunSampleId((current) =>
      manifestSamples.includes(current) ? current : (manifestSamples[0] ?? ""),
    );
    setSelectedSolutionId((current) =>
      manifestSolutions.includes(current)
        ? current
        : (manifestSolutions[0] ?? ""),
    );
  }, [
    activeProject,
    liveRun?.manifest.id,
    liveRun?.manifest.project_id,
    liveRun?.manifest.sample_ids,
    liveRun?.manifest.solution_ids,
    projectSamples,
    solutions,
  ]);

  const refreshRun = useCallback(
    async (
      runId: string,
      generation: number,
      resultSampleId = selectedRunSampleIdRef.current || undefined,
    ) => {
      if (bootstrap === null) {
        return;
      }
      snapshotAbortRef.current?.abort();
      const controller = new AbortController();
      snapshotAbortRef.current = controller;
      try {
        await refreshRunSnapshot({
          client: bootstrap.client,
          runId,
          signal: controller.signal,
          resultSampleId,
          isCurrent: () => runGuardRef.current.isCurrent(generation, runId),
          applySnapshot: (snapshot) => {
            setLiveRun((current) =>
              current === null
                ? applyRunSnapshot(null, snapshot, solutions)
                : applyRunSnapshot(current, snapshot, solutions),
            );
          },
          setError: setRunError,
        });
      } finally {
        if (snapshotAbortRef.current === controller) {
          snapshotAbortRef.current = null;
        }
      }
    },
    [bootstrap, solutions],
  );

  useEffect(() => {
    if (
      bootstrap === null ||
      liveRun === null ||
      liveRun.manifest.status === "completed" ||
      liveRun.manifest.status === "failed"
    ) {
      return;
    }
    const runId = liveRun.manifest.id;
    const generation = runGenerationRef.current;
    if (generation === null || !runGuardRef.current.isCurrent(generation, runId)) {
      return;
    }
    const unsubscribe = bootstrap.client.subscribeToRun(
      runId,
      {
        onEvent: (event, metadata) => {
          if (!runGuardRef.current.isCurrent(generation, runId)) {
            return;
          }
          setLiveRun((current) =>
            current === null || current.manifest.id !== runId
              ? current
              : reduceRunEvent(current, event, metadata.eventId),
          );
          if (
            event.type === "task_completed" &&
            event.result === undefined &&
            event.task.sample_id === selectedRunSampleIdRef.current
          ) {
            void bootstrap.client
              .getRunResult(
                runId,
                event.task.sample_id,
                event.task.solution_id,
              )
              .then((result) => {
                if (runGuardRef.current.isCurrent(generation, runId)) {
                  setLiveRun((current) =>
                    current === null
                      ? current
                      : replaceRunResults(current, [result]),
                  );
                }
              })
              .catch((error: unknown) => {
                if (runGuardRef.current.isCurrent(generation, runId)) {
                  setRunError(errorMessage(error));
                }
              });
          }
          if (event.type === "run_completed" || event.type === "run_failed") {
            void refreshRun(runId, generation);
          }
        },
        onConnectionChange: (connection) => {
          if (!runGuardRef.current.isCurrent(generation, runId)) {
            return;
          }
          setLiveRun((current) =>
            current === null || current.manifest.id !== runId
              ? current
              : setRunConnection(current, connection),
          );
        },
        onError: () => {
          if (runGuardRef.current.isCurrent(generation, runId)) {
            void refreshRun(runId, generation);
          }
        },
      },
      { lastEventId: liveRun.lastEventId },
    );
    return unsubscribe;
  }, [bootstrap, liveRun?.manifest.id, refreshRun]);

  useEffect(() => {
    const manifest = liveRun?.manifest;
    if (
      manifest !== undefined &&
      (manifest.status === "completed" || manifest.status === "failed") &&
      manifest.project_id === activeProjectIdRef.current
    ) {
      void refreshRunHistory(manifest.project_id, false);
      void refreshProjectSamples(manifest.project_id);
    }
  }, [
    liveRun?.manifest.id,
    liveRun?.manifest.project_id,
    liveRun?.manifest.status,
    refreshProjectSamples,
    refreshRunHistory,
  ]);

  useEffect(() => {
    const manifest = liveRun?.manifest;
    if (bootstrap === null || manifest === undefined) {
      aggregateRefreshRef.current.clear();
      setAggregate(null);
      setAggregateError(null);
      return;
    }
    if (manifest.completed_pairs === 0) {
      setAggregate(null);
      setAggregateError(null);
    }
    const generation = runGenerationRef.current;
    aggregateRefreshRef.current.update({
      runKey: `${manifest.project_id}:${manifest.id}`,
      completedPairs: manifest.completed_pairs,
      status: manifest.status,
      fetch: async (signal) => {
        try {
          const incoming = await bootstrap.client.getProjectAggregate(
            manifest.project_id,
            manifest.id,
            signal,
          );
          if (
            !signal.aborted &&
            generation !== null &&
            runGuardRef.current.isCurrent(generation, manifest.id) &&
            incoming.project_id === manifest.project_id &&
            incoming.run_id === manifest.id
          ) {
            setAggregate(scopeProjectAggregateToManifest(incoming, manifest));
            setAggregateError(null);
          }
        } catch (error) {
          if (
            !signal.aborted &&
            generation !== null &&
            runGuardRef.current.isCurrent(generation, manifest.id)
          ) {
            setAggregateError(errorMessage(error));
          }
          throw error;
        }
      },
    });
  }, [
    bootstrap,
    liveRun?.manifest.completed_pairs,
    liveRun?.manifest.id,
    liveRun?.manifest.project_id,
    liveRun?.manifest.status,
  ]);

  useEffect(
    () => () => {
      runGuardRef.current.invalidate();
      snapshotAbortRef.current?.abort();
      resultsAbortRef.current?.abort();
      historyGuardRef.current.invalidate();
      historyAbortRef.current?.abort();
      restoreGuardRef.current.invalidate();
      restoreAbortRef.current?.abort();
      transitionGuardRef.current.invalidate();
      sampleRefreshGenerationRef.current += 1;
      aggregateRefreshRef.current.clear();
    },
    [],
  );

  function selectRunSample(sampleId: string) {
    const run = liveRun;
    if (
      run !== null &&
      run.manifest.project_id === activeProject?.id &&
      run.manifest.sample_ids.includes(sampleId) &&
      projectSamples.some((sample) => sample.id === sampleId)
    ) {
      setSelectedRunSampleId(sampleId);
      const generation = runGenerationRef.current;
      if (bootstrap !== null && generation !== null) {
        setResultLoad({
          runId: run.manifest.id,
          sampleId,
          status: "loading",
        });
        resultsAbortRef.current?.abort();
        const controller = new AbortController();
        resultsAbortRef.current = controller;
        void bootstrap.client
          .listRunResults(run.manifest.id, sampleId, controller.signal)
          .then((results) => {
            if (
              !controller.signal.aborted &&
              runGuardRef.current.isCurrent(generation, run.manifest.id)
            ) {
              setLiveRun((current) =>
                current === null ? current : replaceRunResults(current, results),
              );
              setResultLoad((current) =>
                current?.runId === run.manifest.id &&
                current.sampleId === sampleId
                  ? null
                  : current,
              );
            }
          })
          .catch((error: unknown) => {
            if (
              !controller.signal.aborted &&
              runGuardRef.current.isCurrent(generation, run.manifest.id)
            ) {
              setRunError(errorMessage(error));
              setResultLoad((current) =>
                current?.runId === run.manifest.id &&
                current.sampleId === sampleId
                  ? { ...current, status: "error" }
                  : current,
              );
            }
          })
          .finally(() => {
            if (resultsAbortRef.current === controller) {
              resultsAbortRef.current = null;
            }
          });
      }
    }
  }

  function selectRunSolution(solutionId: string) {
    const run = liveRun;
    if (
      run !== null &&
      run.manifest.project_id === activeProject?.id &&
      run.manifest.solution_ids.includes(solutionId) &&
      solutions.some((solution) => solution.id === solutionId)
    ) {
      setSelectedSolutionId(solutionId);
    }
  }

  function applyRestoredRunSnapshot(snapshot: RunSnapshot): void {
    snapshotAbortRef.current?.abort();
    resultsAbortRef.current?.abort();
    aggregateRefreshRef.current.clear();
    const generation = runGuardRef.current.begin();
    if (!runGuardRef.current.bind(generation, snapshot.manifest.id)) {
      return;
    }
    runGenerationRef.current = generation;
    setAggregate(null);
    setAggregateError(null);
    setLiveRun(applyRunSnapshot(null, snapshot, solutions));
    setSelectedSampleIds(new Set(snapshot.manifest.sample_ids));
    setSelectedSolutionIds(new Set(snapshot.manifest.solution_ids));
    setSelectedRunSampleId(snapshot.manifest.sample_ids[0] ?? "");
    setBrowserSampleId(snapshot.manifest.sample_ids[0] ?? "");
    setSelectedSolutionId(snapshot.manifest.solution_ids[0] ?? "");
  }

  async function restoreRun(run: RunHistoryItem) {
    const project = activeProject;
    if (
      bootstrap === null ||
      project === null ||
      run.project_id !== project.id ||
      project.id !== activeProjectIdRef.current
    ) {
      setRunError("That run does not belong to the active project.");
      return;
    }
    if (launchLockRef.current.pending) {
      setRunError("Wait for the new run request to finish before restoring another run.");
      return;
    }
    const transitionGeneration = transitionGuardRef.current.begin(project.id);
    const restoreGeneration = restoreGuardRef.current.begin(project.id);
    resultsAbortRef.current?.abort();
    restoreAbortRef.current?.abort();
    const controller = new AbortController();
    restoreAbortRef.current = controller;
    setRestoringRunId(run.run_id);
    setRunError(null);
    try {
      const snapshot = await bootstrap.client.getRun(run.run_id, controller.signal);
      if (
        controller.signal.aborted ||
        !restoreGuardRef.current.isCurrent(restoreGeneration, project.id) ||
        !transitionGuardRef.current.isCurrent(
          transitionGeneration,
          project.id,
        ) ||
        project.id !== activeProjectIdRef.current
      ) {
        return;
      }
      if (!snapshotMatchesHistoryRun(snapshot, run)) {
        setRunError("The selected run could not be restored for this project.");
        return;
      }

      applyRestoredRunSnapshot(snapshot);
      navigateWorkspace("evaluation", { runId: run.run_id });
    } catch (error) {
      if (
        !controller.signal.aborted &&
        restoreGuardRef.current.isCurrent(restoreGeneration, project.id) &&
        transitionGuardRef.current.isCurrent(
          transitionGeneration,
          project.id,
        ) &&
        project.id === activeProjectIdRef.current
      ) {
        setRunError(errorMessage(error));
      }
    } finally {
      if (
        restoreAbortRef.current === controller &&
        restoreGuardRef.current.isCurrent(restoreGeneration, project.id)
      ) {
        restoreAbortRef.current = null;
        setRestoringRunId(null);
      }
    }
  }

  async function restoreRunFromUrl(
    runId: string,
    navigation: WorkspaceNavigationRequest,
  ): Promise<void> {
    if (bootstrap === null || !isCurrentWorkspaceNavigation(navigation)) {
      return;
    }
    const sourceProjectId = activeProjectIdRef.current;
    if (launchLockRef.current.pending) {
      setRunError(
        "Wait for the new run request to finish before restoring another run.",
      );
      navigateWorkspace(viewRef.current, {
        runId: liveRun?.manifest.id ?? null,
        historyMode: "replace",
      });
      return;
    }
    const transitionGeneration =
      transitionGuardRef.current.begin(sourceProjectId);
    const restoreGeneration = restoreGuardRef.current.begin(sourceProjectId);
    resultsAbortRef.current?.abort();
    restoreAbortRef.current?.abort();
    const controller = new AbortController();
    restoreAbortRef.current = controller;
    setRestoringRunId(runId);
    setRunError(null);
    try {
      const snapshot = await bootstrap.client.getRun(runId, controller.signal);
      if (
        controller.signal.aborted ||
        !restoreGuardRef.current.isCurrent(
          restoreGeneration,
          sourceProjectId,
        ) ||
        !transitionGuardRef.current.isCurrent(
          transitionGeneration,
          sourceProjectId,
        ) ||
        sourceProjectId !== activeProjectIdRef.current ||
        !isCurrentWorkspaceNavigation(navigation)
      ) {
        return;
      }
      const linkedProject = projects.find(
        (candidate) => candidate.id === snapshot.manifest.project_id,
      );
      if (snapshot.manifest.id !== runId || linkedProject === undefined) {
        setRunError("The linked run could not be restored for this workspace.");
        navigateWorkspace(viewRef.current, {
          runId: null,
          historyMode: "replace",
        });
        return;
      }
      if (
        activeProject?.id === snapshot.manifest.project_id &&
        activeProject.id === activeProjectIdRef.current
      ) {
        applyRestoredRunSnapshot(snapshot);
        return;
      }

      const projectChanged =
        linkedProject.id !== activeProjectIdRef.current;
      runGuardRef.current.invalidate();
      runGenerationRef.current = null;
      snapshotAbortRef.current?.abort();
      resultsAbortRef.current?.abort();
      if (projectChanged) {
        projectGenerationRef.current += 1;
      }
      historyGuardRef.current.invalidate();
      historyAbortRef.current?.abort();
      sampleRefreshGenerationRef.current += 1;
      aggregateRefreshRef.current.clear();
      setLiveRun(null);
      setAggregate(null);
      setAggregateError(null);
      setRunHistory([]);
      setRunHistoryError(null);
      setRunHistoryLoading(false);
      pendingUrlSnapshotRef.current = {
        snapshot,
        navigation,
        sourceProjectId:
          stableProjectIdRef.current || sourceProjectId,
      };
      setActiveProject(null);
      setSamples([]);
      setSolutions([]);
      setBrowserSampleId("");
      setSelectedRunSampleId("");
      if (projectChanged) {
        activeProjectIdRef.current = linkedProject.id;
        setActiveProjectId(linkedProject.id);
      } else {
        setProjectLoadRevision((current) => current + 1);
      }
      setLoading(true);
    } catch (error) {
      if (
        !controller.signal.aborted &&
        restoreGuardRef.current.isCurrent(
          restoreGeneration,
          sourceProjectId,
        ) &&
        transitionGuardRef.current.isCurrent(
          transitionGeneration,
          sourceProjectId,
        ) &&
        sourceProjectId === activeProjectIdRef.current &&
        isCurrentWorkspaceNavigation(navigation)
      ) {
        setRunError(errorMessage(error));
        navigateWorkspace(viewRef.current, {
          runId: null,
          historyMode: "replace",
        });
      }
    } finally {
      if (
        restoreAbortRef.current === controller &&
        restoreGuardRef.current.isCurrent(
          restoreGeneration,
          sourceProjectId,
        )
      ) {
        restoreAbortRef.current = null;
        setRestoringRunId(null);
      }
    }
  }

  function setWorkspaceView(next: WorkspaceView): void {
    viewRef.current = next;
    setView(next);
  }

  function isCurrentWorkspaceNavigation(
    request: WorkspaceNavigationRequest,
  ): boolean {
    return typeof window === "undefined"
      ? workspaceNavigationGuardRef.current.isCurrent(
          request,
          request.location,
        )
      : canApplyWorkspaceNavigation(
          workspaceNavigationGuardRef.current,
          request,
          window.location.href,
        );
  }

  function applyWorkspaceLocation(
    location: WorkspaceUrlState,
    restoreFromUrl: boolean,
  ): void {
    setWorkspaceView(location.view);
    const navigationGuard = workspaceNavigationGuardRef.current;
    const previousRunId = navigationGuard.currentRunId;
    const navigation = navigationGuard.capture(location);
    if (previousRunId === location.runId) {
      return;
    }

    restoreAbortRef.current?.abort();
    restoreAbortRef.current = null;
    restoreGuardRef.current.invalidate();
    setRestoringRunId(null);

    const pendingSnapshot = pendingUrlSnapshotRef.current;
    pendingUrlSnapshotRef.current = null;
    if (pendingSnapshot !== null) {
      if (location.runId === null) {
        const fallbackProjectId = pendingSnapshot.sourceProjectId;
        if (
          fallbackProjectId !== "" &&
          fallbackProjectId !== activeProjectIdRef.current
        ) {
          projectGenerationRef.current += 1;
          activeProjectIdRef.current = fallbackProjectId;
          setActiveProjectId(fallbackProjectId);
          setLoading(true);
        }
      } else {
        projectGenerationRef.current += 1;
      }
    }

    if (
      restoreFromUrl &&
      bootstrap !== null &&
      location.runId !== null &&
      (pendingSnapshot !== null ||
        location.runId !== liveRun?.manifest.id)
    ) {
      void restoreRunFromUrl(location.runId, navigation);
    }
  }

  function navigateWorkspace(
    next: WorkspaceView,
    {
      runId,
      historyMode = "push",
    }: {
      runId?: string | null;
      historyMode?: WorkspaceHistoryMode;
    } = {},
  ): void {
    setWorkspaceView(next);
    if (typeof window === "undefined") {
      return;
    }
    const currentLocation = parseWorkspaceUrl(window.location.href);
    const nextRunId =
      next === "home"
        ? null
        : runId === undefined
          ? currentLocation.runId
          : runId;
    updateWorkspaceUrl(
      window,
      { view: next, runId: nextRunId },
      historyMode,
    );
    applyWorkspaceLocation({ view: next, runId: nextRunId }, false);
  }

  function selectProject(projectId: string) {
    if (!projects.some((project) => project.id === projectId)) {
      return;
    }
    activeProjectIdRef.current = projectId;
    projectGenerationRef.current += 1;
    runGuardRef.current.invalidate();
    snapshotAbortRef.current?.abort();
    resultsAbortRef.current?.abort();
    historyGuardRef.current.invalidate();
    historyAbortRef.current?.abort();
    restoreGuardRef.current.invalidate();
    restoreAbortRef.current?.abort();
    pendingUrlSnapshotRef.current = null;
    transitionGuardRef.current.invalidate();
    sampleRefreshGenerationRef.current += 1;
    aggregateRefreshRef.current.clear();
    setLiveRun(null);
    setAggregate(null);
    setRunError(null);
    setRunHistory([]);
    setRunHistoryError(null);
    setRunHistoryLoading(false);
    setRestoringRunId(null);
    setActiveProject(null);
    setSamples([]);
    setSolutions([]);
    setBrowserSampleId("");
    setSelectedRunSampleId("");
    setActiveProjectId(projectId);
    const nextView =
      viewRef.current === "home" || viewRef.current === "admin"
        ? viewRef.current
        : "evaluation";
    navigateWorkspace(nextView, { runId: null, historyMode: "replace" });
  }

  function selectWorkspaceView(next: WorkspaceView) {
    navigateWorkspace(next);
  }

  function openBuilder(sampleId?: string) {
    if (sampleId !== undefined) {
      const sample = projectSamples.find((item) => item.id === sampleId);
      if (sample?.scoring_role !== "scored") {
        return;
      }
      setBrowserSampleId(sampleId);
      setSelectedSampleIds(new Set([sampleId]));
    }
    navigateWorkspace("builder");
  }

  async function launchRun() {
    if (bootstrap === null || activeProject === null) {
      return;
    }
    const selectedSolutions = solutions
      .filter(
        (solution) =>
          selectedSolutionIds.has(solution.id) &&
          getCompatibility(activeProject, solution) !== "unsupported",
      )
      .sort((left, right) => left.order - right.order);
    const sampleIds = projectSamples
      .filter(
        (sample) =>
          sample.scoring_role === "scored" && selectedSampleIds.has(sample.id),
      )
      .map((sample) => sample.id);
    const solutionIds = selectedSolutions.map((solution) => solution.id);
    if (sampleIds.length === 0 || solutionIds.length === 0) {
      setRunError("Choose at least one runnable receipt and compatible solution.");
      return;
    }
    const request: RunCreateRequest = {
      sample_ids: sampleIds,
      solution_ids: solutionIds,
      schema_version: activeProject.schema.version,
      prompt_version: activeProject.prompt.version,
      execution_mode: bootstrap.health.execution_mode,
    };
    const fingerprint = runDraftFingerprint(activeProject.id, request);
    const attempt = launchLockRef.current.begin(fingerprint);
    if (attempt === null) {
      return;
    }
    const transitionGeneration = transitionGuardRef.current.begin(activeProject.id);
    restoreGuardRef.current.invalidate();
    restoreAbortRef.current?.abort();
    setRestoringRunId(null);
    setLaunchPending(true);
    setRunError(null);
    let outcome: LaunchAttemptOutcome = "retryable";
    try {
      const accepted = await bootstrap.client.createRun(
        activeProject.id,
        request,
        attempt.idempotencyKey,
      );
      outcome = "applied";
      if (
        !transitionGuardRef.current.isCurrent(
          transitionGeneration,
          activeProject.id,
        ) ||
        activeProject.id !== activeProjectIdRef.current
      ) {
        return;
      }
      snapshotAbortRef.current?.abort();
      resultsAbortRef.current?.abort();
      aggregateRefreshRef.current.clear();
      const generation = runGuardRef.current.begin();
      runGenerationRef.current = generation;
      if (!runGuardRef.current.bind(generation, accepted.run_id)) {
        return;
      }
      setAggregate(null);
      setAggregateError(null);
      const manifest: RunManifest = {
        id: accepted.run_id,
        project_id: activeProject.id,
        sample_ids: sampleIds,
        solution_ids: solutionIds,
        schema_version: activeProject.schema.version,
        prompt_version: activeProject.prompt.version,
        status: accepted.status,
        total_pairs: accepted.total_pairs,
        completed_pairs: 0,
        created_at: new Date().toISOString(),
        execution_mode: bootstrap.health.execution_mode,
        reference_only_solution_ids: selectedSolutions
          .filter((solution) => !isComparableQuality(activeProject, solution))
          .map((solution) => solution.id),
      };
      setLiveRun(createInitialRunState(manifest, solutions));
      setSelectedRunSampleId(sampleIds[0] ?? "");
      setSelectedSolutionId(solutionIds[0] ?? "");
      navigateWorkspace("evaluation", { runId: accepted.run_id });
      void refreshRunHistory(activeProject.id, false);
      void refreshProjectSamples(activeProject.id);
      await refreshRun(accepted.run_id, generation, sampleIds[0]);
    } catch (error) {
      if (
        transitionGuardRef.current.isCurrent(
          transitionGeneration,
          activeProject.id,
        ) &&
        activeProject.id === activeProjectIdRef.current
      ) {
        setRunError(errorMessage(error));
      }
      outcome = launchFailureOutcome(error);
    } finally {
      launchLockRef.current.finish(fingerprint, outcome);
      setLaunchPending(false);
    }
  }

  function activateCustomProject(refreshed: Project) {
    runGuardRef.current.invalidate();
    runGenerationRef.current = null;
    snapshotAbortRef.current?.abort();
    resultsAbortRef.current?.abort();
    projectGenerationRef.current += 1;
    historyGuardRef.current.invalidate();
    historyAbortRef.current?.abort();
    restoreGuardRef.current.invalidate();
    restoreAbortRef.current?.abort();
    pendingUrlSnapshotRef.current = null;
    transitionGuardRef.current.invalidate();
    sampleRefreshGenerationRef.current += 1;
    aggregateRefreshRef.current.clear();
    setLiveRun(null);
    setAggregate(null);
    setAggregateError(null);
    setRunError(null);
    setRunHistory([]);
    setRunHistoryError(null);
    setRunHistoryLoading(false);
    setRestoringRunId(null);
    setProjects((current) => [
      ...current.filter((project) => project.id !== refreshed.id),
      refreshed,
    ]);
    setActiveProject(null);
    setBrowserSampleId("");
    setSelectedRunSampleId("");
    activeProjectIdRef.current = refreshed.id;
    setActiveProjectId(refreshed.id);
    navigateWorkspace("dataset", { runId: null });
  }

  async function createCustomProject(draft: CustomProjectDraft) {
    if (bootstrap === null) {
      throw new Error("The server connection is not ready.");
    }
    const validation = validateCustomProjectDraft(draft, solutions);
    if (validation.schema === null || validation.issues.length > 0) {
      throw new Error("Resolve the client preflight issues before submitting.");
    }
    const created = await bootstrap.client.createProject(
      buildCustomProjectPayload(draft, solutions),
    );
    try {
      for (const image of draft.images) {
        if (image.file === null) {
          throw new Error(`${image.filename} must be selected again before upload.`);
        }
        try {
          await bootstrap.client.uploadSample(
            created.id,
            image.file,
            parseOptionalGold(image.gold_text),
          );
        } catch (error) {
          throw new Error(`${image.filename}: ${errorMessage(error)}`);
        }
      }
    } catch (error) {
      const uploadMessage = errorMessage(error);
      try {
        await bootstrap.client.deleteProject(created.id);
      } catch (cleanupError) {
        setProjects((current) => [
          ...current.filter((project) => project.id !== created.id),
          created,
        ]);
        throw new Error(
          `${uploadMessage} The incomplete project remains visible because rollback failed: ${errorMessage(
            cleanupError,
          )}`,
        );
      }
      throw new Error(`${uploadMessage} The incomplete project was removed.`);
    }
    activateCustomProject(await bootstrap.client.getProject(created.id));
  }

  async function importLabeledDataset(file: File) {
    if (bootstrap === null) {
      throw new Error("The server connection is not ready.");
    }
    activateCustomProject(await bootstrap.client.importLabeledDataset(file));
  }

  function requestDeleteRun(runId: string) {
    setDeleteError(null);
    setPendingDelete({ type: "run", id: runId });
  }

  function requestDeleteProject(projectId: string, projectName: string) {
    setDeleteError(null);
    setPendingDelete({ type: "project", id: projectId, name: projectName });
  }

  function cancelDelete() {
    if (deletePending) {
      return;
    }
    setPendingDelete(null);
    setDeleteError(null);
  }

  async function confirmDelete() {
    if (bootstrap === null || pendingDelete === null) {
      return;
    }
    setDeletePending(true);
    setDeleteError(null);
    try {
      if (pendingDelete.type === "run") {
        await bootstrap.client.deleteRun(pendingDelete.id);
        if (liveRun?.manifest.id === pendingDelete.id) {
          runGuardRef.current.invalidate();
          snapshotAbortRef.current?.abort();
          resultsAbortRef.current?.abort();
          aggregateRefreshRef.current.clear();
          setLiveRun(null);
          setAggregate(null);
          setAggregateError(null);
          setRunError(null);
          navigateWorkspace(viewRef.current, {
            runId: null,
            historyMode: "replace",
          });
        }
        if (activeProjectIdRef.current !== "") {
          void refreshRunHistory(activeProjectIdRef.current, false);
        }
      } else {
        await bootstrap.client.deleteProject(pendingDelete.id);
        const remaining = projects.filter(
          (project) => project.id !== pendingDelete.id,
        );
        setProjects(remaining);
        if (activeProjectIdRef.current === pendingDelete.id) {
          const fallback = remaining[0];
          if (fallback !== undefined) {
            selectProject(fallback.id);
          } else {
            runGuardRef.current.invalidate();
            historyGuardRef.current.invalidate();
            setLiveRun(null);
            setAggregate(null);
            setAggregateError(null);
            setRunError(null);
            setRunHistory([]);
            setActiveProject(null);
            setSamples([]);
            setSolutions([]);
            setBrowserSampleId("");
            setSelectedRunSampleId("");
            activeProjectIdRef.current = "";
            setActiveProjectId("");
            navigateWorkspace("home", {
              runId: null,
              historyMode: "replace",
            });
          }
        }
      }
      setAdminRefreshToken((current) => current + 1);
      setPendingDelete(null);
    } catch (error) {
      setDeleteError(errorMessage(error));
    } finally {
      setDeletePending(false);
    }
  }

  workspacePopstateHandlerRef.current = (location) => {
    if (typeof window !== "undefined") {
      updateWorkspaceUrl(window, location, "replace");
    }
    applyWorkspaceLocation(location, true);
  };

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    return subscribeToWorkspacePopstate(window, (location) => {
      workspacePopstateHandlerRef.current(location);
    });
  }, []);

  if (loading && activeProject === null) {
    return (
      <main className="boot-state" aria-live="polite">
        <span className="boot-mark">
          <span />
        </span>
        <p>Loading authorized receipt projects…</p>
      </main>
    );
  }

  if (loadError !== null || bootstrap === null) {
    return (
      <main className="boot-state boot-error" role="alert">
        <h1>Receipt Bench is unavailable</h1>
        <p>{loadError ?? "The API did not return a runtime profile."}</p>
        <button
          className="primary-button"
          type="button"
          onClick={() => setBootRevision((value) => value + 1)}
        >
          Retry server
        </button>
      </main>
    );
  }

  if (projects.length === 0 || activeProject === null) {
    return (
      <main className="boot-state">
        <h1>No authorized projects</h1>
        <p>The server did not return a project available to this session.</p>
      </main>
    );
  }

  return (
    <>
      <AppShell
        projects={projects}
        activeProject={activeProject}
        view={view}
        health={bootstrap.health}
        source={bootstrap.source}
        runState={liveRun}
        runHistory={historySurfaces.recent}
        runHistoryError={runHistoryError}
        runHistoryLoading={runHistoryLoading}
        restoringRunId={restoringRunId}
        samples={projectSamples}
        selectedSampleId={selectedRunSampleId}
        onProjectChange={selectProject}
        onViewChange={selectWorkspaceView}
        onSampleChange={selectRunSample}
        onRunSelect={(run) => void restoreRun(run)}
        onRunHistoryRefresh={() =>
          void refreshRunHistory(activeProject.id)
        }
        onDeleteProjectRequest={() =>
          requestDeleteProject(activeProject.id, activeProject.name)
        }
        onDeleteRunRequest={() => {
          if (liveRun !== null) {
            requestDeleteRun(liveRun.manifest.id);
          }
        }}
      >
        {runError !== null && (
          <div className="global-error" role="alert">
            <strong>Operation failed</strong>
            <span>{runError}</span>
            <button type="button" onClick={() => setRunError(null)}>
              Dismiss
            </button>
          </div>
        )}
        {view === "home" && (
          <HomeWorkspace
            projects={projects}
            project={activeProject}
            solutions={solutions}
            runState={liveRun}
            runHistory={historySurfaces.full}
            onOpenDataset={() => navigateWorkspace("dataset")}
            onCreateDataset={() => navigateWorkspace("new_project")}
            onBuildRun={() => openBuilder()}
            onOpenEvaluation={() => navigateWorkspace("evaluation")}
            onOpenRuns={() => navigateWorkspace("runs")}
            onOpenReports={() => navigateWorkspace("reports")}
          />
        )}
        {view === "evaluation" && (
          <EvaluationStudio
            client={client}
            project={activeProject}
            samples={projectSamples}
            solutions={solutions}
            runState={liveRun}
            aggregate={aggregate}
            aggregateError={aggregateError}
            resultLoadStatus={
              resultLoad !== null &&
              resultLoad.runId === liveRun?.manifest.id &&
              resultLoad.sampleId === selectedRunSampleId
                ? resultLoad.status
                : "idle"
            }
            selectedSampleId={selectedRunSampleId}
            selectedSolutionId={selectedSolutionId}
            onSampleChange={selectRunSample}
            onSolutionChange={selectRunSolution}
            onBuildRun={() => openBuilder()}
          />
        )}
        {view === "dataset" && (
          <DatasetBrowser
            project={activeProject}
            samples={projectSamples}
            selectedSampleId={browserSampleId}
            onSampleSelect={setBrowserSampleId}
            onBuildRun={openBuilder}
          />
        )}
        {view === "catalog" && (
          <ModelsCatalog project={activeProject} solutions={solutions} />
        )}
        {view === "builder" && (
          <RunBuilder
            project={activeProject}
            samples={projectSamples}
            solutions={solutions}
            selectedSampleIds={selectedSampleIds}
            selectedSolutionIds={selectedSolutionIds}
            schemaConfirmed={schemaConfirmed}
            promptConfirmed={promptConfirmed}
            executionMode={bootstrap.health.execution_mode}
            launchPending={launchPending}
            onSampleToggle={(id) =>
              setSelectedSampleIds((current) => toggleSet(current, id))
            }
            onSolutionToggle={(id) =>
              setSelectedSolutionIds((current) => toggleSet(current, id))
            }
            onSelectSamples={(ids) => setSelectedSampleIds(new Set(ids))}
            onSelectSolutions={(ids) => setSelectedSolutionIds(new Set(ids))}
            onSchemaConfirm={setSchemaConfirmed}
            onPromptConfirm={setPromptConfirmed}
            onLaunch={() => void launchRun()}
          />
        )}
        {view === "runs" && (
          <RunsWorkspace
            project={activeProject}
            runs={historySurfaces.full}
            activeRunId={liveRun?.manifest.id ?? null}
            loading={runHistoryLoading}
            error={runHistoryError}
            restoringRunId={restoringRunId}
            onRefresh={() => void refreshRunHistory(activeProject.id)}
            onOpen={(run) => void restoreRun(run)}
            onDelete={requestDeleteRun}
            onBuildRun={() => openBuilder()}
          />
        )}
        {view === "reports" && (
          <ReportsWorkspace
            project={activeProject}
            solutions={solutions}
            runState={liveRun}
            aggregate={aggregate}
            aggregateError={aggregateError}
            onOpenRuns={() => navigateWorkspace("runs")}
            onBuildRun={() => openBuilder()}
            onOpenEvaluation={() => navigateWorkspace("evaluation")}
            onOpenEvidence={(sampleId, solutionId) => {
              selectRunSample(sampleId);
              selectRunSolution(solutionId);
              navigateWorkspace("evaluation");
            }}
          />
        )}
        {view === "admin" && (
          <AdminCenter
            client={client}
            projects={projects}
            project={activeProject}
            solutions={solutions}
            refreshToken={adminRefreshToken}
            onDeleteDataset={requestDeleteProject}
            onDeleteRun={requestDeleteRun}
          />
        )}
        {view === "new_project" && (
          <CustomProjectWizard
            solutions={solutions}
            onCreate={createCustomProject}
            onImport={importLabeledDataset}
          />
        )}
      </AppShell>
      <ConfirmDialog
        open={pendingDelete !== null}
        title={
          pendingDelete?.type === "run"
            ? "Delete this run?"
            : "Delete this dataset?"
        }
        description={
          pendingDelete === null ? (
            ""
          ) : pendingDelete.type === "run" ? (
            <p>
              Run <code>{pendingDelete.id}</code> will be permanently deleted, along
              with its results and cost/latency telemetry. This cannot be undone.
            </p>
          ) : (
            <p>
              <strong>{pendingDelete.name}</strong> will be permanently deleted. All
              receipts, runs, and artifacts stored for this dataset will be removed
              from storage. This cannot be undone.
            </p>
          )
        }
        confirmLabel={pendingDelete?.type === "run" ? "Delete run" : "Delete dataset"}
        pending={deletePending}
        error={deleteError}
        onConfirm={() => void confirmDelete()}
        onCancel={cancelDelete}
      />
    </>
  );
}
