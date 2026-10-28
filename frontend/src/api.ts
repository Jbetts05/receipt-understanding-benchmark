import type {
  CustomProjectCreatePayload,
  HealthStatus,
  JsonObject,
  Project,
  ProjectAggregate,
  ReceiptSample,
  RunAccepted,
  RunCreateRequest,
  RunEvent,
  RunHistoryItem,
  RunResult,
  RunSnapshot,
  Solution,
} from "./types";

export interface ApiErrorPayload {
  detail?: unknown;
  code?: unknown;
  issues?: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly issues: string[];

  constructor(
    status: number,
    message: string,
    options: { code?: string | null; issues?: string[] } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = options.code ?? null;
    this.issues = options.issues ?? [];
  }
}

export const MAX_FILENAME_UTF8_BYTES = 512;
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
export const MAX_UPLOAD_FILES = 100;
export const MAX_GOLD_JSON_BYTES = 128 * 1024;
export const MAX_DATASET_PACKAGE_BYTES = 100 * 1024 * 1024;
export const SUPPORTED_IMAGE_TYPES = new Set(["image/jpeg", "image/png"]);
export const SAMPLE_UPLOAD_FIELDS = {
  image: "image",
  gold: "gold",
} as const;

function encodeUtf8Base64Url(value: string): string {
  const bytes = new TextEncoder().encode(value);
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  let encoded = "";
  for (let index = 0; index < bytes.length; index += 3) {
    const first = bytes[index] ?? 0;
    const second = bytes[index + 1];
    const third = bytes[index + 2];
    encoded += alphabet.charAt(first >> 2);
    encoded += alphabet.charAt(((first & 0x03) << 4) | ((second ?? 0) >> 4));
    if (second !== undefined) {
      encoded += alphabet.charAt(((second & 0x0f) << 2) | ((third ?? 0) >> 6));
    }
    if (third !== undefined) {
      encoded += alphabet.charAt(third & 0x3f);
    }
  }
  return encoded;
}

export function encodeFilenameHeader(filename: string): string {
  const byteLength = new TextEncoder().encode(filename).length;
  if (byteLength === 0 || byteLength > MAX_FILENAME_UTF8_BYTES) {
    throw new Error(
      `Filename metadata must be between 1 and ${MAX_FILENAME_UTF8_BYTES} UTF-8 bytes`,
    );
  }
  return encodeUtf8Base64Url(filename);
}

export function encodePathSegment(value: string): string {
  return encodeURIComponent(value);
}

export const apiEndpoints = {
  health: "/api/health",
  projects: "/api/projects",
  importLabeledDataset: "/api/projects/import-labeled",
  project: (projectId: string) =>
    `/api/projects/${encodePathSegment(projectId)}`,
  samples: (projectId: string) =>
    `/api/projects/${encodePathSegment(projectId)}/samples`,
  sampleImage: (projectId: string, sampleId: string) =>
    `/api/projects/${encodePathSegment(projectId)}/samples/${encodePathSegment(
      sampleId,
    )}/image`,
  solutions: (projectId: string) =>
    `/api/projects/${encodePathSegment(projectId)}/solutions`,
  runs: (projectId: string) =>
    `/api/projects/${encodePathSegment(projectId)}/runs`,
  projectRuns: (projectId: string, limit: number) =>
    `/api/projects/${encodePathSegment(projectId)}/runs?limit=${limit}`,
  run: (runId: string) => `/api/runs/${encodePathSegment(runId)}`,
  compactRun: (runId: string, resultSampleId?: string) =>
    `/api/runs/${encodePathSegment(runId)}?compact=true${
      resultSampleId === undefined
        ? ""
        : `&result_sample_id=${encodePathSegment(resultSampleId)}`
    }`,
  runEvents: (runId: string) =>
    `/api/runs/${encodePathSegment(runId)}/events`,
  runResults: (runId: string, sampleId?: string) =>
    `/api/runs/${encodePathSegment(runId)}/results?compact=true${
      sampleId === undefined ? "" : `&sample_id=${encodePathSegment(sampleId)}`
    }`,
  runResult: (runId: string, sampleId: string, solutionId: string) =>
    `/api/runs/${encodePathSegment(runId)}/results/${encodePathSegment(
      sampleId,
    )}/${encodePathSegment(solutionId)}`,
  aggregate: (projectId: string, runId: string) =>
    `/api/projects/${encodePathSegment(projectId)}/runs/${encodePathSegment(
      runId,
    )}/aggregate`,
  runDownload: (runId: string) =>
    `/api/runs/${encodePathSegment(runId)}/download`,
} as const;

