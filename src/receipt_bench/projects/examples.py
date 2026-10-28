from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]

from receipt_bench.projects.models import (
    ArtifactReference,
    ContractModel,
    JsonObject,
    Project,
    PromptVersion,
    Sample,
    ensure_json_object,
)
from receipt_bench.projects.normalization import normalize_answer


class ResolvedPromptExample(ContractModel):
    sample_id: str
    selection_reason: str
    image: ArtifactReference
    answer: JsonObject
    ocr_text: str | None = None


def _verified_bytes(reference: ArtifactReference, repository_root: Path) -> bytes:
    path = repository_root / Path(reference.path)
    if not path.is_file():
        raise FileNotFoundError(f"Prompt example artifact not found: {path}")
    content = path.read_bytes()
    if reference.sha256 is not None:
        actual = hashlib.sha256(content).hexdigest()
        if actual != reference.sha256:
            raise ValueError(
                f"Prompt example checksum mismatch for {path}: "
                f"declared {reference.sha256}, calculated {actual}"
            )
    return content


def load_gold_answer(
    project: Project,
    sample: Sample,
    *,
    repository_root: Path,
) -> JsonObject:
    raw = _verified_bytes(sample.gold, repository_root)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid gold JSON for {sample.sample_id}") from exc
    root = ensure_json_object(payload, context=f"Gold for {sample.sample_id}")

    if project.project_id == "sroie":
        required = ("company", "address", "date", "total")
        missing = [key for key in required if key not in root]
        if missing:
            raise ValueError(f"SROIE gold {sample.sample_id} is missing {missing}")
        answer = ensure_json_object(
            {key: root[key] for key in required},
            context=f"SROIE answer for {sample.sample_id}",
        )
    elif project.project_id == "cord-v2":
        gt_parse = root.get("gt_parse")
        answer = ensure_json_object(
            gt_parse,
            context=f"CORD gt_parse for {sample.sample_id}",
        )
    else:
        answer = root

    normalized = normalize_answer(project.normalization_profile, answer)
    validator = jsonschema.Draft202012Validator(project.project_schema.document)
    errors = sorted(validator.iter_errors(normalized), key=lambda error: list(error.path))
    if errors:
        raise ValueError(
            f"Gold for {sample.sample_id} violates its project schema: {errors[0].message}"
        )
    return normalized


def _sroie_ocr_text(raw: bytes, sample_id: str) -> str:
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Invalid SROIE OCR reference for {sample_id}") from exc
    lines: list[str] = []
    for row in csv.reader(io.StringIO(decoded)):
        if len(row) < 9:
            raise ValueError(f"Malformed SROIE OCR row for {sample_id}")
        text = ",".join(row[8:]).strip()
        if text:
            lines.append(text)
    if not lines:
        raise ValueError(f"Empty SROIE OCR reference for {sample_id}")
    return "\n".join(lines)


def _cord_ocr_text(raw: bytes, sample_id: str) -> str:
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid CORD OCR reference for {sample_id}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("valid_line"), list):
        raise ValueError(f"CORD OCR reference {sample_id} has no valid_line array")
    lines: list[str] = []
    for line in payload["valid_line"]:
        if not isinstance(line, dict) or not isinstance(line.get("words"), list):
            raise ValueError(f"Malformed CORD OCR line for {sample_id}")
        words: list[str] = []
        for word in line["words"]:
            if not isinstance(word, dict):
                continue
            text = word.get("text")
            if isinstance(text, str) and text.strip():
                words.append(text)
        if words:
            lines.append(" ".join(words))
    if not lines:
        raise ValueError(f"Empty CORD OCR reference for {sample_id}")
    return "\n".join(lines)


def load_ocr_example_text(
    project: Project,
    sample: Sample,
    *,
    repository_root: Path,
) -> str:
    if sample.ocr_reference is None:
        raise ValueError(f"Sample {sample.sample_id} has no OCR reference")
    raw = _verified_bytes(sample.ocr_reference, repository_root)
    if project.project_id == "sroie":
        return _sroie_ocr_text(raw, sample.sample_id)
    if project.project_id == "cord-v2":
        return _cord_ocr_text(raw, sample.sample_id)
    return raw.decode("utf-8")


def resolve_prompt_examples(
    project: Project,
    prompt: PromptVersion,
    *,
    repository_root: Path,
) -> tuple[ResolvedPromptExample, ...]:
    """Resolve artifact-backed examples without copying or embedding receipt images."""

    if prompt.project_id != project.project_id:
        raise ValueError("Prompt does not belong to project")
    resolved: list[ResolvedPromptExample] = []
    for sample_id in prompt.example_sample_ids:
        sample = project.sample(sample_id)
        if sample.scoring_role != "few-shot" or sample.few_shot_reason is None:
            raise ValueError(f"Prompt example {sample_id!r} is not excluded from scoring")
        _verified_bytes(sample.image, repository_root)
        resolved.append(
            ResolvedPromptExample(
                sample_id=sample.sample_id,
                selection_reason=sample.few_shot_reason,
                image=sample.image,
                answer=load_gold_answer(project, sample, repository_root=repository_root),
                ocr_text=(
                    load_ocr_example_text(project, sample, repository_root=repository_root)
                    if prompt.mode == "ocr-normalizer"
                    else None
                ),
            )
        )
    return tuple(resolved)


__all__ = [
    "ResolvedPromptExample",
    "load_gold_answer",
    "load_ocr_example_text",
    "resolve_prompt_examples",
]
