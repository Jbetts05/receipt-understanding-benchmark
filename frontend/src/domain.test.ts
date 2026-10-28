import { describe, expect, it } from "vitest";

import {
  buildProjectAggregate,
  compareJson,
  compareProjectJson,
  configurationPresentation,
  getCompatibility,
  getSynchronizedHighlight,
  isTerminalRunStatus,
  metricFamilyForSolution,
  metricFamilyLabel,
  modelDisplayName,
  orderedPromptContracts,
  presentationForCompatibility,
  promptContractOriginNote,
  promptModeLabel,
  qualityLanguage,
  requiresCustomAnalyzerRegistration,
  runLaunchGuard,
  schemaOriginNote,
  scopeProjectAggregateToManifest,
  solutionTypeLabel,
} from "./domain";
import {
  MOCK_RUN_ID,
  makeMockRunResult,
  mockProjects,
  mockSamples,
  mockSolutions,
} from "./mockData";
import type { JsonObject, Project, ReceiptSample, RunResult } from "./types";

describe("project-native comparison", () => {
  it("classifies exact, wrong, missing, and hallucinated fields", () => {
    const gold: JsonObject = {
      vendor: "North Shop",
      date: "2026-08-30",
      total: 10,
    };
    const actual: JsonObject = {
      vendor: " north   shop ",
      total: 10.4,
      confidence: 0.9,
    };

    expect(
      Object.fromEntries(
        compareJson(gold, actual).map((field) => [field.path, field.state]),
      ),
    ).toEqual({
      confidence: "extra",
      date: "missing",
      total: "wrong",
      vendor: "matched",
    });
  });

  it("compares CORD-like arrays without treating item order as quality", () => {
    const project = {
      ...mockProjects[0]!,
      id: "cord-fixture",
      normalization_profile: "cord-v2",
    };
    const gold = { menu: [{ nm: "Tea" }, { nm: "Cake" }] };
    const actual = { menu: [{ nm: "Cake" }, { nm: "Tea" }] };

    expect(
      compareProjectJson(project, gold, actual).every(
        (field) => field.state === "matched",
      ),
    ).toBe(true);
  });

  it("synchronizes one selected field across every output", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solutions = mockSolutions.slice(0, 2);
    const results = solutions.map((solution) =>
      makeMockRunResult(project, sample, solution),
    );

    const highlight = getSynchronizedHighlight("total", sample.gold, results);
    expect(highlight.gold).toBe(24.8);
    expect(highlight.outputs.size).toBe(2);
    expect(highlight.outputs.get("offline-direct")?.state).toBe("matched");
    expect(highlight.outputs.get("offline-ocr")?.state).toBe("wrong");
  });
});

describe("server compatibility and launch guards", () => {
  it("uses declared comparable, reference-only, and unsupported paths", () => {
    const project = mockProjects[0]!;
    expect(getCompatibility(project, mockSolutions[0]!)).toBe("full");
    expect(getCompatibility(project, mockSolutions[2]!)).toBe(
      "partial_reference",
    );
    expect(getCompatibility(project, mockSolutions[3]!)).toBe("unsupported");
  });

  it("blocks unsupported solutions and unconfirmed contracts", () => {
    const project = mockProjects[0]!;
    const blocked = runLaunchGuard(
      {
        sampleIds: ["offline-001"],
        solutionIds: ["offline-unsupported"],
        schemaConfirmed: false,
        promptConfirmed: false,
      },
      project,
      mockSolutions,
    );
    expect(blocked.allowed).toBe(false);
    expect(blocked.reasons).toEqual(
      expect.arrayContaining([
        "Remove unsupported solutions before launching.",
        "Confirm the pinned schema version.",
        "Confirm the pinned prompt version.",
      ]),
    );
  });

  it("presents assisted, standalone, and unavailable configurations explicitly, never implying poor quality", () => {
    const project = mockProjects[0]!;

    const assisted = configurationPresentation(project, mockSolutions[1]!);
    expect(assisted.label).toBe("Assisted · OCR + GPT-5.6 Luna");
    expect(assisted.detail).not.toMatch(/poor|worse|inferior/i);

    const standalone = configurationPresentation(project, mockSolutions[2]!);
    expect(standalone.label).toBe("Standalone native output");
    expect(standalone.detail).not.toMatch(/poor|worse|inferior/i);

    const unavailable = configurationPresentation(project, mockSolutions[3]!);
    expect(unavailable.label).toBe("Custom analyzer unavailable · not configured");
    expect(unavailable.detail).toBe(
      "This analyzer requires a project-specific registration.",
    );
  });

  it("only treats completed or failed runs as terminal for deletion", () => {
    expect(isTerminalRunStatus("completed")).toBe(true);
    expect(isTerminalRunStatus("failed")).toBe(true);
    expect(isTerminalRunStatus("running")).toBe(false);
    expect(isTerminalRunStatus("queued")).toBe(false);
  });
});

