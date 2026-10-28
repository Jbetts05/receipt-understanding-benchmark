"""Generate deterministic source-id/selection metadata under ``data/manifests/``.

This does not download or read any images/annotations; it only materializes,
as committed JSON, the fully deterministic identifiers and URLs that
``receipt_bench.datasets.sroie``/``receipt_bench.datasets.cord`` derive from
``configs/datasets.yaml`` -- so provenance/selection is inspectable and
diffable without needing network access or the raw (gitignored) dataset
files.

Run as a script: ``python -m receipt_bench.datasets.generate_manifests``.
"""

from __future__ import annotations

import json
from pathlib import Path

from receipt_bench.datasets.config import DatasetsConfig
from receipt_bench.datasets.sroie import sroie_source_ids, sroie_urls

DEFAULT_CONFIG_PATH = Path("configs/datasets.yaml")
DEFAULT_OUTPUT_DIR = Path("data/manifests")


def build_sroie_source_metadata(config: DatasetsConfig) -> dict[str, object]:
    """Build the deterministic SROIE source-id/url listing for the config."""
    source = config.sroie
    ids = sroie_source_ids(source)
    return {
        "repo": source.repo,
        "commit": source.commit,
        "split": source.split,
        "count": len(ids),
        "source_ids": [
            {
                "sample_id": f"sroie-{source_id}",
                "source_id": source_id,
                "urls": sroie_urls(source, source_id),
            }
            for source_id in ids
        ],
    }


def build_cord_selection_metadata(config: DatasetsConfig) -> dict[str, object]:
    """Build the deterministic CORD row-selection listing for the config."""
    source = config.cord
    return {
        "dataset": source.dataset,
        "revision": source.revision,
        "split": source.split,
        "total_rows": source.total_rows,
        "sample_count": source.sample_count,
        "selection_seed": source.selection_seed,
        "selection_formula": (
            "sorted(random.Random(selection_seed).sample(range(total_rows), sample_count))"
        ),
        "selected_indices": source.selected_indices,
        "source_ids": [
            {
                "sample_id": f"cord-{index:03d}",
                "index": index,
                "rows_api_url": source.rows_api_template.format(offset=index),
            }
            for index in source.selected_indices
        ],
    }


def write_manifest_metadata(
    config_path: Path = DEFAULT_CONFIG_PATH, output_dir: Path = DEFAULT_OUTPUT_DIR
) -> tuple[Path, Path]:
    """Load ``config_path`` and write the two deterministic metadata JSON files.

    Returns the ``(sroie_path, cord_path)`` pair of files written.
    """
    config = DatasetsConfig.load(config_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    sroie_path = output_dir / "sroie_source_ids.json"
    cord_path = output_dir / "cord_selection.json"

    sroie_path.write_text(
        json.dumps(build_sroie_source_metadata(config), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    cord_path.write_text(
        json.dumps(build_cord_selection_metadata(config), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return sroie_path, cord_path


def main() -> None:
    sroie_path, cord_path = write_manifest_metadata()
    print(f"wrote {sroie_path}")
    print(f"wrote {cord_path}")


if __name__ == "__main__":
    main()
