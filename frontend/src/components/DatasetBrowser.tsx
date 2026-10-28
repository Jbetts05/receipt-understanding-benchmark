import { useEffect, useMemo, useRef, useState } from "react";

import type { Project, ReceiptSample } from "../types";
import { FileIcon, PlayIcon, SearchIcon } from "./Icons";
import { ProjectHeader } from "./ProjectHeader";
import { ReceiptPreview } from "./ReceiptPreview";

export type DatasetFilter = "all" | "scored" | "examples" | "excluded" | "unrun";

const priorLabels: Record<ReceiptSample["prior_run_state"], string> = {
  complete: "Prior run complete",
  partial: "Partial prior run",
  failed: "Prior run failed",
  never: "Not run",
};

export function isGoldScoredSample(sample: ReceiptSample): boolean {
  return (
    sample.scoring_role === "scored" &&
    sample.gold_availability === "available" &&
    sample.gold !== null
  );
}

export function filterDatasetSamples(
  samples: ReceiptSample[],
  query: string,
  filter: DatasetFilter,
): ReceiptSample[] {
  const normalizedQuery = query.trim().toLocaleLowerCase("en-US");
  return samples.filter((sample) => {
    const matchesQuery =
      normalizedQuery === "" ||
      `${sample.id} ${sample.title} ${sample.filename}`
        .toLocaleLowerCase("en-US")
        .includes(normalizedQuery);
    const matchesFilter =
      filter === "all" ||
      (filter === "scored" && isGoldScoredSample(sample)) ||
      (filter === "examples" && sample.scoring_role === "few_shot") ||
      (filter === "excluded" && sample.scoring_role === "excluded") ||
      (filter === "unrun" && sample.prior_run_state === "never");
    return matchesQuery && matchesFilter;
  });
}

export function resolveVisibleSample(
  visibleSamples: ReceiptSample[],
  selectedSampleId: string,
): ReceiptSample | null {
  return (
    visibleSamples.find((sample) => sample.id === selectedSampleId) ??
    visibleSamples[0] ??
    null
  );
}

type DatasetSearchShortcutEvent = Pick<
  KeyboardEvent,
  "altKey" | "ctrlKey" | "defaultPrevented" | "key" | "metaKey" | "target"
>;

function isTypingTarget(target: EventTarget | null): boolean {
  if (target === null) {
    return false;
  }
  const element = target as Partial<HTMLElement>;
  return (
    element.tagName === "INPUT" ||
    element.tagName === "TEXTAREA" ||
    element.tagName === "SELECT" ||
    element.isContentEditable === true
  );
}

export function shouldFocusDatasetSearch(
  event: DatasetSearchShortcutEvent,
): boolean {
  return (
    event.key === "/" &&
    !event.defaultPrevented &&
    !event.altKey &&
    !event.ctrlKey &&
    !event.metaKey &&
    !isTypingTarget(event.target)
  );
}

