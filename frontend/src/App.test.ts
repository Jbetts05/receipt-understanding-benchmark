import { describe, expect, it, vi } from "vitest";

import type { ProductClient } from "./api";
import {
  FULL_RUN_HISTORY_LIMIT,
  RECENT_RUN_HISTORY_LIMIT,
  WorkspaceNavigationGuard,
  canApplyWorkspaceNavigation,
  fetchFullProjectRunHistory,
  parseWorkspaceUrl,
  pendingWorkspaceSnapshotForProject,
  refreshRunSnapshot,
  runHistoryForSurfaces,
  subscribeToWorkspacePopstate,
  updateWorkspaceUrl,
  type PendingWorkspaceSnapshot,
  type WorkspaceUrlState,
} from "./App";
import type { RunHistoryItem, RunSnapshot, WorkspaceView } from "./types";

function snapshot(sampleId: string): RunSnapshot {
  return {
    manifest: {
      id: "new-run",
      project_id: "project",
      sample_ids: [sampleId],
      solution_ids: ["solution"],
      schema_version: "1",
      prompt_version: "1",
      status: "running",
      total_pairs: 1,
      completed_pairs: 0,
      created_at: "2026-08-30T10:00:00Z",
      execution_mode: "mock",
      reference_only_solution_ids: [],
    },
    tasks: [],
    results: [],
  };
}

function historyItem(index: number): RunHistoryItem {
  return {
    run_id: `run-${index}`,
    project_id: "project",
    status: "completed",
    created_at: `2026-08-30T10:${String(59 - index).padStart(2, "0")}:00Z`,
    updated_at: `2026-08-30T10:${String(59 - index).padStart(2, "0")}:30Z`,
    total_pairs: 1,
    completed_pairs: 1,
    execution_mode: "mock",
    sample_count: 1,
    solution_count: 1,
    reference_only_count: 0,
    download_ready: true,
  };
}

function linkedSnapshot(runId: string, projectId: string): RunSnapshot {
  const incoming = snapshot("linked-sample");
  return {
    ...incoming,
    manifest: {
      ...incoming.manifest,
      id: runId,
      project_id: projectId,
    },
  };
}

describe("run snapshot refresh", () => {
  it("uses the new run sample explicitly and clears a previous error", async () => {
    const incoming = snapshot("new-sample");
    const getRun = vi.fn(
      async (
        _runId: string,
        _signal?: AbortSignal,
        _resultSampleId?: string,
      ) => incoming,
    );
    const client: Pick<ProductClient, "getRun"> = { getRun };
    let applied: RunSnapshot | null = null;
    let runError: string | null = "old sample was not found";
    const controller = new AbortController();

    await refreshRunSnapshot({
      client,
      runId: "new-run",
      signal: controller.signal,
      resultSampleId: "new-sample",
      isCurrent: () => true,
      applySnapshot: (value) => {
        applied = value;
      },
      setError: (value) => {
        runError = value;
      },
    });

    expect(getRun).toHaveBeenCalledWith(
      "new-run",
      controller.signal,
      "new-sample",
    );
    expect(applied).toBe(incoming);
    expect(runError).toBeNull();
  });

  it("does not apply or clear errors from an aborted refresh", async () => {
    let resolve: ((value: RunSnapshot) => void) | undefined;
    const pending = new Promise<RunSnapshot>((done) => {
      resolve = done;
    });
    const client: Pick<ProductClient, "getRun"> = {
      getRun: vi.fn(() => pending),
    };
    const applySnapshot = vi.fn();
    const setError = vi.fn();
    const controller = new AbortController();

    const refresh = refreshRunSnapshot({
      client,
      runId: "old-run",
      signal: controller.signal,
      resultSampleId: "old-sample",
      isCurrent: () => true,
      applySnapshot,
      setError,
    });
    controller.abort();
    resolve?.(snapshot("old-sample"));
    await refresh;

    expect(applySnapshot).not.toHaveBeenCalled();
    expect(setError).not.toHaveBeenCalled();
  });
});

describe("run history surfaces", () => {
  it("fetches the API maximum while limiting only the recent-runs rail", async () => {
    const history = Array.from({ length: 12 }, (_, index) => historyItem(index));
    const listProjectRuns = vi.fn(async () => history);
    const signal = new AbortController().signal;

    const full = await fetchFullProjectRunHistory(
      { listProjectRuns },
      "project",
      signal,
    );
    const surfaces = runHistoryForSurfaces(full);

    expect(listProjectRuns).toHaveBeenCalledWith(
      "project",
      FULL_RUN_HISTORY_LIMIT,
      signal,
    );
    expect(FULL_RUN_HISTORY_LIMIT).toBe(100);
    expect(surfaces.full).toBe(full);
    expect(surfaces.full).toHaveLength(12);
    expect(surfaces.recent).toEqual(
      history.slice(0, RECENT_RUN_HISTORY_LIMIT),
    );
    expect(surfaces.recent).toHaveLength(8);
  });
});

