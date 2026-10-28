"""Monotonic wall-clock latency measurement shared across provider adapters."""

from __future__ import annotations

import time


def elapsed_ms(start: float) -> float:
    """Return elapsed wall-clock milliseconds since ``start``.

    ``start`` must be a value previously returned by ``time.monotonic()``.
    """
    return (time.monotonic() - start) * 1000.0
