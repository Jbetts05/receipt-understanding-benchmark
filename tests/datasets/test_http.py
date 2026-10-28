from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from receipt_bench.datasets.errors import DatasetFetchError
from receipt_bench.datasets.http import (
    ensure_downloaded,
    fetch_bytes,
    read_verified,
    relative_posix_path,
    sha256_bytes,
    sidecar_path,
    write_atomic,
    write_verified,
)


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def test_sha256_bytes_is_stable() -> None:
    assert sha256_bytes(b"hello") == sha256_bytes(b"hello")
    assert sha256_bytes(b"hello") != sha256_bytes(b"world")


def test_write_atomic_creates_parents_and_leaves_no_temp_file(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "dir" / "file.bin"
    write_atomic(destination, b"payload")

    assert destination.read_bytes() == b"payload"
    leftovers = list(destination.parent.glob("*.tmp-*"))
    assert leftovers == []


def test_fetch_bytes_returns_body_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok-body")

    with _client(httpx.MockTransport(handler)) as client:
        assert fetch_bytes(client, "https://example.invalid/file") == b"ok-body"


def test_fetch_bytes_raises_on_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    with (
        _client(httpx.MockTransport(handler)) as client,
        pytest.raises(DatasetFetchError, match="404"),
    ):
        fetch_bytes(client, "https://example.invalid/missing")


def test_fetch_bytes_raises_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with (
        _client(httpx.MockTransport(handler)) as client,
        pytest.raises(DatasetFetchError, match="boom"),
    ):
        fetch_bytes(client, "https://example.invalid/unreachable")


def test_ensure_downloaded_fetches_and_writes_once(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"downloaded-bytes")

    destination = tmp_path / "sample.bin"
    with _client(httpx.MockTransport(handler)) as client:
        result = ensure_downloaded(client, "https://example.invalid/sample", destination)

    assert result.content == b"downloaded-bytes"
    assert result.sha256 == sha256_bytes(b"downloaded-bytes")
    assert destination.read_bytes() == b"downloaded-bytes"
    assert calls == 1


def test_ensure_downloaded_is_idempotent_without_network(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"downloaded-bytes")

    destination = tmp_path / "sample.bin"
    with _client(httpx.MockTransport(handler)) as client:
        first = ensure_downloaded(client, "https://example.invalid/sample", destination)
        second = ensure_downloaded(client, "https://example.invalid/sample", destination)

    assert calls == 1  # second call must not hit the network
    assert second.content == first.content
    assert second.sha256 == first.sha256


def test_ensure_downloaded_force_redownloads(tmp_path: Path) -> None:
    responses = [b"first-bytes", b"second-bytes"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=responses.pop(0))

    destination = tmp_path / "sample.bin"
    with _client(httpx.MockTransport(handler)) as client:
        first = ensure_downloaded(client, "https://example.invalid/sample", destination)
        second = ensure_downloaded(
            client, "https://example.invalid/sample", destination, force=True
        )

    assert first.content == b"first-bytes"
    assert second.content == b"second-bytes"
    assert destination.read_bytes() == b"second-bytes"


def test_ensure_downloaded_raises_on_empty_response(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    destination = tmp_path / "sample.bin"
    with (
        _client(httpx.MockTransport(handler)) as client,
        pytest.raises(DatasetFetchError, match="Empty response body"),
    ):
        ensure_downloaded(client, "https://example.invalid/empty", destination)


def test_ensure_downloaded_raises_on_empty_existing_file(tmp_path: Path) -> None:
    destination = tmp_path / "sample.bin"
    destination.write_bytes(b"")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            "network should not be reached for an existing (even empty) file check"
        )

    with (
        _client(httpx.MockTransport(handler)) as client,
        pytest.raises(DatasetFetchError, match="empty"),
    ):
        ensure_downloaded(client, "https://example.invalid/sample", destination)


def test_ensure_downloaded_writes_sidecar_on_fresh_download(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"downloaded-bytes")

    destination = tmp_path / "sample.bin"
    with _client(httpx.MockTransport(handler)) as client:
        result = ensure_downloaded(client, "https://example.invalid/sample", destination)

    sidecar = sidecar_path(destination)
    assert sidecar.is_file()
    assert sidecar.read_text(encoding="utf-8").strip() == result.sha256
    assert result.sha256 == sha256_bytes(b"downloaded-bytes")
    leftovers = list(destination.parent.glob("*.tmp-*"))
    assert leftovers == []


def test_ensure_downloaded_reuse_verifies_against_sidecar(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"downloaded-bytes")

    destination = tmp_path / "sample.bin"
    with _client(httpx.MockTransport(handler)) as client:
        first = ensure_downloaded(client, "https://example.invalid/sample", destination)
        second = ensure_downloaded(client, "https://example.invalid/sample", destination)

    assert calls == 1  # second call verified against the sidecar, no network needed
    assert second.sha256 == first.sha256


def test_ensure_downloaded_rejects_reuse_with_missing_sidecar(tmp_path: Path) -> None:
    destination = tmp_path / "sample.bin"
    write_atomic(destination, b"unverified-bytes")  # no sidecar written

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network should not be reached for an existing-file check")

    with (
        _client(httpx.MockTransport(handler)) as client,
        pytest.raises(DatasetFetchError, match=r"[Mm]issing SHA256 integrity sidecar"),
    ):
        ensure_downloaded(client, "https://example.invalid/sample", destination)


def test_ensure_downloaded_rejects_reuse_with_malformed_sidecar(tmp_path: Path) -> None:
    destination = tmp_path / "sample.bin"
    write_atomic(destination, b"some-bytes")
    write_atomic(sidecar_path(destination), b"not-a-valid-digest")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network should not be reached for an existing-file check")

    with (
        _client(httpx.MockTransport(handler)) as client,
        pytest.raises(DatasetFetchError, match="Malformed SHA256 integrity sidecar"),
    ):
        ensure_downloaded(client, "https://example.invalid/sample", destination)


def test_ensure_downloaded_detects_tampered_cache_against_sidecar(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"original-bytes")

    destination = tmp_path / "sample.bin"
    with _client(httpx.MockTransport(handler)) as client:
        ensure_downloaded(client, "https://example.invalid/sample", destination)

    # Mutate the cached file in place without touching its sidecar -- the
    # sidecar now describes bytes that no longer match what's on disk.
    destination.write_bytes(b"mutated-bytes")

    def unreachable_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network should not be reached for an existing-file check")

    with (
        _client(httpx.MockTransport(unreachable_handler)) as client,
        pytest.raises(DatasetFetchError, match="failed integrity verification"),
    ):
        ensure_downloaded(client, "https://example.invalid/sample", destination)


def test_ensure_downloaded_force_overwrites_stale_or_tampered_cache(tmp_path: Path) -> None:
    responses = [b"first-bytes", b"second-bytes"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=responses.pop(0))

    destination = tmp_path / "sample.bin"
    with _client(httpx.MockTransport(handler)) as client:
        ensure_downloaded(client, "https://example.invalid/sample", destination)
        destination.write_bytes(b"tampered-bytes")  # would fail integrity verification
        result = ensure_downloaded(
            client, "https://example.invalid/sample", destination, force=True
        )

    assert result.content == b"second-bytes"
    assert sidecar_path(destination).read_text(encoding="utf-8").strip() == sha256_bytes(
        b"second-bytes"
    )


def test_write_verified_and_read_verified_round_trip(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "file.bin"
    digest = write_verified(destination, b"payload")

    assert digest == sha256_bytes(b"payload")
    assert read_verified(destination) == b"payload"
    assert sidecar_path(destination).read_text(encoding="utf-8").strip() == digest
    leftovers = list(destination.parent.glob("*.tmp-*"))
    assert leftovers == []


def test_read_verified_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DatasetFetchError, match="does not exist"):
        read_verified(tmp_path / "missing.bin")


def test_relative_posix_path_uses_forward_slashes_for_nested_traversal(tmp_path: Path) -> None:
    # Manifest and image directories are siblings, so the relative path must
    # traverse up and back down -- exactly the shape that previously produced
    # OS-native (backslash-on-Windows) separators via bare ``os.path.relpath``.
    manifest_dir = tmp_path / "manifests"
    image_path = tmp_path / "images" / "sroie" / "000.jpg"
    result = relative_posix_path(image_path, manifest_dir)

    assert "\\" not in result
    assert result == "../images/sroie/000.jpg"


def test_relative_posix_path_direct_child_has_no_leading_traversal(tmp_path: Path) -> None:
    manifest_dir = tmp_path
    image_path = tmp_path / "cord" / "003.jpg"
    result = relative_posix_path(image_path, manifest_dir)

    assert "\\" not in result
    assert result == "cord/003.jpg"
