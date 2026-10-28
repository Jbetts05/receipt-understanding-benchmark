from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from receipt_bench.storage.azure import (
    AzureArtifactRepository,
    AzureBlobSeedCoordinator,
    AzureEventRepository,
    AzureProjectRepository,
    AzureRunRepository,
    AzureTaskQueue,
    AzureTaskRepository,
)
from receipt_bench.storage.models import (
    ArtifactInfo,
    ProjectRecord,
    QueueLease,
    RunRecord,
    SeedLease,
    TaskMessage,
    TaskRecord,
)
from receipt_bench.storage.paths import project_prompt, project_schema, run_manifest, sample_image
from receipt_bench.storage.protocols import (
    ConcurrencyError,
    ImmutableArtifactError,
    LeaseLostError,
    NotFoundError,
)
from receipt_bench.storage.validation import JsonObject, StrictJsonError


class FakeAzureError(RuntimeError):
    def __init__(self, status_code: int, error_code: str | None = None) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code
        self.error_code = error_code


class FakeDownload:
    def __init__(self, data: bytes, etag: str) -> None:
        self._data = data
        self.properties = SimpleNamespace(etag=etag)

    async def readall(self) -> bytes:
        return self._data


class FakeBlob:
    def __init__(self, container: FakeContainer, name: str) -> None:
        self._container = container
        self._name = name

    async def upload_blob(
        self,
        data: bytes,
        *,
        overwrite: bool = False,
        metadata: dict[str, str] | None = None,
        content_settings: object | None = None,
        etag: str | None = None,
        match_condition: object | None = None,
        lease: object | None = None,
    ) -> None:
        del content_settings, match_condition
        active_lease = self._container.leases.get(self._name)
        if active_lease is not None and active_lease.active and lease is not active_lease:
            raise FakeAzureError(412)
        if lease is not None and (not isinstance(lease, FakeLease) or not lease.active):
            raise FakeAzureError(412)
        current_version = self._container.versions.get(self._name)
        if etag is not None and str(current_version) != etag:
            raise FakeAzureError(412)
        if self._name in self._container.values and not overwrite:
            raise FakeAzureError(409, "BlobAlreadyExists")
        self._container.values[self._name] = bytes(data)
        self._container.metadata[self._name] = dict(metadata or {})
        self._container.versions[self._name] = (current_version or 0) + 1

    async def download_blob(self) -> FakeDownload:
        if self._name not in self._container.values:
            raise FakeAzureError(404)
        data = self._container.values[self._name]
        etag = str(self._container.versions[self._name])
        replacement = self._container.mutate_after_download.pop(self._name, None)
        if replacement is not None:
            self._container.values[self._name] = replacement
            self._container.versions[self._name] += 1
        return FakeDownload(data, etag)

    async def get_blob_properties(self) -> SimpleNamespace:
        if self._name not in self._container.values:
            raise FakeAzureError(404)
        return SimpleNamespace(
            etag=str(self._container.versions[self._name]),
            metadata=self._container.metadata[self._name],
            content_settings=SimpleNamespace(
                content_type=self._container.metadata[self._name].get(
                    "content_type",
                    "application/octet-stream",
                )
            ),
        )

    async def exists(self) -> bool:
        return self._name in self._container.values

    async def acquire_lease(self, *, lease_duration: int) -> FakeLease:
        current = self._container.leases.get(self._name)
        if current is not None and current.active:
            raise FakeAzureError(412)
        lease = FakeLease(self._container, self._name, lease_duration)
        self._container.leases[self._name] = lease
        if self._container.after_acquire is not None:
            self._container.after_acquire()
        return lease

    async def delete_blob(self, *, delete_snapshots: str) -> None:
        assert delete_snapshots == "include"
        if self._name not in self._container.values:
            raise FakeAzureError(404)
        del self._container.values[self._name]
        self._container.metadata.pop(self._name, None)
        self._container.versions.pop(self._name, None)
        self._container.leases.pop(self._name, None)


class FakeLease:
    def __init__(self, container: FakeContainer, name: str, duration: int) -> None:
        self._container = container
        self._name = name
        self.duration = duration
        self.id = f"lease-{container.next_lease}"
        container.next_lease += 1
        self.active = True

    async def renew(self) -> None:
        if not self.active or self._container.leases.get(self._name) is not self:
            raise FakeAzureError(412)
        if self._container.after_renew is not None:
            self._container.after_renew()

    async def release(self) -> None:
        if not self.active or self._container.leases.get(self._name) is not self:
            raise FakeAzureError(412)
        self.active = False


class FakeContainer:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.versions: dict[str, int] = {}
        self.leases: dict[str, FakeLease] = {}
        self.next_lease = 1
        self.list_calls = 0
        self.mutate_after_download: dict[str, bytes] = {}
        self.after_acquire: Callable[[], None] | None = None
        self.after_renew: Callable[[], None] | None = None

    def get_blob_client(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)

    async def list_blobs(self, *, name_starts_with: str) -> AsyncIterator[SimpleNamespace]:
        self.list_calls += 1
        for name in sorted(self.values):
            if name.startswith(name_starts_with):
                yield SimpleNamespace(name=name)


class FakeEntity(dict[str, object]):
    def __init__(self, value: dict[str, object], etag: str) -> None:
        super().__init__(value)
        self.metadata = {"etag": etag}


