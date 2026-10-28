import { configurationPresentation, getCompatibility, modelDisplayName, runLaunchGuard } from "../domain";
import type { ExecutionMode, Project, ReceiptSample, Solution } from "../types";
import { CheckIcon, PlayIcon } from "./Icons";
import { ContractDisclosure } from "./ContractDisclosure";
import { ProjectHeader } from "./ProjectHeader";
import { ReceiptPreview } from "./ReceiptPreview";

const kindLabels: Record<
  Exclude<Solution["kind"], "ocr_pipeline" | "ocr_text" | "native_pipeline">,
  string
> = {
  direct: "Direct multimodal",
  native: "Native extraction",
};

const QUICK_SELECTION_LIMIT = 5;

export function quickSelectSolutionIds(
  project: Project,
  solutions: Solution[],
): string[] {
  return solutions
    .map((solution, catalogIndex) => ({ solution, catalogIndex }))
    .filter(
      ({ solution }) => getCompatibility(project, solution) === "full",
    )
    .sort(
      (left, right) =>
        left.solution.order - right.solution.order ||
        left.catalogIndex - right.catalogIndex,
    )
    .slice(0, QUICK_SELECTION_LIMIT)
    .map(({ solution }) => solution.id);
}

/** Legend for a group of solutions that are always assisted by a normalizer. */
function assistedGroupLegend(
  groupSolutions: Solution[],
  serviceNoun: string,
  pluralLabel: string,
): string {
  const normalizers = new Set(groupSolutions.map((solution) => solution.normalizer));
  if (normalizers.size === 1) {
    const [only] = normalizers;
    return only === null || only === undefined
      ? pluralLabel
      : `${serviceNoun} + ${modelDisplayName(only)}`;
  }
  return `${pluralLabel} · mixed normalizers`;
}

