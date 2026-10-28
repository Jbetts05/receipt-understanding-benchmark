import {
  modelDisplayName,
  orderedPromptContracts,
  promptContractOriginNote,
  promptModeLabel,
  schemaOriginNote,
} from "../domain";
import type { Project, Solution } from "../types";

/**
 * Inspectable, read-only (or pre-confirmation) view of a project's pinned
 * schema and prompt contracts: full formatted JSON Schema, full prompt
 * instructions per solution mode, user-editable templates, normalizer
 * identity, pinned example IDs, versions, and hashes. Optionally lists
 * every solution that discloses assistance — either an external normalizer
 * applied after extraction, or model(s) intrinsic to the service's own
 * response — so nothing about the extraction path stays implicit.
 */
export function ContractDisclosure({
  project,
  assistedSolutions,
  compact = false,
}: {
  project: Project;
  /**
   * Solutions to disclose assistance for. Callers should pass only the
   * solutions that have an external normalizer, non-empty `internal_models`,
   * and/or require a per-project custom analyzer registration; this
   * component renders whatever it is given.
   */
  assistedSolutions?: Solution[];
  compact?: boolean;
}) {
  return (
    <div className={`contract-disclosure${compact ? " contract-disclosure-compact" : ""}`}>
      <p className="contract-origin-note">{schemaOriginNote(project.schema)}</p>
      <details className="contract-detail" open={!compact}>
        <summary>
          <span>JSON Schema</span>
          <small>
            v{project.schema.version} · {project.schema.hash}
          </small>
        </summary>
        <pre className="json-block contract-json">
          <code>{JSON.stringify(project.schema.json_schema, null, 2)}</code>
        </pre>
      </details>
      {orderedPromptContracts(project).map((contract) => (
        <details className="contract-detail" key={contract.id}>
          <summary>
            <span>Prompt · {promptModeLabel(contract.mode)}</span>
            <small>
              v{contract.version} · {contract.hash}
            </small>
          </summary>
          <p className="contract-prompt-origin-note">{promptContractOriginNote(project)}</p>
          <pre className="json-block contract-json">
            <code>{contract.instructions}</code>
          </pre>
          {contract.user_template !== null && (
            <details className="contract-detail contract-user-template">
              <summary>
                <span>User-editable template</span>
              </summary>
              <pre className="json-block contract-json">
                <code>{contract.user_template}</code>
              </pre>
            </details>
          )}
          <p className="contract-example-ids">
            {contract.example_ids.length === 0
              ? "No few-shot example IDs are pinned to this prompt."
              : `Pinned example IDs: ${contract.example_ids.join(", ")}`}
          </p>
          <p className="contract-prompt-normalizer">
            {contract.normalizer_model === null
              ? "No normalizer stage for this mode"
              : `Normalizer · ${modelDisplayName(contract.normalizer_model)}`}
          </p>
        </details>
      ))}
      {assistedSolutions !== undefined && (
        <details className="contract-detail">
          <summary>
            <span>Model assistance</span>
            <small>{assistedSolutions.length} solution(s) disclose assistance</small>
          </summary>
          {assistedSolutions.length === 0 ? (
            <p className="contract-example-ids">
              No selected solution discloses external or internal model assistance.
            </p>
          ) : (
            <ul className="contract-normalizer-list">
              {assistedSolutions.map((solution) => (
                <li key={solution.id}>
                  <div className="contract-normalizer-row">
                    <strong>{solution.label}</strong>
                    <span>
                      {solution.normalizer === null
                        ? "No external normalizer · standalone output"
                        : `Assisted · OCR + ${modelDisplayName(solution.normalizer)}`}
                    </span>
                  </div>
                  {solution.internal_models.length > 0 && (
                    <span className="contract-internal-models">
                      Intrinsic to this service:{" "}
                      {solution.internal_models.map(modelDisplayName).join(", ")}
                    </span>
                  )}
                  {solution.requires_project_analyzer && (
                    <span className="contract-internal-models">
                      Automatically configures a schema-hash-keyed analyzer on first
                      use. No training or fine-tuning is performed.
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </details>
      )}
    </div>
  );
}