class FakeTable:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], dict[str, object]] = {}
        self.versions: dict[tuple[str, str], int] = {}
        self.query_calls: list[tuple[str, dict[str, object]]] = []
        self.transaction_calls = 0

    async def create_entity(self, *, entity: dict[str, object]) -> None:
        key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
        if key in self.values:
            raise FakeAzureError(409, "EntityAlreadyExists")
        self.values[key] = dict(entity)
        self.versions[key] = 1

    async def get_entity(self, *, partition_key: str, row_key: str) -> FakeEntity:
        key = (partition_key, row_key)
        if key not in self.values:
            raise FakeAzureError(404)
        return FakeEntity(dict(self.values[key]), str(self.versions[key]))

    async def update_entity(
        self,
        *,
        entity: dict[str, object],
        etag: str,
        **_: object,
    ) -> None:
        key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
        if key not in self.values:
            raise FakeAzureError(404)
        if str(self.versions[key]) != etag:
            raise FakeAzureError(412)
        self.values[key] = dict(entity)
        self.versions[key] += 1

    async def delete_entity(self, *, partition_key: str, row_key: str) -> None:
        key = (partition_key, row_key)
        if key not in self.values:
            raise FakeAzureError(404)
        del self.values[key]
        del self.versions[key]

    async def submit_transaction(
        self,
        operations: list[tuple[object, ...]],
    ) -> None:
        self.transaction_calls += 1
        values_before = {key: dict(value) for key, value in self.values.items()}
        versions_before = dict(self.versions)
        try:
            for operation in operations:
                action = str(operation[0])
                entity = dict(operation[1])  # type: ignore[arg-type]
                options = (
                    dict(operation[2]) if len(operation) > 2 else {}
                )
                if action == "create":
                    await self.create_entity(entity=entity)
                elif action == "update":
                    await self.update_entity(
                        entity=entity,
                        etag=str(options["etag"]),
                    )
                else:
                    raise AssertionError(f"Unsupported transaction action: {action}")
        except BaseException:
            self.values = values_before
            self.versions = versions_before
            raise

    async def query_entities(
        self,
        *,
        query_filter: str,
        parameters: dict[str, object],
    ) -> AsyncIterator[FakeEntity]:
        self.query_calls.append((query_filter, dict(parameters)))
        for key, value in sorted(self.values.items()):
            if "partition" in parameters and key[0] != parameters["partition"]:
                continue
            if "kind" in parameters and value.get("Kind") != parameters["kind"]:
                continue
            if "run" in parameters and value.get("RunId") != parameters["run"]:
                continue
            if (
                "payload_ref" in parameters
                and value.get("PayloadRef") != parameters["payload_ref"]
            ):
                continue
            if (
                "completed" in parameters
                and value.get("FinalizerCompleted") != parameters["completed"]
            ):
                continue
            if (
                "recovery" in parameters
                and value.get("RecoveryPending") != parameters["recovery"]
            ):
                continue
            yield FakeEntity(dict(value), str(self.versions[key]))


class FakeQueue:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.received: list[SimpleNamespace] = []
        self.updated: list[dict[str, object]] = []
        self.deleted: list[dict[str, str]] = []
        self.ttls: list[int] = []

    async def send_message(self, content: str, *, time_to_live: int) -> SimpleNamespace:
        self.ttls.append(time_to_live)
        self.sent.append(content)
        return SimpleNamespace(id=f"message-{len(self.sent)}")

    async def receive_messages(
        self,
        *,
        messages_per_page: int,
        visibility_timeout: int,
    ) -> AsyncIterator[SimpleNamespace]:
        del messages_per_page, visibility_timeout
        for message in self.received:
            yield message
        self.received.clear()

    async def update_message(
        self,
        message: str,
        pop_receipt: str,
        *,
        content: str,
        visibility_timeout: int,
    ) -> SimpleNamespace:
        self.updated.append(
            {
                "message": message,
                "pop_receipt": pop_receipt,
                "content": content,
                "visibility_timeout": visibility_timeout,
            }
        )
        return SimpleNamespace(
            pop_receipt="renewed-receipt",
            next_visible_on=datetime(2026, 8, 30, 0, 1, tzinfo=UTC),
        )

    async def delete_message(self, message: str, pop_receipt: str) -> None:
        self.deleted.append({"message": message, "pop_receipt": pop_receipt})


def _project(now: datetime) -> ProjectRecord:
    return ProjectRecord(
        owner_scope="public",
        project_id="cord-v2",
        name="CORD v2",
        description="Fixed",
        schema_artifact_path=project_schema("cord-v2"),
        prompt_artifact_path=project_prompt("cord-v2", "1.0.0"),
        prompt_version="1.0.0",
        created_at=now,
        updated_at=now,
    )


def _run(now: datetime) -> RunRecord:
    return RunRecord(
        project_id="cord-v2",
        run_id="run-001",
        manifest_artifact_path=run_manifest("run-001"),
        total_tasks=1,
        pending_tasks=1,
        created_at=now,
        updated_at=now,
        metadata={"dispatch_complete": True},
    )


def _task(now: datetime) -> TaskRecord:
    return TaskRecord(
        project_id="cord-v2",
        run_id="run-001",
        sample_id="sample-001",
        solution_id="solution-one",
        image_artifact_path=sample_image("cord-v2", "sample-001"),
        media_type="image/jpeg",
        created_at=now,
        updated_at=now,
    )


def test_blob_and_event_clients_are_fully_mocked_and_conditional() -> None:
    async def scenario() -> None:
        container = FakeContainer()
        artifacts = AzureArtifactRepository(container)
        created = await artifacts.create_json("runs/run-001/manifest.json", {"ok": True})
        assert created.sha256
        assert await artifacts.get_json("runs/run-001/manifest.json") == {"ok": True}
        await artifacts.create_json("runs/run-001/manifest.json", {"ok": True})
        with pytest.raises(ImmutableArtifactError):
            await artifacts.create_json("runs/run-001/manifest.json", {"ok": False})

        events = AzureEventRepository(artifacts, container)
        first, second = await asyncio.gather(
            events.append("run-001", "task_started", {"pair": 1}),
            events.append("run-001", "task_completed", {"pair": 1}),
        )
        assert {first.sequence, second.sequence} == {1, 2}
        assert container.list_calls == 0
        assert await events.latest_sequence("run-001") == 2
        assert [event.sequence for event in await events.read("run-001")] == [1, 2]

    asyncio.run(scenario())


