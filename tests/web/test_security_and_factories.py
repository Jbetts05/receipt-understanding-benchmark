from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from receipt_bench.storage.local import LocalStorage
from receipt_bench.web import WebSettings, create_app, create_hosted_app, create_local_app

from .conftest import PNG_BYTES, base64url, project_payload


def test_host_origin_path_and_body_guards(client: TestClient) -> None:
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400

    cross_origin = client.post(
        "/api/projects",
        json=project_payload(),
        headers={"Origin": "http://evil.example"},
    )
    assert cross_origin.status_code == 403

    same_origin = client.post(
        "/api/projects",
        json=project_payload(),
        headers={"Origin": "http://localhost"},
    )
    assert same_origin.status_code == 201

    unsafe_id = client.get("/api/projects/not%7Ca%7Csafe%7Cid")
    assert unsafe_id.status_code == 404

    oversized = client.post(
        "/api/projects",
        content=b'{"name":"' + b"x" * (256 * 1024) + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert oversized.status_code == 413


def test_upload_headers_format_and_size_limits(tmp_path: Path) -> None:
    settings = WebSettings(
        storage_root=tmp_path / "storage",
        start_worker=False,
        upload_max_bytes=1024,
        image_max_pixels=10_000,
    )
    app = create_local_app(settings)
    with TestClient(app, base_url="http://localhost") as client:
        project = client.post("/api/projects", json=project_payload()).json()
        url = f"/api/projects/{project['id']}/samples"

        missing_name = client.post(
            url,
            content=PNG_BYTES,
            headers={"Content-Type": "image/png"},
        )
        assert missing_name.status_code == 400

        legacy_name = client.post(
            url,
            content=PNG_BYTES,
            headers={
                "Content-Type": "image/png",
                "X-Filename": "receipt.png",
                "X-Filename-B64": base64url(b"receipt.png"),
            },
        )
        assert legacy_name.status_code == 400

        wrong_type = client.post(
            url,
            content=PNG_BYTES,
            headers={
                "Content-Type": "image/jpeg",
                "X-Filename-B64": base64url(b"receipt.png"),
            },
        )
        assert wrong_type.status_code == 415

        too_large = client.post(
            url,
            content=b"x" * 1025,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Filename-B64": base64url(b"receipt.png"),
            },
        )
        assert too_large.status_code == 413


def test_multipart_image_and_gold_upload_remains_supported(tmp_path: Path) -> None:
    settings = WebSettings(
        storage_root=tmp_path / "storage",
        start_worker=False,
        upload_max_bytes=1024,
        gold_max_bytes=1024,
        image_max_pixels=10_000,
    )
    app = create_local_app(settings)
    gold = {"merchant": "Multipart Shop", "total": 12.5}

    with TestClient(app, base_url="http://localhost") as client:
        project = client.post("/api/projects", json=project_payload()).json()
        response = client.post(
            f"/api/projects/{project['id']}/samples",
            files={
                "image": ("receipt.png", PNG_BYTES, "image/png"),
                "gold": (
                    "gold.json",
                    json.dumps(gold).encode("utf-8"),
                    "application/json",
                ),
            },
        )

    assert response.status_code == 201, response.text
    assert response.json()["filename"] == "receipt.png"
    assert response.json()["gold"] == gold


def test_chunked_multipart_upload_stops_at_cumulative_limit(tmp_path: Path) -> None:
    settings = WebSettings(
        storage_root=tmp_path / "storage",
        start_worker=False,
        upload_max_bytes=1024,
        gold_max_bytes=1024,
        image_max_pixels=10_000,
    )
    app = create_local_app(settings)
    boundary = "receipt-boundary"
    preamble = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="image"; filename="receipt.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode("ascii")
    block = b"x" * (16 * 1024)
    multipart_limit = settings.upload_max_bytes + settings.gold_max_bytes + 64 * 1024
    assert len(preamble) + 4 * len(block) <= multipart_limit
    assert len(preamble) + 5 * len(block) > multipart_limit
    blocks_sent = 0

    async def oversized_body() -> AsyncIterator[bytes]:
        nonlocal blocks_sent
        yield preamble
        for _ in range(5):
            blocks_sent += 1
            yield block
        raise AssertionError("The application consumed the stream after exceeding its limit")

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://localhost",
            ) as client:
                response = await client.post(
                    "/api/projects/chunked-project/samples",
                    content=oversized_body(),
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                )
                assert "content-length" not in response.request.headers
                assert response.request.headers["transfer-encoding"] == "chunked"
                assert response.status_code == 413
                assert response.json() == {
                    "detail": f"Request exceeds the {multipart_limit}-byte limit"
                }

    asyncio.run(scenario())
    assert blocks_sent == 5


