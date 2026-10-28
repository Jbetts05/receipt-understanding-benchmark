import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { resultPairKey } from "../domain";
import { mockProjects, mockSamples, mockSolutions } from "../mockData";
import { createInitialRunState, type LiveRunState } from "../streaming";
import type { ProjectAggregate, RunManifest } from "../types";
import {
  copyRunLinkToClipboard,
  EvaluationStudio,
  evidenceViewForKey,
  failurePairKeysForRun,
  resolveEvidenceView,
} from "./EvaluationStudio";
import { LiveWorkspace, visibleRunTasks } from "./LiveWorkspace";

const noop = () => undefined;

function runStateWithFailure(): LiveRunState {
  const samples = mockSamples.slice(0, 2);
  const solution = mockSolutions[0]!;
  const manifest: RunManifest = {
    id: "evaluation-controls-run",
    project_id: mockProjects[0]!.id,
    sample_ids: samples.map((sample) => sample.id),
    solution_ids: [solution.id],
    schema_version: "1.0.0",
    prompt_version: "1.0.0",
    status: "completed",
    total_pairs: samples.length,
    completed_pairs: samples.length,
    created_at: "2026-09-10T00:00:00Z",
    execution_mode: "mock",
    reference_only_solution_ids: [],
  };
  const state = createInitialRunState(manifest, [solution]);
  const failedKey = resultPairKey(samples[0]!.id, solution.id);
  const succeededKey = resultPairKey(samples[1]!.id, solution.id);
  state.tasks[failedKey] = {
    ...state.tasks[failedKey]!,
    status: "failed",
    stage: "failed",
    progress: 1,
    error: "Provider request failed.",
  };
  state.tasks[succeededKey] = {
    ...state.tasks[succeededKey]!,
    status: "succeeded",
    stage: "completed",
    progress: 1,
  };
  return state;
}

function aggregateFor(runState: LiveRunState): ProjectAggregate {
  return {
    format_version: "2.0",
    project_id: runState.manifest.project_id,
    run_id: runState.manifest.id,
    has_gold: false,
    scored_sample_count: 0,
    excluded_prompt_examples: 0,
    solutions: [],
    fields: [],
  };
}

function runStateWithDiagonalFailures(): LiveRunState {
  const samples = mockSamples.slice(0, 2);
  const solutions = mockSolutions.slice(0, 2);
  const manifest: RunManifest = {
    id: "diagonal-failures-run",
    project_id: mockProjects[0]!.id,
    sample_ids: samples.map((sample) => sample.id),
    solution_ids: solutions.map((solution) => solution.id),
    schema_version: "1.0.0",
    prompt_version: "1.0.0",
    status: "completed",
    total_pairs: samples.length * solutions.length,
    completed_pairs: samples.length * solutions.length,
    created_at: "2026-09-10T00:00:00Z",
    execution_mode: "mock",
    reference_only_solution_ids: [],
  };
  const state = createInitialRunState(manifest, solutions);
  Object.values(state.tasks).forEach((task) => {
    task.status = "succeeded";
    task.stage = "completed";
    task.progress = 1;
  });
  const failureKeys = [
    resultPairKey(samples[0]!.id, solutions[1]!.id),
    resultPairKey(samples[1]!.id, solutions[0]!.id),
  ];
  failureKeys.forEach((key, index) => {
    state.tasks[key] = {
      ...state.tasks[key]!,
      status: "failed",
      stage: "failed",
      error: `Diagonal failure ${index + 1}.`,
    };
  });
  return state;
}

function renderStudio(
  runState: LiveRunState,
  aggregate: ProjectAggregate | null = null,
): string {
  return renderToStaticMarkup(
    <EvaluationStudio
      client={{ runDownloadUrl: () => "/download" } as never}
      project={mockProjects[0]!}
      samples={mockSamples}
      solutions={mockSolutions}
      runState={runState}
      aggregate={aggregate}
      aggregateError={null}
      selectedSampleId={mockSamples[0]!.id}
      selectedSolutionId={mockSolutions[0]!.id}
      onSampleChange={noop}
      onSolutionChange={noop}
      onBuildRun={noop}
    />,
  );
}

