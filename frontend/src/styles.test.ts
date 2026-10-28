import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

function rgb(hex: string): [number, number, number] {
  const value = Number.parseInt(hex.slice(1), 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

function luminance(hex: string): number {
  const channels = rgb(hex).map((channel) => {
    const value = channel / 255;
    return value <= 0.04045
      ? value / 12.92
      : ((value + 0.055) / 1.055) ** 2.4;
  });
  return (
    0.2126 * channels[0]! + 0.7152 * channels[1]! + 0.0722 * channels[2]!
  );
}

function contrast(foreground: string, background: string): number {
  const light = Math.max(luminance(foreground), luminance(background));
  const dark = Math.min(luminance(foreground), luminance(background));
  return (light + 0.05) / (dark + 0.05);
}

function lightVariable(name: string): string {
  const root = css.match(/:root\s*\{([\s\S]*?)\}/)?.[1] ?? "";
  const value = root.match(new RegExp(`${name}:\\s*(#[0-9a-f]{6})`, "i"))?.[1];
  if (value === undefined) {
    throw new Error(`Missing ${name}`);
  }
  return value;
}

function darkVariable(name: string): string {
  const root =
    css.match(/html\[data-theme="dark"\]\s*\{([\s\S]*?)\}/)?.[1] ?? "";
  const value = root.match(new RegExp(`${name}:\\s*(#[0-9a-f]{6})`, "i"))?.[1];
  if (value === undefined) {
    throw new Error(`Missing dark ${name}`);
  }
  return value;
}

describe("Clawpilot status colors", () => {
  it("keeps warning and success text above WCAG AA on light status surfaces", () => {
    expect(
      contrast(lightVariable("--cp-success"), lightVariable("--cp-success-soft")),
    ).toBeGreaterThanOrEqual(4.5);
    expect(
      contrast(lightVariable("--cp-warning"), lightVariable("--cp-warning-soft")),
    ).toBeGreaterThanOrEqual(4.5);
  });

  it("keeps light danger and dark muted text above WCAG AA on their surfaces", () => {
    for (const surface of [
      "--cp-bg",
      "--cp-bg-elevated",
      "--cp-surface",
      "--cp-surface-soft",
      "--cp-danger-soft",
    ]) {
      expect(
        contrast(lightVariable("--cp-danger"), lightVariable(surface)),
      ).toBeGreaterThanOrEqual(4.5);
    }
    for (const surface of [
      "--cp-bg",
      "--cp-bg-elevated",
      "--cp-surface",
      "--cp-surface-soft",
      "--cp-nav-bg",
      "--cp-nav-surface",
    ]) {
      expect(
        contrast(darkVariable("--cp-text-muted"), darkVariable(surface)),
      ).toBeGreaterThanOrEqual(4.5);
    }
    for (const surface of ["--cp-nav-bg", "--cp-nav-surface"]) {
      expect(
        contrast(darkVariable("--cp-nav-muted"), darkVariable(surface)),
      ).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("keeps the Clawpilot token set identical across themes", () => {
    const light = (
      css.match(/:root\s*\{([\s\S]*?)\}/)?.[1] ?? ""
    ).match(/--cp-[\w-]+/g) ?? [];
    const dark = (
      css.match(/html\[data-theme="dark"\]\s*\{([\s\S]*?)\}/)?.[1] ?? ""
    ).match(/--cp-[\w-]+/g) ?? [];
    expect(dark.sort()).toEqual(light.sort());
  });

  it("uses Clawpilot variables rather than literal component colors", () => {
    const componentCss = css
      .replace(/:root\s*\{[\s\S]*?\}/, "")
      .replace(/html\[data-theme="dark"\]\s*\{[\s\S]*?\}/, "");
    expect(componentCss).not.toMatch(
      /(?:^|[;{])\s*(?:color|background(?:-color)?|border(?:-\w+)?-color|fill|stroke)\s*:\s*(?:#|rgba?\(|hsla?\()/im,
    );
  });

  it("hides the closed mobile drawer before interaction", () => {
    expect(css).toMatch(
      /@media \(max-width: 900px\)[\s\S]*?\.navigation\s*\{[\s\S]*?visibility:\s*hidden;[\s\S]*?pointer-events:\s*none;/,
    );
    expect(css).toMatch(
      /\.navigation\.is-open\s*\{[\s\S]*?visibility:\s*visible;[\s\S]*?pointer-events:\s*auto;/,
    );
  });

  it("moves recent runs into the navigation when the context rail is hidden", () => {
    expect(css).toMatch(
      /@media \(max-width: 1220px\)[\s\S]*?\.context-rail\s*\{[\s\S]*?display:\s*none;[\s\S]*?\.recent-runs-navigation\s*\{[\s\S]*?display:\s*flex;/,
    );
  });

  it("keeps reduced-motion treatment and evaluation layout in separate rules", () => {
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\}\s*\}\s*\/\* Evaluation studio composition \*\//,
    );
    expect(css).toContain(".receipt-image-viewport img");
  });

  it("keeps pinned contract disclosure and confirmations in explicit grid positions", () => {
    const rules = css.slice(css.indexOf("/* Pinned contract confirmation"));
    expect(rules).toMatch(
      /\.contract-confirmation > \.contract-disclosure\s*\{[\s\S]*?grid-column:\s*2;[\s\S]*?grid-row:\s*1 \/ span 2;/,
    );
    expect(rules).toMatch(
      /\.contract-confirmation > label:first-of-type\s*\{[\s\S]*?grid-column:\s*3;[\s\S]*?grid-row:\s*1;/,
    );
    expect(rules).toMatch(
      /\.contract-confirmation > label:last-of-type\s*\{[\s\S]*?grid-column:\s*3;[\s\S]*?grid-row:\s*2;/,
    );
    expect(rules).toContain("max-width: 68%");
    expect(rules).toContain("@media (max-width: 700px)");
  });

  it("keeps run builder and pinned contract tracks within the workspace column", () => {
    expect(css).toMatch(
      /\.builder-layout\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0,\s*0\.85fr\)\s+minmax\(0,\s*1\.15fr\);/,
    );

    const rules = css.slice(css.indexOf("/* Pinned contract confirmation"));
    expect(rules).toMatch(
      /\.contract-confirmation\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0,\s*0\.75fr\)\s+minmax\(0,\s*1\.2fr\)\s+minmax\(0,\s*0\.8fr\);/,
    );
    expect(rules).toMatch(
      /\.contract-confirmation > \*\s*\{[\s\S]*?min-width:\s*0;/,
    );
  });

  it("provides sorting controls for every rendered table surface", () => {
    const tableComponents = [
      "EvaluationStudio.tsx",
      "BatchResults.tsx",
      "ComparisonWorkspace.tsx",
      "LiveWorkspace.tsx",
      "RunsWorkspace.tsx",
    ];
    for (const filename of tableComponents) {
      const source = readFileSync(
        new URL(`./components/${filename}`, import.meta.url),
        "utf8",
      );
      expect(source).toMatch(
        /SortableColumnHeader|table-sort-button|sortMatrix|sortTasks/,
      );
    }
  });

  it("keeps the labelled import fast path responsive", () => {
    expect(css).toContain(".labeled-import-grid");
    expect(css).toMatch(
      /\.labeled-import-grid\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0,\s*0\.9fr\)\s+minmax\(0,\s*1\.1fr\);/,
    );
    expect(css).toMatch(
      /\.labeled-import-grid > \*\s*\{[\s\S]*?min-width:\s*0;/,
    );
    expect(css).toMatch(
      /@media \(max-width: 800px\)[\s\S]*?\.creation-mode,[\s\S]*?\.labeled-import-grid\s*\{[\s\S]*?grid-template-columns:\s*1fr;/,
    );
  });

  it("shows focus rings around composite search and project controls", () => {
    expect(css).toMatch(
      /\.search-field:focus-within,\s*\.context-project-select:focus-within\s*\{[\s\S]*?outline:\s*2px solid var\(--cp-accent\);[\s\S]*?outline-offset:\s*2px;/,
    );
  });

  it("isolates every horizontally scrollable table surface", () => {
    for (const selector of [
      ".task-matrix-scroll",
      ".aggregate-table-scroll",
      ".performance-table-scroll",
      ".comparison-matrix-scroll",
      ".admin-table-scroll",
      ".runs-table-scroll",
    ]) {
      expect(css).toContain(selector);
    }
    expect(css).toMatch(
      /\.task-matrix-scroll,[\s\S]*?\.runs-table-scroll\s*\{[\s\S]*?isolation:\s*isolate;[\s\S]*?overscroll-behavior-inline:\s*contain;/,
    );
  });

  it("keeps pinned comparison columns above scrolling content with opaque selection paint", () => {
    expect(css).toMatch(
      /\.comparison-matrix thead th\.gold-cell\s*\{\s*z-index:\s*6;/,
    );
    expect(css).toMatch(
      /\.comparison-matrix thead th:first-child\s*\{\s*z-index:\s*7;/,
    );
    expect(css).toMatch(
      /\.comparison-matrix tbody tr\.selected > th,[\s\S]*?\.comparison-matrix tbody tr\.selected > td\.gold-cell\s*\{\s*background:\s*var\(--cp-accent-selection\);/,
    );
  });

  it("keeps sticky table cells on fixed native table tracks", () => {
    expect(css).toMatch(
      /\.task-matrix th:first-child\s*\{[\s\S]*?width:\s*292px;[\s\S]*?min-width:\s*292px;[\s\S]*?max-width:\s*292px;[\s\S]*?overflow:\s*hidden;/,
    );
    expect(css).toMatch(
      /\.aggregate-table thead th:first-child,[\s\S]*?\.aggregate-table tbody th\s*\{[\s\S]*?width:\s*260px;[\s\S]*?min-width:\s*260px;[\s\S]*?max-width:\s*260px;[\s\S]*?overflow:\s*hidden;/,
    );
    expect(css).toMatch(
      /\.comparison-matrix thead th:first-child\s*\{[\s\S]*?width:\s*104px;[\s\S]*?min-width:\s*104px;[\s\S]*?max-width:\s*104px;[\s\S]*?overflow:\s*hidden;/,
    );
    expect(css).toMatch(
      /\.comparison-matrix thead th\.gold-cell\s*\{[\s\S]*?width:\s*158px;[\s\S]*?min-width:\s*158px;[\s\S]*?max-width:\s*158px;[\s\S]*?overflow:\s*hidden;/,
    );
    expect(css).not.toMatch(
      /\.task-matrix tbody th\s*\{[^}]*display:\s*(?:grid|flex|block)/,
    );
    expect(css).not.toMatch(
      /\.aggregate-table tbody th\s*\{[^}]*display:\s*(?:grid|flex|block)/,
    );
  });
});
