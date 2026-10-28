import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { mockProjects, mockSamples } from "../mockData";
import type { ReceiptSample } from "../types";
import {
  DatasetBrowser,
  filterDatasetSamples,
  resolveVisibleSample,
  shouldFocusDatasetSearch,
} from "./DatasetBrowser";

const noop = () => undefined;

function shortcutEvent(
  overrides: Partial<Parameters<typeof shouldFocusDatasetSearch>[0]> = {},
): Parameters<typeof shouldFocusDatasetSearch>[0] {
  return {
    altKey: false,
    ctrlKey: false,
    defaultPrevented: false,
    key: "/",
    metaKey: false,
    target: null,
    ...overrides,
  };
}

describe("DatasetBrowser", () => {
  it("resolves selection from visible receipts and clears it for no matches", () => {
    const selectedSample = mockSamples[0]!;
    const visibleSamples = filterDatasetSamples(
      mockSamples,
      mockSamples[1]!.filename,
      "all",
    );

    expect(visibleSamples.map((sample) => sample.id)).toEqual([
      mockSamples[1]!.id,
    ]);
    expect(resolveVisibleSample(visibleSamples, selectedSample.id)?.id).toBe(
      mockSamples[1]!.id,
    );
    expect(
      resolveVisibleSample(
        filterDatasetSamples(mockSamples, "no such receipt", "all"),
        selectedSample.id,
      ),
    ).toBeNull();
  });

  it("limits the Gold-scored filter to gold-backed scored evidence", () => {
    const noGoldSample = mockSamples[1]!;
    expect(noGoldSample.scoring_role).toBe("scored");
    expect(noGoldSample.gold).toBeNull();

    expect(
      filterDatasetSamples(mockSamples, "", "scored").map(
        (sample) => sample.id,
      ),
    ).toEqual([mockSamples[0]!.id]);
  });

  it("keeps scored receipts without gold runnable outside the Gold-scored filter", () => {
    const noGoldSample = mockSamples[1]!;
    const markup = renderToStaticMarkup(
      <DatasetBrowser
        project={mockProjects[0]!}
        samples={mockSamples}
        selectedSampleId={noGoldSample.id}
        onSampleSelect={noop}
        onBuildRun={noop}
      />,
    );

    expect(markup).toContain("Gold-scored");
    expect(markup).toContain('aria-keyshortcuts="/"');
    expect(markup).toContain(">Build run</button>");
    expect(markup).toContain(">Use in run</button>");
    expect(markup).toContain(noGoldSample.title);
  });

  it.each(["INPUT", "TEXTAREA", "SELECT"])(
    "does not intercept slash while typing in a %s",
    (tagName) => {
      const target = { tagName } as unknown as EventTarget;
      expect(shouldFocusDatasetSearch(shortcutEvent({ target }))).toBe(false);
    },
  );

  it("does not intercept slash in contenteditable elements or modified shortcuts", () => {
    const contenteditable = {
      tagName: "DIV",
      isContentEditable: true,
    } as unknown as EventTarget;

    expect(
      shouldFocusDatasetSearch(shortcutEvent({ target: contenteditable })),
    ).toBe(false);
    expect(shouldFocusDatasetSearch(shortcutEvent({ ctrlKey: true }))).toBe(
      false,
    );
    expect(shouldFocusDatasetSearch(shortcutEvent({ key: "s" }))).toBe(false);
  });

  it("handles an unmodified slash outside editable controls", () => {
    const preventDefault = vi.fn();
    const event = {
      ...shortcutEvent({
        target: { tagName: "BUTTON" } as unknown as EventTarget,
      }),
      preventDefault,
    };

    expect(shouldFocusDatasetSearch(event)).toBe(true);
    expect(preventDefault).not.toHaveBeenCalled();
  });

  it("renders no evidence or run action when there are no visible receipts", () => {
    const selectedSample: ReceiptSample = mockSamples[0]!;
    const markup = renderToStaticMarkup(
      <DatasetBrowser
        project={mockProjects[0]!}
        samples={[]}
        selectedSampleId={selectedSample.id}
        onSampleSelect={noop}
        onBuildRun={noop}
      />,
    );

    expect(markup).toContain("No receipts match");
    expect(markup).not.toContain("Selected receipt evidence");
    expect(markup).not.toContain("Build run");
    expect(markup).not.toContain("Use in run");
  });
});
