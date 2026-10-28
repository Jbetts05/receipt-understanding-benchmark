import { presentationForCompatibility, requiresCustomAnalyzerRegistration } from "./domain";
import type { ConfigurationPresentation } from "./domain";
import {
  encodeFilenameHeader,
  MAX_GOLD_JSON_BYTES,
  MAX_UPLOAD_BYTES,
  MAX_UPLOAD_FILES,
  SUPPORTED_IMAGE_TYPES,
} from "./api";
import type {
  Compatibility,
  CustomProjectCreatePayload,
  CustomProjectDraft,
  JsonObject,
  JsonValue,
  Solution,
  ValidationIssue,
} from "./types";

export interface SchemaValidation {
  schema: JsonObject | null;
  issues: ValidationIssue[];
}

function isObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function validateSchemaText(schemaText: string): SchemaValidation {
  if (schemaText.trim() === "") {
    return {
      schema: null,
      issues: [{ field: "schema", message: "Add a JSON Schema." }],
    };
  }
  let value: unknown;
  try {
    value = JSON.parse(schemaText);
  } catch (error) {
    return {
      schema: null,
      issues: [
        {
          field: "schema",
          message: `Schema is not valid JSON: ${
            error instanceof Error ? error.message : "parse failed"
          }`,
        },
      ],
    };
  }
  if (!isObject(value)) {
    return {
      schema: null,
      issues: [{ field: "schema", message: "Schema root must be a JSON object." }],
    };
  }
  const issues: ValidationIssue[] = [];
  if (
    value.$schema !== undefined &&
    value.$schema !== "https://json-schema.org/draft/2020-12/schema"
  ) {
    issues.push({
      field: "schema",
      message: "Client preflight supports JSON Schema Draft 2020-12.",
    });
  }
  if (value.type !== "object") {
    issues.push({ field: "schema", message: 'Schema root type must be "object".' });
  }
  if (!isObject(value.properties)) {
    issues.push({ field: "schema", message: "Schema must declare object properties." });
  }
  if (
    value.required !== undefined &&
    (!Array.isArray(value.required) ||
      value.required.some((field) => typeof field !== "string"))
  ) {
    issues.push({
      field: "schema",
      message: "Schema required must be an array of property names.",
    });
  }
  const properties = isObject(value.properties) ? value.properties : null;
  if (properties !== null && Array.isArray(value.required)) {
    value.required.forEach((field) => {
      if (typeof field === "string" && !(field in properties)) {
        issues.push({
          field: "schema",
          message: `Required field "${field}" is missing from properties.`,
        });
      }
    });
  }
  return { schema: issues.length === 0 ? value : null, issues };
}

function matchesType(value: JsonValue, type: string): boolean {
  if (type === "null") {
    return value === null;
  }
  if (type === "array") {
    return Array.isArray(value);
  }
  if (type === "object") {
    return isObject(value);
  }
  if (type === "integer") {
    return typeof value === "number" && Number.isInteger(value);
  }
  return typeof value === type;
}

function goldIssues(
  schema: JsonObject,
  goldText: string,
  imageId: string,
): ValidationIssue[] {
  if (goldText.trim() === "") {
    return [];
  }
  if (new TextEncoder().encode(goldText).length > MAX_GOLD_JSON_BYTES) {
    return [
      {
        field: "gold",
        image_id: imageId,
        message: `Gold answer must be no larger than ${MAX_GOLD_JSON_BYTES / 1024} KiB.`,
      },
    ];
  }
  let value: unknown;
  try {
    value = JSON.parse(goldText);
  } catch {
    return [
      {
        field: "gold",
        image_id: imageId,
        message: "Gold answer is not valid JSON.",
      },
    ];
  }
  if (!isObject(value)) {
    return [
      {
        field: "gold",
        image_id: imageId,
        message: "Gold answer must be a JSON object.",
      },
    ];
  }
  const required = Array.isArray(schema.required)
    ? schema.required.filter((field): field is string => typeof field === "string")
    : [];
  const properties = isObject(schema.properties) ? schema.properties : {};
  const issues: ValidationIssue[] = [];
  required.forEach((field) => {
    if (!(field in value)) {
      issues.push({
        field: "gold",
        image_id: imageId,
        message: `Gold answer is missing required field "${field}".`,
      });
    }
  });
  Object.entries(value).forEach(([field, fieldValue]) => {
    const definition = properties[field];
    if (!isObject(definition) || typeof definition.type !== "string") {
      return;
    }
    if (!matchesType(fieldValue, definition.type)) {
      issues.push({
        field: "gold",
        image_id: imageId,
        message: `Gold field "${field}" must be ${definition.type}.`,
      });
    }
  });
  return issues;
}

