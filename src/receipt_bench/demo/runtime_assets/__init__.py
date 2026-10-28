from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path

from receipt_bench.demo.settings import DemoSettings


@dataclass(frozen=True)
class RuntimeAssetPaths:
    benchmark_config: Path
    schema: Path
    pricing: Path


@contextmanager
def runtime_asset_paths(settings: DemoSettings) -> Iterator[RuntimeAssetPaths]:
    """Resolve overrides or package data for both source and wheel installations."""
    package_root = files(__package__)
    with ExitStack() as stack:

        def resolve(override: Path | None, filename: str) -> Path:
            if override is not None:
                return override
            return stack.enter_context(as_file(package_root.joinpath(filename)))

        yield RuntimeAssetPaths(
            benchmark_config=resolve(settings.benchmark_config_path, "solutions.yaml"),
            schema=resolve(settings.schema_path, "expense-receipt.schema.json"),
            pricing=resolve(settings.pricing_path, "pricing.yaml"),
        )


__all__ = ["RuntimeAssetPaths", "runtime_asset_paths"]