function buttonContaining(markup: string, label: string): string {
  const labelIndex = markup.indexOf(label);
  const start = markup.lastIndexOf("<button", labelIndex);
  const end = markup.indexOf("</button>", labelIndex);
  return markup.slice(start, end + "</button>".length);
}

describe("EvaluationStudio controls", () => {
  it("labels configuration accurately and exposes a pressed failure filter", () => {
    const runState = runStateWithFailure();
    const markup = renderStudio(runState);
    const failureButton = buttonContaining(markup, "Failures (1)");

    expect(markup).toContain(
      `<h1 class="sr-only">${mockProjects[0]!.name} evaluation workspace</h1>`,
    );
    expect(markup).toContain(">Configure run</button>");
    expect(markup).not.toContain(">All receipts</button>");
    expect(failureButton).toContain('aria-pressed="false"');
    expect(failureButton).toContain(
      'aria-controls="run-evidence-panel-live"',
    );
    expect(failureButton).not.toContain('disabled=""');

    const failurePairKeys = failurePairKeysForRun(runState);
    const failedTask = visibleRunTasks(runState, failurePairKeys)[0]!;
    expect([...failurePairKeys]).toEqual([failedTask.pair_id]);
    expect(failedTask.status).toBe("failed");
  });

  it("keeps diagonal failure filtering exact without rewriting the manifest", () => {
    const runState = runStateWithDiagonalFailures();
    const originalManifest = runState.manifest;
    const failurePairKeys = failurePairKeysForRun(runState);
    const visibleTasks = visibleRunTasks(runState, failurePairKeys);
    const markup = renderToStaticMarkup(
      <LiveWorkspace
        embedded
        project={mockProjects[0]!}
        runState={runState}
        samples={mockSamples}
        solutions={mockSolutions}
        visiblePairKeys={failurePairKeys}
        onBuildRun={noop}
        onOpenComparison={noop}
      />,
    );

    expect(runState.manifest).toBe(originalManifest);
    expect(runState.manifest.total_pairs).toBe(4);
    expect(visibleTasks.map((task) => task.pair_id)).toEqual([
      resultPairKey(mockSamples[0]!.id, mockSolutions[1]!.id),
      resultPairKey(mockSamples[1]!.id, mockSolutions[0]!.id),
    ]);
    expect(visibleTasks.every((task) => task.status === "failed")).toBe(true);
    expect(visibleTasks[0]!.pair_id).not.toBe(
      resultPairKey(mockSamples[0]!.id, mockSolutions[0]!.id),
    );
    expect(markup).toContain("Filtered task pairs");
    expect(markup).toContain("Diagonal failure 1.");
    expect(markup).toContain("Diagonal failure 2.");
    expect(markup.match(/class="task-cell task-failed"/g)).toHaveLength(2);
    expect(markup).not.toContain('class="task-cell task-succeeded"');
  });

  it("guards aggregate navigation and synchronously falls back to live evidence", () => {
    const runState = runStateWithFailure();
    const unavailableMarkup = renderStudio(runState);
    const unavailableCost = buttonContaining(
      unavailableMarkup,
      "Cost &amp; usage",
    );

    expect(unavailableCost).toContain('disabled=""');
    expect(unavailableCost).toContain('aria-disabled="true"');
    expect(resolveEvidenceView("batch", false)).toBe("live");

    const availableMarkup = renderStudio(runState, aggregateFor(runState));
    const availableCost = buttonContaining(availableMarkup, "Cost &amp; usage");
    expect(availableCost).not.toContain('disabled=""');
    expect(availableCost).toContain('aria-disabled="false"');
    expect(resolveEvidenceView("batch", true)).toBe("batch");
  });

  it("turns clipboard rejection into a recoverable result", async () => {
    const writeText = vi
      .fn<(value: string) => Promise<void>>()
      .mockRejectedValueOnce(new Error("Permission denied"))
      .mockResolvedValueOnce(undefined);
    const clipboard = { writeText };

    await expect(
      copyRunLinkToClipboard(
        "run with spaces",
        "https://example.test/evaluation?project=demo",
        clipboard,
      ),
    ).resolves.toBe("error");
    await expect(
      copyRunLinkToClipboard(
        "run with spaces",
        "https://example.test/evaluation?project=demo",
        clipboard,
      ),
    ).resolves.toBe("copied");

    const copiedUrl = new URL(writeText.mock.calls[1]![0]);
    expect(copiedUrl.searchParams.get("project")).toBe("demo");
    expect(copiedUrl.searchParams.get("run")).toBe("run with spaces");
    await expect(
      copyRunLinkToClipboard(
        "unavailable",
        "https://example.test/evaluation",
        null,
      ),
    ).resolves.toBe("error");
  });

  it("ignores clipboard completion from an obsolete run generation", async () => {
    let finishWrite!: () => void;
    const clipboard = {
      writeText: vi.fn(
        () =>
          new Promise<void>((resolve) => {
            finishWrite = resolve;
          }),
      ),
    };
    let generation = 1;
    let activeRunId = "run-a";
    const pending = copyRunLinkToClipboard(
      "run-a",
      "https://example.test/evaluation",
      clipboard,
      {
        generation,
        isCurrent: (requestGeneration, requestRunId) =>
          generation === requestGeneration && activeRunId === requestRunId,
      },
    );

    generation = 2;
    activeRunId = "run-b";
    finishWrite();

    await expect(pending).resolves.toBe("stale");
  });

  it("links stable evidence tabs and uses one roving tab stop", () => {
    const markup = renderStudio(runStateWithFailure());
    const liveTab = buttonContaining(markup, "Live tasks");
    const pipelineTab = buttonContaining(markup, "Pipeline inspector");
    const batchTab = buttonContaining(markup, "Batch evidence");

    expect(liveTab).toContain('id="run-evidence-tab-live"');
    expect(liveTab).toContain('tabindex="0"');
    expect(liveTab).toContain('aria-selected="true"');
    expect(liveTab).toContain('aria-controls="run-evidence-panel-live"');
    expect(pipelineTab).toContain('id="run-evidence-tab-pipeline"');
    expect(pipelineTab).toContain('tabindex="-1"');
    expect(pipelineTab).toContain('aria-selected="false"');
    expect(pipelineTab).toContain(
      'aria-controls="run-evidence-panel-pipeline"',
    );
    expect(batchTab).toContain('id="run-evidence-tab-batch"');
    expect(batchTab).toContain('tabindex="-1"');
    expect(batchTab).toContain('disabled=""');
    expect(markup).toContain(
      'id="run-evidence-panel-live" class="evaluation-evidence-panel" role="tabpanel" aria-labelledby="run-evidence-tab-live"',
    );
    expect(markup).toContain(
      'id="run-evidence-panel-pipeline" class="evaluation-evidence-panel" role="tabpanel" aria-labelledby="run-evidence-tab-pipeline" hidden=""',
    );
    expect(markup).toContain(
      'id="run-evidence-panel-batch" class="evaluation-evidence-panel" role="tabpanel" aria-labelledby="run-evidence-tab-batch" hidden=""',
    );
  });

  it("moves evidence selection with arrows, Home, and End", () => {
    expect(evidenceViewForKey("live", "ArrowRight", true)).toBe("pipeline");
    expect(evidenceViewForKey("pipeline", "ArrowRight", true)).toBe("batch");
    expect(evidenceViewForKey("batch", "ArrowRight", true)).toBe("live");
    expect(evidenceViewForKey("live", "ArrowLeft", true)).toBe("batch");
    expect(evidenceViewForKey("pipeline", "Home", true)).toBe("live");
    expect(evidenceViewForKey("live", "End", true)).toBe("batch");

    expect(evidenceViewForKey("pipeline", "ArrowRight", false)).toBe("live");
    expect(evidenceViewForKey("live", "ArrowLeft", false)).toBe("pipeline");
    expect(evidenceViewForKey("live", "End", false)).toBe("pipeline");
    expect(evidenceViewForKey("live", "Enter", true)).toBeNull();
  });
});
