import { resultPairKey } from "./domain";
import type {
  RunEvent,
  RunManifest,
  RunResult,
  RunSnapshot,
  RunTask,
  Solution,
} from "./types";

export interface LiveRunState {
  manifest: RunManifest;
  positionKeys: string[];
  tasks: Record<string, RunTask>;
  results: Record<string, RunResult>;
  error: string | null;
  connection: "connecting" | "open" | "reconnecting" | "closed";
  lastEventId: string | null;
}

const stageOrder: Record<RunTask["stage"], number> = {
  queued: 0,
  preflight: 1,
  ocr: 2,
  normalizing: 3,
  extracting: 4,
  evaluating: 5,
  persisting: 6,
  completed: 7,
  failed: 7,
};

function isTerminalTask(task: RunTask): boolean {
  return (
    task.status === "succeeded" ||
    task.status === "failed" ||
    task.status === "invalid"
  );
}

export function compareEventCursors(
  left: string,
  right: string,
): -1 | 0 | 1 | null {
  if (left === right) {
    return 0;
  }
  if (/^\d+$/.test(left) && /^\d+$/.test(right)) {
    const leftValue = BigInt(left);
    const rightValue = BigInt(right);
    return leftValue < rightValue ? -1 : 1;
  }
  return null;
}

export function latestEventCursor(
  left: string | null,
  right: string | null,
): string | null {
  if (left === null) {
    return right;
  }
  if (right === null) {
    return left;
  }
  const comparison = compareEventCursors(left, right);
  return comparison === -1 ? right : left;
}

function mergeManifest(
  current: RunManifest,
  incoming: RunManifest,
): RunManifest {
  const statusRank: Record<RunManifest["status"], number> = {
    queued: 0,
    running: 1,
    completed: 2,
    failed: 2,
  };
  const currentRank = statusRank[current.status];
  const incomingRank = statusRank[incoming.status];
  const useIncoming =
    incomingRank > currentRank || (incomingRank === currentRank && currentRank < 2);
  return {
    ...(useIncoming ? incoming : current),
    completed_pairs: Math.max(
      current.completed_pairs,
      incoming.completed_pairs,
    ),
  };
}

function mergeTask(current: RunTask, incoming: RunTask): RunTask {
  const currentTerminal = isTerminalTask(current);
  const incomingTerminal = isTerminalTask(incoming);
  if (currentTerminal !== incomingTerminal) {
    return incomingTerminal ? incoming : current;
  }
  if (currentTerminal) {
    return current;
  }
  const currentRank = [
    current.retries,
    stageOrder[current.stage],
    current.progress,
    current.elapsed_ms,
  ];
  const incomingRank = [
    incoming.retries,
    stageOrder[incoming.stage],
    incoming.progress,
    incoming.elapsed_ms,
  ];
  for (let index = 0; index < currentRank.length; index += 1) {
    const difference = (incomingRank[index] ?? 0) - (currentRank[index] ?? 0);
    if (difference !== 0) {
      return difference > 0 ? incoming : current;
    }
  }
  return incoming;
}

function queuedTask(
  sampleId: string,
  solutionId: string,
  solutionOrder: number,
  manifest: RunManifest,
): RunTask {
  const referenceOnly = manifest.reference_only_solution_ids.includes(solutionId);
  return {
    pair_id: resultPairKey(sampleId, solutionId),
    sample_id: sampleId,
    solution_id: solutionId,
    solution_order: solutionOrder,
    status: "queued",
    stage: "queued",
    progress: 0,
    retries: 0,
    elapsed_ms: 0,
    estimated_cost_usd: null,
    partial_output: null,
    error: null,
    compatibility: referenceOnly ? "partial_reference" : "full",
    comparable_quality: !referenceOnly,
  };
}

