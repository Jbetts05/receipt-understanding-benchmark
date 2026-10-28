"""Normalized JSONL persistence for per-solution warmup calls.

Warmup calls (see ``receipt_bench.evaluation.runner.run_benchmark``) are real,
potentially paid, provider calls made to absorb one-time connection/provider
warm-up cost before any measured pair runs. They are never scored and never
contribute to a solution's measured end-to-end latency, but discarding their
raw response and usage entirely would understate a run's actual spend. This
module gives them their own normalized record type and JSONL file
(``warmups.jsonl``), completely separate from ``results.jsonl``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import Field, ValidationError

from receipt_bench.evaluation.errors import WarmupIOError
from receipt_bench.models import ComponentMeasurement, StrictModel


class WarmupRecord(StrictModel):
    """One normalized record of a single warmup call for one solution.

    ``warmup_index`` is the 0-based position of this call among that
    solution's warmups in this run (``0`` .. ``config.warmup_requests - 1``).
    ``raw_artifact`` points at the immutable raw response written under this
    run's ``warmups/<solution_id>/`` namespace -- distinct from the ``raw/``
    namespace used for measured pairs, so warmup and measured artifacts are
    never confused with one another.
    """

    run_id: str
    solution_id: str
    warmup_index: int
    started_at: datetime
    completed_at: datetime
    raw_artifact: str
    components: list[ComponentMeasurement] = Field(default_factory=list)


def append_warmup(path: Path, record: WarmupRecord) -> None:
    """Append one normalized ``WarmupRecord`` to a warmups JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(record.model_dump_json())
        handle.write("\n")


def read_warmups(path: Path) -> list[WarmupRecord]:
    """Read all normalized ``WarmupRecord`` rows from a warmups JSONL file.

    Returns an empty list if the file does not exist (a run with
    ``warmup_requests=0``, or one that has not started warmups yet). Raises
    ``WarmupIOError`` for any malformed row, exactly like
    ``receipt_bench.evaluation.results_io.read_results`` does for results.
    """
    if not path.is_file():
        return []
    records: list[WarmupRecord] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise WarmupIOError(f"{path}:{line_number}: invalid JSON ({exc.msg})") from exc
            try:
                records.append(WarmupRecord.model_validate(payload))
            except ValidationError as exc:
                raise WarmupIOError(
                    f"{path}:{line_number}: invalid WarmupRecord ({exc})"
                ) from exc
    return records


def completed_warmup_counts(path: Path) -> dict[str, int]:
    """Return how many warmups are already recorded for each solution id.

    Used by the runner to resume: a solution with ``N`` already-recorded
    warmups only needs ``config.warmup_requests - N`` more, and one already at
    or above the configured count needs none re-run.
    """
    counts: dict[str, int] = {}
    for record in read_warmups(path):
        counts[record.solution_id] = counts.get(record.solution_id, 0) + 1
    return counts


__all__ = [
    "WarmupRecord",
    "append_warmup",
    "completed_warmup_counts",
    "read_warmups",
]
