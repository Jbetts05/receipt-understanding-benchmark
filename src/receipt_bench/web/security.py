from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from collections.abc import Awaitable, Callable
from io import BytesIO
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from receipt_bench.web.settings import WebSettings

_LOCAL_AUTHORITY = re.compile(
    r"^(?:(?P<dns>localhost)|(?P<ipv4>127\.0\.0\.1)|\[(?P<ipv6>::1)\])"
    r"(?::(?P<port>[0-9]{1,5}))?$",
    re.IGNORECASE,
)
_FILENAME_B64 = re.compile(r"^[A-Za-z0-9_-]+$")
_UNSAFE_FILENAME_CHARS = frozenset('<>:"|?*')
_MAX_FILENAME_BYTES = 512
_MAX_FILENAME_HEADER_CHARS = (_MAX_FILENAME_BYTES * 4 + 2) // 3
_IMAGE_MEDIA_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png"}
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def clean_filename(value: str | None) -> str:
    raw = unicodedata.normalize(
        "NFC",
        (value or "receipt").replace("\\", "/").rsplit("/", maxsplit=1)[-1],
    )
    cleaned = "".join(
        "_"
        if character in _UNSAFE_FILENAME_CHARS
        or character == "/"
        or unicodedata.category(character).startswith("C")
        else character
        for character in raw
    ).strip(" .")
    return (cleaned or "receipt")[:120]