def test_azure_deletion_removes_only_requested_run_state_and_indexes() -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 30, tzinfo=UTC)
        container = FakeContainer()
        runs_table = FakeTable()
        tasks_table = FakeTable()
        artifacts = AzureArtifactRepository(container)
        runs = AzureRunRepository(runs_table, artifacts=artifacts)
        tasks = AzureTaskRepository(tasks_table, artifacts=artifacts)

        initial_run = _run(now)
        initial_task = _task(now)
        initial_other_run = replace(
            initial_run,
            run_id="run-002",
            manifest_artifact_path=run_manifest("run-002"),
        )
        initial_other_task = replace(initial_task, run_id="run-002")
        await runs.create(initial_run)
        await runs.create(initial_other_run)
        terminal = await runs.record_terminal(
            "cord-v2", "run-001", initial_task.pair_id, "succeeded"
        )
        await runs.replace(
            replace(
                terminal.run.value,
                status="completed",
                finalizer_completed=True,
                completion_event_emitted=True,
            ),
            expected_etag=terminal.run.etag,
        )
        await tasks.create(initial_task)
        task = await tasks.get("run-001", initial_task.pair_id)
        await tasks.replace(
            replace(task.value, status="succeeded", terminal_event_emitted=True),
            expected_etag=task.etag,
        )
        await tasks._ensure_recovery_index(initial_task)
        await runs._ensure_recovery_index(initial_run)
        await tasks.create(initial_other_task)
        await artifacts.create_json("runs/run-001/results/result.json", {"result": 1})
        await artifacts.create_json("runs/run-002/results/result.json", {"result": 2})

        await runs._delete_entity("runlookup", "run-001", kind="RunLookup")
        assert (await runs.find("run-001")).value.project_id == "cord-v2"
        await tasks.delete_for_run("run-001")
        await artifacts.delete_prefix("runs/run-001/")
        await runs.delete_auxiliary("cord-v2", "run-001")
        await runs.delete("cord-v2", "run-001")
        await tasks.delete_for_run("run-001")
        await artifacts.delete_prefix("runs/run-001/")
        await runs.delete_auxiliary("cord-v2", "run-001")
        await runs.delete("cord-v2", "run-001")

        assert not any(key[0] == "run-001" for key in tasks_table.values)
        assert not any(
            value.get("RunId") == "run-001"
            or (key[0] in {"recovery", "runlookup"} and key[1] == "run-001")
            for key, value in runs_table.values.items()
        )
        assert all(not name.startswith("runs/run-001/") for name in container.values)
        assert await runs.get("cord-v2", "run-002")
        assert await tasks.get("run-002", initial_other_task.pair_id)
        assert await artifacts.exists("runs/run-002/results/result.json")

        with pytest.raises(ValueError):
            await artifacts.delete_prefix("runs/")
        with pytest.raises(ValueError):
            await artifacts.delete_prefix("state/table-payloads/")

    asyncio.run(scenario())


def test_azure_artifact_rejects_content_type_before_blob_mutation() -> None:
    async def scenario() -> None:
        container = FakeContainer()
        artifacts = AzureArtifactRepository(container)
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
            assert path not in container.values

        created = await artifacts.create_bytes(
            path,
            b"valid",
            content_type="application/octet-stream",
        )
        assert created.content_type == "application/octet-stream"
        assert container.values[path] == b"valid"

    asyncio.run(scenario())


def test_table_repositories_use_etags_and_terminal_receipts_without_network() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    async def scenario() -> None:
        projects_table = FakeTable()
        runs_table = FakeTable()
        tasks_table = FakeTable()
        projects = AzureProjectRepository(projects_table)
        runs = AzureRunRepository(runs_table, clock=lambda: now)
        tasks = AzureTaskRepository(tasks_table, clock=lambda: now)

        project = await projects.create(_project(now))
        changed = replace(project.value, description="Changed", updated_at=now)
        project = await projects.replace(changed, expected_etag=project.etag)
        assert project.value.description == "Changed"

        await runs.create(_run(now))
        assert (await runs.find("run-001")).value.run_id == "run-001"
        task = await tasks.create(_task(now))
        claim = await tasks.claim(
            task.value.run_id,
            task.value.pair_id,
            owner="worker-one",
            lease_until=now + timedelta(seconds=30),
            dequeue_count=1,
        )
        assert claim.acquired
        completion = await tasks.finish(
            task.value.run_id,
            task.value.pair_id,
            owner="worker-one",
            outcome="succeeded",
            raw_artifact_path="runs/run-001/results/sample-001/solution-one/raw.json",
            parsed_artifact_path="runs/run-001/results/sample-001/solution-one/parsed.json",
            telemetry_artifact_path=("runs/run-001/results/sample-001/solution-one/telemetry.json"),
        )
        assert completion.applied
        first, duplicate = await asyncio.gather(
            runs.record_terminal("cord-v2", "run-001", task.value.pair_id, "succeeded"),
            runs.record_terminal("cord-v2", "run-001", task.value.pair_id, "succeeded"),
        )
        assert sum(item.applied for item in (first, duplicate)) == 1
        assert (await runs.get("cord-v2", "run-001")).value.pending_tasks == 0
        assert runs_table.transaction_calls == 1

    asyncio.run(scenario())


def test_azure_run_history_uses_project_partition_order_and_safe_limit() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    async def scenario() -> None:
        table = FakeTable()
        runs = AzureRunRepository(table, clock=lambda: now)
        for project_id, run_id, created_at in (
            ("cord-v2", "run-old", now),
            ("other-project", "run-foreign", now + timedelta(minutes=3)),
            ("cord-v2", "run-middle", now + timedelta(minutes=1)),
            ("cord-v2", "run-new", now + timedelta(minutes=2)),
        ):
            run = replace(
                _run(created_at),
                project_id=project_id,
                run_id=run_id,
                manifest_artifact_path=run_manifest(run_id),
            )
            await runs.create(run)

        listed = await runs.list("cord-v2", limit=2)
        assert [item.value.run_id for item in listed] == ["run-new", "run-middle"]
        assert [
            item.value.run_id for item in await runs.list_all("cord-v2")
        ] == ["run-new", "run-middle", "run-old"]
        assert all(item.value.project_id == "cord-v2" for item in listed)
        assert table.query_calls[-1] == (
            "PartitionKey eq @partition and Kind eq @kind",
            {"partition": "cord-v2", "kind": "Run"},
        )

        for invalid_limit in (0, 101, True):
            with pytest.raises(ValueError, match="between 1 and 100"):
                await runs.list("cord-v2", limit=invalid_limit)

    asyncio.run(scenario())


def test_azure_recovery_queries_only_records_marked_pending() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    async def scenario() -> None:
        run_table = FakeTable()
        task_table = FakeTable()
        runs = AzureRunRepository(run_table, clock=lambda: now)
        tasks = AzureTaskRepository(task_table, clock=lambda: now)
        active_run = _run(now)
        completed_run = replace(
            active_run,
            run_id="run-completed",
            manifest_artifact_path=run_manifest("run-completed"),
            pending_tasks=0,
            succeeded_tasks=1,
            status="completed",
            finalizer_completed=True,
            completion_event_emitted=True,
        )
        await runs.create(active_run)
        await runs.create(completed_run)
        active_task = _task(now)
        completed_task = replace(
            active_task,
            run_id="run-completed",
            status="succeeded",
            stage="completed",
            terminal_event_emitted=True,
        )
        await tasks.create(active_task)
        await tasks.create(completed_task)

        assert [item.value.run_id for item in await runs.list_unfinished()] == [
            active_run.run_id
        ]
        assert [item.value.run_id for item in await tasks.list_resumable()] == [
            active_task.run_id
        ]
        assert run_table.query_calls[-1] == (
            "PartitionKey eq @partition and Kind eq @kind",
            {"partition": "recovery", "kind": "RunRecovery"},
        )
        assert task_table.query_calls[-1] == (
            "PartitionKey eq @partition and Kind eq @kind",
            {"partition": "recovery", "kind": "TaskRecovery"},
        )

    asyncio.run(scenario())


