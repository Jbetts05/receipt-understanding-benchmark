import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { resultPairKey } from "./domain";
import {
  makeMockRunResult,
  mockProjects,
  mockSamples,
  mockSolutions,
} from "./mockData";
import { MockProductClient } from "./mockClient";
import { createInitialRunState } from "./streaming";
import type { RunManifest } from "./types";
import {
  AdminCenter,
  getAdminTabDestination,
  type AdminSection,
} from "./components/AdminCenter";
import {
  ComparisonWorkspace,
  getSolutionDetailTabDestination,
} from "./components/ComparisonWorkspace";

const noop = () => undefined;
const adminSections = ["datasets", "runs", "services"] as const;
const solutionDetailTabs = ["parsed", "raw", "pipeline"] as const;

function openingTags(markup: string, element: string, role: string): string[] {
  return (
    markup.match(new RegExp(`<${element}\\b[^>]*role="${role}"[^>]*>`, "g")) ??
    []
  );
}

function tagById(tags: string[], id: string): string {
  const tag = tags.find((candidate) => candidate.includes(`id="${id}"`));
  if (tag === undefined) {
    throw new Error(`Missing tag with id ${id}`);
  }
  return tag;
}

function renderAdminCenter(initialSection: AdminSection): string {
  return renderToStaticMarkup(
    <AdminCenter
      client={new MockProductClient()}
      projects={mockProjects}
      project={mockProjects[0]!}
      solutions={mockSolutions}
      refreshToken={0}
      initialSection={initialSection}
      initialRuns={[]}
      onDeleteDataset={noop}
      onDeleteRun={noop}
    />,
  );
}

function renderComparisonWorkspace(): string {
  const project = mockProjects[0]!;
  const sample = mockSamples[0]!;
  const solution = mockSolutions[0]!;
  const manifest: RunManifest = {
    id: "tab-accessibility-run",
    project_id: project.id,
    sample_ids: [sample.id],
    solution_ids: [solution.id],
    schema_version: "1.0.0",
    prompt_version: "1.0.0",
    status: "completed",
    total_pairs: 1,
    completed_pairs: 1,
    created_at: "2026-09-10T00:00:00Z",
    execution_mode: "mock",
    reference_only_solution_ids: [],
  };
  const result = makeMockRunResult(project, sample, solution, manifest.id);
  const runState = createInitialRunState(manifest, [solution]);
  runState.results[resultPairKey(sample.id, solution.id)] = result;

  return renderToStaticMarkup(
    <ComparisonWorkspace
      project={project}
      samples={[sample]}
      solutions={[solution]}
      runState={runState}
      selectedSampleId={sample.id}
      selectedSolutionId={solution.id}
      onSampleChange={noop}
      onSolutionChange={noop}
      onBuildRun={noop}
    />,
  );
}

describe("accessible tab relationships", () => {
  it("gives Admin Center tabs stable relationships and one roving tab stop", () => {
    const tabs = openingTags(renderAdminCenter("runs"), "button", "tab");

    expect(tabs).toHaveLength(adminSections.length);
    for (const section of adminSections) {
      const tab = tagById(tabs, `admin-center-tab-${section}`);
      expect(tab).toContain(`aria-controls="admin-center-panel-${section}"`);
      expect(tab).toContain(
        section === "runs" ? 'aria-selected="true"' : 'aria-selected="false"',
      );
      expect(tab).toContain(section === "runs" ? 'tabindex="0"' : 'tabindex="-1"');
    }
  });

  it.each(adminSections)(
    "labels the active Admin Center %s panel from its controlling tab",
    (section) => {
      const markup = renderAdminCenter(section);
      const panels = openingTags(markup, "div", "tabpanel");

      expect(panels).toHaveLength(1);
      expect(panels[0]).toContain(`id="admin-center-panel-${section}"`);
      expect(panels[0]).toContain(
        `aria-labelledby="admin-center-tab-${section}"`,
      );
    },
  );

  it("labels the active solution detail panel and roves its tab stop", () => {
    const markup = renderComparisonWorkspace();
    const tabs = openingTags(markup, "button", "tab");

    expect(tabs).toHaveLength(solutionDetailTabs.length);
    for (const tabName of solutionDetailTabs) {
      const tab = tagById(tabs, `solution-detail-tab-${tabName}`);
      expect(tab).toContain(
        `aria-controls="solution-detail-panel-${tabName}"`,
      );
      expect(tab).toContain(
        tabName === "parsed"
          ? 'aria-selected="true"'
          : 'aria-selected="false"',
      );
      expect(tab).toContain(
        tabName === "parsed" ? 'tabindex="0"' : 'tabindex="-1"',
      );
    }

    const panels = openingTags(markup, "div", "tabpanel");
    expect(panels).toHaveLength(1);
    expect(panels[0]).toContain('id="solution-detail-panel-parsed"');
    expect(panels[0]).toContain(
      'aria-labelledby="solution-detail-tab-parsed"',
    );
    expect(panels[0]).toContain('tabindex="0"');
  });
});

describe.each([
  ["Admin Center", getAdminTabDestination],
  ["solution detail", getSolutionDetailTabDestination],
] as const)("%s keyboard navigation", (_name, destination) => {
  it("moves, wraps, and jumps across the tab list", () => {
    expect(destination(0, 3, "ArrowRight")).toBe(1);
    expect(destination(2, 3, "ArrowRight")).toBe(0);
    expect(destination(2, 3, "ArrowLeft")).toBe(1);
    expect(destination(0, 3, "ArrowLeft")).toBe(2);
    expect(destination(1, 3, "Home")).toBe(0);
    expect(destination(1, 3, "End")).toBe(2);
  });

  it("leaves unrelated keys and invalid tab positions alone", () => {
    expect(destination(1, 3, "Enter")).toBeNull();
    expect(destination(-1, 3, "ArrowRight")).toBeNull();
    expect(destination(0, 0, "ArrowRight")).toBeNull();
  });
});
