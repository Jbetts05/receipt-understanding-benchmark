from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from receipt_bench.storage.local import LocalStorage
from receipt_bench.storage.models import TaskMessage, TaskRecord
from receipt_bench.storage.paths import run_manifest, run_tasks
from receipt_bench.storage.validation import JsonValue
from receipt_bench.web import WebSettings, create_local_app
from receipt_bench.web.repositories import RepositoryBundle
from receipt_bench.worker.contracts import ExecutionError, ParsedExecution, RawExecution

from .conftest import (
    PNG_BYTES,
    base64url,
    create_project,
    create_run,
    project_payload,
    upload_sample,
    wait_for_terminal,
)


class RecordingQueue:
    def __init__(self) -> None:
        self.messages: list[TaskMessage] = []

    async def send(self, message: TaskMessage) -> str:
        self.messages.append(message)
        return f"message-{len(self.messages)}"


class OneExactExecutor:
    def __init__(self) -> None:
        self.successful_sample_id: str | None = None

    async def execute(
        self,
        task: TaskRecord,
        image_bytes: bytes,
        media_type: str,
    ) -> RawExecution:
        del image_bytes, media_type
        if task.sample_id != self.successful_sample_id:
            raise ExecutionError(
                "expected_test_failure",
                "This pair failed during extraction.",
                retryable=False,
                telemetry={"latency_ms": 2.0, "estimated_cost_usd": 0.002},
            )
        return RawExecution(
            raw={"merchant": "Store 0", "total": 0.0},
            telemetry={"latency_ms": 1.0, "estimated_cost_usd": 0.0},
        )

    async def parse(
        self,
        task: TaskRecord,
        raw: JsonValue,
    ) -> ParsedExecution:
        del task
        return ParsedExecution(parsed=raw)