export function createInitialRunState(
  manifest: RunManifest,
  solutions: Solution[],
): LiveRunState {
  const solutionOrder = new Map(
    solutions.map((solution) => [solution.id, solution.order]),
  );
  const positionKeys: string[] = [];
  const tasks: Record<string, RunTask> = {};
  manifest.sample_ids.forEach((sampleId) => {
    manifest.solution_ids.forEach((solutionId, solutionIndex) => {
      const key = resultPairKey(sampleId, solutionId);
      positionKeys.push(key);
      tasks[key] = queuedTask(
        sampleId,
        solutionId,
        solutionOrder.get(solutionId) ?? solutionIndex,
        manifest,
      );
    });
  });
  return {
    manifest,
    positionKeys,
    tasks,
    results: {},
    error: null,
    connection:
      manifest.status === "completed" || manifest.status === "failed"
        ? "closed"
        : "connecting",
    lastEventId: null,
  };
}

function taskMatchesManifest(task: RunTask, manifest: RunManifest): boolean {
  return (
    manifest.sample_ids.includes(task.sample_id) &&
    manifest.solution_ids.includes(task.solution_id) &&
    task.pair_id === resultPairKey(task.sample_id, task.solution_id)
  );
}

function resultMatchesManifest(
  result: RunResult,
  manifest: RunManifest,
): boolean {
  return (
    result.run_id === manifest.id &&
    result.project_id === manifest.project_id &&
    manifest.sample_ids.includes(result.sample_id) &&
    manifest.solution_ids.includes(result.solution_id)
  );
}

function snapshotCoversCursor(snapshot: RunSnapshot): boolean {
  const expectedPairs = Math.max(
    snapshot.manifest.total_pairs,
    snapshot.manifest.sample_ids.length *
      snapshot.manifest.solution_ids.length,
  );
  const tasks = snapshot.tasks.filter((task) =>
    taskMatchesManifest(task, snapshot.manifest),
  );
  const taskKeys = new Set(tasks.map((task) => task.pair_id));
  const terminalTaskKeys = new Set(
    tasks.filter(isTerminalTask).map((task) => task.pair_id),
  );

  return (
    taskKeys.size >= expectedPairs &&
    terminalTaskKeys.size >= snapshot.manifest.completed_pairs
  );
}

export function applyRunSnapshot(
  current: LiveRunState | null,
  snapshot: RunSnapshot,
  solutions: Solution[],
): LiveRunState | null {
  if (
    current !== null &&
    (snapshot.manifest.id !== current.manifest.id ||
      snapshot.manifest.project_id !== current.manifest.project_id)
  ) {
    return current;
  }
  const snapshotCursor =
    snapshot.event_cursor === null || snapshot.event_cursor === undefined
      ? null
      : String(snapshot.event_cursor).trim() || null;
  const applicableSnapshotCursor =
    snapshotCursor !== null && snapshotCoversCursor(snapshot)
    ? snapshotCursor
    : null;
  if (
    current?.lastEventId !== null &&
    current?.lastEventId !== undefined &&
    snapshotCursor !== null &&
    compareEventCursors(snapshotCursor, current.lastEventId) === -1
  ) {
    return current;
  }
  if (current === null) {
    const state = createInitialRunState(snapshot.manifest, solutions);
    snapshot.tasks.forEach((task) => {
      if (taskMatchesManifest(task, snapshot.manifest)) {
        state.tasks[task.pair_id] = task;
      }
    });
    snapshot.results.forEach((result) => {
      if (resultMatchesManifest(result, snapshot.manifest)) {
        state.results[resultPairKey(result.sample_id, result.solution_id)] = result;
      }
    });
    return {
      ...state,
      lastEventId: applicableSnapshotCursor,
    };
  }
  const manifest = mergeManifest(current.manifest, snapshot.manifest);
  const state = createInitialRunState(manifest, solutions);
  Object.entries(current.tasks).forEach(([key, task]) => {
    if (taskMatchesManifest(task, manifest) && key in state.tasks) {
      state.tasks[key] = task;
    }
  });
  snapshot.tasks.forEach((task) => {
    if (taskMatchesManifest(task, manifest) && task.pair_id in state.tasks) {
      state.tasks[task.pair_id] = mergeTask(
        state.tasks[task.pair_id]!,
        task,
      );
    }
  });
  Object.entries(current.results).forEach(([key, result]) => {
    if (resultMatchesManifest(result, manifest)) {
      state.results[key] = result;
    }
  });
  snapshot.results.forEach((result) => {
    if (resultMatchesManifest(result, manifest)) {
      const key = resultPairKey(result.sample_id, result.solution_id);
      state.results[key] ??= result;
    }
  });
  return {
    ...state,
    connection: current.connection,
    error: current.error,
    lastEventId: latestEventCursor(
      current.lastEventId,
      applicableSnapshotCursor,
    ),
  };
}

