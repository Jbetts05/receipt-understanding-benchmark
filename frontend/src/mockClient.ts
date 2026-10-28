import type {
  ProductClient,
  RunSubscriptionHandlers,
  RunSubscriptionOptions,
} from "./api";
import { ApiError } from "./api";
import { buildProjectAggregate, getCompatibility, isComparableQuality, requiresCustomAnalyzerRegistration, resultPairKey } from "./domain";
import {
  makeMockRunResult,
  mockProjects,
  mockSamples,
  mockSolutions,
} from "./mockData";
import type {
  CustomProjectCreatePayload,
  JsonObject,
  Project,
  ProjectAggregate,
  ReceiptSample,
  RunAccepted,
  RunCreateRequest,
  RunEvent,
  RunHistoryItem,
  RunManifest,
  RunResult,
  RunSnapshot,
  RunTask,
  Solution,
} from "./types";

function clone<T>(value: T): T {
  return structuredClone(value);
}

function slug(value: string): string {
  return (
    value
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLocaleLowerCase("en-US")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 48) || "custom-project"
  );
}

/**
 * Mirrors the backend's per-project compatibility declarations for a
 * newly-created custom project. Schema-capable paths are fully comparable;
 * standalone OCR without transcripts and fixed-contract native prebuilt
 * services remain runnable reference evidence.
 */
function customCompatibility(solutions: Solution[], projectId: string) {
  return solutions.map((solution) => {
    if (
      solution.kind === "ocr_text" ||
      (solution.kind === "native" &&
        !requiresCustomAnalyzerRegistration(solution))
    ) {
      return {
        solution_id: solution.id,
        compatibility: "partial_reference",
        comparable_quality: false,
        reason:
          solution.kind === "ocr_text"
            ? "No OCR transcript was imported; output, latency, and cost remain available."
            : "Native prebuilt output is retained without forcing it into an arbitrary schema.",
        deterministic_mapping_id: null,
        projectId,
      } as const;
    }
    const reason =
      requiresCustomAnalyzerRegistration(solution)
          ? "A schema-hash-keyed analyzer is configured without training."
          : "Output is constrained by the custom schema.";
    return {
      solution_id: solution.id,
      compatibility: "full",
      comparable_quality: true,
      reason,
      deterministic_mapping_id: null,
      projectId,
    } as const;
  });
}

function taskFor(result: RunResult): RunTask {
  return {
    pair_id: resultPairKey(result.sample_id, result.solution_id),
    sample_id: result.sample_id,
    solution_id: result.solution_id,
    solution_order: result.solution_order,
    status: result.status,
    stage: result.status === "succeeded" ? "completed" : "failed",
    progress: 1,
    retries: Math.max(0, result.telemetry.attempts - 1),
    elapsed_ms: result.telemetry.latency_ms,
    estimated_cost_usd: result.telemetry.estimated_cost_usd,
    partial_output: result.parsed_output,
    error: result.error?.message ?? null,
    compatibility: result.compatibility,
    comparable_quality: result.comparable_quality,
  };
}

export class MockProductClient implements ProductClient {
  private projects = clone(mockProjects);
  private samples = clone(mockSamples);
  private solutions = clone(mockSolutions);
  private runs = new Map<string, RunSnapshot>();
  private sequence = 0;

  async health() {
    return {
      status: "ok" as const,
      profile: "local" as const,
      execution_mode: "mock" as const,
    };
  }

  async listProjects(): Promise<Project[]> {
    return clone(this.projects);
  }

  async getProject(projectId: string): Promise<Project> {
    const project = this.projects.find((item) => item.id === projectId);
    if (project === undefined) {
      throw new ApiError(404, "Project not found", { code: "project_not_found" });
    }
    return clone(project);
  }

  async listSamples(projectId: string): Promise<ReceiptSample[]> {
    await this.getProject(projectId);
    return clone(this.samples.filter((sample) => sample.project_id === projectId));
  }

  async listSolutions(projectId: string): Promise<Solution[]> {
    await this.getProject(projectId);
    return clone(this.solutions);
  }

