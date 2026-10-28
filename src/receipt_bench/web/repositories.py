from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from receipt_bench.storage.models import TaskRecord
from receipt_bench.storage.protocols import (
    ArtifactRepository,
    EventRepository,
    ProjectRepository,
    RunRepository,
    SeedCoordinator,
    TaskQueue,
    TaskRepository,
)

CloseCallback = Callable[[], object | Awaitable[object]]


class StorageBundle(Protocol):
    @property
    def artifacts(self) -> ArtifactRepository: ...

    @property
    def projects(self) -> ProjectRepository: ...

    @property
    def runs(self) -> RunRepository: ...

    @property
    def tasks(self) -> TaskRepository: ...

    @property
    def events(self) -> EventRepository: ...

    @property
    def queue(self) -> TaskQueue: ...

    @property
    def seeds(self) -> SeedCoordinator: ...


@dataclass(frozen=True)
class RepositoryBundle:
    artifacts: ArtifactRepository
    projects: ProjectRepository
    runs: RunRepository
    tasks: TaskRepository
    events: EventRepository
    queue: TaskQueue
    seeds: SeedCoordinator | None = None
    close_callback: CloseCallback | None = None

    @classmethod
    def from_storage(cls, storage: StorageBundle) -> RepositoryBundle:
        close = getattr(storage, "close", None)
        return cls(
            artifacts=storage.artifacts,
            projects=storage.projects,
            runs=storage.runs,
            tasks=storage.tasks,
            events=storage.events,
            queue=storage.queue,
            seeds=getattr(storage, "seeds", None),
            close_callback=close if callable(close) else None,
        )

    async def close(self) -> None:
        if self.close_callback is None:
            return
        result = self.close_callback()
        if inspect.isawaitable(result):
            await result


class WorkerRunner(Protocol):
    async def run_forever(self, stop: asyncio.Event) -> None: ...


@runtime_checkable
class TaskDispatcher(Protocol):
    async def enqueue(self, tasks: list[TaskRecord] | tuple[TaskRecord, ...]) -> None: ...


__all__ = ["RepositoryBundle", "StorageBundle", "TaskDispatcher", "WorkerRunner"]