export function replaceRunResults(
  state: LiveRunState,
  incoming: RunResult[],
): LiveRunState {
  const results: Record<string, RunResult> = { ...state.results };
  incoming.forEach((result) => {
    if (resultMatchesManifest(result, state.manifest)) {
      results[resultPairKey(result.sample_id, result.solution_id)] = result;
    }
  });
  return { ...state, results };
}

export function reduceRunEvent(
  state: LiveRunState,
  event: RunEvent,
  eventId: string | null = null,
): LiveRunState {
  if (
    eventId !== null &&
    state.lastEventId !== null &&
    (compareEventCursors(eventId, state.lastEventId) === -1 ||
      compareEventCursors(eventId, state.lastEventId) === 0)
  ) {
    return state;
  }
  const nextEventId = eventId ?? state.lastEventId;
  if (
    (event.type === "run_started" ||
      event.type === "run_completed" ||
      event.type === "run_failed") &&
    (event.run.id !== state.manifest.id ||
      event.run.project_id !== state.manifest.project_id)
  ) {
    return state;
  }
  if (event.type === "run_started") {
    return {
      ...state,
      manifest: mergeManifest(state.manifest, event.run),
      error: null,
      lastEventId: nextEventId,
    };
  }
  if (event.type === "run_completed") {
    return {
      ...state,
      manifest: mergeManifest(state.manifest, event.run),
      lastEventId: nextEventId,
    };
  }
  if (event.type === "run_failed") {
    return {
      ...state,
      manifest: mergeManifest(state.manifest, event.run),
      error: event.message,
      lastEventId: nextEventId,
    };
  }
  const task = event.task;
  if (!taskMatchesManifest(task, state.manifest) || !(task.pair_id in state.tasks)) {
    return state;
  }
  if (event.type === "task_completed") {
    if (
      event.result !== undefined &&
      !resultMatchesManifest(event.result, state.manifest)
    ) {
      return state;
    }
    const previous = state.tasks[task.pair_id];
    const wasTerminal =
      previous?.status === "succeeded" ||
      previous?.status === "failed" ||
      previous?.status === "invalid";
    return {
      ...state,
      manifest: {
        ...state.manifest,
        completed_pairs: wasTerminal
          ? state.manifest.completed_pairs
          : Math.min(
              state.manifest.total_pairs,
              state.manifest.completed_pairs + 1,
            ),
      },
      tasks: {
        ...state.tasks,
        [task.pair_id]: mergeTask(state.tasks[task.pair_id]!, task),
      },
      results:
        event.result === undefined
          ? state.results
          : { ...state.results, [task.pair_id]: event.result },
      lastEventId: nextEventId,
    };
  }
  return {
    ...state,
    tasks: {
      ...state.tasks,
      [task.pair_id]: mergeTask(state.tasks[task.pair_id]!, task),
    },
    lastEventId: nextEventId,
  };
}

export function setRunConnection(
  state: LiveRunState,
  connection: LiveRunState["connection"],
): LiveRunState {
  return { ...state, connection };
}
