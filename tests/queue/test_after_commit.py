from __future__ import annotations

import asyncio
from typing import Any

import pytest

from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import (
    db,
    get_active_connection,
    on_commit,
)
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.job import Job


class AfterCommitTestJob(Job):
    queue = "after_commit_test"

    def __init__(self, key: str, value: str = "ok", **kwargs: Any) -> None:
        super().__init__(key, value, **kwargs)
        self.key = key
        self.value = value

    async def handle(self) -> None:
        pass


@pytest.fixture
async def queue_dispatcher() -> QueueDispatcher:
    dispatcher = QueueDispatcher.get_instance()
    await dispatcher.clear("after_commit_test")
    return dispatcher


@pytest.mark.asyncio
async def test_immediate_dispatch_default(queue_dispatcher: QueueDispatcher) -> None:
    job = AfterCommitTestJob(key="test_immediate")
    job_id = await job.dispatch(after_commit=False)

    assert job_id == job.job_id
    assert await queue_dispatcher.size("after_commit_test") == 1


@pytest.mark.asyncio
async def test_after_commit_outside_transaction(queue_dispatcher: QueueDispatcher) -> None:
    # When no transaction is active, after_commit=True dispatches immediately
    assert get_active_connection() is None
    job = AfterCommitTestJob(key="test_no_tx")
    job_id = await job.dispatch(after_commit=True)

    assert job_id == job.job_id
    assert await queue_dispatcher.size("after_commit_test") == 1


@pytest.mark.asyncio
async def test_after_commit_inside_transaction_success(
    ensure_db_pool: DatabasePool, queue_dispatcher: QueueDispatcher
) -> None:
    job = AfterCommitTestJob(key="test_commit_success")

    async with db.transaction():
        job_id = await job.dispatch(after_commit=True)
        assert job_id == job.job_id

        # While transaction is active, the job MUST NOT be visible in Redis queue
        assert await queue_dispatcher.size("after_commit_test") == 0

    # Once transaction successfully commits, the job MUST be in Redis queue
    assert await queue_dispatcher.size("after_commit_test") == 1


@pytest.mark.asyncio
async def test_after_commit_inside_transaction_rollback(
    ensure_db_pool: DatabasePool, queue_dispatcher: QueueDispatcher
) -> None:
    job = AfterCommitTestJob(key="test_rollback")

    with pytest.raises(RuntimeError, match="simulated failure"):
        async with db.transaction():
            await job.dispatch(after_commit=True)
            assert await queue_dispatcher.size("after_commit_test") == 0
            raise RuntimeError("simulated failure")

    # After rollback, the job MUST NOT be dispatched
    assert await queue_dispatcher.size("after_commit_test") == 0


@pytest.mark.asyncio
async def test_nested_transaction_savepoint_commit(
    ensure_db_pool: DatabasePool, queue_dispatcher: QueueDispatcher
) -> None:
    job1 = AfterCommitTestJob(key="outer_job")
    job2 = AfterCommitTestJob(key="inner_job")

    async with db.transaction() as tx:
        await job1.dispatch(after_commit=True)

        async with tx.transaction():
            await job2.dispatch(after_commit=True)
            assert await queue_dispatcher.size("after_commit_test") == 0

        # Savepoint committed, but outer still open
        assert await queue_dispatcher.size("after_commit_test") == 0

    # Outer committed -> both jobs dispatched
    assert await queue_dispatcher.size("after_commit_test") == 2


@pytest.mark.asyncio
async def test_nested_transaction_savepoint_rollback(
    ensure_db_pool: DatabasePool, queue_dispatcher: QueueDispatcher
) -> None:
    job1 = AfterCommitTestJob(key="outer_survives")
    job2 = AfterCommitTestJob(key="inner_dropped")

    async with db.transaction() as tx:
        await job1.dispatch(after_commit=True)

        try:
            async with tx.transaction():
                await job2.dispatch(after_commit=True)
                raise ValueError("savepoint rollback")
        except ValueError:
            pass

        assert await queue_dispatcher.size("after_commit_test") == 0

    # Outer committed, inner rolled back -> only job1 dispatched
    assert await queue_dispatcher.size("after_commit_test") == 1


@pytest.mark.asyncio
async def test_on_commit_hook_sync_and_async(
    ensure_db_pool: DatabasePool,
) -> None:
    executed: list[str] = []

    def sync_hook() -> None:
        executed.append("sync")

    async def async_hook() -> None:
        await asyncio.sleep(0.01)
        executed.append("async")

    async with db.transaction() as tx:
        tx.on_commit(sync_hook)
        tx.on_commit(async_hook)
        assert executed == []

    assert executed == ["sync", "async"]


@pytest.mark.asyncio
async def test_on_commit_outside_transaction() -> None:
    called = []
    on_commit(lambda: called.append("direct"))
    assert called == ["direct"]
