import type { ReactNode } from "react";

import type { Project } from "../types";
import { ContractDisclosure } from "./ContractDisclosure";
import { ExternalIcon } from "./Icons";

export function ProjectHeader({
  project,
  eyebrow,
  title,
  description,
  actions,
  compact = false,
}: {
  project: Project;
  eyebrow: string;
  title: string;
  description?: string;
  actions?: ReactNode;
  compact?: boolean;
}) {
  return (
    <header className={`project-header${compact ? " project-header-compact" : ""}`}>
      <div className="project-title-row">
        <div>
          <span className="eyebrow">{eyebrow}</span>
          <h1>{title}</h1>
          <p>{description ?? project.description}</p>
        </div>
        {actions !== undefined && <div className="project-header-actions">{actions}</div>}
      </div>
      <div className="project-facts" aria-label="Project facts">
        <div>
          <span>Schema</span>
          <strong>v{project.schema.version}</strong>
          <code>{project.schema.hash.replace("sha256:", "")}</code>
        </div>
        <div>
          <span>Prompt</span>
          <strong>v{project.prompt.version}</strong>
          <code>{project.prompt.hash.replace("sha256:", "")}</code>
        </div>
        <div>
          <span>Evaluation set</span>
          <strong>{project.scored_sample_count} scored</strong>
          <small>{project.prompt_example_count} prompt examples excluded</small>
        </div>
        {project.kind === "custom" && (
          <div>
            <span>Dataset revision</span>
            <strong>
              {project.dataset_state === "frozen"
                ? "Frozen revision"
                : "Editable draft"}
            </strong>
            {project.dataset_revision_hash === null ? (
              <small>
                {project.import_mode === "manual"
                  ? "Manual upload"
                  : "Revision pending"}
              </small>
            ) : (
              <code title={project.dataset_revision_hash}>
                {project.dataset_revision_hash.slice(0, 24)}
              </code>
            )}
          </div>
        )}
      </div>
      <details className="header-contract-disclosure">
        <summary>Inspect pinned schema &amp; prompt</summary>
        <ContractDisclosure project={project} compact />
      </details>
      {project.attribution.url !== null && (
        <a
          className="dataset-source-link"
          href={project.attribution.url}
          target="_blank"
          rel="noreferrer"
        >
          <span>Dataset source · {project.attribution.label}</span>
          <ExternalIcon />
        </a>
      )}
    </header>
  );
}
