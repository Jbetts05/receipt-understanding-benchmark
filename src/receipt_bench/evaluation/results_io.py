from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from receipt_bench.evaluation.errors import ResultsIOError
from receipt_bench.models import SolutionOutput


def append_result(path: Path, output: SolutionOutput) -> None:
    """Append one normalized ``SolutionOutput`` record to a results JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(output.model_dump_json())
        handle.write("\n")


def read_results(path: Path) -> list[SolutionOutput]:
    """Read all normalized ``SolutionOutput`` records from a results JSONL file.

    Returns an empty list if the file does not exist yet (a run that has not started).
    Raises ``ResultsIOError`` for any malformed row.
    """
    if not path.is_file():
        return []
    results: list[SolutionOutput] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ResultsIOError(f"{path}:{line_number}: invalid JSON ({exc.msg})") from exc
            try:
                results.append(SolutionOutput.model_validate(payload))
            except ValidationError as exc:
                raise ResultsIOError(
                    f"{path}:{line_number}: invalid SolutionOutput ({exc})"
                ) from exc
    return results


def existing_pairs(path: Path) -> set[tuple[str, str]]:
    """Return the ``(sample_id, solution_id)`` pairs already recorded as complete."""
    return {(result.sample_id, result.solution_id) for result in read_results(path)}