describe("project-separated mock aggregates", () => {
  it("never reports quality without gold while retaining operational measures", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[1]!;
    const solution = mockSolutions[0]!;
    const result = makeMockRunResult(project, sample, solution);
    const aggregate = buildProjectAggregate(
      project,
      MOCK_RUN_ID,
      [sample],
      [solution],
      [result],
    );
    const row = aggregate.solutions[0]!;

    expect(aggregate.has_gold).toBe(false);
    expect(row.normalized_field_f1.value).toBeNull();
    expect(row.whole_answer_exactness.value).toBeNull();
    expect(row.success_rate.value).toBe(1);
    expect(row.input_tokens.value).toBe(420);
    expect(row.output_tokens.value).toBe(84);
    expect(row.total_tokens.value).toBe(504);
    expect(row.total_pages.value).toBe(1);
    expect(row.total_attempts.value).toBe(1);
    expect(row.p95_latency.value).toBe(row.median_latency.value);
    expect(row.cost_scope).toBe("complete");
    expect(qualityLanguage(false)).not.toMatch(/accurate|correct/i);
  });

  it("scores failed gold pairs as zero rather than dropping them", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solution = mockSolutions[0]!;
    const failed: RunResult = {
      ...makeMockRunResult(project, sample, solution),
      status: "failed",
      parsed_output: null,
      comparisons: [],
      error: { code: "failed", message: "failed" },
    };
    const row = buildProjectAggregate(
      project,
      MOCK_RUN_ID,
      [sample],
      [solution],
      [failed],
    ).solutions[0]!;

    expect(row.normalized_field_f1.value).toBe(0);
    expect(row.whole_answer_exactness.value).toBe(0);
    expect(row.success_rate.value).toBe(0);
    expect(row.cost_scope).toBe("complete");
    expect(row.total_attempts.value).toBe(1);
    expect(
      buildProjectAggregate(
        project,
        MOCK_RUN_ID,
        [sample],
        [solution],
        [failed],
      ).fields.map((field) => ({
        path: field.path,
        missing: field.missing,
        f1: field.normalized_field_f1.value,
      })),
    ).toEqual([
      { path: "date", missing: 1, f1: 0 },
      { path: "total", missing: 1, f1: 0 },
      { path: "vendor", missing: 1, f1: 0 },
    ]);
  });

  it("filters mixed project and run results from evidence", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solution = mockSolutions[0]!;
    const otherProject: Project = {
      ...project,
      id: "other-project",
      short_name: "OTHER",
      compatibility: project.compatibility.map((item) => ({ ...item })),
    };
    const otherSample: ReceiptSample = {
      ...sample,
      id: "other-001",
      project_id: otherProject.id,
    };
    const mixedResults: RunResult[] = [
      makeMockRunResult(project, sample, solution),
      {
        ...makeMockRunResult(otherProject, otherSample, solution),
        run_id: MOCK_RUN_ID,
      },
      {
        ...makeMockRunResult(project, sample, solution),
        run_id: "different-run",
      },
    ];

    const aggregate = buildProjectAggregate(
      project,
      MOCK_RUN_ID,
      [sample, otherSample],
      [solution],
      mixedResults,
    );
    expect(aggregate.solutions[0]!.success_rate.evidence).toHaveLength(1);
    expect(
      aggregate.solutions[0]!.success_rate.evidence[0]?.sample_id,
    ).toBe("offline-001");
  });

  it("never scores contract-excluded rows and counts only few-shot exclusions", () => {
    const project = mockProjects[0]!;
    const solution = mockSolutions[0]!;
    const excluded: ReceiptSample = {
      ...mockSamples[0]!,
      id: "offline-excluded",
      prompt_example: true,
      scoring_role: "excluded",
      exclusion_reason: "Duplicate receipt.",
    };
    const fewShot: ReceiptSample = {
      ...mockSamples[0]!,
      id: "offline-few-shot",
      prompt_example: true,
      scoring_role: "few_shot",
    };
    const result = makeMockRunResult(project, excluded, solution);
    const aggregate = buildProjectAggregate(
      project,
      MOCK_RUN_ID,
      [excluded, fewShot],
      [solution],
      [result],
    );

    expect(aggregate.has_gold).toBe(false);
    expect(aggregate.scored_sample_count).toBe(0);
    expect(aggregate.excluded_prompt_examples).toBe(1);
    expect(aggregate.solutions[0]?.normalized_field_f1.value).toBeNull();
  });

  it("scopes server aggregate evidence to the immutable manifest", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solution = mockSolutions[0]!;
    const result = makeMockRunResult(project, sample, solution, MOCK_RUN_ID);
    const aggregate = buildProjectAggregate(
      project,
      MOCK_RUN_ID,
      [sample],
      [solution],
      [result],
    );
    aggregate.solutions[0]!.success_rate.evidence.push({
      sample_id: "outside-manifest",
      solution_id: solution.id,
      result_url: "/foreign",
    });
    const scoped = scopeProjectAggregateToManifest(aggregate, {
      id: MOCK_RUN_ID,
      project_id: project.id,
      sample_ids: [sample.id],
      solution_ids: [solution.id],
      schema_version: "1",
      prompt_version: "1",
      status: "completed",
      total_pairs: 1,
      completed_pairs: 1,
      created_at: "2026-08-30T00:00:00Z",
      execution_mode: "mock",
      reference_only_solution_ids: [],
    });

    expect(scoped?.solutions[0]?.success_rate.evidence).toHaveLength(1);
    expect(scoped?.solutions[0]?.success_rate.evidence[0]?.sample_id).toBe(
      sample.id,
    );
  });
});

