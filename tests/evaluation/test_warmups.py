from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from receipt_bench.evaluation.errors import WarmupIOError
from receipt_bench.evaluation.warmups import (
    WarmupRecord,
    append_warmup,
    completed_warmup_counts,
    read_warmups,
)
from receipt_bench.models import ComponentMeasurement, Usage


def _record(solution_id: str, warmup_index: int, run_id: str = "20260101T000000Z") -> WarmupRecord:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return WarmupRecord(
        run_id=run_id,
        solution_id=solution_id,
        warmup_index=warmup_index,
        started_at=now,
        completed_at=now,
        raw_artifact=f"/artifacts/{solution_id}/{warmup_index}.json",
        components=[
            ComponentMeasurement(
                component="extraction",
                latency_ms=42.0,
                usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
                estimated_cost=0.01,
            )
        ],
    )


def test_read_warmups_returns_empty_list_when_file_missing(tmp_path: Path) -> None:
    assert read_warmups(tmp_path / "does-not-exist.jsonl") == []


def test_append_and_read_warmups_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "warmups.jsonl"
    append_warmup(path, _record("solution-a", 0))
    append_warmup(path, _record("solution-a", 1))
    append_warmup(path, _record("solution-b", 0))

    records = read_warmups(path)

    assert [(r.solution_id, r.warmup_index) for r in records] == [
        ("solution-a", 0),
        ("solution-a", 1),
        ("solution-b", 0),
    ]
    assert records[0].components[0].usage.input_tokens == 100


def test_append_warmup_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "run" / "warmups.jsonl"
    append_warmup(path, _record("solution-a", 0))
    assert path.is_file()


def test_read_warmups_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "warmups.jsonl"
    append_warmup(path, _record("solution-a", 0))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n   \n")
    assert len(read_warmups(path)) == 1


def test_read_warmups_rejects_invalid_json_with_line_number(tmp_path: Path) -> None:
    path = tmp_path / "warmups.jsonl"
    append_warmup(path, _record("solution-a", 0))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json at all\n")
    with pytest.raises(WarmupIOError, match=r":2: .*invalid JSON"):
        read_warmups(path)


def test_read_warmups_rejects_schema_violation(tmp_path: Path) -> None:
    path = tmp_path / "warmups.jsonl"
    path.write_text('{"solution_id": "solution-a"}\n', encoding="utf-8")
    with pytest.raises(WarmupIOError, match=r":1: .*invalid WarmupRecord"):
        read_warmups(path)


def test_completed_warmup_counts_groups_by_solution(tmp_path: Path) -> None:
    path = tmp_path / "warmups.jsonl"
    append_warmup(path, _record("solution-a", 0))
    append_warmup(path, _record("solution-a", 1))
    append_warmup(path, _record("solution-b", 0))

    assert completed_warmup_counts(path) == {"solution-a": 2, "solution-b": 1}


def test_completed_warmup_counts_empty_when_file_missing(tmp_path: Path) -> None:
    assert completed_warmup_counts(tmp_path / "does-not-exist.jsonl") == {}