function issueMessages(payload: ApiErrorPayload): string[] {
  if (!Array.isArray(payload.issues)) {
    return [];
  }
  return payload.issues.flatMap((issue) => {
    if (typeof issue === "string") {
      return [issue];
    }
    if (
      typeof issue === "object" &&
      issue !== null &&
      "message" in issue &&
      typeof issue.message === "string"
    ) {
      return [issue.message];
    }
    return [];
  });
}

async function throwForFailedResponse(response: Response): Promise<never> {
  let detail = `Request failed with status ${response.status}`;
  let code: string | null = null;
  let issues: string[] = [];
  try {
    const payload = (await response.json()) as ApiErrorPayload;
    if (typeof payload.detail === "string") {
      detail = payload.detail;
    } else if (Array.isArray(payload.detail)) {
      const messages = payload.detail.flatMap((entry) =>
        typeof entry === "object" &&
        entry !== null &&
        "msg" in entry &&
        typeof entry.msg === "string"
          ? [entry.msg]
          : [],
      );
      if (messages.length > 0) {
        detail = messages.join(" · ");
      }
    }
    code = typeof payload.code === "string" ? payload.code : null;
    issues = issueMessages(payload);
  } catch {
    // The HTTP status remains useful for non-JSON responses.
  }
  throw new ApiError(response.status, detail, { code, issues });
}

export async function requestJson<T>(
  fetcher: typeof fetch,
  url: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetcher(url, init);
  if (!response.ok) {
    await throwForFailedResponse(response);
  }
  return (await response.json()) as T;
}

/**
 * Issues a request that is expected to succeed with an empty body (204 No
 * Content), such as a DELETE. Falls through to the shared error parser for
 * any non-2xx response.
 */
export async function requestNoContent(
  fetcher: typeof fetch,
  url: string,
  init?: RequestInit,
): Promise<void> {
  const response = await fetcher(url, init);
  if (!response.ok) {
    await throwForFailedResponse(response);
  }
}

export function parseRunEvent(value: string, eventType?: string): RunEvent {
  const parsed = JSON.parse(value) as RunEvent | Omit<RunEvent, "type">;
  if (
    typeof parsed === "object" &&
    parsed !== null &&
    "type" in parsed &&
    typeof parsed.type === "string"
  ) {
    return parsed as RunEvent;
  }
  if (eventType === undefined) {
    throw new Error("Run event is missing a type");
  }
  return { ...parsed, type: eventType } as RunEvent;
}

export interface RunEventMetadata {
  eventId: string | null;
  reconnectCount: number;
}

export interface RunSubscriptionHandlers {
  onEvent: (event: RunEvent, metadata: RunEventMetadata) => void;
  onConnectionChange?: (
    state: "connecting" | "open" | "reconnecting" | "closed",
  ) => void;
  onError?: (error: Error) => void;
}

export interface RunSubscriptionOptions {
  lastEventId?: string | null;
  reconnectDelayMs?: number;
  maxReconnectDelayMs?: number;
}

export interface EventSourceLike {
  onopen: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  addEventListener(type: string, listener: EventListener): void;
  removeEventListener(type: string, listener: EventListener): void;
  close(): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;

export interface ProductClient {
  health(): Promise<HealthStatus>;
  listProjects(): Promise<Project[]>;
  getProject(projectId: string): Promise<Project>;
  listSamples(projectId: string): Promise<ReceiptSample[]>;
  listSolutions(projectId: string): Promise<Solution[]>;
  createProject(payload: CustomProjectCreatePayload): Promise<Project>;
  importLabeledDataset(file: File): Promise<Project>;
  /**
   * POSTs multipart/form-data with an `image` File and, when supplied, a
   * `gold` application/json Blob. The browser owns the multipart boundary.
   */
  uploadSample(
    projectId: string,
    file: File,
    gold: JsonObject | null,
  ): Promise<ReceiptSample>;
  createRun(
    projectId: string,
    request: RunCreateRequest,
    idempotencyKey?: string,
  ): Promise<RunAccepted>;
  listProjectRuns(
    projectId: string,
    limit?: number,
    signal?: AbortSignal,
  ): Promise<RunHistoryItem[]>;
  getRun(
    runId: string,
    signal?: AbortSignal,
    resultSampleId?: string,
  ): Promise<RunSnapshot>;
  listRunResults(
    runId: string,
    sampleId?: string,
    signal?: AbortSignal,
  ): Promise<RunResult[]>;
  getRunResult(
    runId: string,
    sampleId: string,
    solutionId: string,
  ): Promise<RunResult>;
  getProjectAggregate(
    projectId: string,
    runId: string,
    signal?: AbortSignal,
  ): Promise<ProjectAggregate>;
  /**
   * Permanently deletes a run and its persisted artifacts. The server
   * returns 204 No Content. Only terminal (completed/failed) runs should be
   * offered for deletion by the UI.
   */
  deleteRun(runId: string): Promise<void>;
  /**
   * Permanently deletes a custom project along with every receipt, run, and
   * artifact stored under it. The server returns 204 No Content. Built-in
   * projects have no delete affordance in the UI.
   */
  deleteProject(projectId: string): Promise<void>;
  sampleImageUrl(projectId: string, sampleId: string): string;
  runDownloadUrl(runId: string): string;
  subscribeToRun(
    runId: string,
    handlers: RunSubscriptionHandlers,
    options?: RunSubscriptionOptions,
  ): () => void;
}

const RUN_EVENT_TYPES: RunEvent["type"][] = [
  "run_started",
  "task_started",
  "task_progress",
  "task_completed",
  "run_completed",
  "run_failed",
];

export class HttpProductClient implements ProductClient {
  private readonly eventSourceFactory: EventSourceFactory;

