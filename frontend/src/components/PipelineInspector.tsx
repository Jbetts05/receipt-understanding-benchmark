import { useEffect, useState } from "react";

import type {
  PipelineComponent,
  Project,
  ReceiptSample,
  RunResult,
  Solution,
} from "../types";
import { AlertIcon, CheckIcon, FileIcon, InfoIcon, PipelineIcon } from "./Icons";
import { ProjectHeader } from "./ProjectHeader";
import { ReceiptPreview } from "./ReceiptPreview";

function metric(value: number | null, kind: "tokens" | "pages" | "cost"): string {
  if (value === null) {
    return "—";
  }
  if (kind === "cost") {
    return `$${value.toFixed(6)}`;
  }
  return value.toLocaleString();
}

/**
 * Schema validation has no meaning for a raw OCR text result (there is no
 * schema to validate against), so `schema_valid: null` renders a distinct
 * neutral state rather than being folded into "invalid".
 */
function schemaValidationStatus(schemaValid: boolean | null): {
  className: "valid" | "invalid" | "not-applicable";
  label: string;
} {
  if (schemaValid === null) {
    return { className: "not-applicable", label: "No schema to validate" };
  }
  return schemaValid
    ? { className: "valid", label: "Schema valid" }
    : { className: "invalid", label: "Schema invalid" };
}

function latency(milliseconds: number): string {
  return milliseconds >= 1_000
    ? `${(milliseconds / 1_000).toFixed(2)} s`
    : `${Math.round(milliseconds)} ms`;
}

function artifactLabel(component: PipelineComponent): {
  input: string;
  output: string;
} {
  if (component.id === "ocr") {
    return { input: "Receipt image", output: "OCR output" };
  }
  if (component.id === "normalizer") {
    return { input: "Normalizer input", output: "Normalizer output" };
  }
  if (component.id === "native") {
    return { input: "Receipt image", output: "Native response" };
  }
  return { input: "Model context", output: "Native response" };
}

function componentKey(component: PipelineComponent, index: number): string {
  return `${component.id}:${component.attempt}:${index}`;
}

