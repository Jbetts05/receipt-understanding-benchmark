from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")


def safe_segment(value: str) -> str:
    cleaned = _SAFE_SEGMENT.sub("_", value).strip("._")
    if not cleaned:
        raise ValueError(f"Value cannot form a safe path segment: {value!r}")
    return cleaned


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write_json(
        self,
        run_id: str,
        solution_id: str,
        sample_id: str,
        payload: Any,
    ) -> Path:
        target = (
            self.root
            / safe_segment(run_id)
            / "raw"
            / safe_segment(solution_id)
            / f"{safe_segment(sample_id)}.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return target
