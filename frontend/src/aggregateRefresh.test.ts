import { afterEach, describe, expect, it, vi } from "vitest";

import { AggregateRefreshController } from "./aggregateRefresh";

afterEach(() => {
  vi.useRealTimers();
});

describe("AggregateRefreshController", () => {
  it("bounds 100 rapid completions and fetches exactly once on terminal", async () => {
    vi.useFakeTimers();
    const calls: AbortSignal[] = [];
    const fetchAggregate = (signal: AbortSignal) => {
      calls.push(signal);
      return new Promise<void>(() => undefined);
    };
    const controller = new AggregateRefreshController(1_500, () => Date.now());

    for (let completedPairs = 1; completedPairs <= 100; completedPairs += 1) {
      controller.update({
        runKey: "project:run",
        completedPairs,
        status: "running",
        fetch: fetchAggregate,
      });
    }
    expect(calls).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(1_500);
    expect(calls).toHaveLength(2);
    expect(calls[0]?.aborted).toBe(true);

    controller.update({
      runKey: "project:run",
      completedPairs: 100,
      status: "completed",
      fetch: fetchAggregate,
    });
    controller.update({
      runKey: "project:run",
      completedPairs: 100,
      status: "completed",
      fetch: fetchAggregate,
    });

    expect(calls).toHaveLength(3);
    expect(calls[1]?.aborted).toBe(true);
    expect(calls[2]?.aborted).toBe(false);
    controller.dispose();
  });

  it("retries terminal delivery with backoff and stops after success", async () => {
    vi.useFakeTimers();
    const fetchAggregate = vi
      .fn<(signal: AbortSignal) => Promise<void>>()
      .mockRejectedValueOnce(new Error("temporary aggregate failure"))
      .mockResolvedValue(undefined);
    const controller = new AggregateRefreshController(
      1_500,
      () => Date.now(),
      [100, 250],
    );

    controller.update({
      runKey: "project:run",
      completedPairs: 10,
      status: "completed",
      fetch: fetchAggregate,
    });
    await vi.advanceTimersByTimeAsync(99);
    expect(fetchAggregate).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(1);
    expect(fetchAggregate).toHaveBeenCalledTimes(2);

    controller.update({
      runKey: "project:run",
      completedPairs: 10,
      status: "completed",
      fetch: fetchAggregate,
    });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(fetchAggregate).toHaveBeenCalledTimes(2);
    controller.dispose();
  });

  it("bounds terminal retries and cancels stale-run work", async () => {
    vi.useFakeTimers();
    const failedFetch = vi.fn(async () => {
      throw new Error("aggregate unavailable");
    });
    const nextRunFetch = vi.fn(async () => undefined);
    const controller = new AggregateRefreshController(
      1_500,
      () => Date.now(),
      [100, 200],
    );

    controller.update({
      runKey: "project:old-run",
      completedPairs: 10,
      status: "failed",
      fetch: failedFetch,
    });
    await vi.advanceTimersByTimeAsync(100);
    expect(failedFetch).toHaveBeenCalledTimes(2);

    controller.update({
      runKey: "project:new-run",
      completedPairs: 1,
      status: "completed",
      fetch: nextRunFetch,
    });
    await vi.advanceTimersByTimeAsync(10_000);

    expect(failedFetch).toHaveBeenCalledTimes(2);
    expect(nextRunFetch).toHaveBeenCalledTimes(1);
    controller.dispose();
  });

  it("stops after the configured terminal retry budget", async () => {
    vi.useFakeTimers();
    const fetchAggregate = vi.fn(async () => {
      throw new Error("aggregate unavailable");
    });
    const controller = new AggregateRefreshController(
      1_500,
      () => Date.now(),
      [100, 200],
    );

    controller.update({
      runKey: "project:run",
      completedPairs: 10,
      status: "failed",
      fetch: fetchAggregate,
    });
    await vi.advanceTimersByTimeAsync(100);
    expect(fetchAggregate).toHaveBeenCalledTimes(2);

    await vi.advanceTimersByTimeAsync(200);
    expect(fetchAggregate).toHaveBeenCalledTimes(3);

    controller.update({
      runKey: "project:run",
      completedPairs: 10,
      status: "failed",
      fetch: fetchAggregate,
    });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(fetchAggregate).toHaveBeenCalledTimes(3);
    controller.dispose();
  });
});
