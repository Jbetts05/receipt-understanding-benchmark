import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  encodeFilenameHeader,
  HttpProductClient,
  MAX_FILENAME_UTF8_BYTES,
  MAX_GOLD_JSON_BYTES,
  type EventSourceLike,
} from "./api";
import { mockProjects } from "./mockData";
import type { RunManifest } from "./types";

function decodeFilenameHeader(value: string): string {
  const base64 = value.replaceAll("-", "+").replaceAll("_", "/");
  const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  const binary = atob(padded);
  return new TextDecoder().decode(
    Uint8Array.from(binary, (character) => character.charCodeAt(0)),
  );
}

class FakeEventSource implements EventSourceLike {
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  readonly listeners = new Map<string, Set<EventListener>>();
  closed = false;

  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: EventListener) {
    this.listeners.get(type)?.delete(listener);
  }

  close() {
    this.closed = true;
  }

  emit(type: string, data: unknown, lastEventId: string) {
    const event = {
      data: JSON.stringify(data),
      lastEventId,
    } as MessageEvent<string>;
    this.listeners.get(type)?.forEach((listener) => listener(event));
  }

  emitRaw(type: string, data: string, lastEventId: string) {
    const event = { data, lastEventId } as MessageEvent<string>;
    this.listeners.get(type)?.forEach((listener) => listener(event));
  }

  fail() {
    this.onerror?.({} as Event);
  }
}

afterEach(() => {
  vi.useRealTimers();
});

describe("filename request metadata", () => {
  it("encodes CJK and emoji filenames as bounded ASCII base64url", () => {
    const filename = "領収書-ランチ🧾.png";
    const encoded = encodeFilenameHeader(filename);

    expect(encoded).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(decodeFilenameHeader(encoded)).toBe(filename);
  });

  it("rejects filename metadata larger than the server limit", () => {
    const oversized = "🧾".repeat(Math.floor(MAX_FILENAME_UTF8_BYTES / 4) + 1);
    expect(() => encodeFilenameHeader(oversized)).toThrow(
      `${MAX_FILENAME_UTF8_BYTES} UTF-8 bytes`,
    );
  });
});