def test_task_recovery_marker_accepts_generic_azure_resource_exists_error() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    class ResourceExistsError(RuntimeError):
        status_code = 409

    class GenericExistsTable(FakeTable):
        async def create_entity(self, *, entity: dict[str, object]) -> None:
            key = (str(entity["PartitionKey"]), str(entity["RowKey"]))
            if key in self.values:
                raise ResourceExistsError("Operation returned Conflict")
            await super().create_entity(entity=entity)

    async def scenario() -> None:
        table = GenericExistsTable()
        tasks = AzureTaskRepository(table, clock=lambda: now)
        task = _task(now)

        stored = await tasks.create(task)

        assert stored.value == task
        assert ("recovery", tasks._recovery_row_key(task.run_id, task.pair_id)) in table.values

    asyncio.run(scenario())


def test_confirmed_dispatch_id_fences_azure_task_claims() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    async def scenario() -> None:
        tasks = AzureTaskRepository(FakeTable(), clock=lambda: now)
        task = replace(
            _task(now),
            dispatch_token="dlogical",
            dispatch_message_id="message-one",
            dispatch_updated_at=now,
        )
        await tasks.create(task)
        stale = await tasks.claim(
            task.run_id,
            task.pair_id,
            owner="worker-stale",
            lease_until=now + timedelta(seconds=30),
            dispatch_token="dlogical",
            message_id="message-two",
        )
        assert not stale.acquired
        assert stale.task.value.attempts == 0

        current = await tasks.claim(
            task.run_id,
            task.pair_id,
            owner="worker-current",
            lease_until=now + timedelta(seconds=30),
            dispatch_token="dlogical",
            message_id="message-one",
        )
        assert current.acquired
        assert current.task.value.attempts == 1
        assert current.task.value.dispatch_message_id == "message-one"

    asyncio.run(scenario())


def test_queue_client_uses_visibility_receipts_and_separate_poison_queue() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    async def scenario() -> None:
        queue_client = FakeQueue()
        poison_client = FakeQueue()
        queue = AzureTaskQueue(queue_client, poison_client, clock=lambda: now)
        message = TaskMessage(
            project_id="cord-v2",
            run_id="run-001",
            sample_id="sample-001",
            solution_id="solution-one",
        )
        await queue.send(message)
        assert queue_client.ttls == [-1]
        queue_client.received.append(
            SimpleNamespace(
                id="message-1",
                pop_receipt="receipt-1",
                content=queue_client.sent[0],
                dequeue_count=3,
                next_visible_on=datetime(2026, 8, 30, 0, 1, tzinfo=UTC),
            )
        )
        lease = (await queue.receive(max_messages=1, visibility_timeout=30))[0]
        renewed = await queue.renew(lease, visibility_timeout=30)
        assert renewed.pop_receipt == "renewed-receipt"
        assert queue_client.updated[0]["message"] == "message-1"
        await queue.poison(renewed, reason_code="retry_exhausted")
        assert len(poison_client.sent) == 1
        assert poison_client.ttls == [-1]
        assert queue_client.deleted == [{"message": "message-1", "pop_receipt": "renewed-receipt"}]

    asyncio.run(scenario())


def test_malformed_azure_queue_item_is_deleted_without_blocking_batch() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    async def scenario() -> None:
        queue_client = FakeQueue()
        queue = AzureTaskQueue(queue_client, FakeQueue(), clock=lambda: now)
        valid = TaskMessage(
            project_id="cord-v2",
            run_id="run-001",
            sample_id="sample-001",
            solution_id="solution-one",
        )
        await queue.send(valid)
        queue_client.received.extend(
            [
                SimpleNamespace(
                    id="malformed-message",
                    pop_receipt="malformed-receipt",
                    content="{not-json",
                    dequeue_count=1,
                    next_visible_on=now + timedelta(seconds=30),
                ),
                SimpleNamespace(
                    id="valid-message",
                    pop_receipt="valid-receipt",
                    content=queue_client.sent[0],
                    dequeue_count=1,
                    next_visible_on=now + timedelta(seconds=30),
                ),
            ]
        )

        leases = await queue.receive(max_messages=2, visibility_timeout=30)

        assert [lease.message for lease in leases] == [valid]
        assert queue_client.deleted == [
            {
                "message": "malformed-message",
                "pop_receipt": "malformed-receipt",
            }
        ]

    asyncio.run(scenario())


def test_event_idempotency_uses_durable_marker_and_constant_time_counter() -> None:
    async def scenario() -> None:
        container = FakeContainer()
        artifacts = AzureArtifactRepository(container)
        events = AzureEventRepository(artifacts, container)
        first, duplicate = await asyncio.gather(
            events.append(
                "run-001",
                "task_completed",
                {"pair": "sample-001|solution-one"},
                idempotency_key="task-terminal:sample-001|solution-one",
            ),
            events.append(
                "run-001",
                "task_completed",
                {"pair": "sample-001|solution-one"},
                idempotency_key="task-terminal:sample-001|solution-one",
            ),
        )
        assert first.sequence == duplicate.sequence == 1
        assert container.list_calls == 0
        records = await events.read("run-001")
        assert [record.sequence for record in records] == [1]

    asyncio.run(scenario())


def test_event_counter_pairs_content_with_etag_from_the_same_download() -> None:
    async def scenario() -> None:
        container = FakeContainer()
        artifacts = AzureArtifactRepository(container)
        events = AzureEventRepository(artifacts, container)
        first = await events.append("run-001", "task_started", {"pair": 1})
        assert first.sequence == 1

        counter_path = "runs/run-001/event-counter.json"
        container.mutate_after_download[counter_path] = b'{"sequence":2}\n'
        second = await events.append("run-001", "task_completed", {"pair": 1})

        assert second.sequence == 3
        assert container.values[counter_path] == b'{"sequence":3}\n'

    asyncio.run(scenario())


def test_event_reader_waits_for_delayed_lower_sequence() -> None:
    class DelayedFirstEventArtifacts(AzureArtifactRepository):
        def __init__(self, container: FakeContainer) -> None:
            super().__init__(container)
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.delayed = False

        async def create_bytes(
            self,
            path: str,
            data: bytes,
            *,
            content_type: str = "application/octet-stream",
            metadata: JsonObject | None = None,
        ) -> ArtifactInfo:
            if path.endswith("/events/00000000000000000001.json") and not self.delayed:
                self.delayed = True
                self.started.set()
                await self.release.wait()
            return await super().create_bytes(
                path,
                data,
                content_type=content_type,
                metadata=metadata,
            )

    async def scenario() -> None:
        container = FakeContainer()
        artifacts = DelayedFirstEventArtifacts(container)
        events = AzureEventRepository(artifacts, container)
        first = asyncio.create_task(events.append("run-001", "task_started", {"pair": 1}))
        await artifacts.started.wait()
        second = asyncio.create_task(events.append("run-001", "task_completed", {"pair": 1}))
        await asyncio.sleep(0)
        assert not second.done()
        assert await events.read("run-001") == []

        artifacts.release.set()
        assert (await first).sequence == 1
        assert (await second).sequence == 2
        assert [event.sequence for event in await events.read("run-001")] == [1, 2]
        assert container.list_calls == 0

    asyncio.run(scenario())


