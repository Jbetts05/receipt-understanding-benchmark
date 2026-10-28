export type AggregateRunStatus = "queued" | "running" | "completed" | "failed";

export interface AggregateRefreshInput {
  runKey: string;
  completedPairs: number;
  status: AggregateRunStatus;
  fetch: (signal: AbortSignal) => Promise<void>;
}

const DEFAULT_TERMINAL_RETRY_DELAYS_MS = [500, 1_000, 2_000] as const;

/**
 * Coalesces completion bursts, aborts superseded HTTP work, and retries the
 * final terminal refresh with bounded backoff.
 */
export class AggregateRefreshController {
  private runKey: string | null = null;
  private lastStartedAt = Number.NEGATIVE_INFINITY;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private request: AbortController | null = null;
  private terminalState: "idle" | "pending" | "delivered" | "exhausted" =
    "idle";
  private terminalRetryIndex = 0;
  private disposed = false;

  constructor(
    private readonly intervalMs = 5_000,
    private readonly now: () => number = () => Date.now(),
    private readonly terminalRetryDelaysMs: readonly number[] =
      DEFAULT_TERMINAL_RETRY_DELAYS_MS,
  ) {}

  update(input: AggregateRefreshInput): void {
    if (this.disposed) {
      return;
    }
    if (input.runKey !== this.runKey) {
      this.reset(input.runKey);
    }
    if (this.terminalState !== "idle") {
      return;
    }
    const terminal = input.status === "completed" || input.status === "failed";
    if (terminal) {
      this.terminalState = "pending";
      this.clearTimer();
      this.start(input.fetch, true);
      return;
    }
    if (input.completedPairs <= 0) {
      return;
    }
    if (this.timer !== null) {
      return;
    }
    const delay = Math.max(0, this.intervalMs - (this.now() - this.lastStartedAt));
    if (delay === 0) {
      this.start(input.fetch);
      return;
    }
    this.timer = setTimeout(() => {
      this.timer = null;
      if (!this.disposed && this.terminalState === "idle") {
        this.start(input.fetch);
      }
    }, delay);
  }

  clear(): void {
    this.reset(null);
  }

  dispose(): void {
    this.disposed = true;
    this.reset(null);
  }

  private start(
    fetchAggregate: (signal: AbortSignal) => Promise<void>,
    terminal = false,
  ): void {
    this.lastStartedAt = this.now();
    this.request?.abort();
    const controller = new AbortController();
    this.request = controller;
    void (async () => fetchAggregate(controller.signal))().then(
      () => this.finish(controller, fetchAggregate, terminal, true),
      () => this.finish(controller, fetchAggregate, terminal, false),
    );
  }

  private finish(
    controller: AbortController,
    fetchAggregate: (signal: AbortSignal) => Promise<void>,
    terminal: boolean,
    succeeded: boolean,
  ): void {
    if (this.request !== controller) {
      return;
    }
    this.request = null;
    if (!terminal || controller.signal.aborted || this.disposed) {
      return;
    }
    if (succeeded) {
      this.terminalState = "delivered";
      this.clearTimer();
      return;
    }
    const delay = this.terminalRetryDelaysMs[this.terminalRetryIndex];
    if (delay === undefined) {
      this.terminalState = "exhausted";
      return;
    }
    this.terminalRetryIndex += 1;
    this.timer = setTimeout(() => {
      this.timer = null;
      if (!this.disposed && this.terminalState === "pending") {
        this.start(fetchAggregate, true);
      }
    }, Math.max(0, delay));
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private reset(runKey: string | null): void {
    this.clearTimer();
    this.request?.abort();
    this.request = null;
    this.runKey = runKey;
    this.lastStartedAt = Number.NEGATIVE_INFINITY;
    this.terminalState = "idle";
    this.terminalRetryIndex = 0;
  }
}
