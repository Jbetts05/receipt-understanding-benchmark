from __future__ import annotations


class ManifestError(ValueError):
    """Raised when a dataset manifest row cannot be parsed as a ``ReceiptSample``."""


class ResultsIOError(ValueError):
    """Raised when a normalized results JSONL file contains a malformed row."""


class WarmupIOError(ValueError):
    """Raised when a normalized warmups JSONL file contains a malformed row."""


class RunConfigurationError(RuntimeError):
    """Raised when the benchmark runner cannot proceed due to a configuration problem.

    Examples include an empty manifest, a config that references a solution with no
    matching adapter, or a raw artifact that already exists for a pair that is not yet
    recorded as complete (which would violate artifact immutability).
    """


class PricingConfigError(ValueError):
    """Raised when a present ``configs/pricing.yaml`` snapshot is malformed.

    The pricing snapshot is optional (its absence is not an error -- see
    :func:`receipt_bench.evaluation.pricing.load_pricing_snapshot`), but a file that
    does exist and cannot be parsed as a valid snapshot is never silently ignored.
    """
