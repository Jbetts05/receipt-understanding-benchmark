import { useEffect, useMemo, useRef, useState, type ChangeEvent } from "react";

import {
  customConfigurationPresentation,
  customSolutionCompatibility,
  validateCustomProjectDraft,
  validateSchemaText,
} from "../wizard";
import { modelDisplayName } from "../domain";
import { MAX_UPLOAD_BYTES, MAX_UPLOAD_FILES, SUPPORTED_IMAGE_TYPES } from "../api";
import type {
  CustomImageDraft,
  CustomProjectDraft,
  Solution,
  ValidationIssue,
} from "../types";
import {
  ArrowIcon,
  CheckIcon,
  FileIcon,
  PlusIcon,
  UploadIcon,
} from "./Icons";
import { LabeledDatasetImport } from "./LabeledDatasetImport";

const schemaTemplate = `{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["vendor", "date", "total"],
  "properties": {
    "vendor": { "type": "string" },
    "date": { "type": "string" },
    "total": { "type": "number" }
  }
}`;

const promptTemplate =
  "Extract the receipt fields defined by the JSON Schema. Preserve printed values and return only valid JSON.";

const stepLabels = ["Project", "Images & gold", "Schema & prompt", "Solutions"];

function issueFor(
  issues: ValidationIssue[],
  field: ValidationIssue["field"],
  imageId?: string,
): string | null {
  return (
    issues.find(
      (issue) =>
        issue.field === field &&
        (imageId === undefined || issue.image_id === imageId),
    )?.message ?? null
  );
}