def test_builtin_projects_expose_dataset_contracts_and_stable_order(
    client: TestClient,
) -> None:
    response = client.get("/api/projects")

    assert response.status_code == 200
    projects = response.json()
    assert [project["id"] for project in projects[:2]] == ["sroie", "cord-v2"]
    sroie, cord = projects[:2]
    assert (sroie["sample_count"], sroie["scored_sample_count"]) == (80, 76)
    assert (cord["sample_count"], cord["prompt_example_count"]) == (20, 2)
    assert sroie["rights"]["state"] == "restricted"
    assert sroie["available"] is True
    assert cord["attribution"]["license"] == "CC-BY-4.0"
    assert "schema" in cord and "schema_" not in cord
    assert cord["schema"]["origin"] == "dataset_derived"
    assert "gt_parse" in cord["schema"]["derivation"]
    assert cord["prompt"]["version"]
    assert {item["mode"] for item in cord["prompt_contracts"]} == {
        "direct-multimodal",
        "ocr-normalizer",
    }
    normalizer_prompt = next(
        item
        for item in cord["prompt_contracts"]
        if item["mode"] == "ocr-normalizer"
    )
    assert normalizer_prompt["normalizer_model"] == "gpt-5.6-luna"
    assert len(cord["selected_solution_ids"]) == 22
    assert cord["compatibility"]

    detail = client.get("/api/projects/cord-v2")
    assert detail.status_code == 200
    assert detail.json() == cord
    assert "blob.core" not in response.text.casefold()
    assert "sig=" not in response.text.casefold()

    samples = client.get("/api/projects/sroie/samples").json()
    assert sum(sample["scoring_role"] == "scored" for sample in samples) == 76
    assert sum(sample["scoring_role"] == "few_shot" for sample in samples) == 2
    excluded = [
        sample for sample in samples if sample["scoring_role"] == "excluded"
    ]
    assert len(excluded) == 2
    assert all(sample["exclusion_reason"] for sample in excluded)
    assert all(not sample["prompt_example"] for sample in excluded)

    rejected = client.post(
        "/api/projects/sroie/runs",
        json={
            "sample_ids": [excluded[0]["id"]],
            "solution_ids": [sroie["selected_solution_ids"][0]],
            "schema_version": sroie["schema"]["version"],
            "prompt_version": sroie["prompt"]["version"],
            "execution_mode": "mock",
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "excluded_sample_selected"


def test_custom_project_and_sample_validation(client: TestClient) -> None:
    non_finite = client.post(
        "/api/projects",
        content=(
            b'{"name":"Non-finite schema","description":"","schema":'
            b'{"type":"object","x-value":NaN},"prompt":"Extract",'
            b'"selected_solution_ids":["gpt-5.4-mini"]}'
        ),
        headers={"Content-Type": "application/json"},
    )
    assert non_finite.status_code == 422
    assert non_finite.json()["code"] == "invalid_schema"

    deep_schema: dict[str, Any] = {"type": "string"}
    for _ in range(20):
        deep_schema = {
            "type": "object",
            "properties": {"child": deep_schema},
        }
    too_deep = project_payload(name="Deep schema")
    too_deep["schema"] = deep_schema
    deep_response = client.post("/api/projects", json=too_deep)
    assert deep_response.status_code == 422
    assert deep_response.json()["code"] == "schema_too_deep"

    invalid_schema = project_payload(name="Invalid schema")
    invalid_schema["schema"] = {"type": "array", "items": {"type": "string"}}
    assert client.post("/api/projects", json=invalid_schema).status_code == 422

    unsupported_schema = project_payload(name="Unsupported schema")
    unsupported_schema["schema"] = {
        "type": "object",
        "properties": {"anything": True},
    }
    unsupported_response = client.post("/api/projects", json=unsupported_schema)
    assert unsupported_response.status_code == 422
    assert unsupported_response.json()["code"] == "schema_not_supported"

    unsafe_pattern = project_payload(name="Unsafe pattern")
    unsafe_pattern["schema"] = {
        "type": "object",
        "properties": {
            "value": {"type": "string", "pattern": "^(a+)+$"},
        },
    }
    unsafe_pattern_response = client.post("/api/projects", json=unsafe_pattern)
    assert unsafe_pattern_response.status_code == 422
    assert unsafe_pattern_response.json()["code"] == "schema_not_supported"

    too_many_properties = project_payload(name="Too many properties")
    too_many_properties["schema"] = {
        "type": "object",
        "properties": {
            f"field_{index}": {"type": "string"} for index in range(101)
        },
        "additionalProperties": False,
    }
    property_response = client.post("/api/projects", json=too_many_properties)
    assert property_response.status_code == 422
    assert property_response.json()["code"] == "schema_too_complex"

    provider_deep: dict[str, Any] = {"type": "string"}
    for index in range(5):
        provider_deep = {
            "type": "object",
            "properties": {f"level_{index}": provider_deep},
            "additionalProperties": False,
        }
    provider_depth_payload = project_payload(name="Provider deep")
    provider_depth_payload["schema"] = provider_deep
    provider_depth_response = client.post(
        "/api/projects",
        json=provider_depth_payload,
    )
    assert provider_depth_response.status_code == 422
    assert provider_depth_response.json()["code"] == "schema_too_deep"

    unsupported_composition = project_payload(name="Unsupported composition")
    unsupported_composition["schema"] = {
        "type": "object",
        "allOf": [
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            }
        ],
    }
    composition_response = client.post(
        "/api/projects",
        json=unsupported_composition,
    )
    assert composition_response.status_code == 422
    assert composition_response.json()["code"] == "schema_not_supported"

    permissive_object = project_payload(name="Permissive object")
    permissive_object["schema"] = {
        "type": "object",
        "properties": {"labels": {"type": "object"}},
        "additionalProperties": {"type": "string"},
    }
    permissive_response = client.post("/api/projects", json=permissive_object)
    assert permissive_response.status_code == 422
    assert permissive_response.json()["code"] == "schema_not_supported"

    object_constraint = project_payload(name="Object constraint")
    object_constraint["schema"] = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "minProperties": 1,
    }
    constraint_response = client.post("/api/projects", json=object_constraint)
    assert constraint_response.status_code == 422
    assert constraint_response.json()["code"] == "schema_not_supported"

    unique_items = project_payload(name="Unique item comparison")
    unique_items["schema"] = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": False},
                "uniqueItems": True,
            }
        },
        "additionalProperties": False,
    }
    unique_response = client.post("/api/projects", json=unique_items)
    assert unique_response.status_code == 422
    assert unique_response.json()["code"] == "schema_not_supported"

    dynamic_reference = project_payload(name="Dynamic reference")
    dynamic_reference["schema"] = {
        "type": "object",
        "properties": {"value": {"$dynamicRef": "#/$defs/value"}},
        "$defs": {"value": {"type": "string"}},
        "additionalProperties": False,
    }
    dynamic_response = client.post("/api/projects", json=dynamic_reference)
    assert dynamic_response.status_code == 422
    assert dynamic_response.json()["code"] == "schema_not_supported"

    definitions: dict[str, Any] = {
        "leaf": {"type": "string"},
    }
    for index in range(20):
        previous = "leaf" if index == 0 else f"level_{index - 1}"
        definitions[f"level_{index}"] = {
            "anyOf": [
                {"$ref": f"#/$defs/{previous}"},
                {"$ref": f"#/$defs/{previous}"},
            ]
        }
    repeated_reference = project_payload(name="Repeated reference DAG")
    repeated_reference["schema"] = {
        "type": "object",
        "properties": {"value": {"$ref": "#/$defs/level_19"}},
        "additionalProperties": False,
        "$defs": definitions,
    }
    reference_started = time.perf_counter()
    repeated_reference_response = client.post(
        "/api/projects",
        json=repeated_reference,
    )
    assert repeated_reference_response.status_code == 201
    assert time.perf_counter() - reference_started < 1.0

    remote_ref = project_payload(name="Remote reference")
    remote_ref["schema"] = {
        "type": "object",
        "properties": {"value": {"$ref": "https://example.test/schema.json"}},
    }
    assert client.post("/api/projects", json=remote_ref).status_code == 422

    project = create_project(client)
    repeated = client.post("/api/projects", json=project_payload())
    assert repeated.status_code == 201
    assert repeated.json()["id"] == project["id"]

    invalid_gold = json.dumps({"merchant": "Shop", "total": "12.00"}).encode()
    response = client.post(
        f"/api/projects/{project['id']}/samples",
        content=PNG_BYTES,
        headers={
            "Content-Type": "image/png",
            "X-Filename-B64": base64url(b"bad.png"),
            "X-Gold-JSON-B64": base64url(invalid_gold),
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_gold"

    formatted = project_payload(name="Formatted gold")
    formatted["schema"] = {
        "type": "object",
        "properties": {
            "date": {"type": "string", "format": "date"},
        },
        "required": ["date"],
        "additionalProperties": False,
    }
    formatted_project = client.post("/api/projects", json=formatted)
    assert formatted_project.status_code == 201
    bad_date_gold = json.dumps({"date": "not-a-date"}).encode()
    bad_date_response = client.post(
        f"/api/projects/{formatted_project.json()['id']}/samples",
        content=PNG_BYTES,
        headers={
            "Content-Type": "image/png",
            "X-Filename-B64": base64url(b"bad-date.png"),
            "X-Gold-JSON-B64": base64url(bad_date_gold),
        },
    )
    assert bad_date_response.status_code == 422
    assert bad_date_response.json()["code"] == "invalid_gold"

    sample = upload_sample(
        client,
        project["id"],
        filename=r"..\private\receipt.png",
        gold={"merchant": "Shop", "total": 12.0},
    )
    assert sample["filename"] == "receipt.png"
    assert sample["gold_availability"] == "available"
    assert sample["gold"] == {"merchant": "Shop", "total": 12.0}
    assert sample["image_url"].startswith("/api/")
    stored_project = asyncio.run(
        client.app.state.repositories.projects.get("public", project["id"])
    )
    assert "samples" not in stored_project.value.metadata
    assert isinstance(stored_project.value.metadata["sample_index_path"], str)

    image = client.get(sample["image_url"])
    assert image.status_code == 200
    assert image.content == PNG_BYTES
    assert image.headers["content-type"] == "image/png"
    assert "private" in image.headers["cache-control"]
    assert "blob.core" not in image.text.casefold()

    large_gold_project = project_payload(name="Large gold body")
    large_gold_project["schema"] = {
        "type": "object",
        "properties": {
            "notes": {"type": "string"},
        },
        "required": ["notes"],
        "additionalProperties": False,
    }
    created_large = client.post("/api/projects", json=large_gold_project)
    assert created_large.status_code == 201
    large_gold = {"notes": "x" * 60_000}
    large_gold_bytes = json.dumps(large_gold).encode()
    multipart = client.post(
        f"/api/projects/{created_large.json()['id']}/samples",
        files={
            "image": ("收据.png", PNG_BYTES, "image/png"),
            "gold": ("gold.json", large_gold_bytes, "application/json"),
        },
    )
    assert multipart.status_code == 201, multipart.text
    assert multipart.json()["filename"] == "收据.png"
    assert multipart.json()["gold"] == large_gold


def test_custom_project_content_understanding_schema_capability(
    client: TestClient,
) -> None:
    supported = project_payload(
        name="Dynamic Content Understanding",
        solutions=["content-understanding-custom"],
    )
    response = client.post("/api/projects", json=supported)
    assert response.status_code == 201
    custom = next(
        item
        for item in response.json()["compatibility"]
        if item["solution_id"] == "content-understanding-custom"
    )
    assert custom["compatibility"] == "full"
    assert custom["comparable_quality"] is True
    assert (
        custom["deterministic_mapping_id"]
        == "content-understanding-dynamic-schema-v1"
    )

    unsupported = project_payload(
        name="Unsupported Content Understanding union",
        solutions=["content-understanding-custom"],
    )
    unsupported["schema"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "value": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            }
        },
    }
    rejected = client.post("/api/projects", json=unsupported)
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "content_understanding_schema_not_supported"