describe("HTTP product contract", () => {
  it("uses typed API routes for server bootstrap and run creation", async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      requests.push({ url, init });
      if (url === "/api/health") {
        return new Response(
          JSON.stringify({
            status: "ok",
            profile: "local",
            execution_mode: "mock",
          }),
          { status: 200 },
        );
      }
      if (url === "/api/projects") {
        return new Response(JSON.stringify(mockProjects), { status: 200 });
      }
      return new Response(
        JSON.stringify({
          run_id: "run-123",
          status: "queued",
          total_pairs: 1,
          event_url: "/api/runs/run-123/events",
          snapshot_url: "/api/runs/run-123",
          results_url: "/api/runs/run-123/results",
          download_url: "/api/runs/run-123/download",
        }),
        { status: 202 },
      );
    }) as typeof fetch;
    const client = new HttpProductClient("", fetcher);

    expect((await client.health()).execution_mode).toBe("mock");
    expect(await client.listProjects()).toHaveLength(1);
    await client.createRun(
      "offline-demo",
      {
        sample_ids: ["offline-001"],
        solution_ids: ["offline-direct"],
        schema_version: "1.0.0",
        prompt_version: "1.0.0",
        execution_mode: "mock",
      },
      "stable-key",
    );

    expect(requests[2]?.url).toBe("/api/projects/offline-demo/runs");
    expect(requests[2]?.init?.headers).toMatchObject({
      "Content-Type": "application/json",
      "Idempotency-Key": "stable-key",
    });
    expect(JSON.parse(String(requests[2]?.init?.body))).toMatchObject({
      sample_ids: ["offline-001"],
      solution_ids: ["offline-direct"],
    });
  });

  it("lists bounded project run history through the private project route", async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ url: String(input), init });
      return new Response(JSON.stringify([]), { status: 200 });
    }) as typeof fetch;
    const client = new HttpProductClient("/base", fetcher);
    const controller = new AbortController();

    await expect(
      client.listProjectRuns("project/一", 8, controller.signal),
    ).resolves.toEqual([]);
    expect(requests).toEqual([
      {
        url: "/base/api/projects/project%2F%E4%B8%80/runs?limit=8",
        init: { signal: controller.signal },
      },
    ]);
    await expect(client.listProjectRuns("project", 0)).rejects.toThrow(
      "between 1 and 100",
    );
    expect(requests).toHaveLength(1);
  });

  it("requests compact snapshots and sample-scoped result batches", async () => {
    const requests: string[] = [];
    const fetcher = (async (input: RequestInfo | URL) => {
      requests.push(String(input));
      return new Response(JSON.stringify([]), { status: 200 });
    }) as typeof fetch;
    const client = new HttpProductClient("", fetcher);

    await client.getRun("run/一", undefined, "sample/二");
    await client.listRunResults("run/一", "sample/二");

    expect(requests).toEqual([
      "/api/runs/run%2F%E4%B8%80?compact=true&result_sample_id=sample%2F%E4%BA%8C",
      "/api/runs/run%2F%E4%B8%80/results?compact=true&sample_id=sample%2F%E4%BA%8C",
    ]);
  });

  it("sends Unicode filenames and gold in multipart body fields, never headers", async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ url: String(input), init });
      return new Response(JSON.stringify({ id: "sample-1" }), { status: 201 });
    }) as typeof fetch;
    const client = new HttpProductClient("", fetcher);
    const file = new File(["image-bytes"], "領収書🧾.png", {
      type: "image/png",
    });

    await client.uploadSample("custom-project", file, { total: 4.2 });

    expect(requests[0]?.url).toBe("/api/projects/custom-project/samples");
    expect(requests[0]?.init?.headers).toBeUndefined();
    const body = requests[0]?.init?.body;
    expect(body).toBeInstanceOf(FormData);
    const form = body as FormData;
    const uploadedImage = form.get("image");
    const uploadedGold = form.get("gold");
    expect(uploadedImage).toBeInstanceOf(File);
    expect((uploadedImage as File).name).toBe(file.name);
    expect((uploadedImage as File).type).toBe("image/png");
    expect(uploadedGold).toBeInstanceOf(Blob);
    expect((uploadedGold as Blob).type).toBe("application/json");
    expect(JSON.parse(await (uploadedGold as Blob).text())).toEqual({ total: 4.2 });
  });

  it("imports a bounded labelled ZIP through the atomic project endpoint", async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ url: String(input), init });
      return new Response(JSON.stringify({ id: "imported-project" }), {
        status: 201,
      });
    }) as typeof fetch;
    const client = new HttpProductClient("", fetcher);
    const file = new File(["zip-bytes"], "receipts.zip", {
      type: "application/zip",
    });

    await client.importLabeledDataset(file);

    expect(requests).toHaveLength(1);
    expect(requests[0]?.url).toBe("/api/projects/import-labeled");
    expect(requests[0]?.init?.method).toBe("POST");
    expect(requests[0]?.init?.headers).toBeUndefined();
    const body = requests[0]?.init?.body;
    expect(body).toBeInstanceOf(FormData);
    const uploadedPackage = (body as FormData).get("package");
    expect(uploadedPackage).toBeInstanceOf(File);
    expect(uploadedPackage).toMatchObject({
      name: "receipts.zip",
      type: "application/zip",
    });
    expect(await (uploadedPackage as File).text()).toBe("zip-bytes");
  });

  it("omits the optional gold multipart field when no answer is supplied", async () => {
    let body: BodyInit | null | undefined;
    const fetcher = (async (_input: RequestInfo | URL, init?: RequestInit) => {
      body = init?.body;
      return new Response(JSON.stringify({ id: "sample-1" }), { status: 201 });
    }) as typeof fetch;
    const client = new HttpProductClient("", fetcher);

    await client.uploadSample(
      "custom-project",
      new File(["image"], "食料品.png", { type: "image/png" }),
      null,
    );

    const form = body as FormData;
    expect((form.get("image") as File).name).toBe("食料品.png");
    expect(form.has("gold")).toBe(false);
  });

  it("rejects gold bodies above 128 KiB before making a request", async () => {
    const fetcher = vi.fn() as unknown as typeof fetch;
    const client = new HttpProductClient("", fetcher);
    const gold = { value: "x".repeat(MAX_GOLD_JSON_BYTES) };

    await expect(
      client.uploadSample(
        "custom-project",
        new File(["image"], "receipt.png", { type: "image/png" }),
        gold,
      ),
    ).rejects.toThrow("128 KiB");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("preserves server validation codes and messages", async () => {
    const fetcher = (async () =>
      new Response(
        JSON.stringify({
          detail: "Image dimensions exceed the limit",
          code: "invalid_image_dimensions",
        }),
        { status: 413 },
      )) as typeof fetch;
    const client = new HttpProductClient("", fetcher);

    await expect(client.listProjects()).rejects.toMatchObject({
      status: 413,
      code: "invalid_image_dimensions",
      message: "Image dimensions exceed the limit",
    } satisfies Partial<ApiError>);
  });

  it("issues DELETE against the fixed run and project routes and tolerates 204", async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ url: String(input), init });
      return new Response(null, { status: 204 });
    }) as typeof fetch;
    const client = new HttpProductClient("/base", fetcher);

    await expect(client.deleteRun("run/一")).resolves.toBeUndefined();
    await expect(client.deleteProject("project/一")).resolves.toBeUndefined();

    expect(requests).toEqual([
      { url: "/base/api/runs/run%2F%E4%B8%80", init: { method: "DELETE" } },
      { url: "/base/api/projects/project%2F%E4%B8%80", init: { method: "DELETE" } },
    ]);
  });

  it("surfaces delete errors instead of resolving optimistically", async () => {
    const fetcher = (async () =>
      new Response(
        JSON.stringify({ detail: "Only a terminal run can be deleted.", code: "run_not_terminal" }),
        { status: 409 },
      )) as typeof fetch;
    const client = new HttpProductClient("", fetcher);

    await expect(client.deleteRun("run-1")).rejects.toMatchObject({
      status: 409,
      code: "run_not_terminal",
      message: "Only a terminal run can be deleted.",
    } satisfies Partial<ApiError>);
  });

  it("centralizes encoded image, result detail, aggregate, and download routes", async () => {
    const urls: string[] = [];
    const fetcher = (async (input: RequestInfo | URL) => {
      urls.push(String(input));
      return new Response(JSON.stringify({}), { status: 200 });
    }) as typeof fetch;
    const client = new HttpProductClient("/base", fetcher);

    await client.getRunResult("run/一", "sample one", "solution+one");
    await client.getProjectAggregate("project/一", "run one");

    expect(urls).toEqual([
      "/base/api/runs/run%2F%E4%B8%80/results/sample%20one/solution%2Bone",
      "/base/api/projects/project%2F%E4%B8%80/runs/run%20one/aggregate",
    ]);
    expect(client.sampleImageUrl("project/一", "sample one")).toBe(
      "/base/api/projects/project%2F%E4%B8%80/samples/sample%20one/image",
    );
    expect(client.runDownloadUrl("run/一")).toBe(
      "/base/api/runs/run%2F%E4%B8%80/download",
    );
  });
});