  async createProject(payload: CustomProjectCreatePayload): Promise<Project> {
    let id = slug(payload.name);
    let suffix = 2;
    while (this.projects.some((project) => project.id === id)) {
      id = `${slug(payload.name)}-${suffix}`;
      suffix += 1;
    }
    const declarations = customCompatibility(this.solutions, id);
    const unknown = payload.selected_solution_ids.filter(
      (solutionId) =>
        !declarations.some((item) => item.solution_id === solutionId),
    );
    if (unknown.length > 0) {
      throw new ApiError(422, `Unknown solution IDs: ${unknown.join(", ")}`, {
        code: "unknown_solution",
      });
    }
    const project: Project = {
      id,
      name: payload.name,
      short_name: payload.name.slice(0, 10).toLocaleUpperCase("en-US"),
      kind: "custom",
      description: payload.description || "Owner-managed custom receipt extraction project.",
      sample_count: 0,
      scored_sample_count: 0,
      prompt_example_count: 0,
      gold_sample_count: 0,
      schema: {
        id: `${id}-schema`,
        version: "1.0.0",
        hash: "sha256:offline-custom-schema",
        json_schema: payload.schema,
        origin: "user_supplied",
        derivation: null,
      },
      prompt: {
        id: `${id}-prompt`,
        version: "1.0.0",
        hash: "sha256:offline-custom-prompt",
        instructions: payload.prompt,
        example_ids: [],
      },
      // Both prompt contracts are generated from the single prompt the owner
      // authored at project creation, one per solution mode, so the contract
      // inspector never has to special-case custom projects.
      prompt_contracts: [
        {
          id: `${id}-prompt-direct`,
          version: "1.0.0",
          hash: "sha256:offline-custom-prompt-direct",
          mode: "direct-multimodal",
          instructions: payload.prompt,
          example_ids: [],
          user_template: payload.prompt,
          normalizer_model: null,
        },
        {
          id: `${id}-prompt-ocr-normalizer`,
          version: "1.0.0",
          hash: "sha256:offline-custom-prompt-ocr-normalizer",
          mode: "ocr-normalizer",
          instructions: `${payload.prompt}\n\nApply this to the raw OCR transcription of the receipt.`,
          example_ids: [],
          user_template: payload.prompt,
          normalizer_model: "gpt-5.6-luna",
        },
      ],
      rights: {
        state: "owner_managed",
        label: "Owner managed",
        detail: "The project owner manages image rights and retention.",
      },
      leakage: {
        state: "user_supplied",
        label: "User-supplied data",
        detail: "No benchmark leakage assertion is made.",
      },
      attribution: {
        label: "Private project",
        citation: "Uploaded by project owner",
        license: "Not specified",
        url: null,
      },
      created_at: new Date().toISOString(),
      available: true,
      availability_reason: null,
      normalization_profile: "custom",
      selected_solution_ids: [...payload.selected_solution_ids],
      compatibility: declarations.map(({ projectId: _, ...item }) => item),
      dataset_state: "draft",
      dataset_revision_hash: null,
      import_mode: "manual",
      has_ocr_references: false,
    };
    this.projects.push(project);
    this.solutions = this.solutions.map((solution) => {
      const declaration = declarations.find((item) => item.solution_id === solution.id)!;
      return {
        ...solution,
        compatibility: {
          ...solution.compatibility,
          [id]: declaration.compatibility,
        },
        comparable_quality: {
          ...solution.comparable_quality,
          [id]: declaration.comparable_quality,
        },
      };
    });
    return clone(project);
  }

  async importLabeledDataset(_file: File): Promise<Project> {
    throw new ApiError(
      501,
      "Labelled ZIP import requires the server-backed product client.",
      { code: "server_required" },
    );
  }

  async uploadSample(
    projectId: string,
    file: File,
    gold: JsonObject | null,
  ): Promise<ReceiptSample> {
    const project = await this.getProject(projectId);
    const sampleId = `offline-upload-${String(
      this.samples.filter((item) => item.project_id === projectId).length + 1,
    ).padStart(3, "0")}`;
    const sample: ReceiptSample = {
      id: sampleId,
      project_id: projectId,
      title: file.name.replace(/\.[^.]+$/, ""),
      filename: file.name,
      source_index: null,
      prompt_example: false,
      gold_availability: gold === null ? "none" : "available",
      prior_run_state: "never",
      gold,
      visual: {
        merchant: file.name.replace(/\.[^.]+$/, "").toLocaleUpperCase("en-US"),
        address: ["OFFLINE UPLOAD"],
        date: "",
        receipt_number: sampleId,
        currency: "",
        lines: [],
        subtotal: "",
        tax: "",
        total: "",
      },
      image_url:
        typeof URL.createObjectURL === "function" ? URL.createObjectURL(file) : null,
      scoring_role: "scored",
      image_available: true,
    };
    this.samples.push(sample);
    this.projects = this.projects.map((item) =>
      item.id === projectId
        ? {
            ...item,
            sample_count: item.sample_count + 1,
            scored_sample_count: item.scored_sample_count + (gold === null ? 0 : 1),
            gold_sample_count: item.gold_sample_count + (gold === null ? 0 : 1),
          }
        : item,
    );
    return clone(sample);
  }

