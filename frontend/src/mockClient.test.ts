import { describe, expect, it } from "vitest";

import { MockProductClient } from "./mockClient";
import { mockSolutions } from "./mockData";

const validSchema = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  type: "object",
  required: ["vendor", "total"],
  properties: {
    vendor: { type: "string" },
    total: { type: "number" },
  },
};

describe("MockProductClient.createProject compatibility", () => {
  it("marks schema-capable solutions full and native or OCR evidence reference-only", async () => {
    const client = new MockProductClient();
    const project = await client.createProject({
      name: "Custom pilot",
      description: "",
      schema: validSchema,
      prompt: "Extract vendor and total.",
      selected_solution_ids: mockSolutions
        .filter((solution) => solution.kind !== "ocr_text")
        .map((solution) => solution.id),
    });

    const declarationByKind = new Map(
      project.compatibility.map((item) => [
        item.solution_id,
        item,
      ]),
    );
    for (const solution of mockSolutions) {
      const declaration = declarationByKind.get(solution.id)!;
      if (
        solution.kind === "ocr_text" ||
        ["offline-reference", "offline-native-measurable"].includes(solution.id)
      ) {
        expect(declaration.compatibility).toBe("partial_reference");
        expect(declaration.comparable_quality).toBe(false);
      } else {
        expect(declaration.compatibility).toBe("full");
        expect(declaration.comparable_quality).toBe(true);
      }
    }
  });

  it("distinguishes no-training schema configuration from fixed native output", async () => {
    const client = new MockProductClient();
    const project = await client.createProject({
      name: "Custom pilot 2",
      description: "",
      schema: validSchema,
      prompt: "Extract vendor and total.",
      selected_solution_ids: [],
    });

    const registrationGated = mockSolutions.find(
      (solution) => solution.id === "offline-unsupported",
    )!;
    const alwaysAvailableNative = mockSolutions.find(
      (solution) => solution.id === "offline-native-measurable",
    )!;
    const declarations = new Map(
      project.compatibility.map((item) => [item.solution_id, item]),
    );

    expect(declarations.get(registrationGated.id)?.reason).toMatch(
      /configured without training/i,
    );
    expect(declarations.get(alwaysAvailableNative.id)?.reason).toMatch(
      /without forcing it into an arbitrary schema/i,
    );
  });

  it("allows selecting standalone OCR as reference evidence", async () => {
    const client = new MockProductClient();
    const ocrTextSolution = mockSolutions.find(
      (solution) => solution.kind === "ocr_text",
    )!;
    const project = await client.createProject({
      name: "Custom pilot 3",
      description: "",
      schema: validSchema,
      prompt: "Extract vendor and total.",
      selected_solution_ids: [ocrTextSolution.id],
    });
    expect(project.selected_solution_ids).toEqual([ocrTextSolution.id]);
    expect(
      project.compatibility.find(
        (item) => item.solution_id === ocrTextSolution.id,
      )?.compatibility,
    ).toBe("partial_reference");
  });

  it("allows selecting a native_pipeline solution for a new custom project", async () => {
    const client = new MockProductClient();
    const nativePipelineSolution = mockSolutions.find(
      (solution) => solution.kind === "native_pipeline",
    )!;
    const project = await client.createProject({
      name: "Custom pilot 4",
      description: "",
      schema: validSchema,
      prompt: "Extract vendor and total.",
      selected_solution_ids: [nativePipelineSolution.id],
    });
    expect(project.selected_solution_ids).toEqual([nativePipelineSolution.id]);
    const declaration = project.compatibility.find(
      (item) => item.solution_id === nativePipelineSolution.id,
    );
    expect(declaration?.compatibility).toBe("full");
  });
});