export function RunBuilder({
  project,
  samples,
  solutions,
  selectedSampleIds,
  selectedSolutionIds,
  schemaConfirmed,
  promptConfirmed,
  executionMode,
  launchPending,
  onSampleToggle,
  onSolutionToggle,
  onSelectSamples,
  onSelectSolutions,
  onSchemaConfirm,
  onPromptConfirm,
  onLaunch,
}: {
  project: Project;
  samples: ReceiptSample[];
  solutions: Solution[];
  selectedSampleIds: Set<string>;
  selectedSolutionIds: Set<string>;
  schemaConfirmed: boolean;
  promptConfirmed: boolean;
  executionMode: ExecutionMode;
  launchPending: boolean;
  onSampleToggle: (sampleId: string) => void;
  onSolutionToggle: (solutionId: string) => void;
  onSelectSamples: (sampleIds: string[]) => void;
  onSelectSolutions: (solutionIds: string[]) => void;
  onSchemaConfirm: (checked: boolean) => void;
  onPromptConfirm: (checked: boolean) => void;
  onLaunch: () => void;
}) {
  const runnableSampleIds = samples
    .filter(
      (sample) =>
        sample.scoring_role === "scored" && selectedSampleIds.has(sample.id),
    )
    .map((sample) => sample.id);
  const guard = runLaunchGuard(
    {
      sampleIds: runnableSampleIds,
      solutionIds: [...selectedSolutionIds],
      schemaConfirmed,
      promptConfirmed,
    },
    project,
    solutions,
  );
  const groups = (["direct", "native", "ocr_pipeline", "native_pipeline", "ocr_text"] as const)
    .map((kind) => ({
      kind,
      solutions: solutions.filter((solution) => solution.kind === kind),
    }))
    .filter((group) => group.solutions.length > 0);
  const disclosedSelectedSolutions = solutions.filter(
    (solution) =>
      selectedSolutionIds.has(solution.id) &&
      (solution.normalizer !== null ||
        solution.internal_models.length > 0 ||
        solution.requires_project_analyzer),
  );
  const quickSolutionIds = quickSelectSolutionIds(project, solutions);
  const quickSelectionLabel =
    quickSolutionIds.length === QUICK_SELECTION_LIMIT
      ? "Quick five"
      : `Quick ${quickSolutionIds.length}`;

  return (
    <div className="view-shell run-builder-view">
      <ProjectHeader
        project={project}
        eyebrow="Run builder"
        title="Pin the experiment"
        description="Choose evidence and solution paths. The schema and prompt become immutable when the run starts."
        compact
      />

      <div className="builder-layout">
        <section className="builder-section" aria-labelledby="receipt-selection-heading">
          <div className="section-title-row">
            <div>
              <span className="step-index">01</span>
              <h2 id="receipt-selection-heading">Receipts</h2>
              <p>Prompt examples and excluded duplicates remain inspectable but cannot run.</p>
            </div>
            <div className="inline-actions">
              <button
                className="text-button"
                type="button"
                onClick={() =>
                  onSelectSamples(
                    samples
                      .filter((sample) => sample.scoring_role === "scored")
                      .map((sample) => sample.id),
                  )
                }
              >
                Select scored
              </button>
              <button
                className="text-button"
                type="button"
                onClick={() => onSelectSamples([])}
              >
                Clear
              </button>
            </div>
          </div>
          <div className="builder-receipts">
            {samples.map((sample) => {
              const selected = selectedSampleIds.has(sample.id);
              const runEligible = sample.scoring_role === "scored";
              return (
                <label
                  className={`builder-receipt-row${selected ? " selected" : ""}${
                    runEligible ? "" : " disabled"
                  }`}
                  key={sample.id}
                >
                  <input
                    type="checkbox"
                    checked={selected}
                    disabled={!runEligible}
                    onChange={() => onSampleToggle(sample.id)}
                  />
                  <span className="mini-receipt">
                    <ReceiptPreview sample={sample} compact />
                  </span>
                  <span className="builder-row-copy">
                    <strong>{sample.title}</strong>
                    <code>{sample.id}</code>
                    {sample.scoring_role === "excluded" && (
                      <small className="builder-exclusion-reason">
                        {sample.exclusion_reason ?? "Excluded by the project contract."}
                      </small>
                    )}
                  </span>
                  <span
                    className={`builder-row-meta${
                      sample.scoring_role === "excluded" ? " excluded" : ""
                    }`}
                  >
                    {sample.scoring_role === "few_shot"
                      ? "Prompt example"
                      : sample.scoring_role === "excluded"
                        ? "Excluded"
                        : sample.gold === null
                          ? "Runnable · no gold"
                          : "Scored"}
                  </span>
                  <span className="selection-check" aria-hidden="true">
                    <CheckIcon />
                  </span>
                </label>
              );
            })}
          </div>
          <p className="subset-note">
            Showing {samples.length} authorized server records from {project.sample_count} project
            receipts.
          </p>
        </section>

        <section className="builder-section" aria-labelledby="solution-selection-heading">
          <div className="section-title-row">
            <div>
              <span className="step-index">02</span>
              <h2 id="solution-selection-heading">Solutions</h2>
              <p>Each row names its standalone or assisted configuration and scoring role.</p>
            </div>
            <div className="inline-actions">
              <button
                className="text-button"
                type="button"
                onClick={() =>
                  onSelectSolutions(
                    solutions
                      .filter(
                        (solution) => getCompatibility(project, solution) !== "unsupported",
                      )
                      .map((solution) => solution.id),
                  )
                }
              >
                Select all compatible
              </button>
              <button
                className="text-button"
                type="button"
                onClick={() => onSelectSolutions(quickSolutionIds)}
              >
                {quickSelectionLabel}
              </button>
              <button
                className="text-button"
                type="button"
                onClick={() => onSelectSolutions([])}
              >
                Clear
              </button>
            </div>
          </div>
          <div className="solution-groups">
            {groups.map(({ kind, solutions: groupSolutions }) => (
              <fieldset className="solution-group" key={kind}>
                <legend>
                  {kind === "ocr_pipeline"
                    ? assistedGroupLegend(groupSolutions, "OCR", "OCR pipelines")
                    : kind === "native_pipeline"
                      ? assistedGroupLegend(groupSolutions, "Extraction API", "Native pipelines")
                      : kind === "ocr_text"
                        ? "OCR text (standalone)"
                        : kindLabels[kind]}
                </legend>
                {groupSolutions.map((solution) => {
                  const compatibility = getCompatibility(project, solution);
                  const selected = selectedSolutionIds.has(solution.id);
                  const presentation = configurationPresentation(project, solution);
                  return (
                    <label
                      className={`solution-row${selected ? " selected" : ""}${
                        compatibility === "unsupported" ? " disabled" : ""
                      }`}
                      key={solution.id}
                    >
                      <input
                        type="checkbox"
                        checked={selected}
                        disabled={compatibility === "unsupported"}
                        onChange={() => onSolutionToggle(solution.id)}
                      />
                      <span className="solution-order">
                        {String(solution.order + 1).padStart(2, "0")}
                      </span>
                      <span className="builder-row-copy">
                        <strong>{solution.label}</strong>
                        <small>
                          {solution.provider}
                          {solution.normalizer !== null
                            ? ` · ${modelDisplayName(solution.normalizer)} normalization`
                            : ""}
                          {solution.lifecycle === "preview" ? " · preview" : ""}
                        </small>
                      </span>
                      <span
                        className={`compatibility compatibility-${compatibility}`}
                        title={presentation.detail}
                      >
                        {presentation.label}
                      </span>
                    </label>
                  );
                })}
              </fieldset>
            ))}
          </div>
        </section>
      </div>

      <section className="contract-confirmation" aria-labelledby="contract-heading">
        <div>
          <span className="step-index">03</span>
          <h2 id="contract-heading">Confirm pinned contracts</h2>
          <p>
            Inspect the exact schema, prompt, and model assistance this run will pin before
            confirming.
          </p>
        </div>
        <ContractDisclosure
          project={project}
          assistedSolutions={disclosedSelectedSolutions}
          compact
        />
        <label>
          <input
            type="checkbox"
            checked={schemaConfirmed}
            onChange={(event) => onSchemaConfirm(event.target.checked)}
          />
          <span>
            <strong>Schema v{project.schema.version}</strong>
            <small>{project.schema.hash}</small>
          </span>
        </label>
        <label>
          <input
            type="checkbox"
            checked={promptConfirmed}
            onChange={(event) => onPromptConfirm(event.target.checked)}
          />
          <span>
            <strong>Prompt v{project.prompt.version}</strong>
            <small>
              {project.prompt_example_count} examples · {project.prompt.hash}
            </small>
          </span>
        </label>
      </section>

      <div className="launch-bar">
        <div>
          <span>Exact workload</span>
          <strong>{guard.pairCount} receipt × solution pairs</strong>
          <small>
            {runnableSampleIds.length} receipts · {selectedSolutionIds.size} solutions ·
            bounded parallel execution
          </small>
        </div>
        {!guard.allowed && guard.reasons.length > 0 && (
          <p role="status">{guard.reasons[0]}</p>
        )}
        <button
          className="primary-button launch-button"
          type="button"
          disabled={!guard.allowed || launchPending}
          aria-busy={launchPending}
          onClick={onLaunch}
        >
          <PlayIcon />
          {launchPending ? "Launching run…" : `Launch ${executionMode} run`}
        </button>
      </div>
    </div>
  );
}
