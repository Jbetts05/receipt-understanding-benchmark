from __future__ import annotations

from dataclasses import dataclass

from receipt_bench.models import ReceiptSample


@dataclass(frozen=True)
class DemoInput:
    receipt_id: str
    sample_id: str
    title: str
    filename: str
    image_bytes: bytes
    media_type: str
    sha256: str
    sample: ReceiptSample | None

    @property
    def has_gold(self) -> bool:
        return self.sample is not None


__all__ = ["DemoInput"]