def test_hosted_profile_rights_https_forwarding_and_factory_hooks(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "hosted-storage")
    settings = WebSettings(
        profile="hosted",
        allowed_hosts=("api.example.test",),
        start_worker=False,
    )
    app = create_hosted_app(settings=settings, storage=storage)
    assert app.state.bind_host == "0.0.0.0"
    assert app.state.worker is None

    with TestClient(app, base_url="https://api.example.test") as client:
        projects = client.get("/api/projects")
        assert projects.status_code == 200
        sroie = next(item for item in projects.json() if item["id"] == "sroie")
        assert sroie["available"] is False
        assert sroie["rights"]["state"] == "restricted"
        assert client.get("/api/projects/sroie/samples").status_code == 451
        assert client.get("/docs").status_code == 404

        project = client.post("/api/projects", json=project_payload()).json()
        sample = client.post(
            f"/api/projects/{project['id']}/samples",
            content=PNG_BYTES,
            headers={
                "Content-Type": "image/png",
                "X-Filename-B64": base64url(b"receipt.png"),
            },
        ).json()
        mode_mismatch = client.post(
            f"/api/projects/{project['id']}/runs",
            json={
                "sample_ids": [sample["id"]],
                "solution_ids": ["gpt-5.4-mini"],
                "schema_version": project["schema"]["version"],
                "prompt_version": project["prompt"]["version"],
                "execution_mode": "mock",
            },
        )
        assert mode_mismatch.status_code == 409
        assert mode_mismatch.json()["code"] == "execution_mode_mismatch"
        dispatch_unavailable = client.post(
            f"/api/projects/{project['id']}/runs",
            json={
                "sample_ids": [sample["id"]],
                "solution_ids": ["gpt-5.4-mini"],
                "schema_version": project["schema"]["version"],
                "prompt_version": project["prompt"]["version"],
                "execution_mode": "live",
            },
        )
        assert dispatch_unavailable.status_code == 503
        assert dispatch_unavailable.json()["code"] == "dispatch_unavailable"

    forwarded_storage = LocalStorage(tmp_path / "forwarded-storage")
    forwarded_app = create_hosted_app(settings=settings, storage=forwarded_storage)
    with TestClient(forwarded_app, base_url="http://api.example.test") as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/ready").status_code == 200
        assert client.get("/api/projects").status_code == 400
        forwarded = client.get(
            "/api/projects",
            headers={"X-Forwarded-Proto": "https"},
        )
        assert forwarded.status_code == 200

    probe_storage = LocalStorage(tmp_path / "probe-storage")
    probe_app = create_hosted_app(settings=settings, storage=probe_storage)
    with TestClient(probe_app, base_url="http://platform-probe.internal") as client:
        assert client.get("/api/ready").status_code == 200
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/projects").status_code == 400

    cleared_storage = LocalStorage(tmp_path / "cleared-storage")
    cleared_app = create_hosted_app(
        settings=settings.model_copy(update={"sroie_rights_evidence": "approval-123"}),
        storage=cleared_storage,
    )
    with TestClient(cleared_app, base_url="https://api.example.test") as client:
        sroie = client.get("/api/projects/sroie").json()
        assert sroie["available"] is True
        assert sroie["rights"]["state"] == "cleared"


def test_profiles_bind_safely_and_readiness_is_injectable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError):
        WebSettings(profile="local", host="0.0.0.0")
    with pytest.raises(ValidationError):
        WebSettings(profile="local", allow_live_execution=True)
    with pytest.raises(ValidationError):
        WebSettings(profile="hosted", host="127.0.0.1")

    monkeypatch.setenv("PORT", "9123")
    monkeypatch.setenv("WEBSITE_HOSTNAME", "api.example.test")
    hosted = WebSettings.from_env("hosted")
    assert (hosted.host, hosted.port) == ("0.0.0.0", 9123)

    app = create_local_app(
        WebSettings(storage_root=tmp_path / "readiness", start_worker=False),
        readiness_probe=lambda: False,
    )
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/api/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.headers["content-security-policy"].startswith("default-src 'self'")


