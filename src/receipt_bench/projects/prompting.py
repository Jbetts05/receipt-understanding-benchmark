from __future__ import annotations

import json

from receipt_bench.projects.models import ProjectSchema, PromptVersion


def render_user_prompt(
    prompt: PromptVersion,
    project_schema: ProjectSchema,
    *,
    ocr_text: str | None = None,
) -> str:
    if prompt.schema_id != project_schema.schema_id:
        raise ValueError("Prompt and project schema do not match")
    if prompt.mode == "ocr-normalizer":
        if ocr_text is None or not ocr_text.strip():
            raise ValueError("OCR-normalizer prompts require non-empty OCR text")
    elif ocr_text is not None:
        raise ValueError("Direct multimodal prompts do not accept OCR text")

    schema_json = json.dumps(
        project_schema.document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    rendered = prompt.user_template.replace("{schema_json}", schema_json)
    if prompt.mode == "ocr-normalizer":
        rendered = rendered.replace("{ocr_text}", ocr_text or "")
    return rendered


__all__ = ["render_user_prompt"]
