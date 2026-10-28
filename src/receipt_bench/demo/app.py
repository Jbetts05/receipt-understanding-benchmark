from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
import secrets
import unicodedata
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib.resources import files
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from starlette.types import ASGIApp, Receive, Scope, Send

from receipt_bench.config import BenchmarkConfig
from receipt_bench.demo.analysis import gold_fields
from receipt_bench.demo.api_models import (
    DatasetSummary,
    DemoConfigResponse,
    DemoReceiptSummary,
    DemoSolution,
    RunAccepted,
    RunCreateRequest,
    RunSnapshot,
    UploadAccepted,
)
from receipt_bench.demo.assets import (
    DemoAssetManifest,
    DemoReceiptAsset,
    image_path,
    load_asset_manifest,
    load_verified_image,
)
from receipt_bench.demo.domain import DemoInput
from receipt_bench.demo.orchestrator import (
    AdapterFactory,
    DemoOrchestrator,
    solution_catalog,
    solution_presets,
)
from receipt_bench.demo.runtime_assets import runtime_asset_paths
from receipt_bench.demo.settings import DemoSettings
from receipt_bench.demo.store import (
    ActiveRunLimitError,
    RunEvent,
    RunRecord,
    RunReservation,
    RunReservationConflictError,
    RunStore,
    UploadCapacityError,
    UploadReservation,
    UploadStore,
)
from receipt_bench.evaluation.pricing import load_pricing_snapshot
from receipt_bench.prompting import load_json_schema

_LOCAL_AUTHORITY = re.compile(
    r"^(?:(?P<dns>localhost)|(?P<ipv4>127\.0\.0\.1)|\[(?P<ipv6>::1)\])"
    r"(?::(?P<port>[0-9]{1,5}))?$",
    re.IGNORECASE,
)
_FILENAME_B64 = re.compile(r"^[A-Za-z0-9_-]+$")
_FILENAME_HEADER = b"x-filename-b64"
_LEGACY_FILENAME_HEADER = b"x-filename"
_MAX_FILENAME_BYTES = 512
_MAX_FILENAME_HEADER_CHARS = (_MAX_FILENAME_BYTES * 4 + 2) // 3
_UNSAFE_FILENAME_CHARS = frozenset('<>:"|?*')
_IMAGE_MEDIA_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
}
_MAX_IMAGE_PIXELS = 40_000_000


def _clean_filename(value: str | None) -> str:
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