def test_event_publication_recovers_pending_idempotent_write_without_gap() -> None:
    class CrashAfterEventWrite(AzureArtifactRepository):
        def __init__(self, container: FakeContainer) -> None:
            super().__init__(container)
            self.failed = False

        async def create_bytes(
            self,
            path: str,
            data: bytes,
            *,
            content_type: str = "application/octet-stream",
            metadata: JsonObject | None = None,
        ) -> ArtifactInfo:
            created = await super().create_bytes(
                path,
                data,
                content_type=content_type,
                metadata=metadata,
            )
            if "/events/" in path and not self.failed:
                self.failed = True
                raise RuntimeError("injected crash after event write")
            return created

    async def scenario() -> None:
        container = FakeContainer()
        crashing = AzureEventRepository(CrashAfterEventWrite(container), container)
        with pytest.raises(RuntimeError, match="injected crash"):
            await crashing.append(
                "run-001",
                "task_completed",
                {"pair": "sample-001|solution-one"},
                idempotency_key="task-terminal:sample-001|solution-one",
            )
        assert await crashing.read("run-001") == []

        recovered = AzureEventRepository(AzureArtifactRepository(container), container)
        duplicate = await recovered.append(
            "run-001",
            "task_completed",
            {"pair": "sample-001|solution-one"},
            idempotency_key="task-terminal:sample-001|solution-one",
        )
        second = await recovered.append("run-001", "run_completed", {"ok": True})
        assert duplicate.sequence == 1
        assert second.sequence == 2
        assert [event.sequence for event in await recovered.read("run-001")] == [1, 2]
        assert container.list_calls == 0

    asyncio.run(scenario())


def test_azure_event_retry_returns_ambiguously_committed_operation() -> None:
    class AmbiguousCommitEvents(AzureEventRepository):
        def __init__(self, artifacts: AzureArtifactRepository, container: FakeContainer) -> None:
            super().__init__(artifacts, container)
            self.failed = False

        async def _write_counter(
            self,
            run_id: str,
            counter: JsonObject,
            *,
            etag: str,
            lease: object,
        ) -> str:
            updated_etag = await super()._write_counter(
                run_id,
                counter,
                etag=etag,
                lease=lease,
            )
            if counter.get("pending") is not None and not self.failed:
                self.failed = True
                raise ConcurrencyError("injected ambiguous pending commit")
            return updated_etag

    async def scenario() -> None:
        container = FakeContainer()
        artifacts = AzureArtifactRepository(container)
        events = AmbiguousCommitEvents(artifacts, container)

        created = await events.append("run-001", "task_started", {"pair": 1})

        assert created.sequence == 1
        assert [event.sequence for event in await events.read("run-001")] == [1]
        assert container.values["runs/run-001/event-counter.json"] == b'{"sequence":1}\n'

    asyncio.run(scenario())


def test_large_table_payload_is_blob_backed_using_utf16_limit() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    async def scenario() -> None:
        container = FakeContainer()
        artifacts = AzureArtifactRepository(container)
        table = FakeTable()
        tasks = AzureTaskRepository(table, clock=lambda: now, artifacts=artifacts)
        task = replace(_task(now), metadata={"large": "x" * 40_000})
        stored = await tasks.create(task)
        entity = table.values[(task.run_id, task.pair_id)]
        assert "Payload" not in entity
        first_ref = str(entity["PayloadRef"])
        assert first_ref.startswith("state/table-payloads/task/")
        assert first_ref.count("/") == 4
        assert stored.value.metadata == task.metadata
        assert await artifacts.exists(first_ref)

        updated = replace(task, metadata={"large": "y" * 40_000})
        replaced = await tasks.replace(updated, expected_etag=stored.etag)
        second_ref = str(table.values[(task.run_id, task.pair_id)]["PayloadRef"])
        assert second_ref != first_ref
        assert await artifacts.exists(first_ref)
        assert await artifacts.exists(second_ref)
        assert replaced.value.metadata == updated.metadata

        await tasks.delete_for_run(task.run_id)
        assert not await artifacts.exists(first_ref)
        assert not await artifacts.exists(second_ref)

        without_blobs = AzureTaskRepository(FakeTable(), clock=lambda: now)
        with pytest.raises(StrictJsonError, match="UTF-16"):
            await without_blobs.create(task)

    asyncio.run(scenario())


def test_table_delete_failure_keeps_live_overflow_payload_retryable() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    class FailingDeleteTable(FakeTable):
        def __init__(self) -> None:
            super().__init__()
            self.fail_delete = True

        async def delete_entity(self, *, partition_key: str, row_key: str) -> None:
            if self.fail_delete:
                self.fail_delete = False
                raise FakeAzureError(503)
            await super().delete_entity(partition_key=partition_key, row_key=row_key)

    async def scenario() -> None:
        container = FakeContainer()
        artifacts = AzureArtifactRepository(container)
        table = FailingDeleteTable()
        tasks = AzureTaskRepository(table, clock=lambda: now, artifacts=artifacts)
        task = replace(_task(now), metadata={"large": "x" * 40_000})
        await tasks.create(task)
        payload_ref = str(table.values[(task.run_id, task.pair_id)]["PayloadRef"])

        with pytest.raises(FakeAzureError):
            await tasks.delete_for_run(task.run_id)

        assert (task.run_id, task.pair_id) in table.values
        assert await artifacts.exists(payload_ref)

        await tasks.delete_for_run(task.run_id)
        assert (task.run_id, task.pair_id) not in table.values
        assert not await artifacts.exists(payload_ref)

    asyncio.run(scenario())


