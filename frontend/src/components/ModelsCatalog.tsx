import { configurationPresentation, modelDisplayName, solutionTypeLabel } from "../domain";
import type { Project, Solution } from "../types";
import { FlaskIcon } from "./Icons";

const lifecycleLabels: Record<Solution["lifecycle"], string> = {
  ga: "Generally available",
  preview: "Preview",
};

/**
 * Data-driven catalog of every registered extraction service (model,
 * extraction API, or OCR pipeline). Distinct from "Configure run": this view
 * never selects receipts or launches anything, it only discloses what a
 * service is and exactly how it is configured for the active project.
 */
export function ModelsCatalog({
  project,
  solutions,
}: {
  project: Project;
  solutions: Solution[];
}) {
  const ordered = [...solutions].sort((a, b) => a.order - b.order);
  return (
    <div className="view-shell catalog-view">
      <header className="catalog-header">
        <span className="eyebrow">Extractors</span>
        <h1>Models &amp; services</h1>
        <p>
          Every registered extraction service, its provider and lifecycle stage, and
          exactly how it is configured for <strong>{project.name}</strong>.
        </p>
      </header>
      <ul className="catalog-grid" aria-label="Registered extraction services">
        {ordered.map((solution) => {
          const presentation = configurationPresentation(project, solution);
          return (
            <li className="catalog-card" key={solution.id}>
              <div className="catalog-card-heading">
                <span className="catalog-card-icon" aria-hidden="true">
                  <FlaskIcon />
                </span>
                <div>
                  <strong>{solution.label}</strong>
                  <span className="catalog-card-type">{solutionTypeLabel(solution)}</span>
                </div>
                <span className={`lifecycle-badge lifecycle-${solution.lifecycle}`}>
                  {lifecycleLabels[solution.lifecycle]}
                </span>
              </div>
              <dl className="catalog-card-facts">
                <div>
                  <dt>Provider</dt>
                  <dd>{solution.provider}</dd>
                </div>
                <div>
                  <dt>Model</dt>
                  <dd>{solution.model === null ? "—" : modelDisplayName(solution.model)}</dd>
                </div>
                <div>
                  <dt>Normalizer</dt>
                  <dd>
                    {solution.normalizer === null
                      ? "None · standalone output"
                      : modelDisplayName(solution.normalizer)}
                  </dd>
                </div>
                <div>
                  <dt>Internal models</dt>
                  <dd>
                    {solution.internal_models.length === 0
                      ? "None disclosed"
                      : solution.internal_models.map(modelDisplayName).join(", ")}
                  </dd>
                </div>
              </dl>
              {solution.internal_models.length > 0 && (
                <p className="catalog-internal-models-note">
                  This service's own response is intrinsically produced with{" "}
                  {solution.internal_models.map(modelDisplayName).join(", ")}. This is not the
                  external normalizer above — it cannot be swapped or disabled, so it is disclosed
                  here for transparency.
                </p>
              )}
              <div className="catalog-configuration">
                <strong>{presentation.label}</strong>
                <p>{presentation.detail}</p>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