  async createRun(
    projectId: string,
    request: RunCreateRequest,
  ): Promise<RunAccepted> {
    const project = await this.getProject(projectId);
    const projectSamples = this.samples.filter(
      (sample) =>
        sample.project_id === projectId && request.sample_ids.includes(sample.id),
    );
    const selectedSolutions = this.solutions
      .filter((solution) => request.solution_ids.includes(solution.id))
      .sort((left, right) => left.order - right.order);
    if (
      projectSamples.length !== request.sample_ids.length ||
      selectedSolutions.length !== request.solution_ids.length
    ) {
      throw new ApiError(422, "Run selection contains an unknown item", {
        code: "invalid_selection",
      });
    }
    if (
      selectedSolutions.some(
        (solution) => getCompatibility(project, solution) === "unsupported",
      )
    ) {
      throw new ApiError(422, "Run selection contains an unsupported solution", {
        code: "unsupported_solution",
      });
    }
    const runId = `offline-run-${Date.now().toString(36)}-${++this.sequence}`;
    const results = projectSamples.flatMap((sample) =>
      selectedSolutions.map((solution) =>
        makeMockRunResult(project, sample, solution, runId),
      ),
    );
    const manifest: RunManifest = {
      id: runId,
      project_id: projectId,
      sample_ids: request.sample_ids,
      solution_ids: selectedSolutions.map((solution) => solution.id),
      schema_version: request.schema_version,
      prompt_version: request.prompt_version,
      status: "completed",
      total_pairs: results.length,
      completed_pairs: results.length,
      created_at: new Date().toISOString(),
      execution_mode: "mock",
      reference_only_solution_ids: selectedSolutions
        .filter((solution) => !isComparableQuality(project, solution))
        .map((solution) => solution.id),
    };
    this.runs.set(runId, {
      manifest,
      tasks: results.map(taskFor),
      results,
      event_cursor: "1",
    });
    return {
      run_id: runId,
      status: "queued",
      total_pairs: results.length,
      event_url: `/api/runs/${runId}/events`,
      snapshot_url: `/api/runs/${runId}`,
      results_url: `/api/runs/${runId}/results`,
      download_url: `/api/runs/${runId}/download`,
    };
  }

