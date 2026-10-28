from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from receipt_bench.evaluation.errors import RunConfigurationError


@dataclass(frozen=True)
class RunPlan:
    run_id: str
    sample_ids: tuple[str, ...]
    solution_ids: tuple[str, ...]

    @property
    def expected_pairs(self) -> set[tuple[str, str]]:
        return {
            (sample_id, solution_id)
            for sample_id in self.sample_ids
            for solution_id in self.solution_ids
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "format_version": "1.0",
            "run_id": self.run_id,
            "sample_ids": list(self.sample_ids),
            "solution_ids": list(self.solution_ids),
        }


def _string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
        or len(set(value)) != len(value)
    ):
        raise RunConfigurationError(f"Run plan {field} must contain unique strings")
    return tuple(value)


def load_run_plan(path: Path) -> RunPlan:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunConfigurationError(f"Cannot read run plan {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("format_version") != "1.0":
        raise RunConfigurationError(f"Run plan is invalid or unsupported: {path}")
    run_id = document.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RunConfigurationError("Run plan run_id must be a non-empty string")
    return RunPlan(
        run_id=run_id,
        sample_ids=_string_tuple(document.get("sample_ids"), field="sample_ids"),
        solution_ids=_string_tuple(document.get("solution_ids"), field="solution_ids"),
    )


def ensure_run_plan(path: Path, plan: RunPlan) -> RunPlan:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        plan.to_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        existing = load_run_plan(path)
        if existing != plan:
            raise RunConfigurationError(
                f"Run {plan.run_id!r} already has a different immutable run plan"
            ) from None
        return existing
    return plan


__all__ = ["RunPlan", "ensure_run_plan", "load_run_plan"]
