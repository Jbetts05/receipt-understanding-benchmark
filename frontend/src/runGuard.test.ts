import { describe, expect, it } from "vitest";

import {
  ProjectRequestGuard,
  RunGenerationGuard,
  RunLaunchLock,
  runDraftFingerprint,
  snapshotMatchesHistoryRun,
} from "./runGuard";
import type { RunHistoryItem, RunSnapshot } from "./types";

describe("RunGenerationGuard", () => {
  it("ignores a stale terminal snapshot after a newer run starts", async () => {
    const guard = new RunGenerationGuard();
    const applied: string[] = [];
    let releaseStale: (() => void) | undefined;
    const staleReady = new Promise<void>((resolve) => {
      releaseStale = resolve;
    });

    const firstGeneration = guard.begin();
    expect(guard.bind(firstGeneration, "run-one")).toBe(true);
    const staleCompletion = staleReady.then(() => {
      if (guard.isCurrent(firstGeneration, "run-one")) {
        applied.push("run-one");
      }
    });

    const secondGeneration = guard.begin();
    expect(guard.bind(secondGeneration, "run-two")).toBe(true);
    if (guard.isCurrent(secondGeneration, "run-two")) {
      applied.push("run-two");
    }
    releaseStale?.();
    await staleCompletion;

    expect(applied).toEqual(["run-two"]);
    expect(guard.isCurrent(firstGeneration, "run-one")).toBe(false);
  });

  it("cannot bind a run response from an obsolete start request", () => {
    const guard = new RunGenerationGuard();
    const obsolete = guard.begin();
    guard.begin();

    expect(guard.bind(obsolete, "late-run")).toBe(false);
    expect(guard.isCurrent(obsolete)).toBe(false);
  });

  it("uses generation as well as ID for repeated deterministic runs", () => {
    const guard = new RunGenerationGuard();
    const first = guard.begin();
    guard.bind(first, "mock-same-id");
    const second = guard.begin();
    guard.bind(second, "mock-same-id");

    expect(guard.isCurrent(first, "mock-same-id")).toBe(false);
    expect(guard.isCurrent(second, "mock-same-id")).toBe(true);
  });
});

describe("project-scoped history guards", () => {
  it("rejects a stale history response after the active project changes", () => {
    const guard = new ProjectRequestGuard();
    const first = guard.begin("project-one");
    const second = guard.begin("project-two");

    expect(guard.isCurrent(first, "project-one")).toBe(false);
    expect(guard.isCurrent(second, "project-one")).toBe(false);
    expect(guard.isCurrent(second, "project-two")).toBe(true);
  });

  it("requires both run and project identity before restoring a snapshot", () => {
    const snapshot = {
      manifest: {
        id: "run-one",
        project_id: "project-one",
        sample_ids: ["sample-one"],
        solution_ids: ["solution-one"],
        schema_version: "1",
        prompt_version: "1",
        status: "completed",
        total_pairs: 1,
        completed_pairs: 1,
        created_at: "2026-08-30T10:00:00Z",
        execution_mode: "mock",
        reference_only_solution_ids: [],
      },
      tasks: [],
      results: [],
    } satisfies RunSnapshot;
    const history = {
      run_id: "run-one",
      project_id: "project-one",
      status: "completed",
      created_at: "2026-08-30T10:00:00Z",
      updated_at: "2026-08-30T10:01:00Z",
      total_pairs: 1,
      completed_pairs: 1,
      execution_mode: "mock",
      sample_count: 1,
      solution_count: 1,
      reference_only_count: 0,
      download_ready: true,
    } satisfies RunHistoryItem;

    expect(snapshotMatchesHistoryRun(snapshot, history)).toBe(true);
    expect(
      snapshotMatchesHistoryRun(snapshot, {
        ...history,
        project_id: "project-two",
      }),
    ).toBe(false);
    expect(
      snapshotMatchesHistoryRun(snapshot, {
        ...history,
        run_id: "run-two",
      }),
    ).toBe(false);
  });
});

describe("RunLaunchLock", () => {
  const request = {
    sample_ids: ["receipt-1"],
    solution_ids: ["solution-1"],
    schema_version: "1",
    prompt_version: "1",
    execution_mode: "live" as const,
  };

  it("takes a synchronous lock before rapid double activation can POST twice", () => {
    const lock = new RunLaunchLock(() => "idempotency-1");
    const fingerprint = runDraftFingerprint("project-1", request);
    const posts: string[] = [];
    const activate = () => {
      const attempt = lock.begin(fingerprint);
      if (attempt !== null) {
        posts.push(attempt.idempotencyKey);
      }
    };

    activate();
    activate();

    expect(posts).toEqual(["idempotency-1"]);
    expect(lock.pending).toBe(true);
  });

  it("reuses one key after a lost response and after a stale generation", () => {
    const keys = ["stable-key", "other-key", "new-key"];
    const lock = new RunLaunchLock(() => keys.shift()!);
    const generations = new RunGenerationGuard();
    const fingerprint = runDraftFingerprint("project-1", request);
    const first = lock.begin(fingerprint)!;
    lock.finish(fingerprint, "retryable");
    const retry = lock.begin(fingerprint)!;

    expect(retry.idempotencyKey).toBe(first.idempotencyKey);

    const staleGeneration = generations.begin();
    generations.invalidate();
    expect(generations.bind(staleGeneration, "ignored-run")).toBe(false);
    lock.finish(fingerprint, "retryable");
    const otherFingerprint = runDraftFingerprint("project-2", request);
    const other = lock.begin(otherFingerprint)!;
    expect(other.idempotencyKey).toBe("other-key");
    lock.finish(otherFingerprint, "applied");
    const staleRetry = lock.begin(fingerprint)!;
    expect(staleRetry.idempotencyKey).toBe("stable-key");
    lock.finish(fingerprint, "applied");

    expect(lock.begin(fingerprint)?.idempotencyKey).toBe("new-key");
  });
});