  async listProjectRuns(
    projectId: string,
    limit = 100,
  ): Promise<RunHistoryItem[]> {
    await this.getProject(projectId);
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
      throw new RangeError("Run history limit must be between 1 and 100");
    }
    return [...this.runs.values()]
      .filter((snapshot) => snapshot.manifest.project_id === projectId)
      .map(({ manifest }) => ({
        run_id: manifest.id,
        project_id: manifest.project_id,
        status: manifest.status,
        created_at: manifest.created_at,
        updated_at: manifest.created_at,
        total_pairs: manifest.total_pairs,
        completed_pairs: manifest.completed_pairs,
        execution_mode: manifest.execution_mode,
        sample_count: manifest.sample_ids.length,
        solution_count: manifest.solution_ids.length,
        reference_only_count: manifest.reference_only_solution_ids.length,
        download_ready: manifest.status === "completed",
      }))
      .sort(
        (left, right) =>
          right.created_at.localeCompare(left.created_at) ||
          right.run_id.localeCompare(left.run_id),
      )
      .slice(0, limit)
      .map(clone);
  }

  async getRun(
    runId: string,
    _signal?: AbortSignal,
    resultSampleId?: string,
  ): Promise<RunSnapshot> {
    const snapshot = this.runs.get(runId);
    if (snapshot === undefined) {
      throw new ApiError(404, "Run not found", { code: "run_not_found" });
    }
    const copy = clone(snapshot);
    const selectedSampleId = resultSampleId ?? copy.manifest.sample_ids[0];
    copy.results = copy.results.filter(
      (result) => result.sample_id === selectedSampleId,
    );
    return copy;
  }

  async listRunResults(
    runId: string,
    sampleId?: string,
    _signal?: AbortSignal,
  ): Promise<RunResult[]> {
    const snapshot = this.runs.get(runId);
    if (snapshot === undefined) {
      throw new ApiError(404, "Run not found", { code: "run_not_found" });
    }
    return clone(
      snapshot.results.filter(
        (result) => sampleId === undefined || result.sample_id === sampleId,
      ),
    );
  }

  async getRunResult(
    runId: string,
    sampleId: string,
    solutionId: string,
  ): Promise<RunResult> {
    const result = (await this.listRunResults(runId)).find(
      (item) =>
        item.sample_id === sampleId && item.solution_id === solutionId,
    );
    if (result === undefined) {
      throw new ApiError(404, "Result not found", { code: "result_not_found" });
    }
    return result;
  }

  async getProjectAggregate(
    projectId: string,
    runId: string,
  ): Promise<ProjectAggregate> {
    const project = await this.getProject(projectId);
    const snapshot = this.runs.get(runId);
    if (snapshot === undefined) {
      throw new ApiError(404, "Run not found", { code: "run_not_found" });
    }
    if (snapshot.manifest.project_id !== projectId) {
      throw new ApiError(404, "Run not found for project", {
        code: "run_not_found",
      });
    }
    return buildProjectAggregate(
      project,
      runId,
      this.samples,
      this.solutions.filter((solution) =>
        snapshot.manifest.solution_ids.includes(solution.id),
      ),
      snapshot.results,
    );
  }

  async deleteRun(runId: string): Promise<void> {
    const snapshot = this.runs.get(runId);
    if (snapshot === undefined) {
      throw new ApiError(404, "Run not found", { code: "run_not_found" });
    }
    if (
      snapshot.manifest.status !== "completed" &&
      snapshot.manifest.status !== "failed"
    ) {
      throw new ApiError(409, "Only a terminal run can be deleted", {
        code: "run_not_terminal",
      });
    }
    this.runs.delete(runId);
  }

  async deleteProject(projectId: string): Promise<void> {
    const project = this.projects.find((item) => item.id === projectId);
    if (project === undefined) {
      throw new ApiError(404, "Project not found", { code: "project_not_found" });
    }
    if (project.kind !== "custom") {
      throw new ApiError(403, "Built-in projects cannot be deleted", {
        code: "project_not_deletable",
      });
    }
    this.projects = this.projects.filter((item) => item.id !== projectId);
    this.samples = this.samples.filter((item) => item.project_id !== projectId);
    [...this.runs.entries()].forEach(([runId, snapshot]) => {
      if (snapshot.manifest.project_id === projectId) {
        this.runs.delete(runId);
      }
    });
    this.solutions = this.solutions.map((solution) => {
      const { [projectId]: _removedCompat, ...compatibility } = solution.compatibility;
      const { [projectId]: _removedQuality, ...comparableQuality } =
        solution.comparable_quality;
      return {
        ...solution,
        compatibility,
        comparable_quality: comparableQuality,
      };
    });
  }

  sampleImageUrl(projectId: string, sampleId: string): string {
    return `/api/projects/${encodeURIComponent(projectId)}/samples/${encodeURIComponent(
      sampleId,
    )}/image`;
  }

  runDownloadUrl(runId: string): string {
    return `/api/runs/${encodeURIComponent(runId)}/download`;
  }

  subscribeToRun(
    runId: string,
    handlers: RunSubscriptionHandlers,
    _options?: RunSubscriptionOptions,
  ): () => void {
    let closed = false;
    const timer = setTimeout(async () => {
      if (closed) {
        return;
      }
      try {
        const snapshot = await this.getRun(runId);
        handlers.onConnectionChange?.("open");
        const event: RunEvent = {
          type: "run_completed",
          run: snapshot.manifest,
        };
        handlers.onEvent(event, { eventId: "1", reconnectCount: 0 });
        handlers.onConnectionChange?.("closed");
      } catch (error) {
        handlers.onError?.(
          error instanceof Error ? error : new Error("Offline run failed"),
        );
      }
    }, 20);
    return () => {
      closed = true;
      clearTimeout(timer);
      handlers.onConnectionChange?.("closed");
    };
  }
}