def decode_base64url_header(
    request: Request,
    name: str,
    *,
    max_decoded_bytes: int,
    required: bool,
) -> bytes | None:
    raw_name = name.casefold().encode("ascii")
    values = [
        value for key, value in request.scope.get("headers", []) if key.lower() == raw_name
    ]
    if not values:
        if required:
            raise HTTPException(status_code=400, detail=f"{name} is required")
        return None
    if len(values) != 1:
        raise HTTPException(status_code=400, detail=f"Exactly one {name} header is allowed")
    try:
        encoded = values[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{name} must be ASCII base64url") from exc
    max_chars = (max_decoded_bytes * 4 + 2) // 3
    if len(encoded) > max_chars:
        raise HTTPException(status_code=431, detail=f"{name} is too large")
    if not encoded or _FILENAME_B64.fullmatch(encoded) is None:
        raise HTTPException(status_code=400, detail=f"{name} is not valid base64url")
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        decoded = base64.b64decode(
            (encoded + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{name} is not valid base64url") from exc
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != encoded or len(decoded) > max_decoded_bytes:
        raise HTTPException(status_code=400, detail=f"{name} is not canonical base64url")
    return decoded


def decode_filename(request: Request) -> str:
    if "x-filename" in request.headers:
        raise HTTPException(
            status_code=400,
            detail="X-Filename is unsupported; use UTF-8 base64url in X-Filename-B64",
        )
    decoded = decode_base64url_header(
        request,
        "X-Filename-B64",
        max_decoded_bytes=_MAX_FILENAME_BYTES,
        required=True,
    )
    assert decoded is not None
    try:
        filename = decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Filename metadata is not valid UTF-8") from exc
    if not filename or any(
        unicodedata.category(character) in {"Cc", "Cs"} for character in filename
    ):
        raise HTTPException(status_code=400, detail="Filename metadata contains invalid characters")
    return clean_filename(filename)


def declared_body_size(request: Request, limit: int) -> int | None:
    content_length = request.headers.get("content-length")
    if content_length is None:
        return None
    try:
        size = int(content_length)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc
    if size < 0:
        raise HTTPException(status_code=400, detail="Invalid Content-Length header")
    if size > limit:
        raise HTTPException(status_code=413, detail=f"Request exceeds the {limit}-byte limit")
    return size


async def read_limited_body(request: Request, limit: int) -> bytes:
    declared_body_size(request, limit)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail=f"Upload exceeds the {limit}-byte limit")
        body.extend(chunk)
    if not body:
        raise HTTPException(status_code=400, detail="Upload body is empty")
    return bytes(body)


def validate_image(data: bytes, settings: WebSettings, declared_type: str | None) -> str:
    try:
        with Image.open(BytesIO(data)) as image:
            if (
                image.width > settings.image_max_dimension
                or image.height > settings.image_max_dimension
                or image.width * image.height > settings.image_max_pixels
            ):
                raise HTTPException(status_code=413, detail="Image dimensions exceed the limit")
            media_type = _IMAGE_MEDIA_TYPES.get(image.format or "")
            if media_type is None:
                raise HTTPException(status_code=415, detail="Supported formats are JPEG and PNG")
            image.verify()
    except HTTPException:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(status_code=413, detail="Image dimensions exceed the limit") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=415, detail="Upload is not a valid JPEG or PNG") from exc
    normalized_declared = (declared_type or "").split(";", maxsplit=1)[0].strip().casefold()
    if normalized_declared and normalized_declared not in {
        "application/octet-stream",
        media_type,
    }:
        raise HTTPException(
            status_code=415,
            detail="Declared Content-Type does not match the image",
        )
    return media_type


def _parse_authority(value: str) -> tuple[str, int | None] | None:
    if value.casefold() == "::1":
        return "::1", None
    if value.endswith("."):
        return None
    try:
        parsed = urlsplit(f"//{value}")
        port = parsed.port
    except ValueError:
        return None
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed.hostname.casefold(), port


def _header_values(scope: Scope, name: bytes) -> list[str]:
    return [
        value.decode("latin-1")
        for key, value in scope.get("headers", [])
        if key.lower() == name
    ]


class _CumulativeBodyLimitExceeded(Exception):
    pass


def _limited_receive(receive: Receive, max_bytes: int) -> Receive:
    total = 0
    exceeded = False

    async def receive_limited() -> Message:
        nonlocal exceeded, total
        if exceeded:
            raise _CumulativeBodyLimitExceeded
        message = await receive()
        if message["type"] == "http.request":
            total += len(message.get("body", b""))
            if total > max_bytes:
                exceeded = True
                raise _CumulativeBodyLimitExceeded
        return message

    return receive_limited


class RequestGuardMiddleware:
    def __init__(self, app: ASGIApp, *, settings: WebSettings) -> None:
        self._app = app
        self._settings = settings

    def _allowed_authority(self, authority: tuple[str, int | None]) -> bool:
        host, _ = authority
        if self._settings.profile == "local":
            return host in _LOOPBACK_HOSTS
        return host in self._settings.allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        method = str(scope.get("method", "")).upper()
        path = str(scope.get("path", ""))
        hosted_health_probe = (
            scope["type"] == "http"
            and self._settings.profile == "hosted"
            and method in {"GET", "HEAD"}
            and path in {"/api/health", "/api/ready"}
        )
        if hosted_health_probe:
            await self._app(scope, receive, send)
            return
        hosts = _header_values(scope, b"host")
        authority = _parse_authority(hosts[0]) if len(hosts) == 1 else None
        if authority is None or not self._allowed_authority(authority):
            await JSONResponse({"detail": "Host is not allowed"}, status_code=400)(
                scope, receive, send
            )
            return

        effective_authority = authority
        scheme = str(scope.get("scheme", "http")).casefold()
        if self._settings.trust_forwarded_headers:
            forwarded_hosts = _header_values(scope, b"x-forwarded-host")
            forwarded_protos = _header_values(scope, b"x-forwarded-proto")
            if forwarded_hosts:
                candidate = (
                    _parse_authority(forwarded_hosts[0])
                    if len(forwarded_hosts) == 1
                    else None
                )
                if candidate is None or not self._allowed_authority(candidate):
                    await JSONResponse(
                        {"detail": "Forwarded host is not allowed"},
                        status_code=400,
                    )(scope, receive, send)
                    return
                effective_authority = candidate
            if forwarded_protos:
                if len(forwarded_protos) != 1 or forwarded_protos[0].casefold() not in {
                    "http",
                    "https",
                }:
                    await JSONResponse(
                        {"detail": "Forwarded protocol is invalid"},
                        status_code=400,
                    )(scope, receive, send)
                    return
                scheme = forwarded_protos[0].casefold()
        if self._settings.require_https and scheme != "https":
            await JSONResponse({"detail": "HTTPS is required"}, status_code=400)(
                scope, receive, send
            )
            return

        if scope["type"] == "http" and path.startswith("/api/") and method not in {
            "GET",
            "HEAD",
            "OPTIONS",
            "TRACE",
        }:
            origins = _header_values(scope, b"origin")
            fetch_sites = [item.casefold() for item in _header_values(scope, b"sec-fetch-site")]
            if (
                len(origins) > 1
                or (origins and not _origin_matches(origins[0], effective_authority, scheme))
                or any(item in {"cross-site", "same-site"} for item in fetch_sites)
            ):
                await JSONResponse(
                    {"detail": "Cross-origin API mutations are forbidden"},
                    status_code=403,
                )(scope, receive, send)
                return
        await self._app(scope, receive, send)


def _origin_matches(
    origin: str,
    authority: tuple[str, int | None],
    scheme: str,
) -> bool:
    try:
        parsed = urlsplit(origin)
        origin_port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() != scheme
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    if parsed.hostname is None or parsed.hostname.casefold() != authority[0]:
        return False
    default = 443 if scheme == "https" else 80
    return (origin_port or default) == (authority[1] or default)


class JsonBodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        sample_multipart_max_bytes: int | None = None,
        dataset_multipart_max_bytes: int | None = None,
    ) -> None:
        self._app = app
        self._max_bytes = max_bytes
        self._sample_multipart_max_bytes = sample_multipart_max_bytes
        self._dataset_multipart_max_bytes = dataset_multipart_max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or str(scope.get("method", "")).upper() not in {
            "POST",
            "PUT",
            "PATCH",
        }:
            await self._app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        multipart_limit = (
            self._sample_multipart_max_bytes
            if path.endswith("/samples")
            else self._dataset_multipart_max_bytes
            if path == "/api/projects/import-labeled"
            else None
        )
        if multipart_limit is not None:
            content_types = _header_values(scope, b"content-type")
            if (
                not content_types
                or not content_types[0].casefold().startswith("multipart/form-data")
            ):
                await self._app(scope, receive, send)
                return
            limit_detail = f"Request exceeds the {multipart_limit}-byte limit"
            lengths = _header_values(scope, b"content-length")
            if lengths:
                try:
                    declared = int(lengths[0]) if len(lengths) == 1 else -1
                except ValueError:
                    declared = -1
                if declared < 0:
                    await JSONResponse(
                        {"detail": "Invalid Content-Length header"},
                        status_code=400,
                    )(scope, receive, send)
                    return
                if declared > multipart_limit:
                    await JSONResponse(
                        {"detail": limit_detail},
                        status_code=413,
                    )(scope, receive, send)
                    return
            try:
                await self._app(
                    scope,
                    _limited_receive(receive, multipart_limit),
                    send,
                )
            except _CumulativeBodyLimitExceeded:
                await JSONResponse(
                    {"detail": limit_detail},
                    status_code=413,
                )(scope, receive, send)
            return
        lengths = _header_values(scope, b"content-length")
        if lengths:
            try:
                declared = int(lengths[0]) if len(lengths) == 1 else self._max_bytes + 1
            except ValueError:
                declared = -1
            if declared < 0:
                await JSONResponse({"detail": "Invalid Content-Length header"}, status_code=400)(
                    scope, receive, send
                )
                return
            if declared > self._max_bytes:
                await JSONResponse({"detail": "Request body is too large"}, status_code=413)(
                    scope, receive, send
                )
                return

        buffered: list[Message] = []
        total = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.disconnect":
                break
            total += len(message.get("body", b""))
            if total > self._max_bytes:
                await JSONResponse({"detail": "Request body is too large"}, status_code=413)(
                    scope, receive, send
                )
                return
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            if buffered:
                return buffered.pop(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        await self._app(scope, replay, send)


async def add_security_headers(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    forwarded_proto = request.headers.get("x-forwarded-proto", "").casefold()
    if request.url.scheme.casefold() == "https" or forwarded_proto == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    )
    return response


__all__ = [
    "JsonBodyLimitMiddleware",
    "RequestGuardMiddleware",
    "add_security_headers",
    "clean_filename",
    "declared_body_size",
    "decode_base64url_header",
    "decode_filename",
    "read_limited_body",
    "validate_image",
]
