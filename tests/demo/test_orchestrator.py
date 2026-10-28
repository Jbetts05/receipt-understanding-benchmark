from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from receipt_bench.config import BenchmarkConfig, SolutionConfig
from receipt_bench.demo.api_models import DemoSolution
from receipt_bench.demo.domain import DemoInput
from receipt_bench.demo.orchestrator import DemoOrchestrator
from receipt_bench.demo.settings import DemoSettings


class _FailingRecord:
    run_id = "run-cancel"
    is_mock = True

    def __init__(self) -> None:
        self.inputs = [
            DemoInput(
                receipt_id="receipt-1",
                sample_id="sample-1",
                title="Receipt",
                filename="receipt.jpg",
                image_bytes=b"image",
                media_type="image/jpeg",
                sha256="0" * 64,
                sample=None,
            )
        ]
        self.solutions = [
            DemoSolution(
                id=f"solution-{index}",
                label=f"Solution {index}",
                order=index,
                kind="direct",
                api="azure-openai",
                lifecycle="ga",
            )
            for index in range(2)
        ]
        self.added_results = 0

    async def publish(self, event: str, data: dict[str, Any]) -> None:
        if event == "solution_started" and data["solution_id"] == "solution-0":
            raise RuntimeError("synthetic orchestration failure")
        await asyncio.sleep(5)

    async def add_result(self, result: Any) -> None:
        del result
        self.added_results += 1


def test_pair_failure_cancels_sibling_tasks(tmp_path: Path) -> None:
    settings = DemoSettings(
        mock_delay_scale=0,
        static_dir=tmp_path / "static",
    )
    config = BenchmarkConfig(
        schema_path=Path("schema.json"),
        dataset_manifest=Path("manifest.jsonl"),
        output_root=tmp_path,
        random_seed=1,
        solutions=[
            SolutionConfig(id=f"solution-{index}", kind="direct", api="azure-openai")
            for index in range(2)
        ],
    )
    orchestrator = DemoOrchestrator(
        settings=settings,
        benchmark_config=config,
        schema={},
        pricing=None,
    )
    record = _FailingRecord()

    async def run() -> None:
        with pytest.raises(RuntimeError, match="synthetic orchestration failure"):
            await orchestrator._run_pairs(record, None)  # type: ignore[arg-type]
        await asyncio.sleep(0)

    asyncio.run(run())
    assert record.added_results == 0
