import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const index = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const initializer = readFileSync(
  new URL("../public/theme-init.js", import.meta.url),
  "utf8",
);
const builtIndex = readFileSync(
  new URL(
    "../../src/receipt_bench/demo/static/index.html",
    import.meta.url,
  ),
  "utf8",
);

function hasOnlyExternalScripts(markup: string): boolean {
  const scripts = [...markup.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)];
  return scripts.length > 0 && scripts.every((match) => /\bsrc=/.test(match[1] ?? ""));
}

function runThemeInitializer(search: string, prefersDark: boolean): string | null {
  let theme: string | null = null;
  const fakeWindow = {
    location: { search },
    matchMedia: () => ({ matches: prefersDark }),
  };
  const fakeDocument = {
    documentElement: {
      setAttribute: (name: string, value: string) => {
        if (name === "data-theme") {
          theme = value;
        }
      },
    },
  };
  Function("window", "document", initializer)(fakeWindow, fakeDocument);
  return theme;
}

describe("CSP-safe theme initialization", () => {
  it("uses only self-hosted external scripts in the HTML shell", () => {
    const scripts = [...index.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)];
    expect(scripts).toHaveLength(2);
    expect(hasOnlyExternalScripts(index)).toBe(true);
    expect(hasOnlyExternalScripts(builtIndex)).toBe(true);
    expect(index).toContain('src="/theme-init.js"');
    expect(builtIndex).toContain('src="/theme-init.js"');
  });

  it("honors an explicit dark theme and otherwise starts in light mode", () => {
    expect(runThemeInitializer("?scoutTheme=light", true)).toBe("light");
    expect(runThemeInitializer("?scoutTheme=dark", false)).toBe("dark");
    expect(runThemeInitializer("", true)).toBe("light");
    expect(runThemeInitializer("", false)).toBe("light");
  });
});