  constructor(
    private readonly baseUrl = "",
    private readonly fetcher: typeof fetch = fetch,
    eventSourceFactory?: EventSourceFactory,
  ) {
    this.eventSourceFactory =
      eventSourceFactory ?? ((url) => new EventSource(url) as EventSourceLike);
  }

  private url(path: string): string {
    return `${this.baseUrl}${path}`;
  }

  health(): Promise<HealthStatus> {
    return requestJson<HealthStatus>(
      this.fetcher,
      this.url(apiEndpoints.health),
    );
  }

  listProjects(): Promise<Project[]> {
    return requestJson<Project[]>(
      this.fetcher,
      this.url(apiEndpoints.projects),
    );
  }

  getProject(projectId: string): Promise<Project> {
    return requestJson<Project>(
      this.fetcher,
      this.url(apiEndpoints.project(projectId)),
    );
  }

  listSamples(projectId: string): Promise<ReceiptSample[]> {
    return requestJson<ReceiptSample[]>(
      this.fetcher,
      this.url(apiEndpoints.samples(projectId)),
    );
  }

  listSolutions(projectId: string): Promise<Solution[]> {
    return requestJson<Solution[]>(
      this.fetcher,
      this.url(apiEndpoints.solutions(projectId)),
    );
  }

  createProject(payload: CustomProjectCreatePayload): Promise<Project> {
    return requestJson<Project>(
      this.fetcher,
      this.url(apiEndpoints.projects),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    );
  }

  importLabeledDataset(file: File): Promise<Project> {
    if (
      !file.name.toLocaleLowerCase("en-US").endsWith(".zip") ||
      file.size <= 0 ||
      file.size > MAX_DATASET_PACKAGE_BYTES
    ) {
      return Promise.reject(
        new Error(
          `Dataset package must be a non-empty ZIP no larger than ${
            MAX_DATASET_PACKAGE_BYTES / 1024 / 1024
          } MB`,
        ),
      );
    }
    const form = new FormData();
    form.append("package", file, file.name);
    return requestJson<Project>(
      this.fetcher,
      this.url(apiEndpoints.importLabeledDataset),
      { method: "POST", body: form },
    );
  }

  async uploadSample(
    projectId: string,
    file: File,
    gold: JsonObject | null,
  ): Promise<ReceiptSample> {
    encodeFilenameHeader(file.name);
    const form = new FormData();
    form.append(SAMPLE_UPLOAD_FIELDS.image, file, file.name);
    if (gold !== null) {
      const serialized = JSON.stringify(gold);
      if (new TextEncoder().encode(serialized).length > MAX_GOLD_JSON_BYTES) {
        throw new Error(
          `Gold JSON must be no larger than ${MAX_GOLD_JSON_BYTES / 1024} KiB`,
        );
      }
      form.append(
        SAMPLE_UPLOAD_FIELDS.gold,
        new Blob([serialized], { type: "application/json" }),
        "gold.json",
      );
    }
    return requestJson<ReceiptSample>(
      this.fetcher,
      this.url(apiEndpoints.samples(projectId)),
      { method: "POST", body: form },
    );
  }

  createRun(
    projectId: string,
    request: RunCreateRequest,
    idempotencyKey?: string,
  ): Promise<RunAccepted> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (idempotencyKey !== undefined) {
      headers["Idempotency-Key"] = idempotencyKey;
    }
    return requestJson<RunAccepted>(
      this.fetcher,
      this.url(apiEndpoints.runs(projectId)),
      {
        method: "POST",
        headers,
        body: JSON.stringify(request),
      },
    );
  }

