"""Dataset acquisition and normalization for the receipt-understanding benchmark.

This package owns the approved 80 SROIE + 20 CORD v2 dataset mix: pinned source
configuration (:mod:`receipt_bench.datasets.config`), atomic/idempotent HTTP
download and checksum utilities (:mod:`receipt_bench.datasets.http`), native
label parsing helpers (:mod:`receipt_bench.datasets.money`,
:mod:`receipt_bench.datasets.dates`), per-source acquisition and
native-label-to-canonical-receipt mapping (:mod:`receipt_bench.datasets.sroie`,
:mod:`receipt_bench.datasets.cord`), and the deterministic manifest writer
exposed as :func:`receipt_bench.datasets.prepare.prepare_dataset`.

No raw images are bundled with this package: it only ever downloads or reads
image bytes at prepare time, and image files are written under a
``.gitignore``-excluded directory (``data/raw/`` by default).
"""

from __future__ import annotations