def test_ambiguous_table_create_never_deletes_committed_overflow_payload() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    class AmbiguousCreateTable(FakeTable):
        async def create_entity(self, *, entity: dict[str, object]) -> None:
            await super().create_entity(entity=entity)
            if entity.get("Kind") == "Task":
                raise FakeAzureError(503)

    async def scenario() -> None:
        container = FakeContainer()
        artifacts = AzureArtifactRepository(container)
        table = AmbiguousCreateTable()
        tasks = AzureTaskRepository(table, clock=lambda: now, artifacts=artifacts)
        task = replace(_task(now), metadata={"large": "x" * 40_000})

        with pytest.raises(FakeAzureError):
            await tasks.create(task)

        entity = table.values[(task.run_id, task.pair_id)]
        payload_ref = str(entity["PayloadRef"])
        assert await artifacts.exists(payload_ref)
        assert (await tasks.get(task.run_id, task.pair_id)).value == task

    asyncio.run(scenario())


def test_payload_cleanup_failure_resumes_from_durable_table_marker() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    async def scenario() -> None:
        container = FakeContainer()
        artifacts = AzureArtifactRepository(container)
        table = FakeTable()
        tasks = AzureTaskRepository(table, clock=lambda: now, artifacts=artifacts)
        task = replace(_task(now), metadata={"large": "x" * 40_000})
        await tasks.create(task)
        payload_ref = str(table.values[(task.run_id, task.pair_id)]["PayloadRef"])
        original_delete_prefix = artifacts.delete_table_payload_prefix
        failed = False

        async def fail_once(prefix: str) -> int:
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("simulated Blob cleanup failure")
            return await original_delete_prefix(prefix)

        artifacts.delete_table_payload_prefix = fail_once
        with pytest.raises(OSError, match="simulated Blob cleanup failure"):
            await tasks.delete_for_run(task.run_id)
        artifacts.delete_table_payload_prefix = original_delete_prefix

        assert any(
            key[0] == "payloaddeletions" for key in table.values
        )
        assert await artifacts.exists(payload_ref)

        await tasks.delete_for_run(task.run_id)
        assert not any(
            key[0] == "payloaddeletions" for key in table.values
        )
        assert not await artifacts.exists(payload_ref)

    asyncio.run(scenario())


def test_project_delete_resume_finishes_committed_payload_cleanup() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    async def scenario() -> None:
        container = FakeContainer()
        artifacts = AzureArtifactRepository(container)
        table = FakeTable()
        projects = AzureProjectRepository(table, artifacts=artifacts)
        project = replace(_project(now), metadata={"large": "x" * 40_000})
        await projects.create(project)
        payload_ref = str(
            table.values[(project.owner_scope, project.project_id)]["PayloadRef"]
        )
        original_delete_prefix = artifacts.delete_table_payload_prefix

        async def fail_cleanup(prefix: str) -> int:
            raise OSError(f"simulated cleanup failure for {prefix}")

        artifacts.delete_table_payload_prefix = fail_cleanup
        with pytest.raises(OSError, match="simulated cleanup failure"):
            await projects.delete(project.owner_scope, project.project_id)
        artifacts.delete_table_payload_prefix = original_delete_prefix

        assert (project.owner_scope, project.project_id) not in table.values
        assert await artifacts.exists(payload_ref)
        assert await projects.resume_delete(
            project.owner_scope,
            project.project_id,
        )
        assert not await artifacts.exists(payload_ref)
        assert not await projects.resume_delete(
            project.owner_scope,
            project.project_id,
        )

    asyncio.run(scenario())


def test_stale_payload_deletion_marker_fences_identity_recreation() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    async def scenario() -> None:
        container = FakeContainer()
        artifacts = AzureArtifactRepository(container)
        table = FakeTable()
        projects = AzureProjectRepository(table, artifacts=artifacts)
        original = replace(_project(now), metadata={"large": "x" * 40_000})
        replacement = replace(
            original,
            name="Replacement",
            metadata={"large": "y" * 40_000},
        )
        await projects.create(original)
        original_delete_prefix = artifacts.delete_table_payload_prefix

        async def fail_cleanup(prefix: str) -> int:
            raise OSError(f"simulated cleanup failure for {prefix}")

        artifacts.delete_table_payload_prefix = fail_cleanup
        with pytest.raises(OSError, match="simulated cleanup failure"):
            await projects.delete(original.owner_scope, original.project_id)
        artifacts.delete_table_payload_prefix = original_delete_prefix

        with pytest.raises(ConcurrencyError, match="deletion is still in progress"):
            await projects.create(replacement)
        assert (replacement.owner_scope, replacement.project_id) not in table.values

        assert await projects.resume_delete(
            replacement.owner_scope,
            replacement.project_id,
        )
        recreated = await projects.create(replacement)
        assert recreated.value.name == "Replacement"

    asyncio.run(scenario())


def test_identity_lease_serializes_deletion_and_recreation() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    async def scenario() -> None:
        container = FakeContainer()
        artifacts = AzureArtifactRepository(container)
        table = FakeTable()
        deleting = AzureProjectRepository(table, artifacts=artifacts)
        recreating = AzureProjectRepository(table, artifacts=artifacts)
        original = replace(_project(now), metadata={"large": "x" * 40_000})
        replacement = replace(
            original,
            name="Replacement after delete",
            metadata={"large": "y" * 40_000},
        )
        await deleting.create(original)
        marker_ready = asyncio.Event()
        allow_cleanup = asyncio.Event()
        original_complete = deleting._complete_payload_deletion_locked

        async def pause_after_marker(marker: object) -> None:
            marker_ready.set()
            await allow_cleanup.wait()
            await original_complete(marker)

        deleting._complete_payload_deletion_locked = pause_after_marker
        delete_task = asyncio.create_task(
            deleting.delete(original.owner_scope, original.project_id)
        )
        await marker_ready.wait()
        create_task = asyncio.create_task(recreating.create(replacement))
        await asyncio.sleep(0.02)
        assert not create_task.done()

        allow_cleanup.set()
        await delete_task
        recreated = await create_task

        assert recreated.value.name == "Replacement after delete"
        assert (
            await recreating.get(replacement.owner_scope, replacement.project_id)
        ).value == replacement

    asyncio.run(scenario())


def test_stale_sweeper_marker_cannot_delete_recreated_identity() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    async def scenario() -> None:
        container = FakeContainer()
        artifacts = AzureArtifactRepository(container)
        table = FakeTable()
        projects = AzureProjectRepository(table, artifacts=artifacts)
        original = replace(_project(now), metadata={"large": "x" * 40_000})
        replacement = replace(
            original,
            name="Replacement after sweep",
            metadata={"large": "y" * 40_000},
        )
        await projects.create(original)
        entity = await projects._get_entity(
            original.owner_scope,
            original.project_id,
        )
        stale_marker = await projects._ensure_payload_deletion_marker(
            partition_key=original.owner_scope,
            row_key=original.project_id,
            kind="Project",
            legacy_payload_ref=str(entity["PayloadRef"]),
        )
        await projects._complete_payload_deletion(stale_marker)
        await projects.create(replacement)

        await projects._complete_payload_deletion(stale_marker)

        stored = await projects.get(
            replacement.owner_scope,
            replacement.project_id,
        )
        assert stored.value == replacement

    asyncio.run(scenario())