describe("OCR metric family", () => {
  it("classifies ocr_text solutions distinctly from every structured kind", () => {
    const ocrTextSolution = mockSolutions.find((solution) => solution.kind === "ocr_text")!;
    const structuredSolution = mockSolutions[0]!;

    expect(metricFamilyForSolution(ocrTextSolution)).toBe("ocr_text");
    expect(metricFamilyForSolution(structuredSolution)).toBe("structured");
    expect(metricFamilyLabel("ocr_text")).not.toBe(metricFamilyLabel("structured"));
  });

  it("presents a standalone OCR text label distinct from standalone native output", () => {
    const project = mockProjects[0]!;
    const ocrTextSolution = mockSolutions.find((solution) => solution.id === "offline-ocr-text")!;

    const presentation = configurationPresentation(project, ocrTextSolution);
    expect(presentation.label).toBe("Standalone OCR text output");
    expect(presentation.detail).not.toMatch(/poor|worse|inferior/i);
  });

  it("scores ocr_text rows on reference-token recall/CER/WER and never on Field F1", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solution = mockSolutions.find((item) => item.id === "offline-ocr-text")!;
    const result = makeMockRunResult(project, sample, solution);
    const row = buildProjectAggregate(
      project,
      MOCK_RUN_ID,
      [sample],
      [solution],
      [result],
    ).solutions[0]!;

    expect(row.metric_family).toBe("ocr_text");
    expect(row.normalized_field_f1.value).toBeNull();
    expect(row.whole_answer_exactness.value).toBeNull();
    expect(row.labeled_field_coverage).toBeNull();
    expect(row.reference_token_recall?.value).toBeCloseTo(46 / 50, 5);
    expect(row.reference_token_recall?.polarity).toBe("higher_better");
    expect(row.character_error_rate?.value).toBeCloseTo(0.04, 5);
    expect(row.character_error_rate?.polarity).toBe("lower_better");
    expect(row.word_error_rate?.value).toBeCloseTo(0.06, 5);
  });

  it("counts a now-measurable prebuilt's missing field as a miss in Field F1 and schema coverage", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solution = mockSolutions.find((item) => item.id === "offline-native-measurable")!;
    const result = makeMockRunResult(project, sample, solution);
    // The fixture omits `total` for this solution/sample pair to simulate a
    // prebuilt analyzer that does not populate every schema field.
    expect((result.parsed_output as { total?: number } | null)?.total).toBeUndefined();

    const row = buildProjectAggregate(
      project,
      MOCK_RUN_ID,
      [sample],
      [solution],
      [result],
    ).solutions[0]!;

    expect(row.metric_family).toBe("structured");
    expect(row.reference_token_recall).toBeNull();
    expect(row.labeled_field_coverage?.value).toBeCloseTo(2 / 3, 5);
    expect(row.normalized_field_f1.value).toBeCloseTo(0.8, 5);
  });

  it("scopes the new nullable OCR-family measures to the immutable manifest", () => {
    const project = mockProjects[0]!;
    const sample = mockSamples[0]!;
    const solution = mockSolutions.find((item) => item.id === "offline-ocr-text")!;
    const result = makeMockRunResult(project, sample, solution, MOCK_RUN_ID);
    const aggregate = buildProjectAggregate(
      project,
      MOCK_RUN_ID,
      [sample],
      [solution],
      [result],
    );
    aggregate.solutions[0]!.reference_token_recall!.evidence.push({
      sample_id: "outside-manifest",
      solution_id: solution.id,
      result_url: "/foreign",
    });
    const scoped = scopeProjectAggregateToManifest(aggregate, {
      id: MOCK_RUN_ID,
      project_id: project.id,
      sample_ids: [sample.id],
      solution_ids: [solution.id],
      schema_version: "1",
      prompt_version: "1",
      status: "completed",
      total_pairs: 1,
      completed_pairs: 1,
      created_at: "2026-08-30T00:00:00Z",
      execution_mode: "mock",
      reference_only_solution_ids: [],
    });

    expect(scoped?.solutions[0]?.reference_token_recall?.evidence).toHaveLength(1);
    expect(scoped?.solutions[0]?.reference_token_recall?.evidence[0]?.sample_id).toBe(
      sample.id,
    );
    expect(scoped?.solutions[0]?.labeled_field_coverage).toBeNull();
  });
});