export function CustomProjectWizard({
  solutions,
  onCreate,
  onImport,
}: {
  solutions: Solution[];
  onCreate: (draft: CustomProjectDraft) => Promise<void>;
  onImport: (file: File) => Promise<void>;
}) {
  const defaultSolutions = solutions
    .filter((solution) => customSolutionCompatibility(solution) === "full")
    .slice(0, 5)
    .map((solution) => solution.id);
  const [step, setStep] = useState(0);
  const [submitted, setSubmitted] = useState(false);
  const [creating, setCreating] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const [creationMode, setCreationMode] = useState<"package" | "manual">("package");
  const [intakeErrors, setIntakeErrors] = useState<string[]>([]);
  const [draft, setDraft] = useState<CustomProjectDraft>({
    name: "",
    description: "",
    images: [],
    schema_text: schemaTemplate,
    prompt: promptTemplate,
    selected_solution_ids: defaultSolutions,
  });
  const previewUrlsRef = useRef<string[]>([]);
  const validation = useMemo(
    () => validateCustomProjectDraft(draft, solutions),
    [draft, solutions],
  );
  const schemaValidation = useMemo(
    () => validateSchemaText(draft.schema_text),
    [draft.schema_text],
  );

  useEffect(
    () => () => {
      previewUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
    },
    [],
  );

  function addImages(files: FileList) {
    const remaining = Math.max(0, MAX_UPLOAD_FILES - draft.images.length);
    const selected = Array.from(files);
    const accepted = selected.slice(0, remaining);
    const errors: string[] = [];
    if (selected.length > remaining) {
      errors.push(`A project can contain at most ${MAX_UPLOAD_FILES} images.`);
    }
    const next: CustomImageDraft[] = accepted.map((file) => {
      if (!SUPPORTED_IMAGE_TYPES.has(file.type)) {
        errors.push(`${file.name} must be JPEG or PNG.`);
      }
      if (file.size <= 0 || file.size > MAX_UPLOAD_BYTES) {
        errors.push(
          `${file.name} must be no larger than ${Math.round(
            MAX_UPLOAD_BYTES / 1024 / 1024,
          )} MB.`,
        );
      }
      const previewUrl = file.type.startsWith("image/") ? URL.createObjectURL(file) : null;
      if (previewUrl !== null) {
        previewUrlsRef.current.push(previewUrl);
      }
      return {
        id: crypto.randomUUID(),
        filename: file.name,
        media_type: file.type,
        size_bytes: file.size,
        file,
        preview_url: previewUrl,
        gold_text: "",
      };
    });
    setIntakeErrors(errors);
    setDraft((current) => ({ ...current, images: [...current.images, ...next] }));
  }

  function removeImage(imageId: string) {
    setDraft((current) => {
      const image = current.images.find((item) => item.id === imageId);
      if (image?.preview_url !== null && image?.preview_url !== undefined) {
        URL.revokeObjectURL(image.preview_url);
        previewUrlsRef.current = previewUrlsRef.current.filter(
          (url) => url !== image.preview_url,
        );
      }
      return {
        ...current,
        images: current.images.filter((item) => item.id !== imageId),
      };
    });
  }

  function updateGold(imageId: string, goldText: string) {
    setDraft((current) => ({
      ...current,
      images: current.images.map((image) =>
        image.id === imageId ? { ...image, gold_text: goldText } : image,
      ),
    }));
  }

  async function readSchemaFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file === undefined) {
      return;
    }
    const schemaText = await file.text();
    setDraft((current) => ({ ...current, schema_text: schemaText }));
  }

  function toggleSolution(solutionId: string) {
    setDraft((current) => ({
      ...current,
      selected_solution_ids: current.selected_solution_ids.includes(solutionId)
        ? current.selected_solution_ids.filter((id) => id !== solutionId)
        : [...current.selected_solution_ids, solutionId],
    }));
  }

  async function finish() {
    setSubmitted(true);
    if (validation.issues.length === 0) {
      setCreating(true);
      setServerError(null);
      try {
        await onCreate(draft);
      } catch (error) {
        setServerError(
          error instanceof Error ? error.message : "Server validation failed.",
        );
      } finally {
        setCreating(false);
      }
    }
  }

  return (
    <div className="view-shell wizard-view">
      <header className="wizard-header">
        <div>
          <span className="eyebrow">Custom project</span>
          <h1>Define a receipt experiment</h1>
          <p>Images, schema, prompt, optional gold, and compatible solutions remain project-scoped.</p>
        </div>
        <span className="offline-status">
          <span aria-hidden="true" />
          Server validation required
        </span>
      </header>

      <div className="creation-mode" role="group" aria-label="Dataset creation method">
        <button
          type="button"
          className={creationMode === "package" ? "active" : ""}
          aria-pressed={creationMode === "package"}
          onClick={() => setCreationMode("package")}
        >
          Import labelled ZIP
          <small>Images and authoritative answers already exist</small>
        </button>
        <button
          type="button"
          className={creationMode === "manual" ? "active" : ""}
          aria-pressed={creationMode === "manual"}
          onClick={() => setCreationMode("manual")}
        >
          Build manually
          <small>Add images and optional answers one at a time</small>
        </button>
      </div>

      {creationMode === "package" ? (
        <LabeledDatasetImport onImport={onImport} />
      ) : (
        <>
      <nav className="wizard-steps" aria-label="Project creation steps">
        {stepLabels.map((label, index) => (
          <button
            type="button"
            className={step === index ? "active" : step > index ? "complete" : ""}
            aria-current={step === index ? "step" : undefined}
            onClick={() => setStep(index)}
            key={label}
          >
            <span>{step > index ? <CheckIcon /> : index + 1}</span>
            {label}
          </button>
        ))}
      </nav>

      {submitted && validation.issues.length > 0 && (
        <div className="validation-summary" role="alert">
          <strong>Resolve {validation.issues.length} validation issues</strong>
          <ul>
            {validation.issues.slice(0, 5).map((issue, index) => (
              <li key={`${issue.field}-${issue.image_id ?? ""}-${index}`}>{issue.message}</li>
            ))}
          </ul>
        </div>
      )}
      {serverError !== null && (
        <div className="validation-summary" role="alert">
          <strong>Server rejected the project or upload</strong>
          <p>{serverError}</p>
          <small>
            Server schema, gold, file-signature, and image-dimension validation is
            authoritative.
          </small>
        </div>
      )}

      <div className="wizard-stage">
        {step === 0 && (
          <section className="wizard-panel" aria-labelledby="project-details-heading">
            <div className="wizard-panel-heading">
              <span className="step-index">01</span>
              <div>
                <h2 id="project-details-heading">Project details</h2>
                <p>Keep schemas, prompts, runs, and aggregates isolated under this project.</p>
              </div>
            </div>
            <div className="form-grid">
              <label className="form-field">
                <span>Project name</span>
                <input
                  type="text"
                  value={draft.name}
                  placeholder="e.g. EMEA expense receipts"
                  aria-invalid={submitted && issueFor(validation.issues, "name") !== null}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, name: event.target.value }))
                  }
                />
                {submitted && issueFor(validation.issues, "name") !== null && (
                  <small className="field-error">
                    {issueFor(validation.issues, "name")}
                  </small>
                )}
              </label>
              <label className="form-field form-field-wide">
                <span>Description</span>
                <textarea
                  value={draft.description}
                  placeholder="Describe the source, scope, and evaluation intent."
                  rows={4}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      description: event.target.value,
                    }))
                  }
                />
              </label>
            </div>
            <div className="retention-note">
              <strong>Owner-managed rights and retention</strong>
              <p>
                Custom receipt images and provider artifacts may contain sensitive data and
                are retained by the configured backend policy.
              </p>
            </div>
          </section>
        )}

        {step === 1 && (
          <section className="wizard-panel" aria-labelledby="images-heading">
            <div className="wizard-panel-heading">
              <span className="step-index">02</span>
              <div>
                <h2 id="images-heading">Images and optional gold</h2>
                <p>Upload multiple JPEG or PNG receipts. Add gold independently per image.</p>
              </div>
            {intakeErrors.length > 0 && (
               <div className="validation-summary" role="alert">
                 <strong>Some files need attention</strong>
                 <ul>
                   {intakeErrors.map((message) => (
                     <li key={message}>{message}</li>
                   ))}
                 </ul>
               </div>
            )}
            </div>
            <label className="wizard-dropzone">
              <UploadIcon />
              <strong>Add receipt images</strong>
              <span>
                JPEG or PNG · max {Math.round(MAX_UPLOAD_BYTES / 1024 / 1024)} MB each ·{" "}
                {MAX_UPLOAD_FILES} files
              </span>
              <input
                type="file"
                accept=".jpg,.jpeg,.png,image/jpeg,image/png"
                multiple
                onChange={(event) => {
                  if (event.target.files !== null) {
                    addImages(event.target.files);
                  }
                  event.target.value = "";
                }}
              />
            </label>
            {submitted && issueFor(validation.issues, "images") !== null && (
              <small className="field-error">{issueFor(validation.issues, "images")}</small>
            )}
            <div className="wizard-image-list">
              {draft.images.map((image, index) => {
                const imageIssue =
                  issueFor(validation.issues, "images", image.id) ??
                  issueFor(validation.issues, "gold", image.id);
                return (
                  <article className="wizard-image-row" key={image.id}>
                    <div className="wizard-image-preview">
                      {image.preview_url === null ? (
                        <FileIcon />
                      ) : (
                        <img src={image.preview_url} alt="" />
                      )}
                    </div>
                    <div className="wizard-image-heading">
                      <span>{String(index + 1).padStart(2, "0")}</span>
                      <div>
                        <strong>{image.filename}</strong>
                        <small>
                          {image.media_type || "Unknown type"} ·{" "}
                          {(image.size_bytes / 1024).toFixed(1)} KB
                        </small>
                      </div>
                      <button
                        className="text-button danger-text"
                        type="button"
                        onClick={() => removeImage(image.id)}
                      >
                        Remove
                      </button>
                    </div>
                    <label className="form-field">
                      <span>
                        Golden JSON <small>optional</small>
                      </span>
                      <textarea
                        className="code-input"
                        value={image.gold_text}
                        rows={5}
                        placeholder={'{ "vendor": "Example", "date": "2026-08-30", "total": 12.4 }'}
                        aria-invalid={submitted && imageIssue !== null}
                        onChange={(event) => updateGold(image.id, event.target.value)}
                      />
                      {submitted && imageIssue !== null && (
                        <small className="field-error">{imageIssue}</small>
                      )}
                    </label>
                  </article>
                );
              })}
            </div>
          </section>
        )}

        {step === 2 && (
          <section className="wizard-panel" aria-labelledby="contract-editor-heading">
            <div className="wizard-panel-heading">
              <span className="step-index">03</span>
              <div>
                <h2 id="contract-editor-heading">Schema and prompt</h2>
                <p>Validate the extraction contract before selecting solutions.</p>
              </div>
            </div>
            <div className="contract-editor-layout">
              <div className="form-field">
                <div className="form-field-heading">
                  <span>JSON Schema</span>
                  <label className="file-button">
                    <UploadIcon />
                    Load .json
                    <input type="file" accept=".json,application/json" onChange={readSchemaFile} />
                  </label>
                </div>
                <textarea
                  className="code-input schema-editor"
                  value={draft.schema_text}
                  rows={20}
                  spellCheck={false}
                  aria-invalid={schemaValidation.issues.length > 0}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      schema_text: event.target.value,
                    }))
                  }
                />
                <span
                  className={`schema-status ${
                    schemaValidation.issues.length === 0 ? "valid" : "invalid"
                  }`}
                >
                  {schemaValidation.issues.length === 0 ? <CheckIcon /> : <FileIcon />}
                  {schemaValidation.issues[0]?.message ?? "Client preflight passed"}
                </span>
                <small>
                  Convenience check only. The server performs authoritative Draft
                  2020-12 validation.
                </small>
              </div>
              <label className="form-field">
                <span>Extraction prompt</span>
                <textarea
                  value={draft.prompt}
                  rows={10}
                  aria-invalid={submitted && issueFor(validation.issues, "prompt") !== null}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, prompt: event.target.value }))
                  }
                />
                <small>
                  One project pins exactly one schema and one prompt. Every receipt added
                  to this project uses that same contract. At execution, the one schema is
                  supplied alongside the prompt; another schema is never appended.
                </small>
                {submitted && issueFor(validation.issues, "prompt") !== null && (
                  <small className="field-error">
                    {issueFor(validation.issues, "prompt")}
                  </small>
                )}
              </label>
            </div>
          </section>
        )}

        {step === 3 && (
          <section className="wizard-panel" aria-labelledby="compatible-solutions-heading">
            <div className="wizard-panel-heading">
              <span className="step-index">04</span>
              <div>
                <h2 id="compatible-solutions-heading">Compatible solutions</h2>
                <p>Choose standalone or explicitly assisted configurations for this schema.</p>
                <div className="inline-actions">
                  <button
                    type="button"
                    className="text-button"
                    onClick={() =>
                      setDraft((current) => ({
                        ...current,
                        selected_solution_ids: solutions
                          .filter(
                            (solution) => customSolutionCompatibility(solution) !== "unsupported",
                          )
                          .map((solution) => solution.id),
                      }))
                    }
                  >
                    Select all
                  </button>
                  <button
                    type="button"
                    className="text-button"
                    onClick={() =>
                      setDraft((current) => ({ ...current, selected_solution_ids: [] }))
                    }
                  >
                    Clear
                  </button>
                </div>
              </div>
            </div>
            <div className="wizard-solution-list">
              {solutions.map((solution) => {
                const compatibility = customSolutionCompatibility(solution);
                const selected = draft.selected_solution_ids.includes(solution.id);
                const presentation = customConfigurationPresentation(solution);
                return (
                  <label className={`wizard-solution-row${selected ? " selected" : ""}`} key={solution.id}>
                    <input
                      type="checkbox"
                      checked={selected}
                      disabled={compatibility === "unsupported"}
                      onChange={() => toggleSolution(solution.id)}
                    />
                    <span className="solution-order">
                      {String(solution.order + 1).padStart(2, "0")}
                    </span>
                    <span>
                      <strong>{solution.label}</strong>
                      <small>
                        {solution.provider} · {solution.kind.replace("_", " ")}
                        {solution.normalizer !== null
                          ? ` · ${modelDisplayName(solution.normalizer)} normalization`
                          : ""}
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
            </div>
            {submitted && issueFor(validation.issues, "solutions") !== null && (
              <small className="field-error">
                {issueFor(validation.issues, "solutions")}
              </small>
            )}
          </section>
        )}
      </div>

      <div className="wizard-footer">
        <button
          className="secondary-button"
          type="button"
          disabled={step === 0}
          onClick={() => setStep((current) => Math.max(0, current - 1))}
        >
          Back
        </button>
        <div>
          <span>
            {draft.images.length} images · {draft.selected_solution_ids.length} solutions
          </span>
          <strong>
            {draft.images.length * draft.selected_solution_ids.length} potential pairs
          </strong>
        </div>
        {step < stepLabels.length - 1 ? (
          <button
            className="primary-button"
            type="button"
            onClick={() => setStep((current) => Math.min(stepLabels.length - 1, current + 1))}
          >
            Continue
            <ArrowIcon />
          </button>
        ) : (
          <button
            className="primary-button"
            type="button"
            disabled={creating}
            onClick={() => void finish()}
          >
            {creating ? "Creating & uploading…" : "Create project & upload"}
            <CheckIcon />
          </button>
        )}
      </div>
        </>
      )}
    </div>
  );
}
