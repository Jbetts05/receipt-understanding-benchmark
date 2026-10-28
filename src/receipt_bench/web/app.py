from __future__ import annotations

import asyncio
import inspect
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile

from receipt_bench.projects.registry import ProjectRegistry
from receipt_bench.storage.models import EventRecord, TaskRecord
from receipt_bench.storage.protocols import NotFoundError, StorageError
from receipt_bench.storage.validation import (
    JsonObject,
    StrictJsonError,
    require_json_object,
    strict_json_bytes,
    strict_json_loads,
)
from receipt_bench.web.catalog import SolutionCatalog, default_solution_catalog
from receipt_bench.web.dataset_package import (
    DatasetPackageError,
    parse_labeled_dataset_package,
)
from receipt_bench.web.models import (
    CustomProjectCreateRequest,
    HealthView,
    ProjectAggregateView,
    ProjectView,
    ReceiptSampleView,
    RunAccepted,
    RunCreateRequest,
    RunHistoryView,
    RunResultView,
    RunSnapshotView,
    SolutionView,
)
from receipt_bench.web.repositories import (
    RepositoryBundle,
    StorageBundle,
    TaskDispatcher,
    WorkerRunner,
)
from receipt_bench.web.runtime import (
    HostedPairExecutor,
    MockPairExecutor,
    default_hosted_adapter_loader,
)
from receipt_bench.web.security import (
    JsonBodyLimitMiddleware,
    RequestGuardMiddleware,
    add_security_headers,
    clean_filename,
    declared_body_size,
    decode_base64url_header,
    decode_filename,
    read_limited_body,
    validate_image,
)
from receipt_bench.web.service import ProductError, ProductService
from receipt_bench.web.settings import WebSettings
from receipt_bench.worker import DurableWorker, PairExecutor, WorkerConfig

LOGGER = logging.getLogger(__name__)

WorkerFactory = Callable[[ProductService], WorkerRunner]
ExecutorFactory = Callable[[ProductService], PairExecutor]
ReadinessProbe = Callable[[], bool | Awaitable[bool]]
StartupHook = Callable[[], Awaitable[None] | None]


def _chunks(data: bytes, size: int = 64 * 1024) -> AsyncIterator[bytes]:
    async def iterator() -> AsyncIterator[bytes]:
        for offset in range(0, len(data), size):
            yield data[offset : offset + size]

    return iterator()


def _content_disposition(disposition: str, filename: str) -> str:
    safe = "".join(
        character
        if character.isascii() and (character.isalnum() or character in "._-")
        else "_"
        for character in filename
    ).strip("._")
    return f'{disposition}; filename="{safe or "artifact"}"'


def _cursor_value(
    *,
    cursor: int,
    last_event_id: str | None,
    query_last_event_id: str | None,
) -> int:
    values = [cursor]
    for value in (last_event_id, query_last_event_id):
        if value is None or not value.strip():
            continue
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ProductError(
                400,
                "Event resume cursor must be a non-negative integer",
                code="invalid_event_cursor",
            ) from exc
        if parsed < 0:
            raise ProductError(
                400,
                "Event resume cursor must be a non-negative integer",
                code="invalid_event_cursor",
            )
        values.append(parsed)
    return max(values)


async def _task_from_event(
    service: ProductService,
    run_id: str,
    event: EventRecord,
) -> TaskRecord | None:
    sample_id = event.data.get("sample_id")
    solution_id = event.data.get("solution_id")
    if not isinstance(sample_id, str) or not isinstance(solution_id, str):
        return None
    from receipt_bench.storage.validation import pair_id

    try:
        return (
            await service.repositories.tasks.get(
                run_id,
                pair_id(sample_id, solution_id),
            )
        ).value
    except (NotFoundError, ValueError):
        return None