describe("internal model disclosure", () => {
  it("never hides model(s) intrinsic to a service's own response, distinct from the external normalizer", () => {
    const withOneInternalModel = mockSolutions.find(
      (solution) => solution.id === "offline-reference",
    )!;
    const withTwoInternalModels = mockSolutions.find(
      (solution) => solution.id === "offline-native-measurable",
    )!;
    const withNone = mockSolutions.find((solution) => solution.id === "offline-direct")!;

    expect(withOneInternalModel.internal_models).toEqual(["gpt-5.4-mini"]);
    expect(withOneInternalModel.normalizer).toBeNull();
    expect(withTwoInternalModels.internal_models).toEqual(["gpt-5.2", "text-embedding-3-large"]);
    expect(withNone.internal_models).toEqual([]);
  });

  it("formats non-GPT internal model ids (e.g. embedding models) the same as GPT ids", () => {
    expect(modelDisplayName("gpt-5.4-mini")).toBe("GPT-5.4 Mini");
    expect(modelDisplayName("gpt-5.2")).toBe("GPT-5.2");
    expect(modelDisplayName("text-embedding-3-large")).toBe("Text Embedding 3 Large");
  });
});

describe("native_pipeline kind", () => {
  it("scores native_pipeline rows on Field F1, never the OCR metric family", () => {
    const solution = mockSolutions.find((item) => item.kind === "native_pipeline")!;
    expect(metricFamilyForSolution(solution)).toBe("structured");
    expect(solutionTypeLabel(solution)).toBe("Extraction API + GPT-5.6 Luna");
  });

  it("labels a native_pipeline solution's assistance as an assisted Extraction API, never as OCR", () => {
    const project = mockProjects[0]!;
    const solution = mockSolutions.find((item) => item.kind === "native_pipeline")!;
    const presentation = configurationPresentation(project, solution);
    expect(presentation.label).toBe("Assisted · Extraction API + GPT-5.6 Luna");
    expect(presentation.label).not.toContain("OCR");
  });

  it("distinguishes the unsupported label for missing OCR reference transcription from an unregistered custom analyzer", () => {
    const ocrTextSolution = mockSolutions.find((item) => item.kind === "ocr_text")!;
    const nativeSolution = mockSolutions.find((item) => item.kind === "native")!;
    expect(presentationForCompatibility("unsupported", ocrTextSolution, null).label).toBe(
      "OCR transcription unavailable · not configured",
    );
    expect(presentationForCompatibility("unsupported", nativeSolution, null).label).toBe(
      "Custom analyzer unavailable · not configured",
    );
  });
});

describe("custom-analyzer registration", () => {
  it("reads the API-supplied requires_project_analyzer field directly, never inferring it from compatibility state", () => {
    const requiresRegistration = mockSolutions.find(
      (solution) => solution.id === "offline-unsupported",
    )!;
    const alwaysAvailable = mockSolutions.find(
      (solution) => solution.id === "offline-native-measurable",
    )!;
    expect(requiresRegistration.requires_project_analyzer).toBe(true);
    expect(alwaysAvailable.requires_project_analyzer).toBe(false);
    expect(requiresCustomAnalyzerRegistration(requiresRegistration)).toBe(true);
    expect(requiresCustomAnalyzerRegistration(alwaysAvailable)).toBe(false);
    // Flipping the field, not the compatibility map, changes the result.
    expect(
      requiresCustomAnalyzerRegistration({
        ...alwaysAvailable,
        requires_project_analyzer: true,
      }),
    ).toBe(true);
  });
});

