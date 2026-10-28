import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { buildProjectAggregate, resultPairKey } from "./domain";
import {
  makeMockRunResult,
  mockProjects,
  mockSamples,
  mockSolutions,
} from "./mockData";
import { createInitialRunState } from "./streaming";
import type { RunHistoryItem, RunManifest, RunResult } from "./types";
import {
  buildComparisonRows,
  comparisonColumnLabel,
  ComparisonWorkspace,
} from "./components/ComparisonWorkspace";
import { AppShell, PRIMARY_NAVIGATION } from "./components/AppShell";
import {
  AdminCenter,
  type AdminRunInventory,
} from "./components/AdminCenter";
import { MockProductClient } from "./mockClient";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { ContractDisclosure } from "./components/ContractDisclosure";
import { CustomProjectWizard } from "./components/CustomProjectWizard";
import { DatasetBrowser } from "./components/DatasetBrowser";
import { EvaluationStudio } from "./components/EvaluationStudio";
import { HomeWorkspace } from "./components/HomeWorkspace";
import { ModelsCatalog } from "./components/ModelsCatalog";
import { LiveWorkspace } from "./components/LiveWorkspace";
import { PipelineInspector } from "./components/PipelineInspector";
import { ReceiptPreview } from "./components/ReceiptPreview";
import { RunBuilder } from "./components/RunBuilder";
import { RunsWorkspace } from "./components/RunsWorkspace";
import { ReportsWorkspace } from "./components/ReportsWorkspace";

const noop = () => undefined;

function manifest(
  sampleIds: string[],
  solutionIds = ["offline-reference", "offline-direct", "offline-ocr"],
): RunManifest {
  return {
    id: "ui-run",
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
    reference_only_solution_ids: ["offline-reference"],
  };
}

function stateWithResults(run: RunManifest, results: RunResult[]) {
  const state = createInitialRunState(run, mockSolutions);
  results.forEach((result) => {
    state.results[resultPairKey(result.sample_id, result.solution_id)] = result;
  });
  return state;
}