export function DatasetBrowser({
  project,
  samples,
  selectedSampleId,
  onSampleSelect,
  onBuildRun,
}: {
  project: Project;
  samples: ReceiptSample[];
  selectedSampleId: string;
  onSampleSelect: (sampleId: string) => void;
  onBuildRun: (sampleId: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<DatasetFilter>("all");
  const searchInputRef = useRef<HTMLInputElement>(null);
  const visibleSamples = useMemo(
    () => filterDatasetSamples(samples, query, filter),
    [filter, query, samples],
  );
  const selectedSample = resolveVisibleSample(visibleSamples, selectedSampleId);
  const visibleSelectedSampleId = selectedSample?.id ?? null;

  useEffect(() => {
    if (
      visibleSelectedSampleId !== null &&
      visibleSelectedSampleId !== selectedSampleId
    ) {
      onSampleSelect(visibleSelectedSampleId);
    }
  }, [onSampleSelect, selectedSampleId, visibleSelectedSampleId]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!shouldFocusDatasetSearch(event)) {
        return;
      }
      event.preventDefault();
      searchInputRef.current?.focus();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  return (
    <div className="view-shell">
      <ProjectHeader
        project={project}
        eyebrow="Dataset browser"
        title={`${project.short_name} evidence`}
        actions={
          selectedSample !== null ? (
            <button
              className="primary-button"
              type="button"
              disabled={selectedSample.scoring_role !== "scored"}
              onClick={() => onBuildRun(selectedSample.id)}
            >
              <PlayIcon />
              {selectedSample.scoring_role === "scored" ? "Build run" : "Not runnable"}
            </button>
          ) : undefined
        }
      />

      <div className="dataset-toolbar">
        <label className="search-field">
          <SearchIcon />
          <span className="sr-only">Search receipts</span>
          <input
            ref={searchInputRef}
            type="search"
            value={query}
            placeholder="Search receipt ID, title, or filename"
            aria-keyshortcuts="/"
            onChange={(event) => setQuery(event.target.value)}
          />
          <kbd aria-hidden="true">/</kbd>
        </label>
        <div className="segmented-control" aria-label="Dataset filter">
          {(
            [
              ["all", "All"],
              ["scored", "Gold-scored"],
              ["examples", "Prompt examples"],
              ["excluded", "Excluded"],
              ["unrun", "Not run"],
            ] as Array<[DatasetFilter, string]>
          ).map(([id, label]) => (
            <button
              type="button"
              className={filter === id ? "active" : ""}
              aria-pressed={filter === id}
              onClick={() => setFilter(id)}
              key={id}
            >
              {label}
            </button>
          ))}
        </div>
        <span className="result-count" aria-live="polite">
          {visibleSamples.length} shown · {project.sample_count} total
        </span>
      </div>

      <div className="dataset-layout">
        <section className="sample-browser" aria-label="Receipts">
          {visibleSamples.length === 0 ? (
            <div className="empty-state">
              <FileIcon />
              <h2>No receipts match</h2>
              <p>Clear the search or choose another dataset filter.</p>
              <button
                className="text-button"
                type="button"
                onClick={() => {
                  setQuery("");
                  setFilter("all");
                }}
              >
                Reset filters
              </button>
            </div>
          ) : (
            <div className="sample-grid">
              {visibleSamples.map((sample, index) => {
                const selected = selectedSample?.id === sample.id;
                return (
                  <button
                    type="button"
                    className={`sample-tile${selected ? " selected" : ""}`}
                    aria-pressed={selected}
                    onClick={() => onSampleSelect(sample.id)}
                    style={{ "--item-index": index } as React.CSSProperties}
                    key={sample.id}
                  >
                    <span className="sample-thumbnail">
                      <ReceiptPreview sample={sample} compact />
                    </span>
                    <span className="sample-tile-copy">
                      <span className="sample-tile-topline">
                        <code>{sample.id}</code>
                        <span className={`run-state run-${sample.prior_run_state}`}>
                          {priorLabels[sample.prior_run_state]}
                        </span>
                      </span>
                      <strong>{sample.title}</strong>
                      <small>{sample.filename}</small>
                      <span className="sample-badges">
                        {sample.scoring_role === "few_shot" && (
                          <span className="badge badge-accent">Prompt example</span>
                        )}
                        {sample.scoring_role === "excluded" && (
                          <span className="badge badge-excluded">Excluded duplicate</span>
                        )}
                        <span className="badge">
                          {sample.gold_availability === "available"
                            ? "Gold available"
                            : "No gold"}
                        </span>
                      </span>
                      {sample.scoring_role === "excluded" && (
                        <span className="sample-exclusion-reason">
                          {sample.exclusion_reason ?? "Excluded by the project contract."}
                        </span>
                      )}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </section>

        {selectedSample !== null && (
          <aside className="dataset-evidence" aria-label="Selected receipt evidence">
            <div className="selected-receipt-frame">
              <ReceiptPreview sample={selectedSample} />
            </div>
            <div className="evidence-caption">
              <div>
                <span className="eyebrow">Selected receipt</span>
                <h2>{selectedSample.title}</h2>
                <p>
                  {selectedSample.source_index === null
                    ? "Owner upload"
                    : `Pinned source index ${selectedSample.source_index}`}
                </p>
              </div>
              <button
                className="secondary-button"
                type="button"
                disabled={selectedSample.scoring_role !== "scored"}
                onClick={() => onBuildRun(selectedSample.id)}
              >
                {selectedSample.scoring_role === "scored"
                  ? "Use in run"
                  : "Excluded from runs"}
              </button>
            </div>
            <div className="gold-preview">
              <div className="subheading-row">
                <h3>Golden answer</h3>
                <span>
                  {selectedSample.scoring_role === "few_shot"
                    ? "Prompt example · excluded"
                    : selectedSample.scoring_role === "excluded"
                      ? `Excluded · ${
                          selectedSample.exclusion_reason ??
                          "project contract"
                        }`
                    : selectedSample.gold === null
                      ? "Not scored"
                      : "Scored evidence"}
                </span>
              </div>
              {selectedSample.gold !== null && (
                <pre className="json-block">
                  <code>{JSON.stringify(selectedSample.gold, null, 2)}</code>
                </pre>
              )}
            </div>
          </aside>
        )}
      </div>
    </div>
  );
}
