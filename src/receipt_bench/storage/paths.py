from __future__ import annotations

from receipt_bench.storage.validation import validate_artifact_path, validate_id


def project_record(project_id: str) -> str:
    return validate_artifact_path(f"projects/{validate_id(project_id)}/project.json")


def project_schema(project_id: str) -> str:
    return validate_artifact_path(f"projects/{validate_id(project_id)}/schema.json")


def project_prompt(project_id: str, prompt_version: str) -> str:
    return validate_artifact_path(
        f"projects/{validate_id(project_id)}/prompts/"
        f"{validate_id(prompt_version, field='prompt_version')}.json"
    )


def project_sample_index(project_id: str, digest: str) -> str:
    return validate_artifact_path(
        f"projects/{validate_id(project_id)}/sample-index/"
        f"{validate_id(digest, field='sample_index_digest')}.json"
    )


def sample_image(project_id: str, sample_id: str) -> str:
    return validate_artifact_path(
        f"projects/{validate_id(project_id)}/samples/"
        f"{validate_id(sample_id, field='sample_id')}/image"
    )


def sample_gold(project_id: str, sample_id: str) -> str:
    return validate_artifact_path(
        f"projects/{validate_id(project_id)}/samples/"
        f"{validate_id(sample_id, field='sample_id')}/gold.json"
    )


def sample_upload_image(project_id: str, sample_id: str, upload_id: str) -> str:
    return _sample_upload_path(project_id, sample_id, upload_id, "image")


def sample_upload_gold(project_id: str, sample_id: str, upload_id: str) -> str:
    return _sample_upload_path(project_id, sample_id, upload_id, "gold.json")


def sample_ocr_text(project_id: str, sample_id: str) -> str:
    return validate_artifact_path(
        f"projects/{validate_id(project_id)}/samples/"
        f"{validate_id(sample_id, field='sample_id')}/ocr.txt"
    )


def run_manifest(run_id: str) -> str:
    return validate_artifact_path(f"runs/{validate_id(run_id)}/manifest.json")


def run_event(run_id: str, sequence: int) -> str:
    if sequence < 1:
        raise ValueError("Event sequence must be positive")
    return validate_artifact_path(f"runs/{validate_id(run_id)}/events/{sequence:020d}.json")


def result_raw(run_id: str, sample_id: str, solution_id: str) -> str:
    return _result_path(run_id, sample_id, solution_id, "raw.json")


def result_parsed(run_id: str, sample_id: str, solution_id: str) -> str:
    return _result_path(run_id, sample_id, solution_id, "parsed.json")


def result_comparison(run_id: str, sample_id: str, solution_id: str) -> str:
    return _result_path(run_id, sample_id, solution_id, "comparison.json")


def result_telemetry(run_id: str, sample_id: str, solution_id: str) -> str:
    return _result_path(run_id, sample_id, solution_id, "telemetry.json")


def result_execution_attempt(
    run_id: str,
    sample_id: str,
    solution_id: str,
    attempt: int,
) -> str:
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    return _result_path(
        run_id,
        sample_id,
        solution_id,
        f"attempts/{attempt:06d}.json",
    )


def result_parse_attempt(
    run_id: str,
    sample_id: str,
    solution_id: str,
    attempt: int,
) -> str:
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    return _result_path(
        run_id,
        sample_id,
        solution_id,
        f"attempts/{attempt:06d}-parse.json",
    )


def result_attempt_parsed(
    run_id: str,
    sample_id: str,
    solution_id: str,
    attempt_id: str,
) -> str:
    return _result_path(
        run_id,
        sample_id,
        solution_id,
        f"task-attempts/{validate_id(attempt_id, field='attempt_id')}/parsed.json",
    )


def result_attempt_comparison(
    run_id: str,
    sample_id: str,
    solution_id: str,
    attempt_id: str,
) -> str:
    return _result_path(
        run_id,
        sample_id,
        solution_id,
        f"task-attempts/{validate_id(attempt_id, field='attempt_id')}/comparison.json",
    )


def result_attempt_telemetry(
    run_id: str,
    sample_id: str,
    solution_id: str,
    attempt_id: str,
) -> str:
    return _result_path(
        run_id,
        sample_id,
        solution_id,
        f"task-attempts/{validate_id(attempt_id, field='attempt_id')}/telemetry.json",
    )


def run_aggregate(run_id: str) -> str:
    return validate_artifact_path(f"runs/{validate_id(run_id)}/aggregate.json")


def run_download(run_id: str) -> str:
    return validate_artifact_path(f"runs/{validate_id(run_id)}/download.zip")


def run_tasks(run_id: str) -> str:
    return validate_artifact_path(f"runs/{validate_id(run_id)}/tasks.json")


def finalizer_aggregate(run_id: str, attempt_id: str) -> str:
    return validate_artifact_path(
        f"runs/{validate_id(run_id)}/finalizers/"
        f"{validate_id(attempt_id, field='attempt_id')}/aggregate.json"
    )


def finalizer_download(run_id: str, attempt_id: str) -> str:
    return validate_artifact_path(
        f"runs/{validate_id(run_id)}/finalizers/"
        f"{validate_id(attempt_id, field='attempt_id')}/download.zip"
    )


def seed_lock(seed_id: str) -> str:
    return validate_artifact_path(f"seeding/{validate_id(seed_id, field='seed_id')}/lock")


def seed_marker(seed_id: str) -> str:
    return validate_artifact_path(f"seeding/{validate_id(seed_id, field='seed_id')}/marker.json")


def _result_path(
    run_id: str,
    sample_id: str,
    solution_id: str,
    filename: str,
) -> str:
    return validate_artifact_path(
        f"runs/{validate_id(run_id)}/results/"
        f"{validate_id(sample_id, field='sample_id')}/"
        f"{validate_id(solution_id, field='solution_id')}/{filename}"
    )


def _sample_upload_path(
    project_id: str,
    sample_id: str,
    upload_id: str,
    filename: str,
) -> str:
    return validate_artifact_path(
        f"projects/{validate_id(project_id)}/samples/"
        f"{validate_id(sample_id, field='sample_id')}/uploads/"
        f"{validate_id(upload_id, field='upload_id')}/{filename}"
    )


__all__ = [
    "finalizer_aggregate",
    "finalizer_download",
    "project_prompt",
    "project_record",
    "project_sample_index",
    "project_schema",
    "result_attempt_comparison",
    "result_attempt_parsed",
    "result_attempt_telemetry",
    "result_comparison",
    "result_execution_attempt",
    "result_parse_attempt",
    "result_parsed",
    "result_raw",
    "result_telemetry",
    "run_aggregate",
    "run_download",
    "run_event",
    "run_manifest",
    "run_tasks",
    "sample_gold",
    "sample_image",
    "sample_ocr_text",
    "sample_upload_gold",
    "sample_upload_image",
    "seed_lock",
    "seed_marker",
]