describe("schema origin disclosure", () => {
  it("shows the API-supplied exact derivation text verbatim, never a hard-coded kind-based sentence", () => {
    const sroieLikeSchema = {
      id: "sroie-schema",
      version: "2.0.0",
      hash: "sha256:sroie",
      json_schema: { type: "object" as const },
      origin: "dataset_derived" as const,
      derivation:
        "Derived from SROIE's ground-truth entity labels: company, address, date, and total.",
    };
    const cordLikeSchema = {
      ...sroieLikeSchema,
      id: "cord-schema",
      derivation:
        "Derived from CORD's ground-truth menu, subtotal, and total line-item labels.",
    };
    expect(schemaOriginNote(sroieLikeSchema)).toBe(
      "Derived from SROIE's ground-truth entity labels: company, address, date, and total. Every receipt in the project is scored against the same pinned contract.",
    );
    expect(schemaOriginNote(cordLikeSchema)).toBe(
      "Derived from CORD's ground-truth menu, subtotal, and total line-item labels. Every receipt in the project is scored against the same pinned contract.",
    );
    // Distinct provenance text for two dataset_derived schemas proves the
    // note is driven by API data, not a single hard-coded "built-in" string.
    expect(schemaOriginNote(sroieLikeSchema)).not.toBe(schemaOriginNote(cordLikeSchema));
  });

  it("falls back to an origin-driven generic note when the API supplies no exact derivation text", () => {
    const datasetDerivedNoDetail = {
      id: "s",
      version: "1.0.0",
      hash: "sha256:s",
      json_schema: { type: "object" as const },
      origin: "dataset_derived" as const,
      derivation: null,
    };
    const userSupplied = {
      ...datasetDerivedNoDetail,
      origin: "user_supplied" as const,
    };
    expect(schemaOriginNote(datasetDerivedNoDetail)).toContain(
      "derived from this dataset's ground-truth labels",
    );
    expect(schemaOriginNote(userSupplied)).toContain(
      "authored by the project owner",
    );
  });

  it("uses the mock built-in project's own dataset_derived schema and derivation text, not project.kind", () => {
    const project = mockProjects[0]!;
    expect(project.schema.origin).toBe("dataset_derived");
    expect(project.schema.derivation).not.toBeNull();
    expect(schemaOriginNote(project.schema)).toContain(project.schema.derivation!);
  });
});

describe("mode-scoped prompt contracts", () => {
  it("pins one prompt contract per solution mode, never a single merged prompt", () => {
    const project = mockProjects[0]!;
    const contracts = orderedPromptContracts(project);
    expect(contracts).toHaveLength(2);
    expect(contracts.map((contract) => contract.mode)).toEqual([
      "direct-multimodal",
      "ocr-normalizer",
    ]);
  });

  it("labels each mode explicitly and discloses the normalizer only for the OCR-normalizer contract", () => {
    const project = mockProjects[0]!;
    const [direct, ocrNormalizer] = orderedPromptContracts(project);
    expect(promptModeLabel(direct!.mode)).toBe("Direct multimodal");
    expect(promptModeLabel(ocrNormalizer!.mode)).toBe("OCR + normalizer");
    expect(direct!.normalizer_model).toBeNull();
    expect(ocrNormalizer!.normalizer_model).toBe("gpt-5.6-luna");
  });

  it("orders prompt contracts by mode regardless of the API's original array order", () => {
    const project = mockProjects[0]!;
    const reversed: Project = {
      ...project,
      prompt_contracts: [...project.prompt_contracts].reverse(),
    };
    expect(orderedPromptContracts(reversed).map((contract) => contract.mode)).toEqual([
      "direct-multimodal",
      "ocr-normalizer",
    ]);
  });

  it("discloses exact repository-authored contracts for built-in projects and generated contracts for custom projects", () => {
    const builtIn = mockProjects[0]!;
    const custom: Project = { ...builtIn, kind: "custom" };
    expect(promptContractOriginNote(builtIn)).toContain("Exact, repository-authored");
    expect(promptContractOriginNote(custom)).toContain("Generated for this mode");
  });
});