async def _event_payload(
    service: ProductService,
    event: EventRecord,
) -> JsonObject:
    payload = dict(event.data)
    payload["type"] = event.event
    if event.event in {"run_started", "run_completed", "run_failed"}:
        payload.pop("aggregate_artifact_path", None)
        payload.pop("download_artifact_path", None)
        run = (await service.find_run(event.run_id)).value
        manifest = await service.manifest_view(run)
        if event.event == "run_started":
            manifest = manifest.model_copy(
                update={"status": "running", "completed_pairs": 0}
            )
        payload["run"] = cast(JsonObject, manifest.model_dump(mode="json"))
        if event.event == "run_failed":
            payload.setdefault("message", "The run did not complete.")
    elif event.event in {
        "task_started",
        "task_progress",
        "task_completed",
        "task_retry",
        "raw_persisted",
    }:
        task = await _task_from_event(service, event.run_id, event)
        if task is not None:
            task_view = await service.task_view(task, compact=True)
            if event.event == "task_started":
                task_view = task_view.model_copy(
                    update={"status": "running", "stage": "preflight", "progress": 0.08}
                )
            elif event.event == "task_retry":
                task_view = task_view.model_copy(update={"status": "retrying"})
                payload["type"] = "task_progress"
            elif event.event == "raw_persisted":
                payload.pop("path", None)
                task_view = task_view.model_copy(
                    update={"status": "running", "stage": "evaluating", "progress": 0.75}
                )
                payload["type"] = "task_progress"
            payload["task"] = cast(JsonObject, task_view.model_dump(mode="json"))
    return payload


async def _event_stream(
    request: Request,
    service: ProductService,
    run_id: str,
    *,
    after_sequence: int,
    follow: bool,
) -> AsyncIterator[bytes]:
    cursor = after_sequence
    loop = asyncio.get_running_loop()
    last_output = loop.time()
    while True:
        if await request.is_disconnected():
            return
        records = await service.repositories.events.read(
            run_id,
            after_sequence=cursor,
            limit=100,
        )
        terminal_event = False
        for record in records:
            if record.sequence <= cursor:
                continue
            payload = await _event_payload(service, record)
            encoded = strict_json_bytes(payload).decode("utf-8")
            event_name = str(payload.get("type", record.event))
            yield (
                f"id: {record.sequence}\nevent: {event_name}\ndata: {encoded}\n\n"
            ).encode()
            cursor = record.sequence
            last_output = loop.time()
            terminal_event = record.event in {"run_completed", "run_failed"} or terminal_event
        if terminal_event:
            return
        run = (await service.find_run(run_id)).value
        terminal_is_durable = run.status == "failed" or (
            run.status == "completed" and run.completion_event_emitted
        )
        if not records and terminal_is_durable:
            return
        if not follow:
            if not records:
                yield b": heartbeat\n\n"
            return
        now = loop.time()
        if now - last_output >= service.settings.sse_heartbeat_interval:
            yield b": heartbeat\n\n"
            last_output = now
        await asyncio.sleep(service.settings.sse_poll_interval)


