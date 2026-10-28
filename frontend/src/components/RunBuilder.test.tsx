import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { mockProjects, mockSamples, mockSolutions } from "../mockData";
import type { ReceiptSample, Solution } from "../types";
import { quickSelectSolutionIds, RunBuilder } from "./RunBuilder";

const project = mockProjects[0]!;

function renderBuilder(
  solutions: Solution[],
  samples: ReceiptSample[] = mockSamples,
): string {
  return renderToStaticMarkup(
    <RunBuilder
      project={project}
      samples={samples}
      solutions={solutions}
      selectedSampleIds={new Set(samples.map((sample) => sample.id))}
      selectedSolutionIds={new Set(["offline-direct"])}
      schemaConfirmed
      promptConfirmed
      executionMode="mock"
      launchPending={false}
      onSampleToggle={vi.fn()}
      onSolutionToggle={vi.fn()}
      onSelectSamples={vi.fn()}
      onSelectSolutions={vi.fn()}
      onSchemaConfirm={vi.fn()}
      onPromptConfirm={vi.fn()}
      onLaunch={vi.fn()}
    />,
  );
}

describe("RunBuilder quick selection", () => {
  it("selects the first five fully compatible offline solutions in catalog order", () => {
    expect(quickSelectSolutionIds(project, [...mockSolutions].reverse())).toEqual([
      "offline-direct",
      "offline-ocr",
      "offline-ocr-text",
      "offline-native-measurable",
      "offline-native-pipeline",
    ]);
  });

  it("uses an honest label when fewer than five solutions are fully compatible", () => {
    const reducedCatalog = mockSolutions.filter((solution) =>
      [
        "offline-reference",
        "offline-ocr-text",
        "offline-unsupported",
        "offline-direct",
        "offline-ocr",
      ].includes(solution.id),
    );

    expect(quickSelectSolutionIds(project, reducedCatalog)).toEqual([
      "offline-direct",
      "offline-ocr",
      "offline-ocr-text",
    ]);

    const markup = renderBuilder(reducedCatalog);
    expect(markup).toContain(">Quick 3<");
    expect(markup).not.toContain(">Quick five<");
  });
});

describe("RunBuilder runnable receipts", () => {
  it("labels a no-gold receipt as runnable and keeps the run eligible", () => {
    const noGoldSample = mockSamples.find((sample) => sample.gold === null)!;
    const markup = renderBuilder([mockSolutions[0]!], [noGoldSample]);
    const launchButton = markup.slice(markup.indexOf("launch-button"));

    expect(markup).toContain("Runnable · no gold");
    expect(markup).not.toContain("Scored · no gold");
    expect(launchButton).toContain("Launch mock run");
    expect(launchButton).not.toContain('disabled=""');
  });
});