def test_ambiguous_table_deletes_are_verified_before_payload_cleanup() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    class AmbiguousDeleteTable(FakeTable):
        def __init__(self) -> None:
            super().__init__()
            self.target_raised = False
            self.marker_raised = False

        async def delete_entity(self, *, partition_key: str, row_key: str) -> None:
            await super().delete_entity(partition_key=partition_key, row_key=row_key)
            if partition_key == "run-001" and not self.target_raised:
                self.target_raised = True
                raise FakeAzureError(503)
            if partition_key == "payloaddeletions" and not self.marker_raised:
                self.marker_raised = True
                raise FakeAzureError(503)

    async def scenario() -> None:
        container = FakeContainer()
        artifacts = AzureArtifactRepository(container)
        table = AmbiguousDeleteTable()
        tasks = AzureTaskRepository(table, clock=lambda: now, artifacts=artifacts)
        task = replace(_task(now), metadata={"large": "x" * 40_000})
        await tasks.create(task)
        payload_ref = str(table.values[(task.run_id, task.pair_id)]["PayloadRef"])

        await tasks.delete_for_run(task.run_id)

        assert table.target_raised
        assert table.marker_raised
        assert (task.run_id, task.pair_id) not in table.values
        assert not any(key[0] == "payloaddeletions" for key in table.values)
        assert not await artifacts.exists(payload_ref)

    asyncio.run(scenario())


def test_ambiguous_blob_delete_is_success_when_readback_is_absent() -> None:
    class AmbiguousDeleteBlob(FakeBlob):
        async def delete_blob(self, *, delete_snapshots: str) -> None:
            await super().delete_blob(delete_snapshots=delete_snapshots)
            raise FakeAzureError(503)

    class AmbiguousDeleteContainer(FakeContainer):
        def get_blob_client(self, name: str) -> FakeBlob:
            return AmbiguousDeleteBlob(self, name)

    async def scenario() -> None:
        artifacts = AzureArtifactRepository(AmbiguousDeleteContainer())
        path = "projects/project-one/staged/image"
        await artifacts.create_bytes(path, b"image")

        assert await artifacts.delete(path)
        assert not await artifacts.exists(path)

    asyncio.run(scenario())


def test_seed_completion_is_written_under_the_current_blob_lease() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    clock_value = [now]

    async def scenario() -> None:
        container = FakeContainer()
        seeds = AzureBlobSeedCoordinator(container, clock=lambda: clock_value[0])
        stale = await seeds.acquire(
            "builtins-v1",
            owner="instance-one",
            lease_seconds=15,
        )
        assert stale is not None
        clock_value[0] += timedelta(seconds=16)
        with pytest.raises(LeaseLostError, match="expired"):
            await seeds.complete(stale, {"revision": "stale"})
        assert isinstance(stale.handle, FakeLease)
        await stale.handle.release()

        current = await seeds.acquire(
            "builtins-v1",
            owner="instance-two",
            lease_seconds=15,
        )
        assert current is not None
        await seeds.complete(current, {"revision": "current"})
        assert await seeds.is_complete("builtins-v1")
        assert "seeding/builtins-v1/marker.json" not in container.values

    asyncio.run(scenario())


def test_seed_lease_expiry_uses_original_duration_and_pre_request_time() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    clock_value = [now]

    async def scenario() -> None:
        container = FakeContainer()
        container.after_acquire = lambda: clock_value.__setitem__(
            0,
            clock_value[0] + timedelta(seconds=4),
        )
        seeds = AzureBlobSeedCoordinator(container, clock=lambda: clock_value[0])
        lease = await seeds.acquire(
            "builtins-v1",
            owner="instance-one",
            lease_seconds=15,
        )
        assert lease is not None
        assert lease.lease_seconds == 15
        assert lease.lease_until == now + timedelta(seconds=15)

        renew_started = clock_value[0]
        container.after_renew = lambda: clock_value.__setitem__(
            0,
            clock_value[0] + timedelta(seconds=5),
        )
        renewed = await seeds.renew(lease, lease_seconds=15)
        assert renewed.lease_seconds == 15
        assert renewed.lease_until == renew_started + timedelta(seconds=15)
        with pytest.raises(ValueError, match="cannot change"):
            await seeds.renew(renewed, lease_seconds=30)

    asyncio.run(scenario())


def test_generic_azure_http_errors_are_not_misclassified_as_cas_conflicts() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    class HttpResponseError(RuntimeError):
        def __init__(self, status_code: int, error_code: str | None = None) -> None:
            super().__init__(f"status {status_code}")
            self.status_code = status_code
            self.error_code = error_code

    class ErroringTable(FakeTable):
        def __init__(self, status_code: int) -> None:
            super().__init__()
            self.status_code = status_code

        async def update_entity(
            self,
            *,
            entity: dict[str, object],
            etag: str,
            **kwargs: object,
        ) -> None:
            del entity, etag, kwargs
            raise HttpResponseError(self.status_code)

    class CreateErrorTable(FakeTable):
        def __init__(self, error_code: str | None) -> None:
            super().__init__()
            self.error_code = error_code

        async def create_entity(self, *, entity: dict[str, object]) -> None:
            del entity
            raise HttpResponseError(409, self.error_code)

    class ErroringQueue(FakeQueue):
        def __init__(self, error_code: str) -> None:
            super().__init__()
            self.error_code = error_code

        async def update_message(
            self,
            message: str,
            pop_receipt: str,
            *,
            content: str,
            visibility_timeout: int,
        ) -> SimpleNamespace:
            del message, pop_receipt, content, visibility_timeout
            raise HttpResponseError(400, self.error_code)

    async def scenario() -> None:
        for status_code in (409, 429, 500):
            table = ErroringTable(status_code)
            projects = AzureProjectRepository(table)
            stored = await projects.create(_project(now))
            with pytest.raises(HttpResponseError) as raised:
                await projects.replace(stored.value, expected_etag=stored.etag)
            assert raised.value.status_code == status_code

        conflict_table = ErroringTable(412)
        conflict_projects = AzureProjectRepository(conflict_table)
        conflict = await conflict_projects.create(_project(now))
        with pytest.raises(ConcurrencyError):
            await conflict_projects.replace(conflict.value, expected_etag=conflict.etag)

        missing_table = ErroringTable(404)
        missing_projects = AzureProjectRepository(missing_table)
        missing = await missing_projects.create(_project(now))
        with pytest.raises(NotFoundError):
            await missing_projects.replace(missing.value, expected_etag=missing.etag)

        for error_code in (None, "TableBeingDeleted"):
            deleting_projects = AzureProjectRepository(CreateErrorTable(error_code))
            with pytest.raises(HttpResponseError) as deleting:
                await deleting_projects.create(_project(now))
            assert deleting.value.error_code == error_code

        message = TaskMessage(
            project_id="cord-v2",
            run_id="run-001",
            sample_id="sample-001",
            solution_id="solution-one",
        )
        lease = QueueLease(
            message_id="message-1",
            pop_receipt="receipt-1",
            message=message,
            dequeue_count=1,
            lease_until=now + timedelta(seconds=30),
        )
        mismatched = AzureTaskQueue(
            ErroringQueue("PopReceiptMismatch"),
            FakeQueue(),
            clock=lambda: now,
        )
        with pytest.raises(LeaseLostError):
            await mismatched.renew(lease, visibility_timeout=30)

        throttled = AzureTaskQueue(
            ErroringQueue("OperationTimedOut"),
            FakeQueue(),
            clock=lambda: now,
        )
        with pytest.raises(HttpResponseError):
            await throttled.renew(lease, visibility_timeout=30)

    asyncio.run(scenario())