def create_product_app(
    *,
    settings: WebSettings,
    repositories: RepositoryBundle,
    registry: ProjectRegistry | None = None,
    catalog: SolutionCatalog | None = None,
    dispatcher: TaskDispatcher | None = None,
    worker: WorkerRunner | None = None,
    worker_factory: WorkerFactory | None = None,
    readiness_probe: ReadinessProbe | None = None,
    startup_hook: StartupHook | None = None,
) -> FastAPI:
    resolved_registry = registry or ProjectRegistry.with_builtins(
        repository_root=settings.repository_root
    )
    resolved_catalog = catalog or default_solution_catalog()
    service = ProductService(
        repositories=repositories,
        registry=resolved_registry,
        catalog=resolved_catalog,
        settings=settings,
        dispatcher=dispatcher,
    )
    resolved_worker = worker_factory(service) if worker_factory is not None else worker
    if dispatcher is None and isinstance(resolved_worker, TaskDispatcher):
        service.set_dispatcher(resolved_worker)
    multipart_upload_max_bytes = settings.upload_max_bytes + settings.gold_max_bytes + 64 * 1024
    dataset_multipart_max_bytes = settings.dataset_package_max_bytes + 64 * 1024
    resolved_startup_hook = startup_hook
    if resolved_startup_hook is None and settings.seed_builtin_projects:
        from receipt_bench.web.seeding import BuiltInProjectSeeder

        resolved_startup_hook = BuiltInProjectSeeder(
            repositories=repositories,
            registry=resolved_registry,
            settings=settings,
        )
    worker_task_ref: list[asyncio.Task[None] | None] = [None]

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        try:
            if resolved_startup_hook is not None:
                startup_value = resolved_startup_hook()
                if inspect.isawaitable(startup_value):
                    await startup_value
            await service.recover_incomplete_runs()
            if resolved_worker is not None and settings.start_worker:
                worker_task_ref[0] = asyncio.create_task(
                    resolved_worker.run_forever(stop)
                )
            yield
        finally:
            stop.set()
            worker_task = worker_task_ref[0]
            if worker_task is not None:
                completed, pending = await asyncio.wait({worker_task}, timeout=5.0)
                del completed
                if pending:
                    worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
            await repositories.close()

    app = FastAPI(
        title="Receipt Bench API",
        version="2.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        JsonBodyLimitMiddleware,
        max_bytes=settings.request_max_bytes,
        sample_multipart_max_bytes=multipart_upload_max_bytes,
        dataset_multipart_max_bytes=dataset_multipart_max_bytes,
    )
    app.add_middleware(RequestGuardMiddleware, settings=settings)
    app.middleware("http")(add_security_headers)

    @app.exception_handler(ProductError)
    async def product_error_handler(_: Request, exc: ProductError) -> JSONResponse:
        return JSONResponse(
            {"detail": exc.detail, "code": exc.code},
            status_code=exc.status_code,
        )

    @app.exception_handler(StrictJsonError)
    async def strict_json_handler(_: Request, __: StrictJsonError) -> JSONResponse:
        return JSONResponse(
            {"detail": "Request contains invalid strict JSON", "code": "invalid_json"},
            status_code=422,
        )

    @app.exception_handler(StorageError)
    async def storage_error_handler(_: Request, __: StorageError) -> JSONResponse:
        return JSONResponse(
            {
                "detail": "Persistent storage is temporarily unavailable",
                "code": "storage_unavailable",
            },
            status_code=503,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _: Request,
        __: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": "Request validation failed", "code": "invalid_request"},
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
        LOGGER.error(
            "Unhandled request error",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        dependency_failure = isinstance(
            exc,
            (ConnectionError, OSError, TimeoutError),
        ) or type(exc).__module__.startswith("azure.")
        return JSONResponse(
            {
                "detail": (
                    "A required service is temporarily unavailable"
                    if dependency_failure
                    else "The request could not be completed"
                ),
                "code": (
                    "service_unavailable" if dependency_failure else "internal_error"
                ),
            },
            status_code=503 if dependency_failure else 500,
        )

    @app.get("/api/health", response_model=HealthView)
    async def health() -> HealthView:
        return HealthView(
            status="ok",
            profile=settings.profile,
            execution_mode=settings.default_execution_mode,
        )

    @app.get("/api/ready", response_model=HealthView)
    async def ready() -> HealthView | JSONResponse:
        try:
            is_ready = await asyncio.wait_for(service.readiness(), timeout=10)
        except TimeoutError:
            is_ready = False
        worker_task = worker_task_ref[0]
        if (
            is_ready
            and resolved_worker is not None
            and settings.start_worker
            and (worker_task is None or worker_task.done())
        ):
            is_ready = False
        if is_ready and readiness_probe is not None:
            try:
                probe_value = readiness_probe()
                is_ready = bool(
                    await probe_value if inspect.isawaitable(probe_value) else probe_value
                )
            except Exception:
                is_ready = False
        view = HealthView(
            status="ready" if is_ready else "not_ready",
            profile=settings.profile,
            execution_mode=settings.default_execution_mode,
        )
        return view if is_ready else JSONResponse(
            view.model_dump(mode="json"),
            status_code=503,
        )

    @app.get("/api/projects", response_model=list[ProjectView])
    async def list_projects() -> list[ProjectView]:
        return await service.list_projects()

    @app.post("/api/projects", response_model=ProjectView, status_code=201)
    async def create_project(payload: CustomProjectCreateRequest) -> ProjectView:
        return await service.create_project(payload)

    @app.post(
        "/api/projects/import-labeled",
        response_model=ProjectView,
        status_code=201,
    )
    async def import_labeled_dataset(request: Request) -> ProjectView:
        declared_body_size(request, dataset_multipart_max_bytes)
        content_type = request.headers.get("content-type", "")
        if not content_type.casefold().startswith("multipart/form-data"):
            raise ProductError(
                415,
                "Labelled dataset import requires multipart/form-data",
                code="invalid_dataset_package",
            )
        form = await request.form()
        upload = form.get("package")
        if not isinstance(upload, UploadFile):
            raise ProductError(
                422,
                "Labelled dataset import requires a ZIP package",
                code="dataset_package_required",
            )
        filename = clean_filename(upload.filename or "dataset.zip")
        if not filename.casefold().endswith(".zip"):
            await upload.close()
            raise ProductError(
                422,
                "Labelled dataset package filename must end in .zip",
                code="invalid_dataset_package",
            )
        package_bytes = await upload.read(settings.dataset_package_max_bytes + 1)
        await upload.close()
        if len(package_bytes) > settings.dataset_package_max_bytes:
            raise ProductError(
                413,
                f"Dataset package exceeds the "
                f"{settings.dataset_package_max_bytes}-byte limit",
                code="dataset_package_too_large",
            )
        try:
            package = await asyncio.to_thread(
                parse_labeled_dataset_package,
                package_bytes,
                settings,
            )
        except DatasetPackageError as exc:
            raise ProductError(
                exc.status_code,
                exc.detail,
                code=exc.code,
            ) from exc
        return await service.import_labeled_dataset(package)

    @app.get("/api/projects/{project_id}", response_model=ProjectView)
    async def get_project(project_id: str) -> ProjectView:
        return await service.get_project(project_id)

    @app.delete("/api/projects/{project_id}", status_code=204)
    async def delete_project(project_id: str) -> Response:
        await service.delete_project(project_id)
        return Response(status_code=204)

    @app.get("/api/projects/{project_id}/samples", response_model=list[ReceiptSampleView])
    async def list_samples(project_id: str) -> list[ReceiptSampleView]:
        return await service.list_samples(project_id)

    @app.get("/api/projects/{project_id}/solutions", response_model=list[SolutionView])
    async def list_solutions(project_id: str) -> list[SolutionView]:
        return await service.list_solutions(project_id)

    @app.post(
        "/api/projects/{project_id}/samples",
        response_model=ReceiptSampleView,
        status_code=201,
    )
    async def upload_sample(project_id: str, request: Request) -> ReceiptSampleView:
        content_type = request.headers.get("content-type", "")
        gold_bytes: bytes | None = None
        if content_type.casefold().startswith("multipart/form-data"):
            declared_body_size(request, multipart_upload_max_bytes)
            form = await request.form()
            upload = form.get("image")
            if not isinstance(upload, UploadFile):
                raise ProductError(
                    422,
                    "Multipart sample upload requires an image file",
                    code="image_required",
                )
            raw_filename = upload.filename
            if not raw_filename or any(
                character in raw_filename for character in ("\x00", "\r", "\n")
            ):
                raise ProductError(
                    422,
                    "Image filename is invalid",
                    code="invalid_filename",
                )
            filename = clean_filename(raw_filename)
            data = await upload.read(settings.upload_max_bytes + 1)
            await upload.close()
            if len(data) > settings.upload_max_bytes:
                raise ProductError(
                    413,
                    f"Image exceeds the {settings.upload_max_bytes}-byte limit",
                    code="image_too_large",
                )
            media_type = validate_image(data, settings, upload.content_type)
            raw_gold = form.get("gold")
            if isinstance(raw_gold, UploadFile):
                if raw_gold.content_type not in {
                    None,
                    "application/json",
                    "application/octet-stream",
                }:
                    await raw_gold.close()
                    raise ProductError(
                        422,
                        "Multipart gold file must contain JSON",
                        code="invalid_gold",
                    )
                gold_bytes = await raw_gold.read(settings.gold_max_bytes + 1)
                await raw_gold.close()
                if len(gold_bytes) > settings.gold_max_bytes:
                    raise ProductError(
                        413,
                        f"Gold JSON exceeds the {settings.gold_max_bytes}-byte limit",
                        code="gold_too_large",
                    )
            elif raw_gold is not None and not isinstance(raw_gold, str):
                raise ProductError(
                    422,
                    "Multipart gold must be a JSON text field",
                    code="invalid_gold",
                )
            else:
                gold_bytes = (
                    raw_gold.encode("utf-8") if isinstance(raw_gold, str) else None
                )
        else:
            filename = decode_filename(request)
            data = await read_limited_body(request, settings.upload_max_bytes)
            media_type = validate_image(
                data,
                settings,
                request.headers.get("content-type"),
            )
            gold_bytes = decode_base64url_header(
                request,
                "X-Gold-JSON-B64",
                max_decoded_bytes=settings.gold_max_bytes,
                required=False,
            )
        gold: JsonObject | None = None
        if gold_bytes is not None:
            try:
                gold_value = strict_json_loads(
                    gold_bytes,
                    max_bytes=settings.gold_max_bytes,
                )
                gold = require_json_object(gold_value, field="gold")
            except StrictJsonError as exc:
                raise ProductError(
                    422,
                    "Gold metadata must be a strict JSON object",
                    code="invalid_gold",
                ) from exc
        return await service.upload_sample(
            project_id,
            filename=filename,
            media_type=media_type,
            data=data,
            gold=gold,
        )

    @app.get("/api/projects/{project_id}/samples/{sample_id}/image")
    async def sample_image_proxy(
        project_id: str,
        sample_id: str,
    ) -> StreamingResponse:
        data, info, filename = await service.image(project_id, sample_id)
        return StreamingResponse(
            _chunks(data),
            media_type=info.content_type,
            headers={
                "Cache-Control": "private, max-age=0, no-store",
                "ETag": f'"{info.sha256}"',
                "Content-Disposition": _content_disposition("inline", filename),
            },
        )

    @app.post(
        "/api/projects/{project_id}/runs",
        response_model=RunAccepted,
        status_code=202,
    )
    async def create_run(
        project_id: str,
        payload: RunCreateRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> RunAccepted:
        return await service.create_run(
            project_id,
            payload,
            idempotency_key=idempotency_key,
        )

    @app.get(
        "/api/projects/{project_id}/runs",
        response_model=list[RunHistoryView],
    )
    async def list_runs(
        project_id: str,
        response: Response,
        limit: int = Query(default=100, ge=1, le=100),
    ) -> list[RunHistoryView]:
        response.headers["Cache-Control"] = "private, max-age=0, no-store"
        return await service.list_runs(project_id, limit=limit)

    @app.get("/api/runs/{run_id}", response_model=RunSnapshotView)
    async def run_snapshot(
        run_id: str,
        compact: bool = Query(default=False),
        result_sample_id: str | None = Query(default=None),
    ) -> RunSnapshotView:
        return await service.snapshot(
            run_id,
            compact=compact,
            result_sample_id=result_sample_id,
        )

    @app.delete("/api/runs/{run_id}", status_code=204)
    async def delete_run(run_id: str) -> Response:
        await service.delete_run(run_id)
        return Response(status_code=204)

    @app.get("/api/runs/{run_id}/results", response_model=list[RunResultView])
    async def run_results(
        run_id: str,
        sample_id: str | None = Query(default=None),
        compact: bool = Query(default=False),
    ) -> list[RunResultView]:
        return await service.list_results(
            run_id,
            sample_id=sample_id,
            compact=compact,
        )

    @app.get(
        "/api/runs/{run_id}/results/{sample_id}/{solution_id}",
        response_model=RunResultView,
    )
    async def run_result(
        run_id: str,
        sample_id: str,
        solution_id: str,
    ) -> RunResultView:
        return await service.result(run_id, sample_id, solution_id)

    @app.get(
        "/api/runs/{run_id}/results/{sample_id}/{solution_id}/artifacts/{kind}"
    )
    async def result_artifact(
        run_id: str,
        sample_id: str,
        solution_id: str,
        kind: Literal["raw", "parsed", "comparison", "telemetry"],
    ) -> StreamingResponse:
        data, info = await service.artifact(
            run_id,
            sample_id,
            solution_id,
            kind,
        )
        return StreamingResponse(
            _chunks(data),
            media_type=info.content_type,
            headers={
                "Cache-Control": "private, max-age=0, no-store",
                "ETag": f'"{info.sha256}"',
                "Content-Disposition": _content_disposition(
                    "inline",
                    f"{sample_id}-{solution_id}-{kind}.json",
                ),
            },
        )

    @app.get(
        "/api/projects/{project_id}/runs/{run_id}/aggregate",
        response_model=ProjectAggregateView,
    )
    async def project_aggregate(
        project_id: str,
        run_id: str,
    ) -> Response:
        return Response(
            content=await service.aggregate_bytes(project_id, run_id),
            media_type="application/json",
            headers={"Cache-Control": "private, max-age=0, no-store"},
        )

    @app.get("/api/runs/{run_id}/download")
    async def run_download(run_id: str) -> StreamingResponse:
        data, info = await service.download(run_id)
        return StreamingResponse(
            _chunks(data),
            media_type=info.content_type,
            headers={
                "Cache-Control": "private, max-age=0, no-store",
                "ETag": f'"{info.sha256}"',
                "Content-Disposition": _content_disposition(
                    "attachment",
                    f"{run_id}.zip",
                ),
            },
        )

    @app.get("/api/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        request: Request,
        cursor: int = Query(default=0, ge=0),
        last_event_id_query: str | None = Query(default=None, alias="lastEventId"),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        follow: bool = Query(default=True),
    ) -> StreamingResponse:
        await service.find_run(run_id)
        after_sequence = _cursor_value(
            cursor=cursor,
            last_event_id=last_event_id,
            query_last_event_id=last_event_id_query,
        )
        return StreamingResponse(
            _event_stream(
                request,
                service,
                run_id,
                after_sequence=after_sequence,
                follow=follow,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    app.state.product_service = service
    app.state.web_settings = settings
    app.state.repositories = repositories
    app.state.bind_host = settings.host
    app.state.bind_port = settings.port
    app.state.worker = resolved_worker

    static_dir = Path(__file__).resolve().parents[1] / "demo" / "static"
    if static_dir.joinpath("index.html").is_file():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
    else:

        @app.get("/")
        async def frontend_missing() -> JSONResponse:
            return JSONResponse(
                {
                    "name": "Receipt Bench API",
                    "frontend": "packaged frontend is missing",
                },
                status_code=503,
            )

    return app


def create_local_app(
    settings: WebSettings | None = None,
    *,
    storage: StorageBundle | None = None,
    repositories: RepositoryBundle | None = None,
    registry: ProjectRegistry | None = None,
    catalog: SolutionCatalog | None = None,
    executor: PairExecutor | None = None,
    dispatcher: TaskDispatcher | None = None,
    worker: WorkerRunner | None = None,
    readiness_probe: ReadinessProbe | None = None,
    startup_hook: StartupHook | None = None,
) -> FastAPI:
    resolved_settings = settings or WebSettings.from_env("local")
    if resolved_settings.profile != "local":
        raise ValueError("create_local_app requires local WebSettings")
    if repositories is None:
        if storage is None:
            from receipt_bench.storage.local import LocalStorage

            storage = LocalStorage(resolved_settings.storage_root)
        repositories = RepositoryBundle.from_storage(storage)
    resolved_catalog = catalog or default_solution_catalog()

    worker_factory: WorkerFactory | None = None
    if worker is None:
        def build_worker(service: ProductService) -> WorkerRunner:
            resolved_executor = executor or MockPairExecutor(service, resolved_catalog)
            return DurableWorker(
                artifacts=repositories.artifacts,
                runs=repositories.runs,
                tasks=repositories.tasks,
                events=repositories.events,
                queue=repositories.queue,
                executor=resolved_executor,
                finalizer=service.finalize_run,
                config=WorkerConfig(
                    concurrency=16,
                    dispatch_concurrency=32,
                    provider_concurrency=16,
                    solution_concurrency=4,
                    visibility_timeout=30,
                    lease_renewal_interval=10,
                    idle_poll_interval=0.05,
                ),
            )

        worker_factory = build_worker
    return create_product_app(
        settings=resolved_settings,
        repositories=repositories,
        registry=registry,
        catalog=resolved_catalog,
        dispatcher=dispatcher,
        worker=worker,
        worker_factory=worker_factory,
        readiness_probe=readiness_probe,
        startup_hook=startup_hook,
    )


def create_hosted_app(
    *,
    repositories: RepositoryBundle | None = None,
    storage: StorageBundle | None = None,
    storage_factory: Callable[[], StorageBundle] | None = None,
    settings: WebSettings | None = None,
    registry: ProjectRegistry | None = None,
    catalog: SolutionCatalog | None = None,
    executor_factory: ExecutorFactory | None = None,
    dispatcher: TaskDispatcher | None = None,
    worker: WorkerRunner | None = None,
    readiness_probe: ReadinessProbe | None = None,
    startup_hook: StartupHook | None = None,
) -> FastAPI:
    resolved_settings = settings or WebSettings.from_env("hosted")
    if resolved_settings.profile != "hosted":
        raise ValueError("create_hosted_app requires hosted WebSettings")
    if repositories is None:
        if storage is None:
            if storage_factory is None:
                raise ValueError(
                    "Hosted construction requires repositories, storage, or storage_factory"
                )
            storage = storage_factory()
        repositories = RepositoryBundle.from_storage(storage)
    resolved_catalog = catalog or default_solution_catalog()
    worker_factory: WorkerFactory | None = None
    if worker is None and executor_factory is not None:
        def build_worker(service: ProductService) -> WorkerRunner:
            return DurableWorker(
                artifacts=repositories.artifacts,
                runs=repositories.runs,
                tasks=repositories.tasks,
                events=repositories.events,
                queue=repositories.queue,
                executor=executor_factory(service),
                finalizer=service.finalize_run,
            )

        worker_factory = build_worker
    return create_product_app(
        settings=resolved_settings,
        repositories=repositories,
        registry=registry,
        catalog=resolved_catalog,
        dispatcher=dispatcher,
        worker=worker,
        worker_factory=worker_factory,
        readiness_probe=readiness_probe,
        startup_hook=startup_hook,
    )


def create_hosted_azure_app(
    *,
    azure_storage_options: Mapping[str, Any],
    settings: WebSettings | None = None,
    registry: ProjectRegistry | None = None,
    catalog: SolutionCatalog | None = None,
    executor_factory: ExecutorFactory | None = None,
    dispatcher: TaskDispatcher | None = None,
    worker: WorkerRunner | None = None,
    readiness_probe: ReadinessProbe | None = None,
    startup_hook: StartupHook | None = None,
) -> FastAPI:
    """Lazy Azure wiring hook; SDK-backed modules load only when this factory is called."""

    def storage_factory() -> StorageBundle:
        from receipt_bench.storage.azure import AzureStorage

        return cast(
            StorageBundle,
            AzureStorage.from_endpoints(hosted=True, **dict(azure_storage_options)),
        )

    return create_hosted_app(
        storage_factory=storage_factory,
        settings=settings,
        registry=registry,
        catalog=catalog,
        executor_factory=executor_factory,
        dispatcher=dispatcher,
        worker=worker,
        readiness_probe=readiness_probe,
        startup_hook=startup_hook,
    )


def create_app() -> FastAPI:
    """Environment-driven composition root used by the hosted Uvicorn startup."""

    settings = WebSettings.from_env()
    storage_backend = os.getenv(
        "RECEIPT_STORAGE_BACKEND",
        "azure" if settings.profile == "hosted" else "filesystem",
    ).strip().casefold()
    if settings.profile == "local":
        if storage_backend != "filesystem":
            raise ValueError("Local profile requires RECEIPT_STORAGE_BACKEND=filesystem")
        return create_local_app(settings)
    if storage_backend != "azure":
        raise ValueError("Hosted profile requires RECEIPT_STORAGE_BACKEND=azure")
    if os.getenv("AZURE_STORAGE_AUTH_MODE", "managed-identity").strip().casefold() != (
        "managed-identity"
    ):
        raise ValueError("Hosted Azure storage requires managed-identity authentication")
    server_workers = os.getenv("RECEIPT_SERVER_WORKERS", "1").strip()
    if server_workers != "1":
        raise ValueError("Hosted product API requires RECEIPT_SERVER_WORKERS=1")

    catalog = default_solution_catalog()
    registry = ProjectRegistry.with_builtins(repository_root=settings.repository_root)
    executor_factory: ExecutorFactory
    if settings.allow_live_execution:
        adapter_loader = default_hosted_adapter_loader(settings.repository_root)

        def executor_factory(service: ProductService) -> PairExecutor:
            return HostedPairExecutor(
                service,
                catalog,
                adapter_loader=adapter_loader,
            )

    else:
        def executor_factory(service: ProductService) -> PairExecutor:
            return MockPairExecutor(service, catalog)

    return create_hosted_azure_app(
        azure_storage_options=_azure_storage_options_from_env(),
        settings=settings,
        registry=registry,
        catalog=catalog,
        executor_factory=executor_factory,
    )


def _azure_storage_options_from_env() -> dict[str, object]:
    options: dict[str, object] = {
        "blob_endpoint": _required_environment("AZURE_STORAGE_BLOB_ENDPOINT"),
        "queue_endpoint": _required_environment("AZURE_STORAGE_QUEUE_ENDPOINT"),
        "table_endpoint": _required_environment("AZURE_STORAGE_TABLE_ENDPOINT"),
        "artifact_container": os.getenv(
            "AZURE_STORAGE_ARTIFACT_CONTAINER", "artifacts"
        ).strip(),
        "task_queue": os.getenv(
            "AZURE_STORAGE_TASK_QUEUE", "extraction-tasks"
        ).strip(),
        "poison_queue": os.getenv(
            "AZURE_STORAGE_POISON_QUEUE", "extraction-tasks-poison"
        ).strip(),
        "projects_table": os.getenv("AZURE_STORAGE_PROJECTS_TABLE", "Projects").strip(),
        "runs_table": os.getenv("AZURE_STORAGE_RUNS_TABLE", "Runs").strip(),
        "tasks_table": os.getenv("AZURE_STORAGE_TASKS_TABLE", "Tasks").strip(),
    }
    client_id = os.getenv("AZURE_CLIENT_ID", "").strip()
    if client_id:
        options["managed_identity_client_id"] = client_id
    if any(not value for value in options.values()):
        raise ValueError("Azure storage resource names cannot be blank")
    return options


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


__all__ = [
    "ExecutorFactory",
    "ReadinessProbe",
    "StartupHook",
    "WorkerFactory",
    "create_app",
    "create_hosted_app",
    "create_hosted_azure_app",
    "create_local_app",
    "create_product_app",
]
