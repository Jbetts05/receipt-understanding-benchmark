from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from receipt_bench.storage.local import (
    LocalArtifactRepository,
    LocalEventRepository,
    LocalStorage,
)
from receipt_bench.storage.models import (
    ArtifactInfo,
    ProjectRecord,
    RunRecord,
    TaskMessage,
    TaskRecord,
    datetime_text,
)
from receipt_bench.storage.paths import (
    project_prompt,
    project_schema,
    result_raw,
    run_manifest,
    sample_image,
)
from receipt_bench.storage.protocols import (
    ConcurrencyError,
    ImmutableArtifactError,
    LeaseLostError,
)
from receipt_bench.storage.validation import (
    MAX_EVENT_BYTES,
    JsonObject,
    StrictJsonError,
    UnsafeIdentifierError,
    UnsafePathError,
    bounded_text,
    normalize_json,
    strict_json_bytes,
    strict_json_loads,
    validate_artifact_path,
    validate_id,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 30, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _project(clock: MutableClock) -> ProjectRecord:
    return ProjectRecord(
        owner_scope="public",
        project_id="cord-v2",
        name="CORD v2",
        description="Fixed test subset",
        schema_artifact_path=project_schema("cord-v2"),
        prompt_artifact_path=project_prompt("cord-v2", "1.0.0"),
        prompt_version="1.0.0",
        created_at=clock(),
        updated_at=clock(),
    )


def _run(clock: MutableClock, *, total: int = 1) -> RunRecord:
    return RunRecord(
        project_id="cord-v2",
        run_id="run-001",
        manifest_artifact_path=run_manifest("run-001"),
        total_tasks=total,
        pending_tasks=total,
        created_at=clock(),
        updated_at=clock(),
        metadata={"dispatch_complete": True},
    )


def _task(clock: MutableClock, sample_id: str = "sample-001") -> TaskRecord:
    return TaskRecord(
        project_id="cord-v2",
        run_id="run-001",
        sample_id=sample_id,
        solution_id="gpt-5.4-mini",
        image_artifact_path=sample_image("cord-v2", sample_id),
        media_type="image/jpeg",
        provider_keys=("azure-openai",),
        created_at=clock(),
        updated_at=clock(),
    )


def test_paths_and_strict_json_reject_traversal_and_non_finite_values() -> None:
    for value in (
        "../secret",
        "a/b",
        r"a\b",
        ".",
        "name ",
        "MixedCase",
        "CON",
        "nul.txt",
        "LPT1",
    ):
        with pytest.raises(UnsafeIdentifierError):
            validate_id(value)
    for value in (
        "../secret",
        "/absolute",
        r"runs\x",
        "runs//x",
        "runs/./x",
        "runs/aux.json",
        "runs/trailing.",
        "runs/MixedCase.json",
    ):
        with pytest.raises(UnsafePathError):
            validate_artifact_path(value)
    with pytest.raises(StrictJsonError):
        strict_json_bytes({"latency": float("nan")})
    with pytest.raises(StrictJsonError):
        strict_json_bytes({"unsupported": object()})
    for encoding in ("utf-16", "utf-32"):
        with pytest.raises(StrictJsonError, match="UTF-8"):
            strict_json_loads('{"ok":true}'.encode(encoding))
    for value in ("\ud800", "\udfff", {"\ud800": "value"}):
        with pytest.raises(StrictJsonError, match="surrogate"):
            normalize_json(value)
        with pytest.raises(StrictJsonError):
            strict_json_bytes(value)
    with pytest.raises(StrictJsonError, match="UTF-8"):
        strict_json_loads('{"value":"\ud800"}')
    with pytest.raises(StrictJsonError, match="surrogate"):
        bounded_text("\ud800", field="provider_message")
    assert normalize_json("\ud83d\ude00") == "\U0001f600"


def test_immutable_artifacts_are_atomic_idempotent_and_collision_safe(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        path = result_raw("run-001", "sample-001", "gpt-5.4-mini")
        first = await storage.artifacts.create_json(path, {"answer": 42})
        second = await storage.artifacts.create_json(path, {"answer": 42})
        assert first.sha256 == second.sha256
        assert await storage.artifacts.get_json(path) == {"answer": 42}
        with pytest.raises(ImmutableArtifactError):
            await storage.artifacts.create_json(path, {"answer": 43})
        assert not list(tmp_path.rglob("*.tmp"))

    asyncio.run(scenario())


def test_records_use_conditional_versions_and_survive_restart(tmp_path: Path) -> None:
    clock = MutableClock()

    async def scenario() -> None:
        storage = LocalStorage(tmp_path, clock=clock)
        created = await storage.projects.create(_project(clock))
        assert created.etag == "1"
        updated_project = replace(
            created.value,
            description="Updated",
            updated_at=clock(),
        )
        updated = await storage.projects.replace(updated_project, expected_etag=created.etag)
        assert updated.etag == "2"
        with pytest.raises(ConcurrencyError):
            await storage.projects.replace(updated_project, expected_etag=created.etag)

        restarted = LocalStorage(tmp_path, clock=clock)
        loaded = await restarted.projects.get("public", "cord-v2")
        assert loaded.etag == "2"
        assert loaded.value.description == "Updated"
        run = await restarted.runs.create(_run(clock))
        assert (await restarted.runs.find(run.value.run_id)).value == run.value

    asyncio.run(scenario())


def test_run_history_is_project_scoped_newest_first_limited_and_durable(
    tmp_path: Path,
) -> None:
    clock = MutableClock()

    async def scenario() -> None:
        storage = LocalStorage(tmp_path, clock=clock)
        for project_id, run_id in (
            ("cord-v2", "run-old"),
            ("other-project", "run-foreign"),
            ("cord-v2", "run-middle"),
            ("cord-v2", "run-new"),
        ):
            run = replace(
                _run(clock),
                project_id=project_id,
                run_id=run_id,
                manifest_artifact_path=run_manifest(run_id),
            )
            await storage.runs.create(run)
            clock.advance(1)

        restarted = LocalStorage(tmp_path, clock=clock)
        listed = await restarted.runs.list("cord-v2", limit=2)
        assert [item.value.run_id for item in listed] == ["run-new", "run-middle"]
        assert [
            item.value.run_id for item in await restarted.runs.list_all("cord-v2")
        ] == ["run-new", "run-middle", "run-old"]
        assert all(item.value.project_id == "cord-v2" for item in listed)
        assert await restarted.runs.list("missing-project") == []

        for invalid_limit in (0, 101, True):
            with pytest.raises(ValueError, match="between 1 and 100"):
                await restarted.runs.list("cord-v2", limit=invalid_limit)

    asyncio.run(scenario())


def test_terminal_accounting_and_finalizer_claim_are_exactly_once(tmp_path: Path) -> None:
    clock = MutableClock()

    async def scenario() -> None:
        storage = LocalStorage(tmp_path, clock=clock)
        await storage.runs.create(_run(clock))
        task = _task(clock)
        await storage.tasks.create(task)
        claims = await asyncio.gather(
            storage.runs.record_terminal(
                task.project_id,
                task.run_id,
                task.pair_id,
                "succeeded",
            ),
            storage.runs.record_terminal(
                task.project_id,
                task.run_id,
                task.pair_id,
                "succeeded",
            ),
        )
        assert sum(result.applied for result in claims) == 1
        current = await storage.runs.get("cord-v2", "run-001")
        assert current.value.pending_tasks == 0
        assert current.value.succeeded_tasks == 1

        lease_until = clock() + timedelta(seconds=60)
        first, second = await asyncio.gather(
            storage.runs.claim_finalizer(
                "cord-v2",
                "run-001",
                owner="finalizer-one",
                lease_until=lease_until,
            ),
            storage.runs.claim_finalizer(
                "cord-v2",
                "run-001",
                owner="finalizer-two",
                lease_until=lease_until,
            ),
        )
        assert sum(item is not None for item in (first, second)) == 1

    asyncio.run(scenario())


def test_terminal_accounting_recovers_marker_written_before_run_update(
    tmp_path: Path,
) -> None:
    clock = MutableClock()

    async def scenario() -> None:
        storage = LocalStorage(tmp_path, clock=clock)
        run = _run(clock)
        task = _task(clock)
        await storage.runs.create(run)
        marker = storage.runs._terminal_path(
            task.project_id,
            task.run_id,
            task.pair_id,
        )
        marker.parent.mkdir(parents=True)
        marker.write_bytes(
            strict_json_bytes(
                {
                    "pair_id": task.pair_id,
                    "outcome": "succeeded",
                    "poisoned": False,
                    "recorded_at": datetime_text(clock()),
                },
                newline=True,
            )
        )

        recovered = await storage.runs.record_terminal(
            task.project_id,
            task.run_id,
            task.pair_id,
            "succeeded",
        )

        assert not recovered.applied
        assert recovered.run.value.pending_tasks == 0
        assert recovered.run.value.succeeded_tasks == 1

    asyncio.run(scenario())


def test_queue_visibility_renewal_retry_poison_and_restart(tmp_path: Path) -> None:
    clock = MutableClock()

    async def scenario() -> None:
        storage = LocalStorage(tmp_path, clock=clock)
        message = TaskMessage(
            project_id="cord-v2",
            run_id="run-001",
            sample_id="sample-001",
            solution_id="gpt-5.4-mini",
        )
        message_id = await storage.queue.send(message)
        lease = (await storage.queue.receive(max_messages=1, visibility_timeout=10))[0]
        assert lease.message_id == message_id
        assert lease.dequeue_count == 1
        assert await storage.queue.receive(max_messages=1, visibility_timeout=10) == []

        renewed = await storage.queue.renew(lease, visibility_timeout=20)
        with pytest.raises(LeaseLostError):
            await storage.queue.complete(lease)
        clock.advance(11)
        assert await storage.queue.receive(max_messages=1, visibility_timeout=10) == []
        await storage.queue.release(renewed, delay=0)

        restarted = LocalStorage(tmp_path, clock=clock)
        redelivery = (await restarted.queue.receive(max_messages=1, visibility_timeout=10))[0]
        assert redelivery.dequeue_count == 2
        await restarted.queue.poison(redelivery, reason_code="retry_exhausted")
        poison = await restarted.queue.poison_messages()
        assert poison[0]["reason_code"] == "retry_exhausted"
        assert await restarted.queue.receive(max_messages=1, visibility_timeout=10) == []

    asyncio.run(scenario())


def test_malformed_local_queue_item_is_deleted_without_blocking_batch(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        malformed = storage.queue.root / "messages" / "malformed.json"
        malformed.parent.mkdir(parents=True)
        malformed.write_bytes(b"{not-json")
        valid = TaskMessage(
            project_id="cord-v2",
            run_id="run-001",
            sample_id="sample-001",
            solution_id="solution-one",
        )
        await storage.queue.send(valid)

        leases = await storage.queue.receive(max_messages=2, visibility_timeout=10)

        assert [lease.message for lease in leases] == [valid]
        assert not malformed.exists()

    asyncio.run(scenario())


def test_expired_queue_lease_is_redelivered_with_incremented_count(tmp_path: Path) -> None:
    clock = MutableClock()

    async def scenario() -> None:
        storage = LocalStorage(tmp_path, clock=clock)
        await storage.queue.send(
            TaskMessage(
                project_id="cord-v2",
                run_id="run-001",
                sample_id="sample-001",
                solution_id="solution-one",
            )
        )
        first = (await storage.queue.receive(max_messages=1, visibility_timeout=5))[0]
        clock.advance(6)
        second = (await storage.queue.receive(max_messages=1, visibility_timeout=5))[0]
        assert second.dequeue_count == 2
        with pytest.raises(LeaseLostError):
            await storage.queue.complete(first)
        await storage.queue.complete(second)

    asyncio.run(scenario())


def test_events_are_durable_strictly_ordered_and_pollable(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        await asyncio.gather(
            *(
                storage.events.append("run-001", "task_completed", {"index": index})
                for index in range(20)
            )
        )
        restarted = LocalStorage(tmp_path)
        assert await restarted.events.latest_sequence("run-001") == 20
        events = await restarted.events.read("run-001", after_sequence=5, limit=100)
        assert [event.sequence for event in events] == list(range(6, 21))
        assert len({event.sequence for event in events}) == len(events)

    asyncio.run(scenario())


def test_event_idempotency_survives_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        first = LocalStorage(tmp_path)
        created = await first.events.append(
            "run-001",
            "task_completed",
            {"pair": "sample-001|solution-one"},
            idempotency_key="task-terminal:sample-001|solution-one",
        )
        restarted = LocalStorage(tmp_path)
        duplicate = await restarted.events.append(
            "run-001",
            "task_completed",
            {"pair": "sample-001|solution-one"},
            idempotency_key="task-terminal:sample-001|solution-one",
        )
        assert duplicate.sequence == created.sequence
        assert len(await restarted.events.read("run-001")) == 1

    asyncio.run(scenario())


def test_failed_local_event_encoding_does_not_advance_committed_watermark(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        storage = LocalStorage(tmp_path)
        oversized: JsonObject = {"payload": "x" * (MAX_EVENT_BYTES - 100)}
        assert len(strict_json_bytes(oversized)) < MAX_EVENT_BYTES

        with pytest.raises(StrictJsonError):
            await storage.events.append("run-001", "task_completed", oversized)

        created = await storage.events.append(
            "run-001",
            "task_completed",
            {"ok": True},
        )
        assert created.sequence == 1
        assert [event.sequence for event in await storage.events.read("run-001")] == [1]

    asyncio.run(scenario())


def test_local_event_pending_record_recovers_failed_publication_without_gap(
    tmp_path: Path,
) -> None:
    class FailFirstEventWrite(LocalArtifactRepository):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.failed = False

        def _create_bytes(
            self,
            path: str,
            data: bytes,
            *,
            content_type: str,
            metadata: JsonObject | None,
        ) -> ArtifactInfo:
            if "/events/" in path and not self.failed:
                self.failed = True
                raise RuntimeError("injected event publication failure")
            return super()._create_bytes(
                path,
                data,
                content_type=content_type,
                metadata=metadata,
            )

    async def scenario() -> None:
        artifacts = FailFirstEventWrite(tmp_path / "artifacts")
        events = LocalEventRepository(artifacts)
        with pytest.raises(RuntimeError, match="injected"):
            await events.append("run-001", "task_started", {"index": 1})
        assert await events.read("run-001") == []

        second = await events.append("run-001", "task_completed", {"index": 2})

        assert second.sequence == 2
        records = await events.read("run-001")
        assert [record.sequence for record in records] == [1, 2]
        assert [record.data for record in records] == [{"index": 1}, {"index": 2}]

    asyncio.run(scenario())


def test_local_artifact_rejects_content_type_before_path_mutation(tmp_path: Path) -> None:
    async def scenario() -> None:
        artifacts = LocalStorage(tmp_path).artifacts
        path = "runs/run-001/result.bin"
        for invalid in (
            "",
            "not-a-media-type",
            "text/plain\r\nx: injected",
            "a/" + "b" * 256,
            "text/\ud800",
        ):
            with pytest.raises(ValueError):
                await artifacts.create_bytes(path, b"invalid", content_type=invalid)
            assert not await artifacts.exists(path)

        created = await artifacts.create_bytes(
            path,
            b"valid",
            content_type="application/octet-stream",
        )
        assert created.content_type == "application/octet-stream"
        assert await artifacts.get_bytes(path) == b"valid"

    asyncio.run(scenario())


def test_task_and_finalizer_operations_reject_expired_owners(tmp_path: Path) -> None:
    clock = MutableClock()

    async def scenario() -> None:
        storage = LocalStorage(tmp_path, clock=clock)
        await storage.runs.create(_run(clock))
        task = _task(clock)
        await storage.tasks.create(task)
        claim = await storage.tasks.claim(
            task.run_id,
            task.pair_id,
            owner="worker-one",
            lease_until=clock() + timedelta(seconds=5),
        )
        assert claim.acquired
        clock.advance(6)
        with pytest.raises(LeaseLostError):
            await storage.tasks.release_claim(
                task.run_id,
                task.pair_id,
                owner="worker-one",
            )

        finalizer = await storage.runs.claim_finalizer(
            task.project_id,
            task.run_id,
            owner="finalizer-one",
            lease_until=clock() + timedelta(seconds=5),
        )
        assert finalizer is None

        raw = result_raw(task.run_id, task.sample_id, task.solution_id)
        parsed = raw.replace("raw.json", "parsed.json")
        telemetry = raw.replace("raw.json", "telemetry.json")
        replacement = await storage.tasks.claim(
            task.run_id,
            task.pair_id,
            owner="worker-two",
            lease_until=clock() + timedelta(seconds=5),
        )
        assert replacement.acquired
        await storage.tasks.finish(
            task.run_id,
            task.pair_id,
            owner="worker-two",
            outcome="succeeded",
            raw_artifact_path=raw,
            parsed_artifact_path=parsed,
            telemetry_artifact_path=telemetry,
        )
        await storage.runs.record_terminal(
            task.project_id,
            task.run_id,
            task.pair_id,
            "succeeded",
        )
        finalizer = await storage.runs.claim_finalizer(
            task.project_id,
            task.run_id,
            owner="finalizer-one",
            lease_until=clock() + timedelta(seconds=5),
        )
        assert finalizer is not None
        clock.advance(6)
        with pytest.raises(LeaseLostError):
            await storage.runs.complete_finalizer(
                task.project_id,
                task.run_id,
                owner="finalizer-one",
                aggregate_artifact_path="runs/run-001/aggregate.json",
                download_artifact_path="runs/run-001/download.zip",
            )

    asyncio.run(scenario())


def test_seed_coordination_uses_expiring_lease_and_durable_marker(tmp_path: Path) -> None:
    clock = MutableClock()

    async def scenario() -> None:
        storage = LocalStorage(tmp_path, clock=clock)
        first = await storage.seeds.acquire(
            "builtins-v1",
            owner="instance-one",
            lease_seconds=30,
        )
        assert first is not None
        assert (
            await storage.seeds.acquire(
                "builtins-v1",
                owner="instance-two",
                lease_seconds=30,
            )
            is None
        )
        clock.advance(31)
        second = await storage.seeds.acquire(
            "builtins-v1",
            owner="instance-two",
            lease_seconds=30,
        )
        assert second is not None
        with pytest.raises(LeaseLostError):
            await storage.seeds.complete(first, {"revision": "old"})
        await storage.seeds.complete(second, {"revision": "sha256-abc"})
        restarted = LocalStorage(tmp_path, clock=clock)
        assert await restarted.seeds.is_complete("builtins-v1")
        assert (
            await restarted.seeds.acquire(
                "builtins-v1",
                owner="instance-three",
                lease_seconds=30,
            )
            is None
        )

    asyncio.run(scenario())
