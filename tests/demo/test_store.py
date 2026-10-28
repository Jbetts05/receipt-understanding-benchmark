from __future__ import annotations

import asyncio

import pytest

from receipt_bench.demo.api_models import DemoSolution
from receipt_bench.demo.orchestrator import (
    ApplicationConcurrency,
    component_provider_keys,
)
from receipt_bench.demo.settings import DemoSettings
from receipt_bench.demo.store import (
    ActiveRunLimitError,
    RunStore,
    UploadCapacityError,
    UploadStore,
)


def _solution(
    solution_id: str,
    *,
    api: str,
    normalizer: str | None = None,
) -> DemoSolution:
    return DemoSolution(
        id=solution_id,
        label=solution_id,
        order=0,
        kind="ocr_pipeline" if normalizer is not None else "direct",
        api=api,
        lifecycle="ga",
        normalizer=normalizer,
    )


def test_upload_store_enforces_item_and_byte_caps_and_prunes_proactively() -> None:
    now = [0.0]

    async def scenario() -> None:
        store = UploadStore(
            ttl_seconds=10,
            max_items=2,
            max_total_bytes=5,
            clock=lambda: now[0],
        )
        await store.add(filename="a.jpg", media_type="image/jpeg", data=b"aaa", sha256="a")
        with pytest.raises(UploadCapacityError, match="memory limit"):
            await store.add(
                filename="b.jpg",
                media_type="image/jpeg",
                data=b"bbb",
                sha256="b",
            )
        await store.add(filename="b.jpg", media_type="image/jpeg", data=b"bb", sha256="b")
        with pytest.raises(UploadCapacityError, match="2 items"):
            await store.add(filename="c.jpg", media_type="image/jpeg", data=b"c", sha256="c")
        assert await store.usage() == (2, 5)

        now[0] = 11.0
        assert await store.prune_expired() == 2
        assert await store.usage() == (0, 0)

        await store.add(filename="c.jpg", media_type="image/jpeg", data=b"c", sha256="c")
        await store.reserve(4)
        await store.close()
        assert await store.usage() == (0, 0)
        assert await store.reservation_usage() == (0, 0)

    asyncio.run(scenario())


def test_upload_store_counts_concurrent_in_flight_items_and_bytes_atomically() -> None:
    async def scenario() -> None:
        store = UploadStore(ttl_seconds=10, max_items=2, max_total_bytes=5)
        first_ready = asyncio.Event()
        finish_first = asyncio.Event()

        async def first_upload() -> None:
            reservation = await store.reserve(0)
            try:
                await store.resize_reservation(reservation, 4)
                first_ready.set()
                await finish_first.wait()
                await store.commit(
                    reservation=reservation,
                    filename="first.jpg",
                    media_type="image/jpeg",
                    data=b"aaa",
                    sha256="first",
                )
            finally:
                await store.release_reservation(reservation)

        first_task = asyncio.create_task(first_upload())
        await first_ready.wait()
        assert await store.reservation_usage() == (1, 4)

        with pytest.raises(UploadCapacityError, match="memory limit"):
            await store.reserve(2)

        second = await store.reserve(1)
        with pytest.raises(UploadCapacityError, match="memory limit"):
            await store.resize_reservation(second, 2)

        finish_first.set()
        await first_task
        await store.resize_reservation(second, 2)
        try:
            await store.commit(
                reservation=second,
                filename="second.jpg",
                media_type="image/jpeg",
                data=b"bb",
                sha256="second",
            )
        finally:
            await store.release_reservation(second)

        assert await store.usage() == (2, 5)
        assert await store.reservation_usage() == (0, 0)
        await store.close()

    asyncio.run(scenario())


def test_application_limits_include_normalizer_provider_across_runs() -> None:
    direct = _solution("direct", api="azure-openai")
    pipeline = _solution(
        "pipeline",
        api="azure-vision",
        normalizer="gpt-5.4-mini",
    )
    assert component_provider_keys(pipeline) == ("azure-openai", "azure-vision")

    async def scenario() -> None:
        limits = ApplicationConcurrency(
            DemoSettings(
                max_parallel=4,
                provider_concurrency=1,
                solution_concurrency=4,
            ),
            [direct, pipeline],
        )
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_entered = asyncio.Event()

        async def first_run() -> None:
            async with limits.slot(direct):
                first_entered.set()
                await release_first.wait()

        async def second_run() -> None:
            await first_entered.wait()
            async with limits.slot(pipeline):
                second_entered.set()

        first_task = asyncio.create_task(first_run())
        second_task = asyncio.create_task(second_run())
        await first_entered.wait()
        await asyncio.sleep(0)
        assert not second_entered.is_set()
        release_first.set()
        await asyncio.gather(first_task, second_task)
        assert second_entered.is_set()

    asyncio.run(scenario())


def test_run_admission_reservations_are_atomic_and_releasable() -> None:
    async def scenario() -> None:
        store = RunStore(ttl_seconds=300, max_runs=5, max_active_runs=1)
        first = await store.reserve("run-one")
        with pytest.raises(ActiveRunLimitError, match="Active run limit"):
            await store.reserve("run-two")
        await store.release_reservation(first)
        second = await store.reserve("run-two")
        assert second.token is not None
        await store.close()

    asyncio.run(scenario())