describe("workspace URL state", () => {
  const views: WorkspaceView[] = [
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

  it.each(views)("restores the %s workspace on refresh", (view) => {
    expect(
      parseWorkspaceUrl(`https://receipt.test/app?view=${view}`),
    ).toEqual({ view, runId: null });
  });

  it("validates workspace values and preserves run deep links", () => {
    expect(parseWorkspaceUrl("https://receipt.test/app?run=run-1")).toEqual({
      view: "evaluation",
      runId: "run-1",
    });
    expect(
      parseWorkspaceUrl(
        "https://receipt.test/app?view=reports&run=run-1",
      ),
    ).toEqual({ view: "reports", runId: "run-1" });
    expect(
      parseWorkspaceUrl(
        "https://receipt.test/app?view=not-a-workspace&run=run-1",
      ),
    ).toEqual({ view: "evaluation", runId: "run-1" });
    expect(
      parseWorkspaceUrl(
        "https://receipt.test/app?view=not-a-workspace&run=%20",
      ),
    ).toEqual({ view: "home", runId: null });
  });

  it("pushes user navigation and replaces canonical state without losing other query data", () => {
    const location = {
      href: "https://receipt.test/app?tenant=west&view=home",
    };
    const pushState = vi.fn(
      (_data: unknown, _unused: string, url?: string | URL | null) => {
        if (url !== undefined && url !== null) {
          location.href = url.toString();
        }
      },
    );
    const replaceState = vi.fn(
      (_data: unknown, _unused: string, url?: string | URL | null) => {
        if (url !== undefined && url !== null) {
          location.href = url.toString();
        }
      },
    );
    const target = {
      location,
      history: { pushState, replaceState },
    };

    updateWorkspaceUrl(
      target,
      { view: "admin", runId: null },
      "push",
    );
    expect(pushState).toHaveBeenCalledOnce();
    expect(new URL(location.href).searchParams.get("tenant")).toBe("west");
    expect(new URL(location.href).searchParams.get("view")).toBe("admin");

    updateWorkspaceUrl(
      target,
      { view: "evaluation", runId: "run-2" },
      "replace",
    );
    expect(replaceState).toHaveBeenCalledOnce();
    expect(parseWorkspaceUrl(location.href)).toEqual({
      view: "evaluation",
      runId: "run-2",
    });
  });

  it("applies validated workspace state when browser Back changes the URL", () => {
    const location = {
      href: "https://receipt.test/app?view=runs&run=run-2",
    };
    const popstateListeners: Array<() => void> = [];
    const target = {
      location,
      addEventListener: vi.fn(
        (_type: "popstate", listener: () => void) => {
          popstateListeners.push(listener);
        },
      ),
      removeEventListener: vi.fn(
        (_type: "popstate", listener: () => void) => {
          const index = popstateListeners.indexOf(listener);
          if (index !== -1) {
            popstateListeners.splice(index, 1);
          }
        },
      ),
    };
    const states: WorkspaceUrlState[] = [];
    const unsubscribe = subscribeToWorkspacePopstate(target, (state) => {
      states.push(state);
    });

    location.href = "https://receipt.test/app?view=admin";
    popstateListeners[0]?.();
    location.href =
      "https://receipt.test/app?view=invalid&run=previous-run";
    popstateListeners[0]?.();

    expect(states).toEqual([
      { view: "admin", runId: null },
      { view: "evaluation", runId: "previous-run" },
    ]);
    unsubscribe();
    expect(popstateListeners).toHaveLength(0);
  });

  it("rejects the first project B lookup after Back and Forward change its generation", async () => {
    const guard = new WorkspaceNavigationGuard();
    let currentHref =
      "https://receipt.test/app?view=evaluation&run=run-b";
    const projectBNavigation = guard.capture(
      parseWorkspaceUrl(currentHref),
    );
    let resolveLookup: ((value: RunSnapshot) => void) | undefined;
    const lookup = new Promise<RunSnapshot>((resolve) => {
      resolveLookup = resolve;
    });
    let applied: RunSnapshot | null = null;
    const restore = lookup.then((incoming) => {
      if (
        canApplyWorkspaceNavigation(
          guard,
          projectBNavigation,
          currentHref,
        )
      ) {
        applied = incoming;
      }
    });

    currentHref =
      "https://receipt.test/app?view=evaluation&run=run-a";
    guard.capture(parseWorkspaceUrl(currentHref));
    currentHref =
      "https://receipt.test/app?view=evaluation&run=run-b";
    guard.capture(parseWorkspaceUrl(currentHref));
    resolveLookup?.(linkedSnapshot("run-b", "project-b"));
    await restore;

    expect(parseWorkspaceUrl(currentHref).runId).toBe("run-b");
    expect(applied).toBeNull();
  });

  it("rejects the first project B snapshot after Back and Forward during project load", async () => {
    const guard = new WorkspaceNavigationGuard();
    let currentHref =
      "https://receipt.test/app?view=evaluation&run=run-b";
    const pending: PendingWorkspaceSnapshot = {
      snapshot: linkedSnapshot("run-b", "project-b"),
      navigation: guard.capture(parseWorkspaceUrl(currentHref)),
      sourceProjectId: "project-a",
    };
    let finishProjectLoad: (() => void) | undefined;
    const projectLoad = new Promise<void>((resolve) => {
      finishProjectLoad = resolve;
    });
    let applied: RunSnapshot | null = null;
    const loading = projectLoad.then(() => {
      applied = pendingWorkspaceSnapshotForProject(
        pending,
        "project-b",
        guard,
        currentHref,
      );
    });

    currentHref = "https://receipt.test/app?view=home";
    guard.capture(parseWorkspaceUrl(currentHref));
    currentHref =
      "https://receipt.test/app?view=evaluation&run=run-b";
    guard.capture(parseWorkspaceUrl(currentHref));
    finishProjectLoad?.();
    await loading;

    expect(parseWorkspaceUrl(currentHref).runId).toBe("run-b");
    expect(applied).toBeNull();
  });
});
