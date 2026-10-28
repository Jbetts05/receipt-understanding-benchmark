import { useState } from "react";

import { MAX_DATASET_PACKAGE_BYTES } from "../api";
import { CheckIcon, FileIcon, UploadIcon } from "./Icons";

const manifestExample = `{
  "format_version": "1.0",
  "name": "EMEA expense receipts",
  "schema": "schema.json",
  "prompt": "Extract the labelled receipt fields.",
  "samples": [
    {
      "id": "receipt-001",
      "image": "images/receipt-001.jpg",
      "answer": "answers/receipt-001.json"
    }
  ]
}`;

export function LabeledDatasetImport({
  onImport,
}: {
  onImport: (file: File) => Promise<void>;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    if (file === null) {
      setError("Choose a labelled dataset ZIP.");
      return;
    }
    setPending(true);
    setError(null);
    try {
      await onImport(file);
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Dataset import failed.",
      );
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="labeled-import" aria-labelledby="labeled-import-heading">
      <div className="labeled-import-intro">
        <span className="step-index">Fast path</span>
        <div>
          <h2 id="labeled-import-heading">Import a complete labelled dataset</h2>
          <p>
            Bring images, authoritative answer JSON, one schema, and one prompt.
            The server validates the complete package before publishing a frozen
            dataset revision.
          </p>
        </div>
      </div>

      <div className="labeled-import-grid">
        <div className="labeled-import-requirements">
          <h3>ZIP requirements</h3>
          <ul>
            <li>
              <CheckIcon />
              <span>
                Root <code>dataset.json</code> explicitly pairs every image and answer.
              </span>
            </li>
            <li>
              <CheckIcon />
              <span>Every answer must validate against the declared JSON Schema.</span>
            </li>
            <li>
              <CheckIcon />
              <span>
                Optional OCR text must be supplied for every receipt or omitted
                entirely.
              </span>
            </li>
            <li>
              <CheckIcon />
              <span>
                Undeclared files, unsafe paths, missing pairs, and duplicate images
                are rejected.
              </span>
            </li>
          </ul>
          <details className="package-contract-example">
            <summary>View dataset.json example</summary>
            <pre className="json-block">
              <code>{manifestExample}</code>
            </pre>
          </details>
        </div>

        <div className="labeled-import-upload">
          <label className={`wizard-dropzone${file !== null ? " has-file" : ""}`}>
            {file === null ? <UploadIcon /> : <FileIcon />}
            <strong>{file?.name ?? "Choose labelled dataset ZIP"}</strong>
            <span>
              Maximum {MAX_DATASET_PACKAGE_BYTES / 1024 / 1024} MB compressed
              {file !== null ? ` · ${(file.size / 1024 / 1024).toFixed(1)} MB selected` : ""}
            </span>
            <input
              type="file"
              accept=".zip,application/zip"
              disabled={pending}
              onChange={(event) => {
                const selected = event.target.files?.[0] ?? null;
                event.target.value = "";
                setFile(selected);
                setError(null);
              }}
            />
          </label>
          {error !== null && (
            <p className="field-error" role="alert">
              {error}
            </p>
          )}
          <button
            className="primary-button labeled-import-submit"
            type="button"
            disabled={pending || file === null}
            aria-busy={pending}
            onClick={() => void submit()}
          >
            {pending ? "Validating and importing..." : "Validate and import dataset"}
          </button>
          <small>
            Successful imports are frozen. Editing labels later creates a new
            revision rather than changing prior run evidence.
          </small>
        </div>
      </div>
    </section>
  );
}
