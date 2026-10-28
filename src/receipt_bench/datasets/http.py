from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from receipt_bench.datasets.errors import DatasetFetchError

#: A hex-encoded SHA256 digest: exactly 64 lowercase hex characters (matching
#: ``hashlib.sha256(...).hexdigest()``'s output format).
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

_FORCE_GUIDANCE = "Re-run with force=True to re-download and regenerate a verified cache."

USER_AGENT = (
    "receipt-bench-dataset-fetcher/0.1 "
    "(+https://github.com/receipt-bench; dataset acquisition for a reproducible "
    "receipt-extraction benchmark; contact via repository issues)"
)

#: Generous but bounded timeouts: short connect budget, longer read budget for
#: large mirror images and HF API responses.
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def sha256_bytes(data: bytes) -> str:
    """Hex-encoded SHA256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def fetch_bytes(client: httpx.Client, url: str) -> bytes:
    """GET ``url`` and return its response body.

    Fails explicitly (raises ``DatasetFetchError``, no silent fallback) on any
    transport-level error or non-200 HTTP status.
    """
    try:
        response = client.get(url, timeout=DEFAULT_TIMEOUT, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError as exc:
        raise DatasetFetchError(f"HTTP request failed for {url}: {exc}") from exc
    if response.status_code != 200:
        raise DatasetFetchError(f"Unexpected HTTP status {response.status_code} fetching {url}")
    return response.content


def write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically: write to a sibling temp file, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp_path.write_bytes(data)
    os.replace(tmp_path, path)


def relative_posix_path(path: Path, start: Path) -> str:
    """Relative path from ``start`` to ``path``, serialized with POSIX (``/``) separators.

    ``os.path.relpath`` returns OS-native separators (``\\`` on Windows), which
    makes a committed JSONL manifest non-portable across platforms. This
    always renders the relative path with forward slashes regardless of host
    OS, matching how the manifest is consumed elsewhere (plain string joins
    against ``manifest_dir``).
    """
    return os.path.relpath(path, start).replace(os.sep, "/")


def sidecar_path(destination: Path) -> Path:
    """Path of the SHA256 integrity sidecar file that accompanies a cached download.

    Every file this package caches to disk (SROIE images/key/box files, CORD
    images/ground-truth text) is written alongside a ``<name>.sha256`` sidecar
    containing the digest that was true at write time, so a later reuse of the
    cached file can detect mutation/corruption instead of silently trusting
    whatever bytes happen to be on disk.
    """
    return destination.with_name(f"{destination.name}.sha256")


def write_verified(path: Path, data: bytes) -> str:
    """Atomically write ``data`` to ``path`` and persist a matching ``.sha256`` sidecar.

    Both the content file and its sidecar are written via ``write_atomic``
    (temp-file-then-replace), so a crash mid-write can never leave a sidecar
    that describes different bytes than what actually landed at ``path``.
    Returns the hex SHA256 digest that was written to the sidecar.
    """
    digest = sha256_bytes(data)
    write_atomic(path, data)
    write_atomic(sidecar_path(path), f"{digest}\n".encode("ascii"))
    return digest


def read_verified(path: Path) -> bytes:
    """Read a previously cached file from disk and verify it against its ``.sha256`` sidecar.

    This is the read-side counterpart to ``write_verified``: it is the only
    sanctioned way to reuse a cached file without a network request, and it
    fails explicitly (raises ``DatasetFetchError``, no silent fallback) if:

    - ``path`` does not exist or is empty.
    - the sidecar file is missing entirely.
    - the sidecar's content is not a well-formed 64-character lowercase hex
      SHA256 digest.
    - the digest recomputed from the bytes actually on disk does not match
      the sidecar's recorded digest (the cached file was mutated, corrupted,
      or replaced since it was written).

    A missing/malformed/mismatched sidecar can never be silently accepted:
    every failure mode here means the cache cannot be trusted, so this always
    raises rather than falling back to trusting the file's raw content.
    """
    if not path.is_file():
        raise DatasetFetchError(f"Expected cached file does not exist: {path}")
    content = path.read_bytes()
    if not content:
        raise DatasetFetchError(f"Cached file is empty, refusing to reuse: {path}")

    sidecar = sidecar_path(path)
    if not sidecar.is_file():
        raise DatasetFetchError(
            f"Missing SHA256 integrity sidecar for cached file {path} "
            f"(expected {sidecar}); a cache without a sidecar cannot be "
            f"trusted. {_FORCE_GUIDANCE}"
        )
    sidecar_text = sidecar.read_text(encoding="utf-8").strip()
    if not _SHA256_HEX_RE.fullmatch(sidecar_text):
        raise DatasetFetchError(
            f"Malformed SHA256 integrity sidecar for cached file {path}: "
            f"{sidecar_text!r} is not a 64-character hex digest. {_FORCE_GUIDANCE}"
        )
    actual = sha256_bytes(content)
    if actual != sidecar_text:
        raise DatasetFetchError(
            f"Cached file {path} failed integrity verification: on-disk SHA256 "
            f"{actual} does not match its sidecar's recorded digest {sidecar_text}. "
            f"The cache may have been mutated or corrupted since it was "
            f"downloaded. {_FORCE_GUIDANCE}"
        )
    return content


@dataclass(frozen=True)
class DownloadResult:
    """Outcome of an idempotent download: where it lives, its digest, and its bytes."""

    path: Path
    sha256: str
    content: bytes


def ensure_downloaded(
    client: httpx.Client, url: str, destination: Path, *, force: bool = False
) -> DownloadResult:
    """Idempotently and atomically materialize ``url`` at ``destination``.

    If ``destination`` already exists and ``force`` is false, no network request
    is made: the existing bytes are re-read from disk and verified against
    their persisted ``.sha256`` sidecar (see ``read_verified``) -- a cache that
    has been mutated, corrupted, or is missing/has a malformed sidecar always
    fails explicitly rather than being silently accepted, which makes repeated
    ``prepare_dataset`` runs both idempotent (no redundant re-download) and
    tamper-evident. If ``force`` is true, or the file does not yet exist, the
    URL is fetched fresh, its HTTP status is verified, the body is written
    atomically along with a fresh ``.sha256`` sidecar (see ``write_verified``),
    and its digest is returned. Raises ``DatasetFetchError`` if the freshly
    fetched body is empty (see ``read_verified`` for the existing-file failure
    modes).
    """
    if destination.is_file() and not force:
        content = read_verified(destination)
        return DownloadResult(path=destination, sha256=sha256_bytes(content), content=content)

    content = fetch_bytes(client, url)
    if not content:
        raise DatasetFetchError(f"Empty response body fetching {url}")
    digest = write_verified(destination, content)
    return DownloadResult(path=destination, sha256=digest, content=content)


__all__ = [
    "DEFAULT_TIMEOUT",
    "USER_AGENT",
    "DownloadResult",
    "ensure_downloaded",
    "fetch_bytes",
    "read_verified",
    "relative_posix_path",
    "sha256_bytes",
    "sidecar_path",
    "write_atomic",
    "write_verified",
]
