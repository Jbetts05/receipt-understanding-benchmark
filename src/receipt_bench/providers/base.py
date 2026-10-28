from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from receipt_bench.models import CanonicalReceipt, ComponentMeasurement


@dataclass(frozen=True)
class AdapterResponse:
    receipt: CanonicalReceipt
    raw_response: dict[str, Any]
    components: list[ComponentMeasurement]


class SolutionAdapter(ABC):
    def __init__(self, solution_id: str) -> None:
        self.solution_id = solution_id

    @abstractmethod
    def execute(self, image_bytes: bytes, media_type: str) -> AdapterResponse:
        """Run one solution against one receipt image."""


class AdapterConfigurationError(RuntimeError):
    """Raised when an adapter cannot be built from the configured environment."""


class ProviderResponseError(RuntimeError):
    """Raised when a provider returns a response that cannot be interpreted, or when
    the underlying SDK/transport call itself fails (network error, HTTP error status,
    auth failure, etc.).

    ``raw_response``/``components`` are optional and default to ``None``/an empty
    list: a hard failure before any usable response exists has legitimately nothing
    to attach, so normal transport-error call sites are unaffected by this extension.
    Some callers re-raise a *new* ``ProviderResponseError`` carrying the raw
    response/measurement(s) already incurred by an earlier, successful stage of a
    multi-stage adapter - e.g. ``OcrPipelineAdapter`` when its normalizer stage
    fails after a successful OCR call - so that cost/latency telemetry from the
    completed stage(s) is not discarded. Always chained via
    ``raise ProviderResponseError(...) from original_exc`` in that case.
    """

    def __init__(
        self,
        message: str,
        *,
        raw_response: dict[str, Any] | None = None,
        components: list[ComponentMeasurement] | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_response = raw_response
        self.components = components if components is not None else []


class InvalidSolutionOutput(ValueError):
    """Raised when a *successful* provider response cannot be parsed/validated into
    ``CanonicalReceipt``.

    Distinguishes an invalid semantic result (bad/incomplete JSON, a schema
    mismatch) from a hard provider/transport failure (``ProviderResponseError``):
    the call itself succeeded and consumed real latency/tokens, so the raw
    response and component measurement(s) already collected for it are attached
    here rather than discarded, letting the orchestrator record cost/latency
    telemetry for invalid results instead of losing them. Always raised via
    ``raise InvalidSolutionOutput(...) from original_exc``, where
    ``original_exc`` is the underlying ``json.JSONDecodeError``, ``TypeError``,
    or ``pydantic.ValidationError`` that made the response unparseable.
    """

    def __init__(
        self,
        message: str,
        *,
        raw_response: dict[str, Any],
        components: list[ComponentMeasurement],
    ) -> None:
        super().__init__(message)
        self.raw_response = raw_response
        self.components = components