def test_documented_blob_lease_operation_codes_map_to_lease_loss() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    class ErrorRenewHandle:
        def __init__(self, error_code: str) -> None:
            self.error_code = error_code

        async def renew(self) -> None:
            raise FakeAzureError(409, self.error_code)

    async def scenario() -> None:
        seeds = AzureBlobSeedCoordinator(FakeContainer(), clock=lambda: now)
        for error_code in (
            "LeaseIdMismatchWithLeaseOperation",
            "LeaseNotPresentWithLeaseOperation",
            "LeaseIsBrokenAndCannotBeRenewed",
        ):
            lease = SeedLease(
                seed_id="builtins-v1",
                owner="worker-one",
                token="lease-token",
                lease_until=now + timedelta(seconds=15),
                lease_seconds=15,
                handle=ErrorRenewHandle(error_code),
            )
            with pytest.raises(LeaseLostError):
                await seeds.renew(lease, lease_seconds=15)

        unrelated = SeedLease(
            seed_id="builtins-v1",
            owner="worker-one",
            token="lease-token",
            lease_until=now + timedelta(seconds=15),
            lease_seconds=15,
            handle=ErrorRenewHandle("ContainerBeingDeleted"),
        )
        with pytest.raises(FakeAzureError):
            await seeds.renew(unrelated, lease_seconds=15)

    asyncio.run(scenario())


def test_azure_repositories_and_queue_reject_expired_leases() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    clock_value = [now]

    async def scenario() -> None:
        tasks = AzureTaskRepository(FakeTable(), clock=lambda: clock_value[0])
        task = await tasks.create(_task(now))
        claim = await tasks.claim(
            task.value.run_id,
            task.value.pair_id,
            owner="worker-one",
            lease_until=now + timedelta(seconds=5),
        )
        assert claim.acquired
        clock_value[0] += timedelta(seconds=6)
        with pytest.raises(LeaseLostError):
            await tasks.record_raw(
                task.value.run_id,
                task.value.pair_id,
                owner="worker-one",
                raw_artifact_path="runs/run-001/results/sample-001/solution-one/raw.json",
                telemetry_artifact_path=(
                    "runs/run-001/results/sample-001/solution-one/telemetry.json"
                ),
            )

        runs = AzureRunRepository(FakeTable(), clock=lambda: clock_value[0])
        terminal_run = replace(
            _run(clock_value[0]),
            pending_tasks=0,
            succeeded_tasks=1,
            status="running",
        )
        await runs.create(terminal_run)
        finalizer = await runs.claim_finalizer(
            terminal_run.project_id,
            terminal_run.run_id,
            owner="finalizer-one",
            lease_until=clock_value[0] + timedelta(seconds=5),
        )
        assert finalizer is not None
        clock_value[0] += timedelta(seconds=6)
        with pytest.raises(LeaseLostError):
            await runs.complete_finalizer(
                terminal_run.project_id,
                terminal_run.run_id,
                owner="finalizer-one",
                aggregate_artifact_path="runs/run-001/aggregate.json",
                download_artifact_path="runs/run-001/download.zip",
            )

        queue_client = FakeQueue()
        poison_client = FakeQueue()
        queue = AzureTaskQueue(
            queue_client,
            poison_client,
            clock=lambda: clock_value[0],
        )
        message = TaskMessage(
            project_id="cord-v2",
            run_id="run-001",
            sample_id="sample-001",
            solution_id="solution-one",
        )
        await queue.send(message)
        queue_client.received.append(
            SimpleNamespace(
                id="message-1",
                pop_receipt="receipt-1",
                content=queue_client.sent[0],
                dequeue_count=1,
                next_visible_on=clock_value[0] + timedelta(seconds=5),
            )
        )
        lease = (await queue.receive(max_messages=1, visibility_timeout=5))[0]
        clock_value[0] += timedelta(seconds=6)
        with pytest.raises(LeaseLostError, match="expired"):
            await queue.complete(lease)
        assert not queue_client.deleted

    asyncio.run(scenario())


def test_finalizer_completion_reports_applied_vs_already_completed() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)

    async def scenario() -> None:
        runs = AzureRunRepository(FakeTable(), clock=lambda: now)
        terminal_run = replace(
            _run(now),
            pending_tasks=0,
            succeeded_tasks=1,
            status="running",
        )
        await runs.create(terminal_run)
        claimed = await runs.claim_finalizer(
            terminal_run.project_id,
            terminal_run.run_id,
            owner="finalizer-one",
            lease_until=now + timedelta(seconds=30),
        )
        assert claimed is not None
        first = await runs.complete_finalizer(
            terminal_run.project_id,
            terminal_run.run_id,
            owner="finalizer-one",
            aggregate_artifact_path="runs/run-001/aggregate.json",
            download_artifact_path="runs/run-001/download.zip",
        )
        duplicate = await runs.complete_finalizer(
            terminal_run.project_id,
            terminal_run.run_id,
            owner="another-owner",
            aggregate_artifact_path="runs/run-001/aggregate.json",
            download_artifact_path="runs/run-001/download.zip",
        )
        assert first.applied
        assert not duplicate.applied
        assert duplicate.run.value == first.run.value

    asyncio.run(scenario())
