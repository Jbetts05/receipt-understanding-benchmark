import { describe, expect, it } from "vitest";

import { resultPairKey } from "./domain";
import { makeMockRunResult, mockProjects, mockSamples, mockSolutions } from "./mockData";
import {
  applyRunSnapshot,
  createInitialRunState,
  reduceRunEvent,
} from "./streaming";
import type { RunEvent, RunManifest, RunTask } from "./types";

function manifest(
  sampleIds = ["offline-001"],
  solutionIds = ["offline-direct", "offline-ocr"],
): RunManifest {
  return {
    id: "stream-test",
    project_id: "offline-demo",
    sample_ids: sampleIds,
    solution_ids: solutionIds,
    schema_version: "1.0.0",
    prompt_version: "1.0.0",
    status: "running",
    total_pairs: sampleIds.length * solutionIds.length,
    completed_pairs: 0,
    created_at: "2026-08-30T00:00:00Z",
    execution_mode: "mock",
    reference_only_solution_ids: [],
  };
}

function completedTask(sampleId: string, solutionId: string, order: number): RunTask {
  return {
    pair_id: resultPairKey(sampleId, solutionId),
    sample_id: sampleId,
    solution_id: solutionId,
    solution_order: order,
    status: "succeeded",
    stage: "completed",
    progress: 1,
    retries: 0,
    elapsed_ms: 20,
    estimated_cost_usd: 0.001,
    partial_output: {},
    error: null,
    compatibility: "full",
    comparable_quality: true,
  };
}

describe("durable run state", () => {
  it("creates one stable pending position for exactly every manifest pair", () => {
    const run = manifest(
      ["offline-001", "offline-002"],
      ["offline-direct", "offline-ocr"],
    );
    const state = createInitialRunState(run, mockSolutions);

    expect(state.positionKeys).toEqual([
      "offline-001::offline-direct",
      "offline-001::offline-ocr",
      "offline-002::offline-direct",
      "offline-002::offline-ocr",
    ]);
    expect(Object.values(state.tasks).every((task) => task.status === "queued")).toBe(
      true,
    );
  });

  it("does not show a live stream for a restored terminal run", () => {
    const completed = {
      ...manifest(["offline-001"], ["offline-direct"]),
      status: "completed" as const,
      completed_pairs: 1,
    };

    expect(createInitialRunState(completed, mockSolutions).connection).toBe("closed");
  });

  it("hydrates only matching project, run, and manifest results", () => {
    const run = manifest();
    const initial = createInitialRunState(run, mockSolutions);
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solution = mockSolutions[0]!;
    const valid = makeMockRunResult(project, sample, solution, run.id);
    const foreign = { ...valid, run_id: "other-run" };
    const snapshot = {
      manifest: { ...run, completed_pairs: 1 },
      tasks: [completedTask(sample.id, solution.id, solution.order)],
      results: [valid, foreign],
    };

    const hydrated = applyRunSnapshot(initial, snapshot, mockSolutions)!;
    expect(Object.keys(hydrated.results)).toEqual([
      resultPairKey(sample.id, solution.id),
    ]);
    expect(hydrated.tasks["offline-001::offline-ocr"]?.status).toBe("queued");
  });

  it("ignores stale terminal events from another run generation", () => {
    const run = manifest(["offline-001"], ["offline-direct"]);
    const state = createInitialRunState(run, mockSolutions);
    const foreign: RunEvent = {
      type: "run_completed",
      run: { ...run, id: "foreign-run", status: "completed", completed_pairs: 1 },
    };

    expect(reduceRunEvent(state, foreign)).toBe(state);
  });

  it("advances compact task completion events without hydrating result bodies", () => {
    const run = manifest(["offline-001"], ["offline-direct"]);
    const task = completedTask("offline-001", "offline-direct", 0);

    const completed = reduceRunEvent(
      createInitialRunState(run, mockSolutions),
      { type: "task_completed", task },
      "9",
    );

    expect(completed.tasks[task.pair_id]?.status).toBe("succeeded");
    expect(completed.manifest.completed_pairs).toBe(1);
    expect(completed.results).toEqual({});
    expect(completed.lastEventId).toBe("9");
  });

  it("rejects a delayed disconnect snapshot after replay events already arrived", () => {
    const run = manifest(["offline-001"], ["offline-direct"]);
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solution = mockSolutions[0]!;
    const result = makeMockRunResult(project, sample, solution, run.id);
    const task = completedTask(sample.id, solution.id, solution.order);
    const replayed = reduceRunEvent(
      createInitialRunState(run, mockSolutions),
      { type: "task_completed", task, result },
      "12",
    );

    const stale = applyRunSnapshot(
      replayed,
      {
        manifest: run,
        tasks: [],
        results: [],
        event_cursor: "10",
      },
      mockSolutions,
    );

    expect(stale).toBe(replayed);
    expect(stale?.results[resultPairKey(sample.id, solution.id)]).toEqual(result);
    expect(stale?.lastEventId).toBe("12");
  });

  it("does not retain a cursor past state omitted from the snapshot", () => {
    const run = manifest();
    const initial = createInitialRunState(run, mockSolutions);
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solution = mockSolutions[0]!;
    const result = makeMockRunResult(project, sample, solution, run.id);
    const task = completedTask(sample.id, solution.id, solution.order);
    const replayed = reduceRunEvent(
      initial,
      { type: "task_completed", task, result },
      "12",
    );
    const incomplete = applyRunSnapshot(
      replayed,
      {
        manifest: { ...run, completed_pairs: 1 },
        tasks: [task],
        results: [result],
        event_cursor: "20",
      },
      mockSolutions,
    )!;

    expect(incomplete.lastEventId).toBe("12");
    expect(incomplete.results[task.pair_id]).toEqual(result);
    expect(incomplete.tasks["offline-001::offline-ocr"]?.status).toBe("queued");

    const complete = applyRunSnapshot(
      initial,
      {
        manifest: { ...run, completed_pairs: 1 },
        tasks: [task, initial.tasks["offline-001::offline-ocr"]!],
        results: [result],
        event_cursor: "20",
      },
      mockSolutions,
    )!;
    expect(complete.lastEventId).toBe("20");
  });

  it("merges cursorless snapshots without dropping replayed terminal data", () => {
    const run = manifest(["offline-001"], ["offline-direct"]);
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solution = mockSolutions[0]!;
    const result = makeMockRunResult(project, sample, solution, run.id);
    const task = completedTask(sample.id, solution.id, solution.order);
    const replayed = reduceRunEvent(
      createInitialRunState(run, mockSolutions),
      { type: "task_completed", task, result },
      "12",
    );

    const merged = applyRunSnapshot(
      replayed,
      { manifest: run, tasks: [], results: [] },
      mockSolutions,
    )!;

    expect(merged.tasks[task.pair_id]?.status).toBe("succeeded");
    expect(merged.results[task.pair_id]).toEqual(result);
    expect(merged.manifest.completed_pairs).toBe(1);
    expect(merged.lastEventId).toBe("12");
  });
});