describe("durable SSE replay", () => {
  it("reconnects with the latest Last-Event-ID cursor", async () => {
    vi.useFakeTimers();
    const urls: string[] = [];
    const sources: FakeEventSource[] = [];
    const client = new HttpProductClient(
      "",
      fetch,
      (url) => {
        urls.push(url);
        const source = new FakeEventSource();
        sources.push(source);
        return source;
      },
    );
    const manifest: RunManifest = {
      id: "run-123",
      project_id: "offline-demo",
      sample_ids: ["offline-001"],
      solution_ids: ["offline-direct"],
      schema_version: "1.0.0",
      prompt_version: "1.0.0",
      status: "running",
      total_pairs: 1,
      completed_pairs: 0,
      created_at: "2026-08-30T00:00:00Z",
      execution_mode: "mock",
      reference_only_solution_ids: [],
    };
    const events: string[] = [];

    const close = client.subscribeToRun(
      manifest.id,
      {
        onEvent: (event, metadata) =>
          events.push(`${event.type}:${metadata.eventId}`),
      },
      { lastEventId: "7", reconnectDelayMs: 10, maxReconnectDelayMs: 10 },
    );
    expect(urls[0]).toBe("/api/runs/run-123/events?lastEventId=7");
    sources[0]?.emit("run_started", { type: "run_started", run: manifest }, "8");
    sources[0]?.fail();
    await vi.advanceTimersByTimeAsync(10);

    expect(events).toEqual(["run_started:8"]);
    expect(urls[1]).toBe("/api/runs/run-123/events?lastEventId=8");
    close();
  });

  it("does not advance the reconnect cursor past an invalid event", async () => {
    vi.useFakeTimers();
    const urls: string[] = [];
    const sources: FakeEventSource[] = [];
    const client = new HttpProductClient(
      "",
      fetch,
      (url) => {
        urls.push(url);
        const source = new FakeEventSource();
        sources.push(source);
        return source;
      },
    );
    const errors: Error[] = [];
    const close = client.subscribeToRun(
      "run-123",
      {
        onEvent: () => undefined,
        onError: (error) => errors.push(error),
      },
      { lastEventId: "7", reconnectDelayMs: 10, maxReconnectDelayMs: 10 },
    );

    sources[0]?.emitRaw("task_completed", "{invalid", "99");
    sources[0]?.fail();
    await vi.advanceTimersByTimeAsync(10);

    expect(errors).toHaveLength(2);
    expect(urls[1]).toBe("/api/runs/run-123/events?lastEventId=7");
    close();
  });
});
