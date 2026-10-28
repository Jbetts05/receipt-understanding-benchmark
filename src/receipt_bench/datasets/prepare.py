from __future__ import annotations

from pathlib import Path

import httpx

from receipt_bench.datasets.config import DatasetsConfig
from receipt_bench.datasets.cord import iter_cord_samples
from receipt_bench.datasets.http import USER_AGENT
from receipt_bench.datasets.manifest import write_manifest
from receipt_bench.datasets.sroie import iter_sroie_samples
from receipt_bench.models import ReceiptSample

DEFAULT_CONFIG_PATH: Path | None = None


def prepare_dataset(
    config_path: Path | None = DEFAULT_CONFIG_PATH,
    *,
    force: bool = False,
) -> Path:
    """Acquire and normalize the approved 80 SROIE + 20 CORD v2 benchmark dataset.

    Loads and validates ``config_path`` (default ``configs/datasets.yaml``),
    downloads each source's images (and, for SROIE, key/box files) through an
    ``httpx.Client`` configured with a descriptive User-Agent and explicit
    timeouts, maps each source's native ground truth to ``CanonicalReceipt``
    fields, and writes a deterministically ordered ``ReceiptSample`` JSONL
    manifest. Returns the manifest path (``config.manifest_path``, default
    ``data/manifests/benchmark.jsonl``).

    Downloads are atomic and idempotently re-verified from disk on repeat
    calls (no redundant network use) unless ``force`` is set, which always
    re-fetches and overwrites. Raises ``FileNotFoundError``/
    ``receipt_bench.datasets.errors.DatasetConfigError`` for a missing/invalid
    config, and ``receipt_bench.datasets.errors.DatasetFetchError`` /
    ``receipt_bench.datasets.errors.DatasetParsingError`` for any acquisition
    or normalization failure -- there is no silent fallback: any such failure
    aborts the whole run.
    """
    config = (
        DatasetsConfig.load_builtin()
        if config_path is None
        else DatasetsConfig.load(config_path)
    )
    manifest_path = config.manifest_path
    images_root = config.images_root
    manifest_dir = manifest_path.parent

    with httpx.Client(follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        samples: list[ReceiptSample] = []
        samples.extend(
            iter_sroie_samples(
                client,
                config.sroie,
                images_root=images_root,
                manifest_dir=manifest_dir,
                force=force,
            )
        )
        samples.extend(
            iter_cord_samples(
                client, config.cord, images_root=images_root, manifest_dir=manifest_dir, force=force
            )
        )

    samples.sort(key=lambda sample: sample.sample_id)
    return write_manifest(manifest_path, samples)


__all__ = ["DEFAULT_CONFIG_PATH", "prepare_dataset"]
