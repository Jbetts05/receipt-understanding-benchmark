from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from collections.abc import AsyncIterator, Mapping
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient as FastAPITestClient
from httpx import ASGITransport, AsyncClient
from PIL import Image

from receipt_bench.config import AzureSettings, BenchmarkConfig
from receipt_bench.demo.app import create_app
from receipt_bench.demo.settings import DemoSettings
from receipt_bench.models import CanonicalReceipt, ComponentMeasurement
from receipt_bench.providers.base import (
    AdapterResponse,
    ProviderResponseError,
    SolutionAdapter,
)

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "receipt_bench" / "demo" / "static"


class TestClient(FastAPITestClient):
    def __init__(self, app: Any) -> None:
        super().__init__(app, base_url="http://127.0.0.1")


def _filename_headers(filename: str, **headers: str) -> dict[str, str]:
    encoded = base64.urlsafe_b64encode(filename.encode("utf-8")).rstrip(b"=").decode("ascii")
    return {"X-Filename-B64": encoded, **headers}


def _settings(tmp_path: Path, **overrides: Any) -> DemoSettings:
    values: dict[str, Any] = {
        "mode": "mock",
        "allow_live": False,
        "mock_delay_scale": 0.0,
        "static_dir": tmp_path / "missing-static",
    }
    values.update(overrides)
    return DemoSettings(**values)


def _must_not_build_adapters(
    _: BenchmarkConfig,
    __: AzureSettings,
    ___: dict[str, Any],
) -> Mapping[str, SolutionAdapter]:
    raise AssertionError("Mock mode must not construct provider adapters")


def _wait_for_terminal(client: TestClient, snapshot_url: str) -> dict[str, Any]:
    for _ in range(100):
        payload: dict[str, Any] = client.get(snapshot_url).json()
        if payload["status"] != "running":
            return payload
        time.sleep(0.01)
    raise AssertionError("Demo run did not finish")


def _strict_json(content: bytes | str) -> Any:
    def reject_constant(value: str) -> None:
        raise AssertionError(f"Non-standard JSON numeric constant: {value}")

    return json.loads(content, parse_constant=reject_constant)


class _StaticAdapter(SolutionAdapter):
    def __init__(
        self,
        response: AdapterResponse | None = None,
        error: ProviderResponseError | None = None,
    ) -> None:
        super().__init__("gpt-5.6-sol")
        self._response = response
        self._error = error

    def execute(self, image_bytes: bytes, media_type: str) -> AdapterResponse:
        del image_bytes, media_type
        if self._error is not None:
            raise self._error
        if self._response is None:
            raise AssertionError("Fake adapter has no response")
        return self._response