def _decode_filename_header(request: Request) -> str:
    encoded_values = [
        value
        for name, value in request.scope.get("headers", [])
        if name.lower() == _FILENAME_HEADER
    ]
    legacy_values = [
        value
        for name, value in request.scope.get("headers", [])
        if name.lower() == _LEGACY_FILENAME_HEADER
    ]
    if legacy_values:
        raise HTTPException(
            status_code=400,
            detail="X-Filename is unsupported; send UTF-8 base64url in X-Filename-B64",
        )
    if not encoded_values:
        return "receipt"
    if len(encoded_values) != 1:
        raise HTTPException(status_code=400, detail="Exactly one X-Filename-B64 header is allowed")
    try:
        encoded = encoded_values[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail="Filename metadata must be ASCII base64url",
        ) from exc
    if len(encoded) > _MAX_FILENAME_HEADER_CHARS:
        raise HTTPException(status_code=431, detail="Filename metadata is too large")
    if not _FILENAME_B64.fullmatch(encoded):
        raise HTTPException(status_code=400, detail="Filename metadata is not valid base64url")
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        decoded = base64.b64decode(
            (encoded + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Filename metadata is not valid base64url",
        ) from exc
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != encoded:
        raise HTTPException(status_code=400, detail="Filename metadata is not canonical base64url")
    if len(decoded) > _MAX_FILENAME_BYTES:
        raise HTTPException(status_code=431, detail="Filename metadata is too large")
    try:
        filename = decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Filename metadata is not valid UTF-8") from exc
    if not filename or any(
        unicodedata.category(character) in {"Cc", "Cs"} for character in filename
    ):
        raise HTTPException(status_code=400, detail="Filename metadata contains invalid characters")
    return _clean_filename(filename)


def _parse_local_authority(value: str) -> tuple[str, int | None] | None:
    if value.casefold() == "::1":
        return "::1", None
    matched = _LOCAL_AUTHORITY.fullmatch(value)
    if matched is None:
        return None
    raw_port = matched.group("port")
    port = int(raw_port) if raw_port is not None else None
    if port is not None and not 1 <= port <= 65535:
        return None
    if matched.group("dns"):
        host = "localhost"
    elif matched.group("ipv4"):
        host = "127.0.0.1"
    else:
        host = "::1"
    return host, port


def _origin_matches_request(
    origin: str,
    *,
    authority: tuple[str, int | None],
    scheme: str,
) -> bool:
    try:
        parsed = urlsplit(origin)
        _ = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.scheme != scheme
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    origin_authority = _parse_local_authority(parsed.netloc)
    if origin_authority is None or origin_authority[0] != authority[0]:
        return False
    default_port = 443 if scheme == "https" else 80
    return (origin_authority[1] or default_port) == (authority[1] or default_port)


class LocalRequestGuardMiddleware:
    """Reject DNS-rebinding hosts and cross-origin API mutations before routing."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return

        host_values = [
            value.decode("latin-1")
            for name, value in scope.get("headers", [])
            if name.lower() == b"host"
        ]
        authority = _parse_local_authority(host_values[0]) if len(host_values) == 1 else None
        if authority is None:
            await JSONResponse(
                {"detail": "Host must be localhost, 127.0.0.1, or [::1]"},
                status_code=400,
            )(scope, receive, send)
            return

        if (
            scope["type"] == "http"
            and str(scope.get("path", "")).startswith("/api/")
            and str(scope.get("method", "")).upper() not in {"GET", "HEAD", "OPTIONS", "TRACE"}
        ):
            origin_values = [
                value.decode("latin-1")
                for name, value in scope.get("headers", [])
                if name.lower() == b"origin"
            ]
            fetch_sites = [
                value.decode("latin-1").casefold()
                for name, value in scope.get("headers", [])
                if name.lower() == b"sec-fetch-site"
            ]
            origin_allowed = (
                not origin_values
                or (
                    len(origin_values) == 1
                    and _origin_matches_request(
                        origin_values[0],
                        authority=authority,
                        scheme=str(scope.get("scheme", "http")).casefold(),
                    )
                )
            )
            if not origin_allowed or any(site == "cross-site" for site in fetch_sites):
                await JSONResponse(
                    {"detail": "Cross-origin API mutations are forbidden"},
                    status_code=403,
                )(scope, receive, send)
                return

        await self._app(scope, receive, send)


def _declared_body_size(request: Request, limit: int) -> int | None:
    content_length = request.headers.get("content-length")
    if content_length is None:
        return None
    try:
        declared_size = int(content_length)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc
    if declared_size < 0:
        raise HTTPException(status_code=400, detail="Invalid Content-Length header")
    if declared_size > limit:
        raise HTTPException(status_code=413, detail=f"Upload exceeds the {limit}-byte limit")
    return declared_size


async def _read_limited_body(
    request: Request,
    limit: int,
    reserve_bytes: Callable[[int], Awaitable[None]],
) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        next_size = len(body) + len(chunk)
        if next_size > limit:
            raise HTTPException(status_code=413, detail=f"Upload exceeds the {limit}-byte limit")
        if chunk:
            await reserve_bytes(next_size)
            body.extend(chunk)
    if not body:
        raise HTTPException(status_code=400, detail="Upload body is empty")
    return bytes(body)


def _validated_media_type(data: bytes) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.width * image.height > _MAX_IMAGE_PIXELS:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Image exceeds the {_MAX_IMAGE_PIXELS:,}-pixel limit",
                    )
                media_type = _IMAGE_MEDIA_TYPES.get(image.format or "")
                if media_type is None:
                    raise HTTPException(
                        status_code=415,
                        detail="Supported formats are JPEG and PNG",
                    )
                image.verify()
                return media_type
    except HTTPException:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(status_code=413, detail="Image dimensions are too large") from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(
            status_code=415,
            detail="The upload is not a valid JPEG or PNG receipt image",
        ) from exc


def _receipt_summaries(
    manifest: DemoAssetManifest,
    schema: dict[str, Any],
) -> list[DemoReceiptSummary]:
    return [
        DemoReceiptSummary(
            id=asset.demo_id,
            sample_id=asset.sample.sample_id,
            title=asset.title,
            description=asset.description,
            selection_reason=asset.selection_reason,
            image_url=f"/api/receipts/{asset.demo_id}/image",
            width=asset.image_width,
            height=asset.image_height,
            source_row_index=asset.source_row_index,
            labeled_fields=asset.sample.labeled_fields,
            gold_fields=gold_fields(asset.sample, schema),
        )
        for asset in manifest.receipts
    ]


def _resolve_solutions(
    request: RunCreateRequest,
    solutions: list[DemoSolution],
) -> list[DemoSolution]:
    known = {solution.id for solution in solutions}
    selected: set[str]
    if request.preset == "full":
        selected = set(known)
    elif request.preset == "fast":
        selected = {
            solution_id
            for preset in solution_presets(solutions)
            if preset.id == "fast"
            for solution_id in preset.solution_ids
        }
    else:
        selected = set(request.solution_ids)
        if not selected:
            raise HTTPException(
                status_code=422,
                detail="Custom runs require at least one solution_id",
            )
    unknown = sorted(selected - known)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown solution ids: {unknown}")
    return [solution for solution in solutions if solution.id in selected]


def _mock_run_id(
    request: RunCreateRequest,
    inputs: list[DemoInput],
    solutions: list[DemoSolution],
) -> str:
    if request.comparison_mode != "guided":
        raise ValueError("Deterministic run IDs are limited to bundled guided receipts")
    identity = {
        "mock_engine": "receipt-demo-v1",
        "comparison_mode": request.comparison_mode,
        "receipts": [{"sample_id": item.sample_id, "sha256": item.sha256} for item in inputs],
        "solutions": [solution.id for solution in solutions],
    }
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return f"mock-{hashlib.sha256(encoded).hexdigest()[:16]}"


def _random_run_id() -> str:
    return f"run-{secrets.token_hex(16)}"


def _run_manifest(
    *,
    run_id: str,
    request: RunCreateRequest,
    settings: DemoSettings,
    inputs: list[DemoInput],
    solutions: list[DemoSolution],
    assets_by_id: dict[str, DemoReceiptAsset],
    asset_manifest: DemoAssetManifest,
) -> dict[str, Any]:
    receipts: list[dict[str, Any]] = []
    for item in inputs:
        asset = assets_by_id.get(item.receipt_id)
        receipt: dict[str, Any] = {
            "id": item.receipt_id,
            "sample_id": item.sample_id,
            "filename": item.filename,
            "media_type": item.media_type,
            "sha256": item.sha256,
            "gold_available": item.has_gold,
        }
        if asset is not None:
            receipt.update(
                {
                    "dataset": "cord-v2",
                    "split": asset.sample.split,
                    "source_row_index": asset.source_row_index,
                    "labeled_fields": asset.sample.labeled_fields,
                }
            )
        receipts.append(receipt)

    return {
        "format_version": "1.0",
        "run_id": run_id,
        "execution_mode": settings.mode,
        "comparison_mode": request.comparison_mode,
        "mock_data": settings.mode == "mock",
        "scope": {
            "kind": "statistically_tiny_reproducible_demo",
            "receipt_count": len(inputs),
            "solution_count": len(solutions),
            "pair_count": len(inputs) * len(solutions),
            "benchmark_receipt_count": 100,
        },
        "receipts": receipts,
        "solutions": [solution.model_dump(mode="json") for solution in solutions],
        "concurrency": {
            "scope": "application_wide",
            "global": settings.max_parallel,
            "per_provider": settings.provider_concurrency,
            "per_solution": settings.solution_concurrency,
            "max_active_runs": settings.max_active_runs,
            "normalizer_provider_capacity_enforced": True,
        },
        "dataset": (
            asset_manifest.dataset.model_dump(mode="json")
            if request.comparison_mode == "guided"
            else None
        ),
        "evaluation": {
            "gold_metrics_available": request.comparison_mode == "guided",
            "upload_mode_is_exploratory": request.comparison_mode == "upload",
            "ranking_performed": False,
        },
        "privacy": {
            "provider_credentials_sent_to_browser": False,
            "uploads_written_to_disk": False,
            "raw_provider_responses_in_download": False,
        },
    }


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _json_download(content: bytes, filename: str) -> Response:
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _sse_message(event: RunEvent) -> str:
    data = json.dumps(
        event.data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"id: {event.sequence}\nevent: {event.event}\ndata: {data}\n\n"


async def _event_stream(request: Request, record: RunRecord) -> AsyncIterator[str]:
    raw_last_id = request.headers.get("last-event-id", "0")
    try:
        cursor = max(0, int(raw_last_id))
    except ValueError:
        cursor = 0

    while True:
        batch: list[RunEvent] = []
        terminal = False
        timed_out = False
        async with record.condition:
            if cursor >= len(record.events) and record.status == "running":
                try:
                    await asyncio.wait_for(record.condition.wait(), timeout=12.0)
                except TimeoutError:
                    timed_out = True
            batch = record.events[cursor:]
            terminal = record.status != "running" and cursor + len(batch) >= len(record.events)

        if await request.is_disconnected():
            return
        if timed_out and not batch:
            yield ": keep-alive\n\n"
        for event in batch:
            yield _sse_message(event)
            cursor += 1
        if terminal:
            return


def create_app(
    settings: DemoSettings | None = None,
    *,
    adapter_factory: AdapterFactory | None = None,
) -> FastAPI:
    resolved_settings = settings or DemoSettings()
    with runtime_asset_paths(resolved_settings) as runtime_paths:
        benchmark_config = BenchmarkConfig.load(runtime_paths.benchmark_config)
        schema = load_json_schema(runtime_paths.schema)
        pricing = load_pricing_snapshot(runtime_paths.pricing)
    benchmark_config = benchmark_config.model_copy(
        update={
            "solutions": [
                solution
                for solution in benchmark_config.solutions
                if solution.kind not in {"native_pipeline", "ocr_text"}
            ]
        }
    )
    asset_manifest = load_asset_manifest(resolved_settings.asset_manifest_path)
    third_party_notices = (
        files("receipt_bench.demo").joinpath("THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    )
    for asset in asset_manifest.receipts:
        load_verified_image(resolved_settings.asset_manifest_path, asset)

    solutions = solution_catalog(benchmark_config)
    presets = solution_presets(solutions)
    assets_by_id = {asset.demo_id: asset for asset in asset_manifest.receipts}
    upload_store = UploadStore(
        ttl_seconds=resolved_settings.upload_ttl_seconds,
        max_items=resolved_settings.upload_max_items,
        max_total_bytes=resolved_settings.upload_max_total_bytes,
    )
    run_store = RunStore(
        ttl_seconds=resolved_settings.run_ttl_seconds,
        max_runs=resolved_settings.max_retained_runs,
        max_active_runs=resolved_settings.max_active_runs,
    )
    orchestrator = DemoOrchestrator(
        settings=resolved_settings,
        benchmark_config=benchmark_config,
        schema=schema,
        pricing=pricing,
        adapter_factory=adapter_factory,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async def prune_expired_uploads() -> None:
            while True:
                await asyncio.sleep(resolved_settings.upload_cleanup_interval_seconds)
                await upload_store.prune_expired()

        cleanup_task = asyncio.create_task(prune_expired_uploads())
        try:
            yield
        finally:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
            await run_store.close()
            await upload_store.close()

    app = FastAPI(
        title="Receipt comparison demo",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(LocalRequestGuardMiddleware)

    @app.middleware("http")
    async def security_headers(
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> Response:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "mode": resolved_settings.mode,
            "mock": resolved_settings.mode == "mock",
            "live_execution_enabled": resolved_settings.live_execution_enabled,
        }

    @app.get("/api/config", response_model=DemoConfigResponse)
    async def config() -> DemoConfigResponse:
        dataset = asset_manifest.dataset
        return DemoConfigResponse(
            execution_mode=resolved_settings.mode,
            is_mock=resolved_settings.mode == "mock",
            live_execution_enabled=resolved_settings.live_execution_enabled,
            receipts=_receipt_summaries(asset_manifest, schema),
            solutions=solutions,
            presets=presets,
            dataset=DatasetSummary(
                name=dataset.name,
                id=dataset.id,
                revision=dataset.revision,
                split=dataset.split,
                license=dataset.license,
                license_url=dataset.license_url,
                citation=dataset.citation,
                annotation_scope=dataset.annotation_scope,
            ),
            upload_max_bytes=resolved_settings.upload_max_bytes,
            notices=[
                "This is a statistically tiny three-receipt demonstration, not the "
                "100-receipt benchmark.",
                "Accuracy and field matching are valid only for bundled CORD receipts "
                "with canonical gold labels.",
                "Upload mode is exploratory and reports no accuracy.",
                (
                    "All displayed outputs, timings, and costs are deterministic mock data."
                    if resolved_settings.mode == "mock"
                    else (
                        "Provider calls run server-side; credentials are never sent to the browser."
                    )
                ),
            ],
        )

    @app.get("/api/notices", response_class=PlainTextResponse)
    async def notices() -> PlainTextResponse:
        return PlainTextResponse(
            third_party_notices,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/api/receipts/{receipt_id}/image")
    async def receipt_image(receipt_id: str) -> FileResponse:
        asset = assets_by_id.get(receipt_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="Unknown bundled receipt")
        path = image_path(resolved_settings.asset_manifest_path, asset)
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "ETag": f'"{asset.sample.image_sha256}"',
            },
        )

    @app.post("/api/uploads", response_model=UploadAccepted, status_code=201)
    async def upload(request: Request) -> UploadAccepted:
        filename = _decode_filename_header(request)
        declared_size = _declared_body_size(request, resolved_settings.upload_max_bytes)
        try:
            reservation: UploadReservation = await upload_store.reserve(
                declared_size if declared_size is not None else resolved_settings.upload_max_bytes
            )
        except UploadCapacityError as exc:
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={
                    "Retry-After": str(int(resolved_settings.upload_cleanup_interval_seconds))
                },
            ) from exc

        async def reserve_body_bytes(size: int) -> None:
            await upload_store.resize_reservation(reservation, size)

        try:
            try:
                data = await _read_limited_body(
                    request,
                    resolved_settings.upload_max_bytes,
                    reserve_body_bytes,
                )
            except UploadCapacityError as exc:
                raise HTTPException(
                    status_code=429,
                    detail=str(exc),
                    headers={
                        "Retry-After": str(int(resolved_settings.upload_cleanup_interval_seconds))
                    },
                ) from exc
            media_type = _validated_media_type(data)
            digest = hashlib.sha256(data).hexdigest()
            item = await upload_store.commit(
                reservation=reservation,
                filename=filename,
                media_type=media_type,
                data=data,
                sha256=digest,
            )
        finally:
            await upload_store.release_reservation(reservation)
        return UploadAccepted(
            token=item.token,
            filename=item.filename,
            media_type=item.media_type,
            size_bytes=len(item.data),
            sha256=item.sha256,
            expires_in_seconds=resolved_settings.upload_ttl_seconds,
        )

    @app.delete("/api/uploads/{token}", status_code=204)
    async def discard_upload(token: str) -> Response:
        await upload_store.discard(token)
        return Response(status_code=204)

    async def build_inputs(run_request: RunCreateRequest) -> list[DemoInput]:
        if run_request.comparison_mode == "guided":
            if run_request.upload_tokens:
                raise HTTPException(
                    status_code=422,
                    detail="Guided runs cannot include upload_tokens",
                )
            selected_ids = set(run_request.receipt_ids or assets_by_id)
            unknown = sorted(selected_ids - assets_by_id.keys())
            if unknown:
                raise HTTPException(status_code=422, detail=f"Unknown receipt ids: {unknown}")
            selected_assets = [
                asset for asset in asset_manifest.receipts if asset.demo_id in selected_ids
            ]
            return [
                DemoInput(
                    receipt_id=asset.demo_id,
                    sample_id=asset.sample.sample_id,
                    title=asset.title,
                    filename=Path(asset.sample.image_path).name,
                    image_bytes=load_verified_image(
                        resolved_settings.asset_manifest_path,
                        asset,
                    ),
                    media_type="image/jpeg",
                    sha256=asset.sample.image_sha256,
                    sample=asset.sample,
                )
                for asset in selected_assets
            ]

        if run_request.receipt_ids:
            raise HTTPException(
                status_code=422,
                detail="Upload runs cannot include bundled receipt_ids",
            )
        if not run_request.upload_tokens:
            raise HTTPException(
                status_code=422,
                detail="Upload runs require at least one upload_token",
            )
        try:
            uploads = await upload_store.take_many(run_request.upload_tokens)
        except KeyError as exc:
            raise HTTPException(status_code=410, detail=str(exc.args[0])) from exc
        return [
            DemoInput(
                receipt_id=f"upload-{index + 1}-{item.sha256[:8]}",
                sample_id=f"upload-{item.sha256[:12]}",
                title=item.filename,
                filename=item.filename,
                image_bytes=item.data,
                media_type=item.media_type,
                sha256=item.sha256,
                sample=None,
            )
            for index, item in enumerate(uploads)
        ]

    @app.post("/api/runs", response_model=RunAccepted, status_code=202)
    async def create_run(run_request: RunCreateRequest) -> RunAccepted:
        if resolved_settings.mode == "azure" and not resolved_settings.live_execution_enabled:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Azure mode is safety-locked. Set RECEIPT_DEMO_ALLOW_LIVE=true "
                    "server-side to permit paid provider calls."
                ),
            )
        selected_solutions = _resolve_solutions(run_request, solutions)
        inputs: list[DemoInput] = []
        reservation: RunReservation | None = None
        try:
            if run_request.comparison_mode == "guided":
                inputs = await build_inputs(run_request)
                run_id = (
                    _mock_run_id(run_request, inputs, selected_solutions)
                    if resolved_settings.mode == "mock"
                    else _random_run_id()
                )
            else:
                run_id = _random_run_id()

            try:
                reservation = await run_store.reserve(run_id)
            except ActiveRunLimitError as exc:
                raise HTTPException(
                    status_code=429,
                    detail=str(exc),
                    headers={"Retry-After": "1"},
                ) from exc
            except RunReservationConflictError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=str(exc),
                    headers={"Retry-After": "1"},
                ) from exc

            if reservation.is_existing:
                inputs.clear()
                record = await run_store.get(run_id)
            else:
                if run_request.comparison_mode == "upload":
                    # Capacity is reserved before single-use tokens are consumed.
                    inputs = await build_inputs(run_request)
                manifest = _run_manifest(
                    run_id=run_id,
                    request=run_request,
                    settings=resolved_settings,
                    inputs=inputs,
                    solutions=selected_solutions,
                    assets_by_id=assets_by_id,
                    asset_manifest=asset_manifest,
                )
                record = RunRecord(
                    run_id=run_id,
                    comparison_mode=run_request.comparison_mode,
                    execution_mode=resolved_settings.mode,
                    inputs=inputs,
                    solutions=selected_solutions,
                    manifest_bytes=_json_bytes(manifest),
                    total_pairs=len(inputs) * len(selected_solutions),
                )
                created = await run_store.create(record, reservation)
                reservation = None
                if created:
                    task = asyncio.create_task(orchestrator.run(record))
                    await run_store.attach_task(run_id, task)
                else:
                    record.release_input_bytes()
                    record = await run_store.get(run_id)
        finally:
            if reservation is not None:
                await run_store.release_reservation(reservation)

        return RunAccepted(
            run_id=run_id,
            status=record.status,
            total_pairs=record.total_pairs,
            event_url=f"/api/runs/{run_id}/events",
            snapshot_url=f"/api/runs/{run_id}",
            manifest_url=f"/api/runs/{run_id}/manifest",
            results_url=f"/api/runs/{run_id}/results",
        )

    async def get_record(run_id: str) -> RunRecord:
        try:
            return await run_store.get(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found or expired") from exc

    @app.get("/api/runs/{run_id}", response_model=RunSnapshot)
    async def run_snapshot(run_id: str) -> RunSnapshot:
        return (await get_record(run_id)).snapshot()

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request) -> StreamingResponse:
        record = await get_record(run_id)
        return StreamingResponse(
            _event_stream(request, record),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/runs/{run_id}/manifest")
    async def download_manifest(run_id: str) -> Response:
        record = await get_record(run_id)
        return _json_download(record.manifest_bytes, f"{run_id}-manifest.json")

    @app.get("/api/runs/{run_id}/results")
    async def download_results(run_id: str) -> Response:
        record = await get_record(run_id)
        payload = {
            "format_version": "1.0",
            "run_id": record.run_id,
            "manifest_sha256": hashlib.sha256(record.manifest_bytes).hexdigest(),
            "complete": record.status == "completed",
            "comparison_mode": record.comparison_mode,
            "execution_mode": record.execution_mode,
            "mock_data": record.is_mock,
            "results": [result.model_dump(mode="json") for result in record.ordered_results()],
            "notes": [
                "Solution order follows the configured matrix; no ranking or winner is produced.",
                (
                    "Field comparisons use bundled canonical gold and its label mask."
                    if record.comparison_mode == "guided"
                    else "Upload mode has no gold; accuracy and F1 are intentionally omitted."
                ),
            ],
        }
        return _json_download(_json_bytes(payload), f"{run_id}-results.json")

    app.state.demo_settings = resolved_settings
    app.state.run_store = run_store
    app.state.upload_store = upload_store

    if resolved_settings.static_dir.joinpath("index.html").is_file():
        app.mount(
            "/",
            StaticFiles(directory=resolved_settings.static_dir, html=True),
            name="frontend",
        )
    else:

        @app.get("/")
        async def frontend_missing() -> JSONResponse:
            return JSONResponse(
                {
                    "name": "Receipt comparison demo API",
                    "mode": resolved_settings.mode,
                    "frontend": "packaged frontend is missing; reinstall the project",
                    "docs": "/api/docs",
                },
                status_code=503,
            )

    return app


__all__ = ["create_app"]