def test_run_creation_is_stable_dispatched_once_and_idempotent(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "storage")
    queue = RecordingQueue()
    repositories = RepositoryBundle(
        artifacts=storage.artifacts,
        projects=storage.projects,
        runs=storage.runs,
        tasks=storage.tasks,
        events=storage.events,
        queue=queue,  # type: ignore[arg-type]
    )
    app = create_local_app(
        WebSettings(storage_root=tmp_path / "storage", start_worker=False),
        repositories=repositories,
    )
    with TestClient(app, base_url="http://localhost") as client:
        project = create_project(
            client,
            solutions=["gpt-5.4-mini", "azure-vision-read"],
        )
        first = upload_sample(
            client,
            project["id"],
            filename="first.png",
            gold={"merchant": "First", "total": 1},
        )
        second = upload_sample(
            client,
            project["id"],
            filename="second.png",
            gold={"merchant": "Second", "total": 2},
        )
        accepted = create_run(
            client,
            project,
            [second["id"], first["id"]],
            ["azure-vision-read", "gpt-5.4-mini"],
            idempotency_key="stable-request",
        )
        assert accepted["total_pairs"] == 4
        assert len(queue.messages) == 4
        assert len({message.pair_id for message in queue.messages}) == 4
        assert all(message.dispatch_token for message in queue.messages)

        repeated = create_run(
            client,
            project,
            [first["id"], second["id"]],
            ["gpt-5.4-mini", "azure-vision-read"],
            idempotency_key="stable-request",
        )
        assert repeated == accepted
        assert len(queue.messages) == 4

        conflict = client.post(
            f"/api/projects/{project['id']}/runs",
            json={
                "sample_ids": [first["id"]],
                "solution_ids": ["gpt-5.4-mini"],
                "schema_version": project["schema"]["version"],
                "prompt_version": project["prompt"]["version"],
                "execution_mode": "mock",
            },
            headers={"Idempotency-Key": "stable-request"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "idempotency_conflict"

        snapshot = client.get(accepted["snapshot_url"]).json()
        assert snapshot["manifest"]["sample_ids"] == [first["id"], second["id"]]
        assert snapshot["manifest"]["solution_ids"] == [
            "gpt-5.4-mini",
            "azure-vision-read",
        ]
        assert [task["solution_order"] for task in snapshot["tasks"]] == [3, 12, 3, 12]
        assert all(task["status"] == "queued" for task in snapshot["tasks"])
        manifest = json.loads(
            (
                tmp_path
                / "storage"
                / "artifacts"
                / run_manifest(accepted["run_id"])
            ).read_text(encoding="utf-8")
        )
        direct_prompt = manifest["solution_prompts"]["gpt-5.4-mini"]
        ocr_prompt = manifest["solution_prompts"]["azure-vision-read"]
        assert direct_prompt["hash"] == project["prompt"]["hash"].removeprefix("sha256:")
        assert direct_prompt["hash"] != ocr_prompt["hash"]


def test_mock_run_results_native_aggregation_and_artifact_proxies(
    tmp_path: Path,
) -> None:
    app = create_local_app(
        WebSettings(
            storage_root=tmp_path / "storage",
            start_worker=True,
            sse_poll_interval=0.01,
            sse_heartbeat_interval=0.02,
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        project = create_project(
            client,
            solutions=["gpt-5.4-mini", "document-intelligence-receipt"],
        )
        sample = upload_sample(
            client,
            project["id"],
            gold={"merchant": "Store", "total": 4.5},
        )
        accepted = create_run(
            client,
            project,
            [sample["id"]],
            ["document-intelligence-receipt", "gpt-5.4-mini"],
        )
        snapshot = wait_for_terminal(client, accepted["run_id"])

        assert snapshot["manifest"]["status"] == "completed"
        assert snapshot["manifest"]["reference_only_solution_ids"] == [
            "document-intelligence-receipt"
        ]
        assert [task["solution_id"] for task in snapshot["tasks"]] == [
            "gpt-5.4-mini",
            "document-intelligence-receipt",
        ]
        assert all(task["status"] == "succeeded" for task in snapshot["tasks"])
        refreshed_sample = next(
            item
            for item in client.get(
                f"/api/projects/{project['id']}/samples"
            ).json()
            if item["id"] == sample["id"]
        )
        assert refreshed_sample["prior_run_state"] == "complete"

        compact = client.get(
            f"{accepted['snapshot_url']}?compact=true&result_sample_id={sample['id']}"
        )
        assert compact.status_code == 200
        assert len(compact.json()["results"]) == 2
        assert all(result["artifacts"] == {} for result in compact.json()["results"])
        assert all(task["partial_output"] is None for task in compact.json()["tasks"])
        assert (
            client.get(
                f"{accepted['snapshot_url']}?compact=true&result_sample_id=missing"
            ).status_code
            == 404
        )

        results = client.get(accepted["results_url"])
        assert results.status_code == 200
        rows = results.json()
        assert [row["solution_order"] for row in rows] == [3, 9]
        native = next(
            row for row in rows if row["solution_id"] == "document-intelligence-receipt"
        )
        assert native["compatibility"] == "partial_reference"
        assert native["comparable_quality"] is False
        assert native["quality_scored"] is False
        assert all(url["url"].startswith("/api/") for url in native["artifacts"].values())
        assert "blob.core" not in results.text.casefold()
        assert "sig=" not in results.text.casefold()
        compact_results = client.get(
            f"{accepted['results_url']}?compact=true&sample_id={sample['id']}"
        )
        assert compact_results.status_code == 200
        assert len(compact_results.json()) == 2
        assert all(result["artifacts"] == {} for result in compact_results.json())

        detail_url = (
            f"/api/runs/{accepted['run_id']}/results/{sample['id']}/"
            "document-intelligence-receipt"
        )
        detail = client.get(detail_url)
        assert detail.status_code == 200
        assert detail.json() == native
        for artifact in native["artifacts"].values():
            response = client.get(artifact["url"])
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/json"
            json.loads(response.content)

        aggregate = client.get(
            f"/api/projects/{project['id']}/runs/{accepted['run_id']}/aggregate"
        )
        assert aggregate.status_code == 200
        aggregate_rows = {
            row["solution_id"]: row for row in aggregate.json()["solutions"]
        }
        native_aggregate = aggregate_rows["document-intelligence-receipt"]
        assert native_aggregate["comparable_quality"] is False
        assert native_aggregate["normalized_field_f1"]["value"] is None
        assert native_aggregate["whole_answer_exactness"]["value"] is None
        assert native_aggregate["success_rate"]["value"] == 1.0

        first_download = client.get(accepted["download_url"])
        second_download = client.get(accepted["download_url"])
        assert first_download.status_code == 200
        assert first_download.content.startswith(b"PK")
        assert first_download.content == second_download.content
        assert "attachment" in first_download.headers["content-disposition"]
        persisted_tasks = json.loads(
            (
                tmp_path
                / "storage"
                / "artifacts"
                / run_tasks(accepted["run_id"])
            ).read_text(encoding="utf-8")
        )["tasks"]
        assert all(task["estimated_cost_usd"] == 0.0 for task in persisted_tasks)
        assert all(task["elapsed_ms"] > 0 for task in persisted_tasks)


def test_missing_gold_never_reports_quality(tmp_path: Path) -> None:
    app = create_local_app(
        WebSettings(
            storage_root=tmp_path / "no-gold",
            start_worker=True,
            sse_poll_interval=0.01,
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        project = create_project(client, name="No gold")
        sample = upload_sample(client, project["id"], gold=None)
        assert sample["gold_availability"] == "none"
        refreshed_project = client.get(f"/api/projects/{project['id']}").json()
        assert refreshed_project["scored_sample_count"] == 0

        accepted = create_run(client, project, [sample["id"]], ["gpt-5.4-mini"])
        snapshot = wait_for_terminal(client, accepted["run_id"])
        result = snapshot["results"][0]
        assert result["quality_scored"] is False
        assert result["comparisons"] == []

        aggregate = client.get(
            f"/api/projects/{project['id']}/runs/{accepted['run_id']}/aggregate"
        ).json()
        assert aggregate["has_gold"] is False
        assert aggregate["scored_sample_count"] == 0
        assert aggregate["solutions"][0]["normalized_field_f1"]["value"] is None
        assert aggregate["solutions"][0]["whole_answer_exactness"]["value"] is None


def test_quality_aggregate_counts_failed_expected_pairs(tmp_path: Path) -> None:
    executor = OneExactExecutor()
    app = create_local_app(
        WebSettings(
            storage_root=tmp_path / "failed-pairs",
            start_worker=True,
            sse_poll_interval=0.01,
        ),
        executor=executor,
    )
    with TestClient(app, base_url="http://localhost") as client:
        project = create_project(client, name="Failed pair denominator")
        samples = [
            upload_sample(
                client,
                project["id"],
                filename=f"receipt-{index}.png",
                gold={"merchant": f"Store {index}", "total": float(index)},
            )
            for index in range(18)
        ]
        executor.successful_sample_id = samples[0]["id"]
        accepted = create_run(
            client,
            project,
            [sample["id"] for sample in samples],
            ["gpt-5.4-mini"],
        )
        snapshot = wait_for_terminal(client, accepted["run_id"], timeout=30.0)
        assert sum(result["status"] == "succeeded" for result in snapshot["results"]) == 1
        assert sum(result["status"] == "failed" for result in snapshot["results"]) == 17
        prior_states = {
            item["id"]: item["prior_run_state"]
            for item in client.get(
                f"/api/projects/{project['id']}/samples"
            ).json()
        }
        assert prior_states[samples[0]["id"]] == "complete"
        assert {
            prior_states[sample["id"]] for sample in samples[1:]
        } == {"failed"}

        aggregate = client.get(
            f"/api/projects/{project['id']}/runs/{accepted['run_id']}/aggregate"
        )
        assert aggregate.status_code == 200
        payload = aggregate.json()
        metrics = payload["solutions"][0]
        assert payload["scored_sample_count"] == 18
        assert metrics["whole_answer_exactness"]["value"] == pytest.approx(1 / 18)
        assert metrics["normalized_field_f1"]["value"] == pytest.approx(2 / 19)
        assert metrics["success_rate"]["value"] == pytest.approx(1 / 18)
        assert metrics["median_latency"]["value"] == pytest.approx(2.0)
        assert metrics["p95_latency"]["value"] == pytest.approx(2.0)
        assert metrics["median_latency"]["polarity"] == "lower_better"
        assert metrics["estimated_cost"]["value"] == pytest.approx(0.034)
        assert metrics["estimated_cost"]["polarity"] == "lower_better"
        assert metrics["cost_scope"] == "complete"
        assert metrics["total_attempts"]["value"] == 18
        assert metrics["input_tokens"]["value"] is None
        fields = {row["path"]: row for row in payload["fields"]}
        assert set(fields) == {"merchant", "total"}
        for field in fields.values():
            assert field["matched"] == 1
            assert field["wrong"] == 0
            assert field["partial"] == 0
            assert field["missing"] == 17
            assert field["extra"] == 0
            assert field["coverage"]["value"] == pytest.approx(1 / 18)
            assert field["exact_match_rate"]["value"] == pytest.approx(1 / 18)
            assert field["normalized_field_f1"]["value"] == pytest.approx(2 / 19)
