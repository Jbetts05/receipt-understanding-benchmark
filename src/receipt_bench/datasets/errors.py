from __future__ import annotations


class DatasetConfigError(ValueError):
    """Raised when ``configs/datasets.yaml`` is missing or fails validation."""


class DatasetFetchError(RuntimeError):
    """Raised when a network fetch fails or a remote response is missing/malformed.

    Covers transport failures, non-200 HTTP statuses, empty bodies, and responses
    that do not have the shape this package expects (e.g. a HF ``rows`` API
    response missing its ``row`` object). Never swallowed: acquisition aborts.
    """


class DatasetParsingError(ValueError):
    """Raised when native dataset content cannot be parsed into canonical fields.

    Covers malformed JSON/CSV payloads and unparsable date or monetary strings.
    """


__all__ = ["DatasetConfigError", "DatasetFetchError", "DatasetParsingError"]