describe("receipt evaluation surfaces", () => {
  it("names both stages of assisted comparison columns", () => {
    expect(
      comparisonColumnLabel({
        ...mockSolutions[0]!,
        label: "Mistral Document AI",
        short_label: "Doc AI",
        normalizer: "gpt-5.6-luna",
      }),
    ).toBe("Mistral Document AI + GPT-5.6 Luna");
    expect(
      comparisonColumnLabel({
        ...mockSolutions[0]!,
        label: "Document Intelligence receipt + Luna",
        short_label: "DI receipt + Luna",
        normalizer: "gpt-5.6-luna",
      }),
    ).toBe("Document Intelligence receipt + GPT-5.6 Luna");
    expect(comparisonColumnLabel(mockSolutions[0]!)).toBe("Direct");
  });

  it("renders the server image URL instead of generated receipt markup", () => {
    const sample = {
      ...mockSamples[0]!,
      image_url: "/api/projects/authorized/samples/領収書/image",
    };
    const markup = renderToStaticMarkup(<ReceiptPreview sample={sample} />);

    expect(markup).toContain(
      'src="/api/projects/authorized/samples/領収書/image"',
    );
    expect(markup).toContain("Fit");
    expect(markup).toContain("Reset");
    expect(markup).not.toContain("receipt-paper");
  });

  it("keeps gold and every manifest output in stable side-by-side columns", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const run = manifest([sample.id]);
    const direct = makeMockRunResult(project, sample, mockSolutions[0]!, run.id);
    const reference = makeMockRunResult(project, sample, mockSolutions[2]!, run.id);
    const markup = renderToStaticMarkup(
      <ComparisonWorkspace
        project={project}
        samples={[sample]}
        solutions={mockSolutions}
        runState={stateWithResults(run, [direct, reference])}
        selectedSampleId={sample.id}
        selectedSolutionId={direct.solution_id}
        onSampleChange={noop}
        onSolutionChange={noop}
        onBuildRun={noop}
      />,
    );

    expect(markup).toContain("Golden answer");
    expect(markup.indexOf(">Direct<")).toBeLessThan(
      markup.indexOf(">Offline OCR pipeline + GPT-5.6 Luna<"),
    );
    expect(markup.indexOf(">Offline OCR pipeline + GPT-5.6 Luna<")).toBeLessThan(
      markup.indexOf(">Reference<"),
    );
    expect(markup).toContain("Standalone native output");
    expect(markup).toContain("Pending");
    expect(markup).not.toContain("Unavailable custom analyzer");
    expect(markup).not.toContain("Dataset rights");
    expect(markup).toContain("table-sort-button");
    expect(markup).toContain("and sort fields by its output");
    expect(markup).toContain(">By extractor<");
    expect(
      markup.match(/<button[^>]*disabled=""[^>]*>By extractor<\/button>/),
    ).not.toBeNull();
  });

  it("enables extractor-focused receipt review after a run completes", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const run = {
      ...manifest([sample.id], ["offline-direct", "offline-ocr"]),
      status: "completed" as const,
      completed_pairs: 2,
    };
    const markup = renderToStaticMarkup(
      <ComparisonWorkspace
        project={project}
        samples={[sample]}
        solutions={mockSolutions}
        runState={stateWithResults(run, [
          makeMockRunResult(project, sample, mockSolutions[0]!, run.id),
          makeMockRunResult(project, sample, mockSolutions[1]!, run.id),
        ])}
        selectedSampleId={sample.id}
        selectedSolutionId="offline-direct"
        onSampleChange={noop}
        onSolutionChange={noop}
        onBuildRun={noop}
      />,
    );
    const extractorButton =
      markup.match(/<button[^>]*>By extractor<\/button>/)?.[0] ?? "";

    expect(extractorButton).not.toContain('disabled=""');
    expect(extractorButton).toContain(
      'title="Review every receipt for one extractor"',
    );
  });

  it("does not describe an unloaded terminal result as pending", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const run = {
      ...manifest([sample.id], ["offline-direct"]),
      status: "completed" as const,
      completed_pairs: 1,
    };
    const state = createInitialRunState(run, mockSolutions);
    const key = resultPairKey(sample.id, "offline-direct");
    state.tasks[key] = {
      ...state.tasks[key]!,
      status: "succeeded",
      stage: "completed",
      progress: 1,
    };

    const markup = renderToStaticMarkup(
      <ComparisonWorkspace
        project={project}
        samples={[sample]}
        solutions={mockSolutions}
        runState={state}
        resultLoadStatus="error"
        selectedSampleId={sample.id}
        selectedSolutionId="offline-direct"
        onSampleChange={noop}
        onSolutionChange={noop}
        onBuildRun={noop}
      />,
    );

    expect(markup).toContain("Result unavailable");
    expect(markup).toContain("Load failed");
    expect(markup).toContain("Persisted results for this receipt could not be loaded.");
  });

  it("reports no quality when the selected receipt has no gold", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[1]!;
    const run = manifest([sample.id], ["offline-direct"]);
    const result = makeMockRunResult(project, sample, mockSolutions[0]!, run.id);
    const markup = renderToStaticMarkup(
      <ComparisonWorkspace
        project={project}
        samples={[sample]}
        solutions={mockSolutions}
        runState={stateWithResults(run, [result])}
        selectedSampleId={sample.id}
        selectedSolutionId={result.solution_id}
        onSampleChange={noop}
        onSolutionChange={noop}
        onBuildRun={noop}
      />,
    );

    expect(markup).toContain("No golden answer");
    expect(markup).toContain(">No gold<");
    expect(markup).toContain(">Unscored<");
    expect(markup).toContain("Unscored");
    expect(markup).not.toContain("Not returned");
    expect(markup).not.toMatch(/accuracy|field f1/i);
  });

  it("shows an empty parsed JSON root when a receipt has no gold", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[1]!;
    const solution = mockSolutions[0]!;
    const result = {
      ...makeMockRunResult(project, sample, solution),
      parsed_output: {},
      comparisons: [],
      quality_scored: false,
    };
    const rows = buildComparisonRows(sample, [solution], [result]);

    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      path: "$",
      goldPresent: false,
    });
    expect(rows[0]?.outputs.get(solution.id)).toMatchObject({
      value: {},
      state: "unscored",
    });
  });

  it("uses canonical path semantics for reference and model-specific extra fields", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const directSolution = mockSolutions[0]!;
    const ocrSolution = mockSolutions[1]!;
    const referenceSolution = mockSolutions[2]!;
    const directBase = makeMockRunResult(project, sample, directSolution);
    const direct: RunResult = {
      ...directBase,
      parsed_output: { ...directBase.parsed_output!, confidence: 0.93 },
      comparisons: [
        ...directBase.comparisons,
        {
          path: "confidence",
          label: "Confidence",
          state: "extra",
          expected: undefined,
          actual: 0.93,
        },
      ],
    };
    const ocr = makeMockRunResult(project, sample, ocrSolution);
    const reference = makeMockRunResult(project, sample, referenceSolution);
    const rows = buildComparisonRows(
      sample,
      [directSolution, ocrSolution, referenceSolution],
      [direct, ocr, reference],
    );
    const confidence = rows.find((row) => row.path === "confidence")!;
    const total = rows.find((row) => row.path === "total")!;

    expect(confidence.outputs.get(directSolution.id)).toMatchObject({
      value: 0.93,
      state: "extra",
    });
    expect(confidence.outputs.get(ocrSolution.id)?.state).toBe("not_applicable");
    expect(total.outputs.get(referenceSolution.id)).toMatchObject({
      value: 24.8,
      state: "unscored",
    });
    expect(confidence.goldPresent).toBe(false);

    const run = manifest(
      [sample.id],
      [directSolution.id, ocrSolution.id, referenceSolution.id],
    );
    const markup = renderToStaticMarkup(
      <ComparisonWorkspace
        project={project}
        samples={[sample]}
        solutions={mockSolutions}
        runState={stateWithResults(run, [
          { ...direct, run_id: run.id },
          { ...ocr, run_id: run.id },
          { ...reference, run_id: run.id },
        ])}
        selectedSampleId={sample.id}
        selectedSolutionId={directSolution.id}
        onSampleChange={noop}
        onSolutionChange={noop}
        onBuildRun={noop}
      />,
    );

    expect(markup).toContain("Not in gold");
    expect(markup).toContain("Output-only field");
    expect(markup).toContain("absent from this receipt’s dataset annotation");
    expect(markup).toContain("Extra output");
  });

  it("never falls back to a browser-selected receipt outside the run manifest", () => {
    const project = mockProjects[0]!;
    const manifestSample = mockSamples[0]!;
    const browserSample = {
      ...mockSamples[1]!,
      title: "Browser receipt outside run",
    };
    const run = manifest([manifestSample.id], ["offline-direct"]);
    const result = makeMockRunResult(
      project,
      manifestSample,
      mockSolutions[0]!,
      run.id,
    );
    const markup = renderToStaticMarkup(
      <ComparisonWorkspace
        project={project}
        samples={[manifestSample, browserSample]}
        solutions={mockSolutions}
        runState={stateWithResults(run, [result])}
        selectedSampleId={browserSample.id}
        selectedSolutionId={result.solution_id}
        onSampleChange={noop}
        onSolutionChange={noop}
        onBuildRun={noop}
      />,
    );

    expect(markup).toContain(manifestSample.title);
    expect(markup).not.toContain(browserSample.title);
  });

  it("never renders a receipt from another project under a run", () => {
    const project = mockProjects[0]!;
    const manifestSample = mockSamples[0]!;
    const foreignSample = {
      ...mockSamples[1]!,
      id: "foreign-receipt",
      project_id: "another-project",
      title: "Foreign project receipt",
    };
    const run = manifest(
      [manifestSample.id, foreignSample.id],
      ["offline-direct"],
    );
    const result = makeMockRunResult(
      project,
      manifestSample,
      mockSolutions[0]!,
      run.id,
    );
    const markup = renderToStaticMarkup(
      <ComparisonWorkspace
        project={project}
        samples={[foreignSample, manifestSample]}
        solutions={mockSolutions}
        runState={stateWithResults(run, [result])}
        selectedSampleId={foreignSample.id}
        selectedSolutionId={result.solution_id}
        onSampleChange={noop}
        onSolutionChange={noop}
        onBuildRun={noop}
      />,
    );

    expect(markup).toContain(manifestSample.title);
    expect(markup).not.toContain(foreignSample.title);
    expect(markup).not.toContain(foreignSample.id);
  });

  it("disables unsupported solutions in the run builder", () => {
    const markup = renderToStaticMarkup(
      <RunBuilder
        project={mockProjects[0]!}
        samples={mockSamples}
        solutions={mockSolutions}
        selectedSampleIds={new Set(["offline-001"])}
        selectedSolutionIds={new Set(["offline-direct"])}
        schemaConfirmed
        promptConfirmed
        executionMode="mock"
        launchPending={false}
        onSampleToggle={noop}
        onSolutionToggle={noop}
        onSelectSamples={noop}
        onSelectSolutions={noop}
        onSchemaConfirm={noop}
        onPromptConfirm={noop}
        onLaunch={noop}
      />,
    );
    const unsupported = markup.slice(markup.indexOf("solution-row disabled"));

    expect(unsupported).toContain('type="checkbox" disabled=""');
    expect(unsupported).toContain("Unavailable custom analyzer");
    expect(unsupported).toContain("Custom analyzer unavailable");
    expect(markup).not.toContain("Reference-only outputs");
    expect(markup).toContain(">Clear<");
  });

  it("groups a native_pipeline solution under an Extraction API + normalizer legend, never labeled as OCR", () => {
    const markup = renderToStaticMarkup(
      <RunBuilder
        project={mockProjects[0]!}
        samples={mockSamples}
        solutions={mockSolutions}
        selectedSampleIds={new Set(["offline-001"])}
        selectedSolutionIds={new Set(["offline-direct"])}
        schemaConfirmed
        promptConfirmed
        executionMode="mock"
        launchPending={false}
        onSampleToggle={noop}
        onSolutionToggle={noop}
        onSelectSamples={noop}
        onSelectSolutions={noop}
        onSchemaConfirm={noop}
        onPromptConfirm={noop}
        onLaunch={noop}
      />,
    );

    expect(markup).toContain("Extraction API + GPT-5.6 Luna");
    const nativePipelineSection = markup.slice(
      markup.indexOf("Extraction API + GPT-5.6 Luna"),
      markup.indexOf("Extraction API + GPT-5.6 Luna") + 400,
    );
    expect(nativePipelineSection).not.toContain("OCR + GPT-5.6 Luna");
  });

  it("disables launch and exposes pending state while the POST is in flight", () => {
    const markup = renderToStaticMarkup(
      <RunBuilder
        project={mockProjects[0]!}
        samples={mockSamples}
        solutions={mockSolutions}
        selectedSampleIds={new Set(["offline-001"])}
        selectedSolutionIds={new Set(["offline-direct"])}
        schemaConfirmed
        promptConfirmed
        executionMode="live"
        launchPending
        onSampleToggle={noop}
        onSolutionToggle={noop}
        onSelectSamples={noop}
        onSelectSolutions={noop}
        onSchemaConfirm={noop}
        onPromptConfirm={noop}
        onLaunch={noop}
      />,
    );

    expect(markup).toContain("Launching run…");
    expect(markup).toContain('aria-busy="true"');
    expect(markup).toContain('disabled=""');
  });

  it("labels excluded duplicates with their reason and prevents run selection", () => {
    const excluded = {
      ...mockSamples[0]!,
      id: "offline-excluded",
      title: "Duplicate receipt",
      prompt_example: true,
      scoring_role: "excluded" as const,
      exclusion_reason: "Exact duplicate of offline-001.",
    };
    const builder = renderToStaticMarkup(
      <RunBuilder
        project={{ ...mockProjects[0]!, sample_count: 3 }}
        samples={[...mockSamples, excluded]}
        solutions={mockSolutions}
        selectedSampleIds={new Set([excluded.id])}
        selectedSolutionIds={new Set(["offline-direct"])}
        schemaConfirmed
        promptConfirmed
        executionMode="mock"
        launchPending={false}
        onSampleToggle={noop}
        onSolutionToggle={noop}
        onSelectSamples={noop}
        onSelectSolutions={noop}
        onSchemaConfirm={noop}
        onPromptConfirm={noop}
        onLaunch={noop}
      />,
    );
    const excludedRow = builder.slice(
      builder.indexOf("builder-receipt-row selected disabled"),
    );

    expect(excludedRow).toContain('type="checkbox" disabled=""');
    expect(excludedRow).toContain("Excluded");
    expect(excludedRow).toContain("Exact duplicate of offline-001.");
    expect(builder).toContain("Choose at least one receipt.");

    const browser = renderToStaticMarkup(
      <DatasetBrowser
        project={{ ...mockProjects[0]!, sample_count: 1 }}
        samples={[excluded]}
        selectedSampleId={excluded.id}
        onSampleSelect={noop}
        onBuildRun={noop}
      />,
    );
    expect(browser).toContain("Excluded duplicate");
    expect(browser).toContain("Exact duplicate of offline-001.");
    expect(browser).toContain(">Not runnable<");
    expect(browser).toContain('disabled=""');
  });

  it("keeps comparison first and makes every evidence surface reachable", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solution = mockSolutions[0]!;
    const run = manifest([sample.id], [solution.id]);
    const result = makeMockRunResult(project, sample, solution, run.id);
    const runState = stateWithResults(run, [result]);
    const aggregate = buildProjectAggregate(
      project,
      run.id,
      [sample],
      [solution],
      [result],
    );
    const client = {
      runDownloadUrl: () => "/download",
    } as never;
    const markup = renderToStaticMarkup(
      <EvaluationStudio
        client={client}
        project={project}
        samples={[sample]}
        solutions={[solution]}
        runState={runState}
        aggregate={aggregate}
        aggregateError={null}
        selectedSampleId={sample.id}
        selectedSolutionId={solution.id}
        onSampleChange={noop}
        onSolutionChange={noop}
        onBuildRun={noop}
      />,
    );

    expect(markup.indexOf("Receipt inspector")).toBeLessThan(
      markup.indexOf("Operational and artifact detail"),
    );
    expect(markup).toContain("Live tasks");
    expect(markup).toContain("Pipeline inspector");
    expect(markup).toContain("Batch evidence");
    expect(markup).toContain("Receipt × solution grid");
  });

  it("shows raw, parsed, schema, telemetry, and component evidence in the pipeline", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solution = mockSolutions[0]!;
    const result = makeMockRunResult(project, sample, solution);
    const markup = renderToStaticMarkup(
      <PipelineInspector
        embedded
        project={project}
        samples={[sample]}
        solutions={[solution]}
        results={[result]}
        selectedSampleId={sample.id}
        selectedSolutionId={solution.id}
        onSampleChange={noop}
        onSolutionChange={noop}
      />,
    );

    expect(markup).toContain("Raw provider response");
    expect(markup).toContain("Parsed output");
    expect(markup).toContain("Schema valid");
    expect(markup).toContain("Input tokens");
    expect(markup).toContain("Persisted input");
    expect(markup).toContain("Persisted output");
    expect(markup).toContain("Attempts");
  });

  it("distinguishes a pipeline result load failure from an unfinished task", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solution = mockSolutions[0]!;
    const markup = renderToStaticMarkup(
      <PipelineInspector
        embedded
        project={project}
        samples={[sample]}
        solutions={[solution]}
        results={[]}
        resultLoadStatus="error"
        selectedSampleId={sample.id}
        selectedSolutionId={solution.id}
        onSampleChange={noop}
        onSolutionChange={noop}
      />,
    );

    expect(markup).toContain("Pipeline artifact unavailable");
    expect(markup).toContain("The persisted result could not be loaded.");
    expect(markup).not.toContain("No completed pipeline artifact");
  });

  it("presents a neutral not-applicable schema state for standalone OCR text, never invalid", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solution = mockSolutions.find((item) => item.id === "offline-ocr-text")!;
    const result = makeMockRunResult(project, sample, solution);
    expect(result.schema_valid).toBeNull();
    const markup = renderToStaticMarkup(
      <PipelineInspector
        embedded
        project={project}
        samples={[sample]}
        solutions={[solution]}
        results={[result]}
        selectedSampleId={sample.id}
        selectedSolutionId={solution.id}
        onSampleChange={noop}
        onSolutionChange={noop}
      />,
    );

    expect(markup).toContain("No schema to validate");
    expect(markup).not.toContain("Schema invalid");
    expect(markup).not.toContain(">Schema valid<");
  });

  it("marks the closed mobile drawer as an inert hidden dialog", () => {
    vi.stubGlobal("window", {
      matchMedia: () => ({
        matches: true,
        addEventListener: noop,
        removeEventListener: noop,
      }),
    });
    vi.stubGlobal("document", {
      documentElement: { getAttribute: () => "light" },
    });
    try {
      const markup = renderToStaticMarkup(
        <AppShell
          projects={mockProjects}
          activeProject={mockProjects[0]!}
          view="dataset"
          health={{ status: "ok", profile: "local", execution_mode: "mock" }}
          source="server"
          runState={null}
          runHistory={[]}
          runHistoryError={null}
          runHistoryLoading={false}
          restoringRunId={null}
          samples={mockSamples}
          selectedSampleId=""
          onProjectChange={noop}
          onViewChange={noop}
          onSampleChange={noop}
          onRunSelect={noop}
          onRunHistoryRefresh={noop}
          onDeleteProjectRequest={noop}
          onDeleteRunRequest={noop}
        >
          <div>Workspace</div>
        </AppShell>,
      );
      const drawer = markup.slice(markup.indexOf('id="primary-navigation"'));
      expect(drawer).toContain('role="dialog"');
      expect(drawer).toContain('aria-hidden="true"');
      expect(drawer).toMatch(/\binert(?:=""|="true")?/);
      expect(drawer).toContain('tabindex="-1"');
      expect(markup).not.toContain("Dataset rights");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("shows accessible recent project runs without replacing the workspace", () => {
    const history = [
      {
        run_id: "run-20260830-newest",
        project_id: mockProjects[0]!.id,
        status: "running",
        created_at: "2026-08-30T10:30:00Z",
        updated_at: "2026-08-30T10:31:00Z",
        total_pairs: 6,
        completed_pairs: 4,
        execution_mode: "mock",
        sample_count: 2,
        solution_count: 3,
        reference_only_count: 1,
        download_ready: false,
      },
    ] satisfies RunHistoryItem[];
    const markup = renderToStaticMarkup(
      <AppShell
        projects={mockProjects}
        activeProject={mockProjects[0]!}
        view="evaluation"
        health={{ status: "ok", profile: "local", execution_mode: "mock" }}
        source="server"
        runState={null}
        runHistory={history}
        runHistoryError={null}
        runHistoryLoading={false}
        restoringRunId={null}
        samples={mockSamples}
        selectedSampleId=""
        onProjectChange={noop}
        onViewChange={noop}
        onSampleChange={noop}
        onRunSelect={noop}
        onRunHistoryRefresh={noop}
        onDeleteProjectRequest={noop}
        onDeleteRunRequest={noop}
      >
        <div>Receipt comparison workspace</div>
      </AppShell>,
    );

    expect(markup).toContain("Recent runs");
    expect(markup).toContain(
      'aria-label="Restore run run-20260830-newest, running, 4 of 6 pairs"',
    );
    expect(markup).toContain('dateTime="2026-08-30T10:30:00Z"');
    expect(markup).toContain("Receipt comparison workspace");
  });

  it("gives Home its own active navigation destination", () => {
    const markup = renderToStaticMarkup(
      <AppShell
        projects={mockProjects}
        activeProject={mockProjects[0]!}
        view="home"
        health={{ status: "ok", profile: "local", execution_mode: "mock" }}
        source="server"
        runState={null}
        runHistory={[]}
        runHistoryError={null}
        runHistoryLoading={false}
        restoringRunId={null}
        samples={mockSamples}
        selectedSampleId=""
        onProjectChange={noop}
        onViewChange={noop}
        onSampleChange={noop}
        onRunSelect={noop}
        onRunHistoryRefresh={noop}
        onDeleteProjectRequest={noop}
        onDeleteRunRequest={noop}
      >
        <div>Home workspace</div>
      </AppShell>,
    );
    const overview = markup.slice(
      markup.indexOf('id="overview-nav-heading"'),
      markup.indexOf('id="data-nav-heading"'),
    );
    const evaluations = markup.slice(
      markup.indexOf('id="evaluations-nav-heading"'),
      markup.indexOf('id="insights-nav-heading"'),
    );

    expect(overview).toContain("Home");
    expect(overview).toContain('aria-current="page"');
    expect(evaluations).not.toContain('aria-current="page"');
    expect(markup).toContain('aria-label="Go to Receipt Bench home"');
    expect(markup).toContain("<span>Home</span>");
  });

  it("maps every primary menu label to one distinct workspace route", () => {
    const destinations = PRIMARY_NAVIGATION.flatMap((group) =>
      group.items.map((item) => [item.label, item.view] as const),
    );

    expect(destinations).toEqual([
      ["Home", "home"],
      ["Datasets", "dataset"],
      ["Create dataset", "new_project"],
      ["Models & services", "catalog"],
      ["Configure run", "builder"],
      ["Evaluation detail", "evaluation"],
      ["Runs", "runs"],
      ["Reports", "reports"],
      ["Admin Center", "admin"],
    ]);
    expect(new Set(destinations.map(([, route]) => route)).size).toBe(
      destinations.length,
    );

    for (const [label, route] of destinations) {
      const markup = renderToStaticMarkup(
        <AppShell
          projects={mockProjects}
          activeProject={mockProjects[0]!}
          view={route}
          health={{ status: "ok", profile: "local", execution_mode: "mock" }}
          source="server"
          runState={null}
          runHistory={[]}
          runHistoryError={null}
          runHistoryLoading={false}
          restoringRunId={null}
          samples={mockSamples}
          selectedSampleId=""
          onProjectChange={noop}
          onViewChange={noop}
          onSampleChange={noop}
          onRunSelect={noop}
          onRunHistoryRefresh={noop}
          onDeleteProjectRequest={noop}
          onDeleteRunRequest={noop}
        >
          <div>Workspace</div>
        </AppShell>,
      );
      const primaryNavigation = markup.slice(
        markup.indexOf('<div class="navigation-scroll">'),
        markup.indexOf("Recent runs"),
      );
      const activeButton =
        primaryNavigation.match(
          /<button type="button" class="active" aria-current="page">[\s\S]*?<\/button>/,
        )?.[0] ?? "";

      expect(
        primaryNavigation.match(/aria-current="page"/g) ?? [],
        `${label} should be the only active primary route`,
      ).toHaveLength(1);
      expect(activeButton).toContain(`>${label.replace("&", "&amp;")}<`);
    }
  });

  it("gives Models & services and Configure run distinct nav destinations with one active item", () => {
    function extractorsNav(view: "catalog" | "builder"): string {
      const markup = renderToStaticMarkup(
        <AppShell
          projects={mockProjects}
          activeProject={mockProjects[0]!}
          view={view}
          health={{ status: "ok", profile: "local", execution_mode: "mock" }}
          source="server"
          runState={null}
          runHistory={[]}
          runHistoryError={null}
          runHistoryLoading={false}
          restoringRunId={null}
          samples={mockSamples}
          selectedSampleId=""
          onProjectChange={noop}
          onViewChange={noop}
          onSampleChange={noop}
          onRunSelect={noop}
          onRunHistoryRefresh={noop}
          onDeleteProjectRequest={noop}
          onDeleteRunRequest={noop}
        >
          <div>Workspace</div>
        </AppShell>,
      );
      return markup.slice(
        markup.indexOf('id="extractors-nav-heading"'),
        markup.indexOf('id="evaluations-nav-heading"'),
      );
    }

    const catalogView = extractorsNav("catalog");
    expect(catalogView).toContain("Models &amp; services");
    expect(catalogView).toContain("Configure run");
    expect((catalogView.match(/aria-current="page"/g) ?? []).length).toBe(1);
    expect(catalogView.indexOf("aria-current")).toBeLessThan(
      catalogView.indexOf("Models &amp; services"),
    );

    const builderView = extractorsNav("builder");
    expect((builderView.match(/aria-current="page"/g) ?? []).length).toBe(1);
    expect(builderView.indexOf("aria-current")).toBeLessThan(
      builderView.indexOf("Configure run"),
    );
  });

  it("gives Evaluation detail, Runs, and Reports distinct active destinations", () => {
    function navigation(view: "evaluation" | "runs" | "reports"): string {
      const markup = renderToStaticMarkup(
        <AppShell
          projects={mockProjects}
          activeProject={mockProjects[0]!}
          view={view}
          health={{ status: "ok", profile: "local", execution_mode: "mock" }}
          source="server"
          runState={null}
          runHistory={[]}
          runHistoryError={null}
          runHistoryLoading={false}
          restoringRunId={null}
          samples={mockSamples}
          selectedSampleId=""
          onProjectChange={noop}
          onViewChange={noop}
          onSampleChange={noop}
          onRunSelect={noop}
          onRunHistoryRefresh={noop}
          onDeleteProjectRequest={noop}
          onDeleteRunRequest={noop}
        >
          <div>Workspace</div>
        </AppShell>,
      );
      return markup.slice(
        markup.indexOf('id="evaluations-nav-heading"'),
        markup.indexOf("Recent runs"),
      );
    }

    for (const [view, label] of [
      ["evaluation", "Evaluation detail"],
      ["runs", "Runs"],
      ["reports", "Reports"],
    ] as const) {
      const markup = navigation(view);
      expect((markup.match(/aria-current="page"/g) ?? []).length).toBe(1);
      expect(markup.indexOf("aria-current")).toBeLessThan(markup.indexOf(label));
    }
  });
});

describe("runs and reports workspaces", () => {
  const history = [
    {
      run_id: "run-20260831-terminal",
      project_id: mockProjects[0]!.id,
      status: "completed",
      created_at: "2026-08-31T10:30:00Z",
      updated_at: "2026-08-31T10:31:00Z",
      total_pairs: 6,
      completed_pairs: 6,
      execution_mode: "mock",
      sample_count: 2,
      solution_count: 3,
      reference_only_count: 0,
      download_ready: true,
    },
  ] satisfies RunHistoryItem[];

  it("renders a full project run history with restore and delete actions", () => {
    const markup = renderToStaticMarkup(
      <RunsWorkspace
        project={mockProjects[0]!}
        runs={history}
        activeRunId={history[0]!.run_id}
        loading={false}
        error={null}
        restoringRunId={null}
        onRefresh={noop}
        onOpen={noop}
        onDelete={noop}
        onBuildRun={noop}
      />,
    );

    expect(markup).toContain("run-20260831-terminal");
    expect(markup).toContain("Project runs");
    expect(markup).toContain(">Open<");
    expect(markup).toContain(">Delete<");
    expect(markup).toContain(">Active<");
    expect((markup.match(/class="table-sort-button"/g) ?? []).length).toBe(7);
    expect(markup).toContain('aria-sort="descending"');
  });

  describe("home workspace", () => {
    it("renders a distinct product overview with clear next actions", () => {
      const markup = renderToStaticMarkup(
        <HomeWorkspace
          projects={mockProjects}
          project={mockProjects[0]!}
          solutions={mockSolutions}
          runState={null}
          runHistory={[]}
          onOpenDataset={noop}
          onCreateDataset={noop}
          onBuildRun={noop}
          onOpenEvaluation={noop}
          onOpenRuns={noop}
          onOpenReports={noop}
        />,
      );

      expect(markup).toContain("<h1>Receipt Bench</h1>");
      expect(markup).toContain("Current dataset");
      expect(markup).toContain("Start a comparison");
      expect(markup).toContain("Explore datasets");
      expect(markup).toContain("Configure run");
      expect(markup).not.toContain("Extractor performance");
    });

    describe("Admin Center", () => {
      const adminRuns: AdminRunInventory[] = [
        {
          run_id: "run-admin-complete",
          project_id: mockProjects[0]!.id,
          project_name: mockProjects[0]!.name,
          project_short_name: mockProjects[0]!.short_name,
          status: "completed",
          created_at: "2026-09-07T08:00:00Z",
          updated_at: "2026-09-07T08:01:00Z",
          total_pairs: 4,
          completed_pairs: 4,
          execution_mode: "mock",
          sample_count: 2,
          solution_count: 2,
          reference_only_count: 0,
          download_ready: true,
        },
      ];

      it("renders a sortable dataset inventory with protected built-ins", () => {
        const markup = renderToStaticMarkup(
          <AdminCenter
            client={new MockProductClient()}
            projects={mockProjects}
            project={mockProjects[0]!}
            solutions={mockSolutions}
            refreshToken={0}
            initialRuns={adminRuns}
            onDeleteDataset={noop}
            onDeleteRun={noop}
          />,
        );

        expect(markup).toContain("<h1>Admin Center</h1>");
        expect(markup).toContain("Same access as this site");
        expect(markup).toContain("Deployment inventory");
        expect(markup).toContain("Protected");
        expect((markup.match(/class=\"table-sort-button\"/g) ?? []).length).toBe(7);
      });

      it("shows terminal run deletion and keeps services read-only", () => {
        const runsMarkup = renderToStaticMarkup(
          <AdminCenter
            client={new MockProductClient()}
            projects={mockProjects}
            project={mockProjects[0]!}
            solutions={mockSolutions}
            refreshToken={0}
            initialSection="runs"
            initialRuns={adminRuns}
            onDeleteDataset={noop}
            onDeleteRun={noop}
          />,
        );
        const servicesMarkup = renderToStaticMarkup(
          <AdminCenter
            client={new MockProductClient()}
            projects={mockProjects}
            project={mockProjects[0]!}
            solutions={mockSolutions}
            refreshToken={0}
            initialSection="services"
            initialRuns={adminRuns}
            onDeleteDataset={noop}
            onDeleteRun={noop}
          />,
        );

        expect(runsMarkup).toContain("run-admin-complete");
        expect(runsMarkup).toContain(">Delete<");
        expect(servicesMarkup).toContain("Services are read-only here");
        expect(servicesMarkup).toContain("configuration and infrastructure");
        expect(servicesMarkup).not.toContain("Delete service");
      });

      it("does not enable dataset deletion until run inventory is known", () => {
        const customProject = {
          ...mockProjects[0]!,
          id: "custom-admin-project",
          name: "Custom admin project",
          short_name: "CUSTOM",
          kind: "custom" as const,
          dataset_state: "draft" as const,
          import_mode: "manual" as const,
        };
        const loadingMarkup = renderToStaticMarkup(
          <AdminCenter
            client={new MockProductClient()}
            projects={[customProject]}
            project={customProject}
            solutions={mockSolutions}
            refreshToken={0}
            onDeleteDataset={noop}
            onDeleteRun={noop}
          />,
        );
        const readyMarkup = renderToStaticMarkup(
          <AdminCenter
            client={new MockProductClient()}
            projects={[customProject]}
            project={customProject}
            solutions={mockSolutions}
            refreshToken={0}
            initialRuns={[]}
            onDeleteDataset={noop}
            onDeleteRun={noop}
          />,
        );
        const loadingRow = loadingMarkup.slice(
          loadingMarkup.indexOf("Custom admin project"),
          loadingMarkup.indexOf("</tr>", loadingMarkup.indexOf("Custom admin project")),
        );
        const readyRow = readyMarkup.slice(
          readyMarkup.indexOf("Custom admin project"),
          readyMarkup.indexOf("</tr>", readyMarkup.indexOf("Custom admin project")),
        );

        expect(loadingRow).toContain('disabled=""');
        expect(loadingRow).toContain("Wait for the run inventory");
        expect(readyRow).not.toContain('disabled=""');
      });
    });
  });

  describe("labelled dataset import", () => {
    it("defaults create dataset to the complete labelled ZIP fast path", () => {
      const markup = renderToStaticMarkup(
        <CustomProjectWizard
          solutions={mockSolutions}
          onCreate={async () => undefined}
          onImport={async () => undefined}
        />,
      );

      expect(markup).toContain("Import labelled ZIP");
      expect(markup).toContain("Import a complete labelled dataset");
      expect(markup).toContain("dataset.json");
      expect(markup).toContain("authoritative answer JSON");
      expect(markup).toContain("Build manually");
      expect(markup).not.toContain('aria-label="Project creation steps"');
    });
  });

  it("renders aggregate evidence in Reports and a useful no-selection state", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solution = mockSolutions[0]!;
    const result = makeMockRunResult(project, sample, solution, "report-run");
    const aggregate = buildProjectAggregate(
      project,
      "report-run",
      [sample],
      [solution],
      [result],
    );
    const report = renderToStaticMarkup(
      <ReportsWorkspace
        project={project}
        solutions={[solution]}
        runState={null}
        aggregate={aggregate}
        aggregateError={null}
        onOpenRuns={noop}
        onBuildRun={noop}
        onOpenEvaluation={noop}
        onOpenEvidence={noop}
      />,
    );
    const empty = renderToStaticMarkup(
      <ReportsWorkspace
        project={project}
        solutions={[solution]}
        runState={null}
        aggregate={null}
        aggregateError={null}
        onOpenRuns={noop}
        onBuildRun={noop}
        onOpenEvaluation={noop}
        onOpenEvidence={noop}
      />,
    );

    expect(report).toContain("Batch results");
    expect(report).toContain("report-run");
    expect(report).toContain("Solution quality");
    expect(report).toContain("Usage and performance");
    expect(report).toContain("Field quality detail");
    expect(report).toContain("P95 latency");
    expect((report.match(/class="table-sort-button"/g) ?? []).length).toBe(26);
    expect(report).toContain('aria-sort="ascending"');
    expect(empty).toContain("No report selected");
    expect(empty).toContain("View runs");
  });

  it("makes the live task matrix sortable by solution and every receipt", () => {
    const project = mockProjects[0]!;
    const run = manifest(
      [mockSamples[0]!.id, mockSamples[1]!.id],
      [mockSolutions[0]!.id],
    );
    const markup = renderToStaticMarkup(
      <LiveWorkspace
        project={project}
        runState={createInitialRunState(run, mockSolutions)}
        samples={mockSamples}
        solutions={mockSolutions}
        onBuildRun={noop}
        onOpenComparison={noop}
      />,
    );

    expect((markup.match(/class="table-sort-button"/g) ?? []).length).toBe(3);
    expect(markup).toContain('aria-sort="ascending"');
    expect(markup).toContain("Sort by solution or by progress");
  });
});

describe("evaluation studio inspector navigation and sortable performance table", () => {
  it("disables the receipt inspector jump with an explanatory title when no run exists", () => {
    const project = mockProjects[0]!;
    const client = { runDownloadUrl: () => "/download" } as never;
    const markup = renderToStaticMarkup(
      <EvaluationStudio
        client={client}
        project={project}
        samples={mockSamples}
        solutions={mockSolutions}
        runState={null}
        aggregate={null}
        aggregateError={null}
        selectedSampleId={mockSamples[0]!.id}
        selectedSolutionId={mockSolutions[0]!.id}
        onSampleChange={noop}
        onSolutionChange={noop}
        onBuildRun={noop}
      />,
    );
    const nav = markup.match(/<button[^>]*>\s*Receipt inspector/)?.[0] ?? "";

    expect(nav).toContain('disabled=""');
    expect(nav).toContain('aria-disabled="true"');
    expect(nav).toContain("Build a run before inspecting a receipt.");
  });

  it("defaults the extractor performance table to a single Field F1 descending sort marker", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solutions = mockSolutions.slice(0, 2);
    const run = manifest([sample.id], solutions.map((solution) => solution.id));
    const results = solutions.map((solution) =>
      makeMockRunResult(project, sample, solution, run.id),
    );
    const runState = stateWithResults(run, results);
    const aggregate = buildProjectAggregate(
      project,
      run.id,
      [sample],
      solutions,
      results,
    );
    const client = { runDownloadUrl: () => "/download" } as never;
    const markup = renderToStaticMarkup(
      <EvaluationStudio
        client={client}
        project={project}
        samples={[sample]}
        solutions={solutions}
        runState={runState}
        aggregate={aggregate}
        aggregateError={null}
        selectedSampleId={sample.id}
        selectedSolutionId={solutions[0]!.id}
        onSampleChange={noop}
        onSolutionChange={noop}
        onBuildRun={noop}
      />,
    );

    expect(markup).toContain("Field F1");
    expect(markup).not.toContain("Overall accuracy");
    const performanceTable = markup.slice(
      markup.indexOf('<table class="performance-table">'),
      markup.indexOf("</table>", markup.indexOf('<table class="performance-table">')),
    );
    expect(
      performanceTable.match(/aria-sort="(?:ascending|descending)"/g) ?? [],
    ).toEqual([
      'aria-sort="descending"',
    ]);
    const fieldF1Header = markup.slice(
      markup.indexOf(">Field F1<") - 260,
      markup.indexOf(">Field F1<"),
    );
    expect(fieldF1Header).toContain('aria-sort="descending"');
    expect(markup).toContain(
      "Field F1 balances precision and recall of extracted fields",
    );
  });

  it("only renders OCR-family columns when present and never conflates Field F1 with OCR metrics", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const ocrTextSolution = mockSolutions.find((item) => item.id === "offline-ocr-text")!;
    const nativeMeasurable = mockSolutions.find(
      (item) => item.id === "offline-native-measurable",
    )!;
    const solutions = [ocrTextSolution, nativeMeasurable];
    const run = manifest(
      [sample.id],
      solutions.map((solution) => solution.id),
    );
    const results = solutions.map((solution) =>
      makeMockRunResult(project, sample, solution, run.id),
    );
    const runState = stateWithResults(run, results);
    const aggregate = buildProjectAggregate(
      project,
      run.id,
      [sample],
      solutions,
      results,
    );
    const client = { runDownloadUrl: () => "/download" } as never;
    const markup = renderToStaticMarkup(
      <EvaluationStudio
        client={client}
        project={project}
        samples={[sample]}
        solutions={solutions}
        runState={runState}
        aggregate={aggregate}
        aggregateError={null}
        selectedSampleId={sample.id}
        selectedSolutionId={solutions[0]!.id}
        onSampleChange={noop}
        onSolutionChange={noop}
        onBuildRun={noop}
      />,
    );

    expect(markup).toContain(">Labeled coverage<");
    expect(markup).toContain(">Token recall<");
    expect(markup).toContain(">CER<");
    expect(markup).toContain(">WER<");
    expect(markup).toContain(
      "Labeled-field coverage: share of fields present in the golden answers",
    );
    expect(markup).toContain("Reference token recall: share of ground-truth OCR tokens");
    expect(markup).toContain("Character error rate: character-level edit distance");
    expect(markup).toContain("Word error rate: word-level edit distance");

    const performanceSection = markup.slice(
      markup.indexOf('<table class="performance-table"'),
      markup.indexOf("</table>", markup.indexOf('<table class="performance-table"')) + "</table>".length,
    );
    const bodyRows = performanceSection
      .split("<tr")
      .filter((fragment) => fragment.includes('scope="row"'));
    expect(bodyRows).toHaveLength(2);
    const nativeRow = bodyRows.find((fragment) => fragment.includes("Offline native prebuilt"))!;
    const ocrRow = bodyRows.find((fragment) => fragment.includes("Offline OCR text"))!;

    // The structured row reports Field F1/coverage but never OCR metrics.
    expect(nativeRow).toContain("80.0%");
    expect(nativeRow).toContain("66.7%");
    const nativeOcrCells = nativeRow.slice(nativeRow.indexOf("66.7%"));
    expect(nativeOcrCells).toContain("<td>—</td>");

    // The ocr_text row reports token recall/CER/WER but never Field F1/coverage.
    expect(ocrRow).toContain("92.0%");
    expect(ocrRow).toContain("4.0%");
    expect(ocrRow).toContain("6.0%");
    const ocrStructuredCells = ocrRow.slice(0, ocrRow.indexOf("92.0%"));
    expect((ocrStructuredCells.match(/>—</g) ?? []).length).toBeGreaterThanOrEqual(2);
  });
});

describe("destructive-action confirmation", () => {
  it("names the exact run or project, surfaces errors, and never dismisses itself", () => {
    const runMarkup = renderToStaticMarkup(
      <ConfirmDialog
        open
        title="Delete run"
        description={
          <>
            Delete <code>run-20260830-newest</code>? This cannot be undone.
          </>
        }
        confirmLabel="Delete run"
        error="The server rejected the request: run is still in progress."
        onConfirm={noop}
        onCancel={noop}
      />,
    );

    expect(runMarkup).toContain("run-20260830-newest");
    expect(runMarkup).toContain(
      "The server rejected the request: run is still in progress.",
    );
    expect(runMarkup).toContain('role="alertdialog"');
    expect(runMarkup).toContain('role="alert"');
    expect(runMarkup).toContain("Delete run");

    const closedMarkup = renderToStaticMarkup(
      <ConfirmDialog
        open={false}
        title="Delete run"
        description="Delete this run?"
        confirmLabel="Delete run"
        onConfirm={noop}
        onCancel={noop}
      />,
    );
    expect(closedMarkup).toBe("");
  });
});

describe("internal model disclosure", () => {
  it("lists internal models per solution in the Models & services catalog, distinct from the normalizer", () => {
    const project = mockProjects[0]!;
    const markup = renderToStaticMarkup(
      <ModelsCatalog project={project} solutions={mockSolutions} />,
    );

    // offline-native-measurable discloses two internal models.
    const prebuiltCard = markup.slice(
      markup.indexOf("Offline native prebuilt"),
      markup.indexOf("</li>", markup.indexOf("Offline native prebuilt")),
    );
    expect(prebuiltCard).toContain(">Internal models<");
    expect(prebuiltCard).toContain("GPT-5.2, Text Embedding 3 Large");
    expect(prebuiltCard).toContain("None · standalone output");
    expect(prebuiltCard).toContain("intrinsically produced with");

    // offline-direct discloses none.
    const directCard = markup.slice(
      markup.indexOf("Offline direct"),
      markup.indexOf("</li>", markup.indexOf("Offline direct")),
    );
    expect(directCard).toContain("None disclosed");
    expect(directCard).not.toContain("intrinsically produced with");
  });

  it("discloses external normalizer assistance and intrinsic internal models before confirming a run", () => {
    const markup = renderToStaticMarkup(
      <RunBuilder
        project={mockProjects[0]!}
        samples={mockSamples}
        solutions={mockSolutions}
        selectedSampleIds={new Set(["offline-001"])}
        selectedSolutionIds={
          new Set(["offline-direct", "offline-reference", "offline-native-measurable"])
        }
        schemaConfirmed
        promptConfirmed
        executionMode="mock"
        launchPending={false}
        onSampleToggle={noop}
        onSolutionToggle={noop}
        onSelectSamples={noop}
        onSelectSolutions={noop}
        onSchemaConfirm={noop}
        onPromptConfirm={noop}
        onLaunch={noop}
      />,
    );
    const assistanceSection = markup.slice(markup.indexOf("Model assistance"));

    // offline-direct has neither a normalizer nor internal models, so it must
    // not appear in the disclosed-assistance list at all.
    expect(assistanceSection.slice(0, assistanceSection.indexOf("</details>"))).not.toContain(
      "Offline direct",
    );
    expect(assistanceSection).toContain("Offline native reference");
    expect(assistanceSection).toContain("No external normalizer");
    expect(assistanceSection).toContain("Intrinsic to this service:");
    expect(assistanceSection).toContain("GPT-5.4 Mini");
    expect(assistanceSection).toContain("Offline native prebuilt");
    expect(assistanceSection).toContain("GPT-5.2, Text Embedding 3 Large");
  });

  it("discloses a per-project custom analyzer registration requirement even with no normalizer or internal models", () => {
    const markup = renderToStaticMarkup(
      <RunBuilder
        project={mockProjects[0]!}
        samples={mockSamples}
        solutions={mockSolutions}
        selectedSampleIds={new Set(["offline-001"])}
        selectedSolutionIds={new Set(["offline-unsupported"])}
        schemaConfirmed
        promptConfirmed
        executionMode="mock"
        launchPending={false}
        onSampleToggle={noop}
        onSolutionToggle={noop}
        onSelectSamples={noop}
        onSelectSolutions={noop}
        onSchemaConfirm={noop}
        onPromptConfirm={noop}
        onLaunch={noop}
      />,
    );
    const assistanceSection = markup.slice(markup.indexOf("Model assistance"));

    expect(assistanceSection).toContain("Unavailable custom analyzer");
    expect(assistanceSection).toContain(
      "Automatically configures a schema-hash-keyed analyzer",
    );
    expect(assistanceSection).toContain("No training or fine-tuning");
  });
});

describe("schema origin disclosure", () => {
  it("renders the API-supplied exact schema derivation text, not a hard-coded built-in/custom sentence", () => {
    const datasetDerivedProject = mockProjects[0]!;
    const markup = renderToStaticMarkup(
      <ContractDisclosure project={datasetDerivedProject} />,
    );
    expect(markup).toContain("ground-truth labels for vendor, date, and total");
    expect(markup).not.toContain("This built-in schema is a repository-authored contract");

    const userSuppliedProject = {
      ...datasetDerivedProject,
      kind: "custom" as const,
      schema: {
        ...datasetDerivedProject.schema,
        origin: "user_supplied" as const,
        derivation: null,
      },
    };
    const customMarkup = renderToStaticMarkup(
      <ContractDisclosure project={userSuppliedProject} />,
    );
    expect(customMarkup).toContain("authored by the project owner");
  });
});

describe("mode-scoped prompt contract disclosure", () => {
  it("renders a distinct, inspectable prompt contract per solution mode with normalizer identity and user template", () => {
    const project = mockProjects[0]!;
    const markup = renderToStaticMarkup(<ContractDisclosure project={project} />);

    expect(markup).toContain("Prompt · Direct multimodal");
    expect(markup).toContain("Prompt · OCR + normalizer");
    expect(markup).toContain("No normalizer stage for this mode");
    expect(markup).toContain("Normalizer · GPT-5.6 Luna");
    expect(markup).toContain("User-editable template");
    expect(markup).toContain("Exact, repository-authored prompt contract for this mode.");

    const directSection = markup.slice(
      markup.indexOf("Prompt · Direct multimodal"),
      markup.indexOf("Prompt · OCR + normalizer"),
    );
    expect(directSection).not.toContain("Normalizer · GPT-5.6 Luna");
  });

  it("discloses generated prompt contracts for a custom project instead of exact repository-authored ones", () => {
    const project = mockProjects[0]!;
    const customProject = { ...project, kind: "custom" as const };
    const markup = renderToStaticMarkup(<ContractDisclosure project={customProject} />);
    expect(markup).toContain("Generated for this mode from the prompt authored");
    expect(markup).not.toContain("Exact, repository-authored");
  });
});