def test_interactive_api_docs_are_disabled_under_strict_csp(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        assert client.get("/api/docs").status_code == 404
        assert client.get("/api/openapi.json").status_code == 404


def test_config_exposes_three_receipts_fifteen_solutions_and_mock_notice(
    tmp_path: Path,
) -> None:
    with TestClient(
        create_app(_settings(tmp_path), adapter_factory=_must_not_build_adapters)
    ) as client:
        response = client.get("/api/config")
        notices = client.get("/api/notices")

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_mock"] is True
    assert len(payload["receipts"]) == 3
    assert len(payload["solutions"]) == 15
    assert len(payload["presets"][0]["solution_ids"]) == 15
    assert any("100-receipt benchmark" in notice for notice in payload["notices"])
    assert any("mock data" in notice for notice in payload["notices"])
    assert "complete receipt annotations" in payload["dataset"]["annotation_scope"]
    assert notices.status_code == 200
    assert "React DOM 19.2.8" in notices.text
    assert "Vite 7.3.6" in notices.text
    assert "MIT License" in notices.text


def test_request_guard_allows_only_local_hosts_and_same_origin_mutations(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), adapter_factory=_must_not_build_adapters)
    run_body = {
        "comparison_mode": "guided",
        "preset": "custom",
        "receipt_ids": ["clean-simple"],
        "upload_tokens": [],
        "solution_ids": ["gpt-5.6-sol"],
    }
    image = (
        ROOT / "src" / "receipt_bench" / "demo" / "assets" / "cord-v2" / "cord-080.jpg"
    ).read_bytes()
    with TestClient(app) as client:
        for host in (
            "localhost",
            "localhost:8123",
            "127.0.0.1",
            "127.0.0.1:8123",
            "::1",
            "[::1]",
            "[::1]:8123",
        ):
            assert client.get("/api/health", headers={"Host": host}).status_code == 200

        for host in ("evil.example", "127.0.0.2", "[::2]", "::1:8123", "localhost."):
            rejected = client.get("/api/health", headers={"Host": host})
            assert rejected.status_code == 400

        for origin_headers in (
            {"Origin": "https://evil.example"},
            {"Origin": "null"},
            {"Origin": "https://127.0.0.1"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            rejected = client.post("/api/runs", json=run_body, headers=origin_headers)
            assert rejected.status_code == 403

        blocked_upload = client.post(
            "/api/uploads",
            content=image,
            headers={
                **_filename_headers("blocked.jpg"),
                "Origin": "https://evil.example",
            },
        )
        assert blocked_upload.status_code == 403
        assert asyncio.run(app.state.upload_store.reservation_usage()) == (0, 0)

        same_origin = client.post(
            "/api/runs",
            json=run_body,
            headers={"Origin": "http://127.0.0.1"},
        )
        headless = client.delete("/api/uploads/not-present")

    assert same_origin.status_code == 202
    assert headless.status_code == 204
    assert asyncio.run(app.state.upload_store.reservation_usage()) == (0, 0)


def test_upload_filename_metadata_supports_unicode_and_rejects_malformed_values(
    tmp_path: Path,
) -> None:
    image = (
        ROOT / "src" / "receipt_bench" / "demo" / "assets" / "cord-v2" / "cord-080.jpg"
    ).read_bytes()
    app = create_app(_settings(tmp_path), adapter_factory=_must_not_build_adapters)
    filename = "../領収書-ランチ🧾.jpg"
    with TestClient(app) as client:
        uploaded = client.post(
            "/api/uploads",
            content=image,
            headers=_filename_headers(filename),
        )
        assert uploaded.status_code == 201
        assert uploaded.json()["filename"] == "領収書-ランチ🧾.jpg"

        accepted = client.post(
            "/api/runs",
            json={
                "comparison_mode": "upload",
                "preset": "custom",
                "receipt_ids": [],
                "upload_tokens": [uploaded.json()["token"]],
                "solution_ids": ["gpt-5.6-sol"],
            },
        ).json()
        _wait_for_terminal(client, accepted["snapshot_url"])
        manifest = _strict_json(client.get(accepted["manifest_url"]).content)

        malformed = client.post(
            "/api/uploads",
            content=image,
            headers={"X-Filename-B64": "***"},
        )
        invalid_utf8 = client.post(
            "/api/uploads",
            content=image,
            headers={"X-Filename-B64": "_w"},
        )
        control_character = client.post(
            "/api/uploads",
            content=image,
            headers=_filename_headers("bad\nname.jpg"),
        )
        oversized = client.post(
            "/api/uploads",
            content=image,
            headers={"X-Filename-B64": "A" * 684},
        )
        legacy = client.post(
            "/api/uploads",
            content=image,
            headers={"X-Filename": "legacy.jpg"},
        )

    assert manifest["receipts"][0]["filename"] == "領収書-ランチ🧾.jpg"
    assert malformed.status_code == 400
    assert invalid_utf8.status_code == 400
    assert control_character.status_code == 400
    assert oversized.status_code == 431
    assert legacy.status_code == 400
    assert asyncio.run(app.state.upload_store.usage()) == (0, 0)
    assert asyncio.run(app.state.upload_store.reservation_usage()) == (0, 0)


def test_packaged_frontend_is_served_without_a_node_runtime(tmp_path: Path) -> None:
    with TestClient(
        create_app(
            _settings(tmp_path, static_dir=STATIC),
            adapter_factory=_must_not_build_adapters,
        )
    ) as client:
        response = client.get("/")
        script_path = re.search(r'src="(/assets/[^"]+\.js)"', response.text)
        assert script_path is not None
        script = client.get(script_path.group(1))

    assert response.status_code == 200
    assert "Receipt Bench" in response.text
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]


def test_guided_mock_run_streams_partial_results_in_stable_download_order(
    tmp_path: Path,
) -> None:
    body = {
        "comparison_mode": "guided",
        "preset": "fast",
        "receipt_ids": ["clean-simple"],
        "upload_tokens": [],
        "solution_ids": [],
    }
    with TestClient(
        create_app(_settings(tmp_path), adapter_factory=_must_not_build_adapters)
    ) as client:
        accepted_response = client.post("/api/runs", json=body)
        assert accepted_response.status_code == 202
        accepted = accepted_response.json()

        with client.stream("GET", accepted["event_url"]) as stream:
            event_lines = [line for line in stream.iter_lines() if line.startswith("event:")]

        snapshot = client.get(accepted["snapshot_url"]).json()
        first_results = client.get(accepted["results_url"]).content
        first_manifest = client.get(accepted["manifest_url"]).content

        repeated = client.post("/api/runs", json=body).json()
        repeated_results = client.get(repeated["results_url"]).content
        repeated_manifest = client.get(repeated["manifest_url"]).content

    assert event_lines[0] == "event: run_started"
    assert "event: solution_started" in event_lines
    assert event_lines.count("event: solution_completed") == 5
    assert event_lines[-1] == "event: run_completed"
    assert snapshot["status"] == "completed"
    assert snapshot["completed_pairs"] == 5
    assert [result["solution_id"] for result in snapshot["results"]] == snapshot["solution_order"]
    assert all(result["mock"] for result in snapshot["results"])
    assert any(result["arithmetic_consistent"] is False for result in snapshot["results"])
    assert any(len(result["components"]) == 2 for result in snapshot["results"])
    assert any(
        field["state"] == "missing"
        for result in snapshot["results"]
        for field in result["field_comparisons"]
    )
    assert repeated["run_id"] == accepted["run_id"]
    assert repeated_results == first_results
    assert repeated_manifest == first_manifest
    assert first_manifest.endswith(b"\n")
    assert (
        json.loads(first_results)["manifest_sha256"] == hashlib.sha256(first_manifest).hexdigest()
    )


def test_upload_is_single_use_memory_only_and_has_no_gold_metrics(tmp_path: Path) -> None:
    image = (
        ROOT / "src" / "receipt_bench" / "demo" / "assets" / "cord-v2" / "cord-080.jpg"
    ).read_bytes()
    with TestClient(
        create_app(_settings(tmp_path), adapter_factory=_must_not_build_adapters)
    ) as client:
        uploaded = client.post(
            "/api/uploads",
            content=image,
            headers=_filename_headers(
                "../private receipt.jpg",
                **{"Content-Type": "image/jpeg"},
            ),
        )
        assert uploaded.status_code == 201
        token = uploaded.json()["token"]
        body = {
            "comparison_mode": "upload",
            "preset": "custom",
            "receipt_ids": [],
            "upload_tokens": [token],
            "solution_ids": ["gpt-5.6-sol"],
        }
        accepted = client.post("/api/runs", json=body)
        assert accepted.status_code == 202
        run = accepted.json()
        snapshot = _wait_for_terminal(client, run["snapshot_url"])
        manifest = json.loads(client.get(run["manifest_url"]).content)
        reused = client.post("/api/runs", json=body)

    assert snapshot["comparison_mode"] == "upload"
    assert snapshot["results"][0]["field_comparisons"] == []
    assert snapshot["results"][0]["canonical_result"]["receipt_type"] == "mock-upload"
    assert manifest["evaluation"]["gold_metrics_available"] is False
    assert manifest["privacy"]["uploads_written_to_disk"] is False
    assert manifest["receipts"][0]["filename"] == "private receipt.jpg"
    assert reused.status_code == 410


def test_upload_accepts_jpeg_and_png_but_rejects_pdf_and_tiff(tmp_path: Path) -> None:
    jpeg = (
        ROOT / "src" / "receipt_bench" / "demo" / "assets" / "cord-v2" / "cord-080.jpg"
    ).read_bytes()
    with Image.open(BytesIO(jpeg)) as image:
        png_buffer = BytesIO()
        image.save(png_buffer, format="PNG")
        tiff_buffer = BytesIO()
        image.save(tiff_buffer, format="TIFF")

    with TestClient(
        create_app(_settings(tmp_path), adapter_factory=_must_not_build_adapters)
    ) as client:
        accepted_jpeg = client.post(
            "/api/uploads",
            content=jpeg,
            headers=_filename_headers("receipt.jpg"),
        )
        accepted_png = client.post(
            "/api/uploads",
            content=png_buffer.getvalue(),
            headers=_filename_headers("receipt.png"),
        )
        rejected_pdf = client.post(
            "/api/uploads",
            content=b"%PDF-1.7\nnot-a-receipt",
            headers=_filename_headers("receipt.pdf"),
        )
        rejected_tiff = client.post(
            "/api/uploads",
            content=tiff_buffer.getvalue(),
            headers=_filename_headers("receipt.tiff"),
        )
        discarded = client.delete(f"/api/uploads/{accepted_jpeg.json()['token']}")

    assert accepted_jpeg.status_code == 201
    assert accepted_png.status_code == 201
    assert accepted_png.json()["media_type"] == "image/png"
    assert rejected_pdf.status_code == 415
    assert rejected_tiff.status_code == 415
    assert "JPEG and PNG" in rejected_tiff.json()["detail"]
    assert discarded.status_code == 204


def test_identical_upload_bytes_always_create_distinct_private_runs(tmp_path: Path) -> None:
    image = (
        ROOT / "src" / "receipt_bench" / "demo" / "assets" / "cord-v2" / "cord-080.jpg"
    ).read_bytes()
    runs: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    with TestClient(
        create_app(_settings(tmp_path), adapter_factory=_must_not_build_adapters)
    ) as client:
        for filename in ("first-name.jpg", "second-name.jpg"):
            uploaded = client.post(
                "/api/uploads",
                content=image,
                headers=_filename_headers(filename),
            ).json()
            accepted = client.post(
                "/api/runs",
                json={
                    "comparison_mode": "upload",
                    "preset": "custom",
                    "receipt_ids": [],
                    "upload_tokens": [uploaded["token"]],
                    "solution_ids": ["gpt-5.6-sol"],
                },
            ).json()
            _wait_for_terminal(client, accepted["snapshot_url"])
            runs.append(accepted)
            manifests.append(json.loads(client.get(accepted["manifest_url"]).content))

    assert runs[0]["run_id"] != runs[1]["run_id"]
    assert re.fullmatch(r"run-[0-9a-f]{32}", runs[0]["run_id"])
    assert [manifest["receipts"][0]["filename"] for manifest in manifests] == [
        "first-name.jpg",
        "second-name.jpg",
    ]


def test_upload_capacity_returns_429_and_shutdown_clears_pending_bytes(
    tmp_path: Path,
) -> None:
    image = (
        ROOT / "src" / "receipt_bench" / "demo" / "assets" / "cord-v2" / "cord-080.jpg"
    ).read_bytes()
    app = create_app(
        _settings(
            tmp_path,
            upload_max_items=5,
            upload_max_total_bytes=len(image) + 1,
        ),
        adapter_factory=_must_not_build_adapters,
    )
    with TestClient(app) as client:
        first = client.post(
            "/api/uploads",
            content=image,
            headers=_filename_headers("one.jpg", **{"Content-Type": "image/jpeg"}),
        )
        rejected = client.post(
            "/api/uploads",
            content=image,
            headers=_filename_headers("two.jpg", **{"Content-Type": "image/jpeg"}),
        )

    assert first.status_code == 201
    assert rejected.status_code == 429
    assert "memory limit" in rejected.json()["detail"]
    assert int(rejected.headers["Retry-After"]) >= 1
    assert asyncio.run(app.state.upload_store.usage()) == (0, 0)


def test_concurrent_upload_body_capacity_is_reserved_before_reading(tmp_path: Path) -> None:
    image = (
        ROOT / "src" / "receipt_bench" / "demo" / "assets" / "cord-v2" / "cord-080.jpg"
    ).read_bytes()
    app = create_app(
        _settings(
            tmp_path,
            upload_max_items=1,
            upload_max_total_bytes=len(image) + 1,
        ),
        adapter_factory=_must_not_build_adapters,
    )

    async def scenario() -> None:
        body_started = asyncio.Event()
        release_body = asyncio.Event()

        async def slow_body() -> AsyncIterator[bytes]:
            body_started.set()
            yield image[:64]
            await release_body.wait()
            yield image[64:]

        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                first_task = asyncio.create_task(
                    client.post(
                        "/api/uploads",
                        content=slow_body(),
                        headers=_filename_headers(
                            "slow.jpg",
                            **{"Content-Length": str(len(image))},
                        ),
                    )
                )
                await body_started.wait()
                assert await app.state.upload_store.reservation_usage() == (1, len(image))

                rejected = await client.post(
                    "/api/uploads",
                    content=image,
                    headers=_filename_headers("concurrent.jpg"),
                )
                release_body.set()
                accepted = await first_task

                assert rejected.status_code == 429
                assert accepted.status_code == 201
                assert await app.state.upload_store.reservation_usage() == (0, 0)
                assert await app.state.upload_store.usage() == (1, len(image))

    asyncio.run(scenario())


def test_cancelled_upload_releases_in_flight_capacity(tmp_path: Path) -> None:
    image = (
        ROOT / "src" / "receipt_bench" / "demo" / "assets" / "cord-v2" / "cord-080.jpg"
    ).read_bytes()
    app = create_app(
        _settings(
            tmp_path,
            upload_max_items=1,
            upload_max_total_bytes=len(image) + 1,
        ),
        adapter_factory=_must_not_build_adapters,
    )

    async def scenario() -> None:
        body_started = asyncio.Event()
        never_release = asyncio.Event()

        async def cancelled_body() -> AsyncIterator[bytes]:
            body_started.set()
            yield image[:64]
            await never_release.wait()

        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                request_task = asyncio.create_task(
                    client.post(
                        "/api/uploads",
                        content=cancelled_body(),
                        headers=_filename_headers(
                            "cancelled.jpg",
                            **{"Content-Length": str(len(image))},
                        ),
                    )
                )
                await body_started.wait()
                assert await app.state.upload_store.reservation_usage() == (1, len(image))
                request_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request_task
                assert await app.state.upload_store.reservation_usage() == (0, 0)

                accepted = await client.post(
                    "/api/uploads",
                    content=image,
                    headers=_filename_headers("after-cancel.jpg"),
                )
                assert accepted.status_code == 201

    asyncio.run(scenario())


def test_mock_full_preset_exercises_failed_invalid_and_partial_components(
    tmp_path: Path,
) -> None:
    with TestClient(
        create_app(_settings(tmp_path), adapter_factory=_must_not_build_adapters)
    ) as client:
        accepted = client.post(
            "/api/runs",
            json={
                "comparison_mode": "guided",
                "preset": "full",
                "receipt_ids": ["challenging-noisy"],
                "upload_tokens": [],
                "solution_ids": [],
            },
        ).json()
        snapshot = _wait_for_terminal(client, accepted["snapshot_url"])

    by_solution = {result["solution_id"]: result for result in snapshot["results"]}
    assert by_solution["grok-4.6"]["status"] == "failed"
    assert by_solution["mistral-ocr-4.0"]["status"] == "invalid"
    assert by_solution["mistral-ocr-4.0"]["components"]
    assert any(
        comparison["state"] == "extra" for comparison in by_solution["gpt-5.2"]["field_comparisons"]
    )
    assert "No live request" in by_solution["grok-4.6"]["error"]["message"]


def test_azure_mode_remains_locked_without_independent_live_opt_in(
    tmp_path: Path,
) -> None:
    with TestClient(
        create_app(
            _settings(tmp_path, mode="azure", allow_live=False),
            adapter_factory=_must_not_build_adapters,
        )
    ) as client:
        response = client.post(
            "/api/runs",
            json={
                "comparison_mode": "guided",
                "preset": "fast",
                "receipt_ids": ["clean-simple"],
                "upload_tokens": [],
                "solution_ids": [],
            },
        )

    assert response.status_code == 503
    assert "safety-locked" in response.json()["detail"]


@pytest.mark.parametrize(
    ("case", "expected_status", "expected_code"),
    [
        ("receipt_infinity", "invalid", "non_finite_provider_output"),
        ("usage_nan", "invalid", "non_finite_provider_output"),
        ("failed_usage_infinity", "failed", "provider_error"),
    ],
)
def test_non_finite_provider_numbers_are_isolated_and_all_api_json_stays_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_status: str,
    expected_code: str,
) -> None:
    receipt = CanonicalReceipt()
    measurement = ComponentMeasurement(component="model", latency_ms=1.0)
    if case == "receipt_infinity":
        receipt.amounts.total = float("inf")
    else:
        measurement.usage.provider_metadata["nested"] = {
            "value": float("nan") if case == "usage_nan" else float("-inf")
        }

    if case == "failed_usage_infinity":
        adapter = _StaticAdapter(
            error=ProviderResponseError(
                "provider failed with unsafe telemetry",
                components=[measurement],
            )
        )
    else:
        adapter = _StaticAdapter(
            response=AdapterResponse(
                receipt=receipt,
                raw_response={},
                components=[] if case == "receipt_infinity" else [measurement],
            )
        )

    def adapter_factory(
        _: BenchmarkConfig,
        __: AzureSettings,
        ___: dict[str, Any],
    ) -> Mapping[str, SolutionAdapter]:
        return {"gpt-5.6-sol": adapter}

    monkeypatch.setattr("receipt_bench.demo.orchestrator.AzureSettings", lambda: object())
    app = create_app(
        _settings(tmp_path, mode="azure", allow_live=True),
        adapter_factory=adapter_factory,
    )
    with TestClient(app) as client:
        accepted_response = client.post(
            "/api/runs",
            json={
                "comparison_mode": "guided",
                "preset": "custom",
                "receipt_ids": ["clean-simple"],
                "upload_tokens": [],
                "solution_ids": ["gpt-5.6-sol"],
            },
        )
        assert accepted_response.status_code == 202
        accepted = accepted_response.json()
        snapshot_response = client.get(accepted["snapshot_url"])
        for _ in range(100):
            snapshot = _strict_json(snapshot_response.content)
            if snapshot["status"] != "running":
                break
            time.sleep(0.01)
            snapshot_response = client.get(accepted["snapshot_url"])
        else:
            raise AssertionError("Demo run did not finish")

        events_response = client.get(accepted["event_url"])
        results_response = client.get(accepted["results_url"])
        manifest_response = client.get(accepted["manifest_url"])

    result = snapshot["results"][0]
    assert snapshot["status"] == "completed"
    assert result["status"] == expected_status
    assert result["error"]["code"] == expected_code
    assert result["components"] == []

    for response in (snapshot_response, results_response, manifest_response):
        assert response.status_code == 200
        _strict_json(response.content)
        assert b"Infinity" not in response.content
        assert b"NaN" not in response.content
    for line in events_response.text.splitlines():
        if line.startswith("data: "):
            _strict_json(line.removeprefix("data: "))


