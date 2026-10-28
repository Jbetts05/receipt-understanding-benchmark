import { describe, expect, it } from "vitest";

import { MAX_UPLOAD_BYTES } from "./api";
import { mockSolutions } from "./mockData";
import type { CustomProjectDraft } from "./types";
import {
  buildCustomProjectPayload,
  customConfigurationPresentation,
  customSolutionCompatibility,
  validateCustomProjectDraft,
  validateSchemaText,
} from "./wizard";

const validSchema = JSON.stringify({
  $schema: "https://json-schema.org/draft/2020-12/schema",
  type: "object",
  required: ["vendor", "total"],
  properties: {
    vendor: { type: "string" },
    total: { type: "number" },
  },
});

function file(name = "領収書🧾.png", type = "image/png", size = 12) {
  return new File([new Uint8Array(size)], name, { type });
}

function draft(overrides: Partial<CustomProjectDraft> = {}): CustomProjectDraft {
  const upload = file();
  return {
    name: "Expense pilot",
    description: "Owner-managed sample",
    images: [
      {
        id: "image-1",
        filename: upload.name,
        media_type: upload.type,
        size_bytes: upload.size,
        file: upload,
        preview_url: null,
        gold_text: '{"vendor":"North Shop","total":12.4}',
      },
    ],
    schema_text: validSchema,
    prompt: "Extract every field and return only schema-valid JSON.",
    selected_solution_ids: ["offline-direct"],
    ...overrides,
  };
}

describe("custom project client preflight", () => {
  it("reports actionable JSON Schema structure and draft errors", () => {
    expect(validateSchemaText("{").issues[0]?.message).toMatch(/valid JSON/);
    expect(
      validateSchemaText(
        JSON.stringify({
          $schema: "http://json-schema.org/draft-07/schema#",
          type: "object",
          required: ["missing"],
          properties: { vendor: { type: "string" } },
        }),
      ).issues.map((issue) => issue.message),
    ).toEqual(
      expect.arrayContaining([
        expect.stringContaining("Draft 2020-12"),
        expect.stringContaining('"missing"'),
      ]),
    );
  });

  it("validates per-image gold against required field types", () => {
    const invalid = draft({
      images: [
        {
          ...draft().images[0]!,
          gold_text: '{"vendor":"North Shop","total":"12.40"}',
        },
      ],
    });
    expect(validateCustomProjectDraft(invalid, mockSolutions).issues).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          field: "gold",
          image_id: "image-1",
          message: expect.stringContaining("must be number"),
        }),
      ]),
    );
  });

  it("blocks oversize and missing upload files before POST", () => {
    const invalid = draft({
      images: [
        {
          id: "bad",
          filename: "bad.gif",
          media_type: "image/gif",
          size_bytes: MAX_UPLOAD_BYTES + 1,
          file: null,
          preview_url: null,
          gold_text: "",
        },
      ],
      selected_solution_ids: ["offline-ocr-text"],
    });
    const messages = validateCustomProjectDraft(invalid, mockSolutions).issues.map(
      (issue) => issue.message,
    );
    expect(messages).toEqual(
      expect.arrayContaining([
        expect.stringContaining("JPEG or PNG"),
        expect.stringContaining("no larger than"),
        expect.stringContaining("selected again"),
      ]),
    );
  });

  it("builds a typed payload only after complete preflight", () => {
    const value = draft();
    expect(validateCustomProjectDraft(value, mockSolutions).issues).toEqual([]);
    expect(buildCustomProjectPayload(value, mockSolutions)).toMatchObject({
      name: "Expense pilot",
      selected_solution_ids: ["offline-direct"],
      schema: { type: "object" },
    });
  });

  it("separates schema-capable paths from native and OCR reference evidence", () => {
    expect(customSolutionCompatibility(mockSolutions[0]!)).toBe("full");
    expect(customSolutionCompatibility(mockSolutions[2]!)).toBe(
      "partial_reference",
    );
    expect(customSolutionCompatibility(mockSolutions[3]!)).toBe("full");
    expect(
      customSolutionCompatibility(
        mockSolutions.find((solution) => solution.kind === "native_pipeline")!,
      ),
    ).toBe("full");
    expect(customSolutionCompatibility(mockSolutions[4]!)).toBe(
      "partial_reference",
    );
  });

  it("distinguishes lazy-registered custom analyzers from missing-fields-scored prebuilt output in the reason text", () => {
    const lazyRegistered = customConfigurationPresentation(mockSolutions[3]!);
    const prebuilt = customConfigurationPresentation(mockSolutions[2]!);
    const ocrText = customConfigurationPresentation(mockSolutions[4]!);

    expect(lazyRegistered.detail).toContain("configured without training");
    expect(prebuilt.detail).toContain("not force-mapped");
    expect(ocrText.label).toContain("Standalone OCR text output");
    expect(ocrText.detail).toContain("measures output, latency, and cost");
  });
});
