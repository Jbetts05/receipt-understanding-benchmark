from __future__ import annotations

import base64
import json
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from receipt_bench.web import WebSettings, create_local_app

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
TEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "merchant": {"type": "string"},
        "total": {"type": "number"},
    },
    "required": ["merchant", "total"],
    "additionalProperties": False,
}


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def project_payload(
    *,
    name: str = "Test receipts",
    solutions: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": "Offline API test project",
        "schema": TEST_SCHEMA,
        "prompt": "Return the merchant and total as strict JSON.",
        "selected_solution_ids": solutions or ["gpt-5.4-mini"],
    }


def create_project(
    client: TestClient,
    *,
    name: str = "Test receipts",
    solutions: list[str] | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/api/projects",
        json=project_payload(name=name, solutions=solutions),
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def upload_sample(
    client: TestClient,
    project_id: str,
    *,
    filename: str = "receipt.png",
    gold: dict[str, Any] | None = None,
    image: bytes = PNG_BYTES,
) -> dict[str, Any]:
    headers = {
        "Content-Type": "image/png",
        "X-Filename-B64": base64url(filename.encode("utf-8")),
    }
    if gold is not None:
        headers["X-Gold-JSON-B64"] = base64url(
            json.dumps(gold, separators=(",", ":")).encode()
        )
    response = client.post(
        f"/api/projects/{project_id}/samples",
        content=image,
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def create_run(
    client: TestClient,
    project: dict[str, Any],
    sample_ids: list[str],
    solution_ids: list[str],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    response = client.post(
        f"/api/projects/{project['id']}/runs",
        json={
            "sample_ids": sample_ids,
            "solution_ids": solution_ids,
            "schema_version": project["schema"]["version"],
            "prompt_version": project["prompt"]["version"],
            "execution_mode": "mock",
        },
        headers=headers,
    )
    assert response.status_code == 202, response.text
    return cast(dict[str, Any], response.json())


def wait_for_terminal(
    client: TestClient,
    run_id: str,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    snapshot: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200, response.text
        snapshot = response.json()
        if snapshot["manifest"]["status"] in {"completed", "failed"}:
            return snapshot
        time.sleep(0.02)
    pytest.fail(f"Run did not finish: {snapshot}")


@pytest.fixture
def local_app(tmp_path: Path) -> FastAPI:
    return create_local_app(
        WebSettings(
            storage_root=tmp_path / "storage",
            start_worker=False,
            sse_poll_interval=0.01,
            sse_heartbeat_interval=0.02,
        )
    )


@pytest.fixture
def client(local_app: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(local_app, base_url="http://localhost") as test_client:
        yield test_client
