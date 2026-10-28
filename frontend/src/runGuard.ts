import type { RunCreateRequest, RunHistoryItem, RunSnapshot } from "./types";

export class RunGenerationGuard {
  private generation = 0;
  private runId: string | null = null;

  begin(): number {
    this.generation += 1;
    this.runId = null;
    return this.generation;
  }

  bind(generation: number, runId: string): boolean {
    if (generation !== this.generation) {
      return false;
    }
    this.runId = runId;
    return true;
  }

  isCurrent(generation: number, runId?: string): boolean {
    return (
      generation === this.generation &&
      (runId === undefined || this.runId === runId)
    );
  }

  invalidate(): void {
    this.generation += 1;
    this.runId = null;
  }
}

export class ProjectRequestGuard {
  private generation = 0;
  private projectId: string | null = null;

  begin(projectId: string): number {
    this.generation += 1;
    this.projectId = projectId;
    return this.generation;
  }

  isCurrent(generation: number, projectId: string): boolean {
    return generation === this.generation && projectId === this.projectId;
  }

  invalidate(): void {
    this.generation += 1;
    this.projectId = null;
  }
}

export function snapshotMatchesHistoryRun(
  snapshot: RunSnapshot,
  run: RunHistoryItem,
): boolean {
  return (
    snapshot.manifest.id === run.run_id &&
    snapshot.manifest.project_id === run.project_id
  );
}

export type LaunchAttemptOutcome =
  | "applied"
  | "retryable"
  | "definitive_failure";

interface RetainedLaunch {
  fingerprint: string;
  idempotencyKey: string;
}

/**
 * A synchronous gate around the paid create-run request. React state is
 * intentionally not used as the lock because two activations can occur before
 * a render commits.
 */
export class RunLaunchLock {
  private inFlight: RetainedLaunch | null = null;
  private retainedKeys = new Map<string, string>();

  constructor(
    private readonly createKey: () => string = () => crypto.randomUUID(),
  ) {}

  begin(fingerprint: string): RetainedLaunch | null {
    if (this.inFlight !== null) {
      return null;
    }
    const idempotencyKey =
      this.retainedKeys.get(fingerprint) ?? this.createKey();
    this.retainedKeys.set(fingerprint, idempotencyKey);
    const attempt = { fingerprint, idempotencyKey };
    this.inFlight = attempt;
    return attempt;
  }

  finish(fingerprint: string, outcome: LaunchAttemptOutcome): void {
    if (this.inFlight?.fingerprint !== fingerprint) {
      return;
    }
    this.inFlight = null;
    if (outcome === "applied" || outcome === "definitive_failure") {
      this.retainedKeys.delete(fingerprint);
    }
  }

  get pending(): boolean {
    return this.inFlight !== null;
  }
}

export function runDraftFingerprint(
  projectId: string,
  request: RunCreateRequest,
): string {
  return JSON.stringify({
    project_id: projectId,
    sample_ids: request.sample_ids,
    solution_ids: request.solution_ids,
    schema_version: request.schema_version,
    prompt_version: request.prompt_version,
    execution_mode: request.execution_mode,
  });
}