/**
 * Pre-creation compatibility for a custom project. Every kind but `ocr_text`
 * is fully usable against an arbitrary custom schema: `direct`, `ocr_pipeline`,
 * and `native_pipeline` solutions are schema-constrained by the prompt, and
 * every `native` solution — whether an always-available prebuilt extractor or
 * one that otherwise needs a per-project custom-analyzer registration — is
 * "full" here, registering that analyzer lazily the first time it is used.
 * `ocr_text` remains unsupported because custom uploads carry no OCR
 * transcription ground truth to score against.
 */
export function customSolutionCompatibility(solution: Solution): Compatibility {
  if (
    solution.kind === "ocr_text" ||
    (solution.kind === "native" &&
      !requiresCustomAnalyzerRegistration(solution))
  ) {
    return "partial_reference";
  }
  return "full";
}

/**
 * Explicit, API-metadata-driven configuration presentation for the
 * pre-creation custom-project wizard, where no `Project` object (and thus no
 * project-declared `reason` string) exists yet.
 */
export function customConfigurationPresentation(
  solution: Solution,
): ConfigurationPresentation {
  const compatibility = customSolutionCompatibility(solution);
  const reason =
    solution.kind === "ocr_text"
      ? "Without imported OCR transcripts, this standalone OCR configuration measures output, latency, and cost but not OCR quality."
      : requiresCustomAnalyzerRegistration(solution)
        ? "A schema-hash-keyed Content Understanding analyzer is configured without training the first time it is used."
        : solution.kind === "native"
          ? "The native prebuilt output is retained for inspection, latency, and cost; arbitrary uploaded fields are not force-mapped."
          : null;
  return presentationForCompatibility(compatibility, solution, reason);
}

export function validateCustomProjectDraft(
  draft: CustomProjectDraft,
  solutions: Solution[],
): SchemaValidation {
  const schemaValidation = validateSchemaText(draft.schema_text);
  const issues = [...schemaValidation.issues];
  if (draft.name.trim().length < 2) {
    issues.push({
      field: "name",
      message: "Project name must contain at least two characters.",
    });
  }
  if (draft.images.length === 0) {
    issues.push({ field: "images", message: "Add at least one receipt image." });
  }
  if (draft.images.length > MAX_UPLOAD_FILES) {
    issues.push({
      field: "images",
      message: `Add no more than ${MAX_UPLOAD_FILES} receipt images.`,
    });
  }
  draft.images.forEach((image) => {
    if (!SUPPORTED_IMAGE_TYPES.has(image.media_type)) {
      issues.push({
        field: "images",
        image_id: image.id,
        message: `${image.filename} must be JPEG or PNG.`,
      });
    }
    if (image.size_bytes <= 0 || image.size_bytes > MAX_UPLOAD_BYTES) {
      issues.push({
        field: "images",
        image_id: image.id,
        message: `${image.filename} must be no larger than ${Math.round(
          MAX_UPLOAD_BYTES / 1024 / 1024,
        )} MB.`,
      });
    }
    if (image.file === null) {
      issues.push({
        field: "images",
        image_id: image.id,
        message: `${image.filename} must be selected again before upload.`,
      });
    }
    try {
      encodeFilenameHeader(image.filename);
    } catch (error) {
      issues.push({
        field: "images",
        image_id: image.id,
        message:
          error instanceof Error
            ? `${image.filename}: ${error.message}`
            : `${image.filename} has invalid filename metadata.`,
      });
    }
  });
  if (draft.prompt.trim().length < 12) {
    issues.push({
      field: "prompt",
      message: "Prompt instructions must contain at least 12 characters.",
    });
  }
  const selected = solutions.filter((solution) =>
    draft.selected_solution_ids.includes(solution.id),
  );
  if (selected.length === 0) {
    issues.push({
      field: "solutions",
      message: "Choose at least one compatible solution.",
    });
  }
  if (
    selected.some((solution) => customSolutionCompatibility(solution) === "unsupported")
  ) {
    issues.push({
      field: "solutions",
      message: "Remove unsupported solutions.",
    });
  }
  if (schemaValidation.schema !== null) {
    draft.images.forEach((image) => {
      issues.push(...goldIssues(schemaValidation.schema as JsonObject, image.gold_text, image.id));
    });
  }
  return {
    schema: issues.some((issue) => issue.field === "schema")
      ? null
      : schemaValidation.schema,
    issues,
  };
}

export function buildCustomProjectPayload(
  draft: CustomProjectDraft,
  solutions: Solution[],
): CustomProjectCreatePayload {
  const validation = validateCustomProjectDraft(draft, solutions);
  if (validation.schema === null || validation.issues.length > 0) {
    throw new Error("Custom project draft is not valid.");
  }
  return {
    name: draft.name.trim(),
    description: draft.description.trim(),
    schema: validation.schema,
    prompt: draft.prompt.trim(),
    selected_solution_ids: [...draft.selected_solution_ids],
  };
}

export function parseOptionalGold(value: string): JsonObject | null {
  if (value.trim() === "") {
    return null;
  }
  const parsed = JSON.parse(value) as unknown;
  if (!isObject(parsed)) {
    throw new Error("Gold answer must be a JSON object.");
  }
  return parsed;
}