def test_active_run_admission_limit_is_application_wide_in_mock_mode(
    tmp_path: Path,
) -> None:
    first_body = {
        "comparison_mode": "guided",
        "preset": "custom",
        "receipt_ids": ["clean-simple"],
        "upload_tokens": [],
        "solution_ids": ["gpt-5.6-sol"],
    }
    second_body = {
        **first_body,
        "receipt_ids": ["complex-itemized"],
    }
    with TestClient(
        create_app(
            _settings(
                tmp_path,
                max_active_runs=1,
                mock_delay_scale=10.0,
            ),
            adapter_factory=_must_not_build_adapters,
        )
    ) as client:
        first = client.post("/api/runs", json=first_body)
        repeated = client.post("/api/runs", json=first_body)
        rejected = client.post("/api/runs", json=second_body)

    assert first.status_code == 202
    assert repeated.status_code == 202
    assert repeated.json()["run_id"] == first.json()["run_id"]
    assert rejected.status_code == 429
    assert rejected.headers["Retry-After"] == "1"
    assert "Active run limit reached" in rejected.json()["detail"]


def test_admission_rejection_does_not_consume_upload_token(tmp_path: Path) -> None:
    image = (
        ROOT / "src" / "receipt_bench" / "demo" / "assets" / "cord-v2" / "cord-080.jpg"
    ).read_bytes()
    settings = _settings(
        tmp_path,
        max_active_runs=1,
        mock_delay_scale=2.0,
    )
    app = create_app(settings, adapter_factory=_must_not_build_adapters)
    with TestClient(app) as client:
        uploaded = client.post(
            "/api/uploads",
            content=image,
            headers=_filename_headers("retry-me.jpg"),
        ).json()
        first = client.post(
            "/api/runs",
            json={
                "comparison_mode": "guided",
                "preset": "custom",
                "receipt_ids": ["clean-simple"],
                "upload_tokens": [],
                "solution_ids": ["gpt-5.6-sol"],
            },
        )
        upload_body = {
            "comparison_mode": "upload",
            "preset": "custom",
            "receipt_ids": [],
            "upload_tokens": [uploaded["token"]],
            "solution_ids": ["gpt-5.6-sol"],
        }
        rejected = client.post("/api/runs", json=upload_body)
        pending_usage = asyncio.run(app.state.upload_store.usage())
        _wait_for_terminal(client, first.json()["snapshot_url"])
        retried = client.post("/api/runs", json=upload_body)
        retried_snapshot = _wait_for_terminal(client, retried.json()["snapshot_url"])

    assert first.status_code == 202
    assert rejected.status_code == 429
    assert pending_usage == (1, len(image))
    assert retried.status_code == 202
    assert retried_snapshot["comparison_mode"] == "upload"


def test_failed_run_preparation_releases_admission_reservation(tmp_path: Path) -> None:
    with TestClient(
        create_app(
            _settings(tmp_path, max_active_runs=1),
            adapter_factory=_must_not_build_adapters,
        )
    ) as client:
        invalid = client.post(
            "/api/runs",
            json={
                "comparison_mode": "upload",
                "preset": "custom",
                "receipt_ids": [],
                "upload_tokens": ["expired-token"],
                "solution_ids": ["gpt-5.6-sol"],
            },
        )
        valid = client.post(
            "/api/runs",
            json={
                "comparison_mode": "guided",
                "preset": "custom",
                "receipt_ids": ["clean-simple"],
                "upload_tokens": [],
                "solution_ids": ["gpt-5.6-sol"],
            },
        )

    assert invalid.status_code == 410
    assert valid.status_code == 202