def test_zero_argument_hosted_factory_maps_exact_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from receipt_bench.storage.azure import AzureStorage

    captured: dict[str, object] = {}
    adapter_loads = 0

    def fake_from_endpoints(
        cls: type[AzureStorage],
        /,
        **options: object,
    ) -> LocalStorage:
        del cls
        captured.update(options)
        return LocalStorage(tmp_path / "factory-storage")

    def fake_adapter_loader(_: Path) -> object:
        def load() -> dict[str, object]:
            nonlocal adapter_loads
            adapter_loads += 1
            return {}

        return load

    monkeypatch.setattr(
        AzureStorage,
        "from_endpoints",
        classmethod(fake_from_endpoints),
    )
    app_module = importlib.import_module("receipt_bench.web.app")
    monkeypatch.setattr(
        app_module,
        "default_hosted_adapter_loader",
        fake_adapter_loader,
    )
    environment = {
        "RECEIPT_WEB_PROFILE": "hosted",
        "RECEIPT_WEB_ALLOWED_HOSTS": "api.example.test",
        "PORT": "8080",
        "RECEIPT_STORAGE_BACKEND": "azure",
        "RECEIPT_EXECUTION_MODE": "azure",
        "RECEIPT_WORKER_ENABLED": "false",
        "RECEIPT_SEED_BUILTIN_PROJECTS": "false",
        "RECEIPT_SEED_CORD": "false",
        "RECEIPT_SEED_SROIE": "false",
        "AZURE_STORAGE_AUTH_MODE": "managed-identity",
        "AZURE_STORAGE_BLOB_ENDPOINT": "https://example.blob.core.windows.net",
        "AZURE_STORAGE_QUEUE_ENDPOINT": "https://example.queue.core.windows.net",
        "AZURE_STORAGE_TABLE_ENDPOINT": "https://example.table.core.windows.net",
        "AZURE_STORAGE_ARTIFACT_CONTAINER": "private-artifacts",
        "AZURE_STORAGE_TASK_QUEUE": "work-items",
        "AZURE_STORAGE_POISON_QUEUE": "work-items-poison",
        "AZURE_STORAGE_PROJECTS_TABLE": "WebProjects",
        "AZURE_STORAGE_RUNS_TABLE": "WebRuns",
        "AZURE_STORAGE_TASKS_TABLE": "WebTasks",
        "AZURE_CLIENT_ID": "managed-identity-client",
        "RECEIPT_SERVER_WORKERS": "1",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    app = create_app()

    assert app.state.bind_host == "0.0.0.0"
    assert app.state.bind_port == 8080
    assert app.state.web_settings.start_worker is False
    assert adapter_loads == 0
    assert captured == {
        "artifact_container": "private-artifacts",
        "blob_endpoint": "https://example.blob.core.windows.net",
        "hosted": True,
        "managed_identity_client_id": "managed-identity-client",
        "poison_queue": "work-items-poison",
        "projects_table": "WebProjects",
        "queue_endpoint": "https://example.queue.core.windows.net",
        "runs_table": "WebRuns",
        "table_endpoint": "https://example.table.core.windows.net",
        "task_queue": "work-items",
        "tasks_table": "WebTasks",
    }
    with TestClient(app, base_url="https://api.example.test") as client:
        assert client.get("/api/health").json() == {
            "status": "ok",
            "profile": "hosted",
            "execution_mode": "live",
        }
    assert adapter_loads == 0


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("RECEIPT_STORAGE_BACKEND", "filesystem", "requires.*azure"),
        ("AZURE_STORAGE_AUTH_MODE", "connection-string", "managed-identity"),
        ("RECEIPT_SERVER_WORKERS", "2", "requires.*1"),
    ],
)
def test_zero_argument_hosted_factory_rejects_unsafe_runtime_modes(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    match: str,
) -> None:
    monkeypatch.setenv("RECEIPT_WEB_PROFILE", "hosted")
    monkeypatch.setenv("RECEIPT_WEB_ALLOWED_HOSTS", "api.example.test")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=match):
        create_app()


def test_environment_booleans_and_sroie_seed_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RECEIPT_WORKER_ENABLED", "sometimes")
    with pytest.raises(ValueError, match="RECEIPT_WORKER_ENABLED must be a boolean"):
        WebSettings.from_env("local")

    monkeypatch.setenv("RECEIPT_WORKER_ENABLED", "false")
    monkeypatch.setenv("RECEIPT_SEED_BUILTIN_PROJECTS", "true")
    monkeypatch.setenv("RECEIPT_SEED_SROIE", "true")
    with pytest.raises(ValidationError, match="SROIE seeding requires"):
        WebSettings.from_env("hosted")

    for name in (
        "RECEIPT_SEED_BUILTIN_PROJECTS",
        "RECEIPT_SEED_CORD",
        "RECEIPT_SEED_SROIE",
        "RECEIPT_WEB_SROIE_RIGHTS_EVIDENCE",
        "PORT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RECEIPT_WEB_PORT", "9999")
    monkeypatch.setenv("RECEIPT_EXECUTION_MODE", "mock")
    local = WebSettings.from_env("local")
    assert local.port == 8000
    assert (
        local.seed_builtin_projects,
        local.seed_cord,
        local.seed_sroie,
    ) == (True, True, True)