export function PipelineInspector({
  project,
  samples,
  solutions,
  results,
  resultLoadStatus = "idle",
  selectedSampleId,
  selectedSolutionId,
  onSampleChange,
  onSolutionChange,
  embedded = false,
}: {
  project: Project;
  samples: ReceiptSample[];
  solutions: Solution[];
  results: RunResult[];
  resultLoadStatus?: "idle" | "loading" | "error";
  selectedSampleId: string;
  selectedSolutionId: string;
  onSampleChange: (sampleId: string) => void;
  onSolutionChange: (solutionId: string) => void;
  embedded?: boolean;
}) {
  const projectSamples = samples.filter((item) => item.project_id === project.id);
  const sample =
    projectSamples.find((item) => item.id === selectedSampleId) ??
    projectSamples[0] ??
    null;
  const solution =
    solutions.find((item) => item.id === selectedSolutionId) ?? solutions[0] ?? null;
  const result =
    results.find(
      (item) =>
        item.project_id === project.id &&
        item.sample_id === sample?.id &&
        item.solution_id === solution?.id,
    ) ?? null;
  const [activeComponentKey, setActiveComponentKey] = useState<string | null>(
    result?.telemetry.components[0]
      ? componentKey(result.telemetry.components[0], 0)
      : null,
  );

  useEffect(() => {
    setActiveComponentKey(
      result?.telemetry.components[0]
        ? componentKey(result.telemetry.components[0], 0)
        : null,
    );
  }, [result?.sample_id, result?.solution_id, result?.telemetry.components]);

  const activeComponent =
    result?.telemetry.components.find(
      (component, index) =>
        componentKey(component, index) === activeComponentKey,
    ) ??
    result?.telemetry.components[0] ??
    null;
  const recordedErrors =
    result === null
      ? []
      : [
          ...(result.error === null ? [] : [result.error.message]),
          ...result.telemetry.errors,
        ].filter((message, index, messages) => messages.indexOf(message) === index);
  const selectors = (
    <div className="inspector-selectors">
      <label className="select-field">
        <span>Receipt</span>
        <select
          value={sample?.id ?? ""}
          onChange={(event) => onSampleChange(event.target.value)}
        >
          {projectSamples.map((item) => (
            <option value={item.id} key={item.id}>
              {item.id}
            </option>
          ))}
        </select>
      </label>
      <label className="select-field">
        <span>Solution</span>
        <select
          value={solution?.id ?? ""}
          onChange={(event) => onSolutionChange(event.target.value)}
        >
          {solutions.map((item) => (
            <option value={item.id} key={item.id}>
              {item.label}
            </option>
          ))}
        </select>
      </label>
    </div>
  );

  return (
    <div
      className={embedded ? "embedded-workspace pipeline-view" : "view-shell pipeline-view"}
    >
      {embedded ? (
        <header className="embedded-workspace-header">
          <div>
            <span className="eyebrow">Selected trace</span>
            <h2>Pipeline evidence</h2>
            <p>Raw, parsed, component, validation, retry, and cost artifacts.</p>
          </div>
          {selectors}
        </header>
      ) : (
        <ProjectHeader
          project={project}
          eyebrow="Pipeline inspector"
          title="Every transformation remains inspectable"
          description="OCR, normalizer, native response, parsing, telemetry, retries, and errors retain their own artifacts."
          actions={selectors}
          compact
        />
      )}

      {sample === null || solution === null || result === null ? (
        <div
          className="large-empty-state"
          role={resultLoadStatus === "error" ? "alert" : "status"}
        >
          <FileIcon />
          <h2>
            {resultLoadStatus === "loading"
              ? "Loading pipeline artifact"
              : resultLoadStatus === "error"
                ? "Pipeline artifact unavailable"
                : "No completed pipeline artifact"}
          </h2>
          <p>
            {resultLoadStatus === "loading"
              ? "Loading the persisted result for this receipt."
              : resultLoadStatus === "error"
                ? "The persisted result could not be loaded. Retry the receipt selection."
                : "Choose a receipt and a solution with a terminal result."}
          </p>
        </div>
      ) : (
        <>
          <section className="pipeline-summary" aria-label="Pipeline summary">
            <div className="pipeline-receipt-thumb">
              <ReceiptPreview sample={sample} compact />
            </div>
            <div className="pipeline-run-copy">
              <span className="eyebrow">Selected trace</span>
              <h2>{solution.label}</h2>
              <p>
                {sample.id} · {solution.kind.replace("_", " ")} ·{" "}
                {result.status === "succeeded" ? "terminal success" : result.status}
              </p>
            </div>
            <dl>
              <div>
                <dt>Latency</dt>
                <dd>{latency(result.telemetry.latency_ms)}</dd>
              </div>
              <div>
                <dt>Tokens</dt>
                <dd>{metric(result.telemetry.total_tokens, "tokens")}</dd>
              </div>
              <div>
                <dt>Pages</dt>
                <dd>{metric(result.telemetry.total_pages, "pages")}</dd>
              </div>
              <div>
                <dt>Est. cost</dt>
                <dd>{metric(result.telemetry.estimated_cost_usd, "cost")}</dd>
              </div>
              <div>
                <dt>Attempts</dt>
                <dd>{result.telemetry.attempts}</dd>
              </div>
            </dl>
          </section>

          <section className="pipeline-payloads" aria-label="Raw and parsed artifacts">
           <details open>
             <summary>
               <span>Raw provider response</span>
               <small>Persisted evidence</small>
             </summary>
             <pre className="json-block">
               <code>{result.raw_response || "No raw response was persisted."}</code>
             </pre>
           </details>
           <details open>
             <summary>
               <span>Parsed output</span>
               <small className={schemaValidationStatus(result.schema_valid).className}>
                 {schemaValidationStatus(result.schema_valid).label}
               </small>
             </summary>
             <pre className="json-block">
               <code>
                 {result.parsed_output === null
                   ? "No parsed output was persisted."
                   : JSON.stringify(result.parsed_output, null, 2)}
               </code>
             </pre>
           </details>
          </section>

          {recordedErrors.length > 0 && (
            <div className="pipeline-error-log" role="status">
              <AlertIcon />
              <div>
                <strong>{recordedErrors.length} recorded errors</strong>
                <p>{recordedErrors.join(" · ")}</p>
              </div>
            </div>
          )}

          <div className="pipeline-layout">
            <section className="pipeline-components" aria-labelledby="components-heading">
              <div className="section-title-row">
                <div>
                  <h2 id="components-heading">Components</h2>
                  <p>Provider stages are measured separately.</p>
                </div>
              </div>
              <ol className="pipeline-stage-list">
                {result.telemetry.components.map((component, index) => {
                  const key = componentKey(component, index);
                  return (
                  <li key={key}>
                    <button
                      type="button"
                      className={activeComponentKey === key ? "selected" : ""}
                      aria-pressed={activeComponentKey === key}
                      onClick={() => setActiveComponentKey(key)}
                    >
                      <span className="stage-number">
                        {component.status === "succeeded" ? (
                          <CheckIcon />
                        ) : (
                          <AlertIcon />
                        )}
                      </span>
                      <span className="stage-copy">
                        <strong>{component.label}</strong>
                        <small>
                          Attempt {component.attempt} · {latency(component.latency_ms)}
                        </small>
                      </span>
                      <span className="stage-cost">
                        {metric(component.estimated_cost_usd, "cost")}
                      </span>
                    </button>
                  </li>
                  );
                })}
                <li>
                  <div className="pipeline-final-stage">
                    <span className="stage-number">
                      {result.schema_valid === null ? (
                        <InfoIcon />
                      ) : result.schema_valid ? (
                        <CheckIcon />
                      ) : (
                        <AlertIcon />
                      )}
                    </span>
                    <span className="stage-copy">
                      <strong>Schema validation & comparison</strong>
                      <small>
                        {result.schema_valid === null
                          ? "No schema to validate"
                          : result.schema_valid
                            ? "Valid output"
                            : "Invalid output retained"}
                      </small>
                    </span>
                  </div>
                </li>
              </ol>
              {solution.kind === "direct" && (
                <div className="inline-note">
                  Direct multimodal path: no standalone OCR artifact was produced.
                </div>
              )}
              {solution.kind === "native" && (
                <div className="inline-note">
                  Native extraction path: the service response is retained without a separate
                  normalizer.
                </div>
              )}
              {solution.kind === "ocr_text" && (
                <div className="inline-note">
                  Standalone OCR text path: no normalizer or field extraction step runs; this
                  solution is scored for reference-token recall against the OCR text, not Field F1.
                </div>
              )}
            </section>

            <section className="artifact-inspector" aria-labelledby="artifact-heading">
              {activeComponent === null ? (
                <div className="empty-state compact-empty">
                  <PipelineIcon />
                  <h3>No component artifact</h3>
                </div>
              ) : (
                <>
                  <div className="artifact-header">
                    <div>
                      <span className="eyebrow">Component artifact</span>
                      <h2 id="artifact-heading">{activeComponent.label}</h2>
                    </div>
                    <span className={`status-label status-${activeComponent.status}`}>
                      {activeComponent.status}
                    </span>
                  </div>
                  <dl className="component-metrics">
                    <div>
                      <dt>Input tokens</dt>
                      <dd>{metric(activeComponent.input_tokens, "tokens")}</dd>
                    </div>
                    <div>
                      <dt>Output tokens</dt>
                      <dd>{metric(activeComponent.output_tokens, "tokens")}</dd>
                    </div>
                    <div>
                      <dt>Pages</dt>
                      <dd>{metric(activeComponent.pages, "pages")}</dd>
                    </div>
                    <div>
                      <dt>Latency</dt>
                      <dd>{latency(activeComponent.latency_ms)}</dd>
                    </div>
                  </dl>
                  <div className="artifact-block">
                    <div className="subheading-row">
                      <h3>{artifactLabel(activeComponent).input}</h3>
                      <span>Persisted input</span>
                    </div>
                    <pre className="json-block">
                      <code>{activeComponent.input ?? "Binary receipt image"}</code>
                    </pre>
                  </div>
                  <div className="artifact-block">
                    <div className="subheading-row">
                      <h3>{artifactLabel(activeComponent).output}</h3>
                      <span>Persisted output</span>
                    </div>
                    <pre className="json-block artifact-output">
                      <code>{activeComponent.output ?? "No output was returned."}</code>
                    </pre>
                  </div>
                  {activeComponent.error !== null && (
                    <div className="output-error">
                      <AlertIcon />
                      <span>{activeComponent.error}</span>
                    </div>
                  )}
                </>
              )}
            </section>
          </div>
        </>
      )}
    </div>
  );
}