  listProjectRuns(
    projectId: string,
    limit = 100,
    signal?: AbortSignal,
  ): Promise<RunHistoryItem[]> {
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
      return Promise.reject(new RangeError("Run history limit must be between 1 and 100"));
    }
    return requestJson<RunHistoryItem[]>(
      this.fetcher,
      this.url(apiEndpoints.projectRuns(projectId, limit)),
      { signal },
    );
  }

  getRun(
    runId: string,
    signal?: AbortSignal,
    resultSampleId?: string,
  ): Promise<RunSnapshot> {
    return requestJson<RunSnapshot>(
      this.fetcher,
      this.url(apiEndpoints.compactRun(runId, resultSampleId)),
      { signal },
    );
  }

  listRunResults(
    runId: string,
    sampleId?: string,
    signal?: AbortSignal,
  ): Promise<RunResult[]> {
    return requestJson<RunResult[]>(
      this.fetcher,
      this.url(apiEndpoints.runResults(runId, sampleId)),
      { signal },
    );
  }

  getRunResult(
    runId: string,
    sampleId: string,
    solutionId: string,
  ): Promise<RunResult> {
    return requestJson<RunResult>(
      this.fetcher,
      this.url(apiEndpoints.runResult(runId, sampleId, solutionId)),
    );
  }

  getProjectAggregate(
    projectId: string,
    runId: string,
    signal?: AbortSignal,
  ): Promise<ProjectAggregate> {
    return requestJson<ProjectAggregate>(
      this.fetcher,
      this.url(apiEndpoints.aggregate(projectId, runId)),
      { signal },
    );
  }

  deleteRun(runId: string): Promise<void> {
    return requestNoContent(this.fetcher, this.url(apiEndpoints.run(runId)), {
      method: "DELETE",
    });
  }

  deleteProject(projectId: string): Promise<void> {
    return requestNoContent(
      this.fetcher,
      this.url(apiEndpoints.project(projectId)),
      { method: "DELETE" },
    );
  }

  sampleImageUrl(projectId: string, sampleId: string): string {
    return this.url(apiEndpoints.sampleImage(projectId, sampleId));
  }

  runDownloadUrl(runId: string): string {
    return this.url(apiEndpoints.runDownload(runId));
  }

  subscribeToRun(
    runId: string,
    handlers: RunSubscriptionHandlers,
    options: RunSubscriptionOptions = {},
  ): () => void {
    let closed = false;
    let source: EventSourceLike | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let reconnectCount = 0;
    let lastEventId = options.lastEventId?.trim() || null;
    const baseDelay = Math.max(0, options.reconnectDelayMs ?? 750);
    const maxDelay = Math.max(baseDelay, options.maxReconnectDelayMs ?? 8_000);

    const disconnect = () => {
      if (source === null) {
        return;
      }
      source.close();
      source = null;
    };

    const connect = () => {
      if (closed) {
        return;
      }
      handlers.onConnectionChange?.(
        reconnectCount === 0 ? "connecting" : "reconnecting",
      );
      const eventPath = apiEndpoints.runEvents(runId);
      const cursor =
        lastEventId === null
          ? ""
          : `${eventPath.includes("?") ? "&" : "?"}lastEventId=${encodeURIComponent(
              lastEventId,
            )}`;
      source = this.eventSourceFactory(this.url(`${eventPath}${cursor}`));
      const current = source;
      const listeners = RUN_EVENT_TYPES.map((type) => {
        const listener: EventListener = (event) => {
          if (closed || source !== current) {
            return;
          }
          try {
            const message = event as MessageEvent<string>;
            const messageEventId =
              typeof message.lastEventId === "string" &&
              message.lastEventId.trim() !== ""
                ? message.lastEventId.trim()
                : lastEventId;
            const parsed = parseRunEvent(message.data, type);
            handlers.onEvent(parsed, {
              eventId: messageEventId,
              reconnectCount,
            });
            lastEventId = messageEventId;
            if (type === "run_completed" || type === "run_failed") {
              closed = true;
              disconnect();
              handlers.onConnectionChange?.("closed");
            }
          } catch (error) {
            handlers.onError?.(
              error instanceof Error ? error : new Error("Invalid run event"),
            );
          }
        };
        current.addEventListener(type, listener);
        return { type, listener };
      });
      current.onopen = () => handlers.onConnectionChange?.("open");
      current.onerror = () => {
        if (closed || source !== current) {
          return;
        }
        listeners.forEach(({ type, listener }) =>
          current.removeEventListener(type, listener),
        );
        disconnect();
        reconnectCount += 1;
        handlers.onConnectionChange?.("reconnecting");
        handlers.onError?.(new Error("Run event stream disconnected; reconnecting."));
        const delay = Math.min(maxDelay, baseDelay * 2 ** Math.min(reconnectCount - 1, 4));
        reconnectTimer = setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      closed = true;
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
      }
      disconnect();
      handlers.onConnectionChange?.("closed");
    };
  }
}
