from __future__ import annotations

import asyncio
from typing import Any

import pytest

from swiftpy.core.config import Config
from swiftpy.core.container import Container, get_container, set_container
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query
from swiftpy.database.transaction import (
    TransactionClosedError,
    db,
    get_active_connection,
)
from swiftpy.validation.validator import validate


@pytest.fixture(autouse=True)
async def setup_tx_isolation_tables(ensure_db_pool: DatabasePool) -> None:
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS "test_tx_items" CASCADE;
            DROP TABLE IF EXISTS "test_tx_categories" CASCADE;
            CREATE TABLE "test_tx_categories" (
                id SERIAL PRIMARY KEY,
                slug VARCHAR(64) NOT NULL UNIQUE
            );
            CREATE TABLE "test_tx_items" (
                id SERIAL PRIMARY KEY,
                code VARCHAR(64) NOT NULL UNIQUE,
                category_slug VARCHAR(64) NOT NULL
            );
            INSERT INTO "test_tx_categories" (slug) VALUES ('general');
            INSERT INTO "test_tx_items" (code, category_slug) VALUES ('EXISTING-1', 'general');
            """
        )


@pytest.mark.asyncio
async def test_child_task_cannot_use_connection_after_transaction_exits() -> None:
    ready_inside = asyncio.Event()
    parent_exited = asyncio.Event()
    captured_handle: Any = None

    async def child_worker() -> None:
        ready_inside.set()
        await parent_exited.wait()
        # Attempting to run a query via inherited ContextVar must raise TransactionClosedError
        await Query.table("test_tx_items").get()

    async with db.transaction() as tx_conn:
        captured_handle = tx_conn
        child_task = asyncio.create_task(child_worker())
        await ready_inside.wait()

    # Transaction block has exited
    assert captured_handle.closed is True
    assert get_active_connection() is None
    parent_exited.set()

    with pytest.raises(TransactionClosedError):
        await child_task

    # Direct use of the captured handle after exit also raises TransactionClosedError
    with pytest.raises(TransactionClosedError):
        await captured_handle.fetch('SELECT * FROM "test_tx_items"')


@pytest.mark.asyncio
async def test_concurrent_gather_inside_transaction_serializes_cleanly() -> None:
    async with db.transaction():
        await Query.table("test_tx_items").insert(
            {"code": "TX-CONC-1", "category_slug": "general"}
        )
        await Query.table("test_tx_items").insert(
            {"code": "TX-CONC-2", "category_slug": "general"}
        )

        # Concurrent queries on the same transaction must serialize instead of raising InterfaceError
        res1, res2, count_all = await asyncio.gather(
            Query.table("test_tx_items").where("code", "TX-CONC-1").first(),
            Query.table("test_tx_items").where("code", "TX-CONC-2").first(),
            Query.table("test_tx_items").count(),
        )

        assert res1 is not None and res1["code"] == "TX-CONC-1"
        assert res2 is not None and res2["code"] == "TX-CONC-2"
        assert count_all == 3


@pytest.mark.asyncio
async def test_validator_unique_and_exists_inside_transaction_see_uncommitted_rows() -> (
    None
):
    async with db.transaction():
        # Insert uncommitted category and item inside transaction
        await Query.table("test_tx_categories").insert({"slug": "uncommitted_cat"})
        await Query.table("test_tx_items").insert(
            {"code": "UNCOMMITTED-CODE", "category_slug": "uncommitted_cat"}
        )

        # 1. 'UNCOMMITTED-CODE' is detected as already taken, and 'uncommitted_cat' is found by exists
        res_taken = await validate(
            data={"code": "UNCOMMITTED-CODE", "category": "uncommitted_cat"},
            schema={
                "code": "required|unique:test_tx_items,code",
                "category": "required|exists:test_tx_categories,slug",
            },
        )
        assert res_taken.fails()
        assert "code" in res_taken.errors
        assert "category" not in res_taken.errors

        # 2. Fresh code + uncommitted category passes both unique and exists concurrently
        res_valid = await validate(
            data={"code": "FRESH-CODE", "category": "uncommitted_cat"},
            schema={
                "code": "required|unique:test_tx_items,code",
                "category": "required|exists:test_tx_categories,slug",
            },
        )
        assert res_valid.passes()


@pytest.mark.asyncio
async def test_pool_size_one_with_transaction_and_unique_does_not_deadlock() -> None:
    prev_container = get_container()
    cfg = Config.load()
    cfg.set("database.pool.min_size", 1)
    cfg.set("database.pool.max_size", 1)

    single_pool = DatabasePool(cfg)
    await single_pool.init()

    isolated_container = Container()
    isolated_container.instance(Config, cfg)
    isolated_container.instance(DatabasePool, single_pool)
    set_container(isolated_container)

    try:
        async with asyncio.timeout(5.0):
            async with db.transaction():
                await Query.table("test_tx_categories").insert(
                    {"slug": "single_pool_cat"}
                )
                result = await validate(
                    data={"code": "SINGLE-POOL-NEW", "category": "single_pool_cat"},
                    schema={
                        "code": "required|unique:test_tx_items,code",
                        "category": "required|exists:test_tx_categories,slug",
                    },
                )
                assert result.passes()
                raise RuntimeError("Rollback single-pool test transaction")
    except RuntimeError as exc:
        assert "Rollback" in str(exc)
    finally:
        set_container(prev_container)
        await single_pool.close()


@pytest.mark.asyncio
async def test_exception_and_cancellation_rollback_and_release_connection(
    ensure_db_pool: DatabasePool,
) -> None:
    raw_pool = ensure_db_pool.get_pool()
    idle_before = raw_pool.get_idle_size()

    # 1. Exception rolls back and releases connection
    with pytest.raises(ValueError, match="boom"):
        async with db.transaction():
            await Query.table("test_tx_items").insert(
                {"code": "ROLLED-BACK-1", "category_slug": "general"}
            )
            raise ValueError("boom")

    assert get_active_connection() is None
    assert raw_pool.get_idle_size() >= idle_before
    assert (
        await Query.table("test_tx_items").where("code", "ROLLED-BACK-1").first()
        is None
    )

    # 2. Task cancellation rolls back and releases connection
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def cancellable_tx() -> None:
        async with db.transaction():
            await Query.table("test_tx_items").insert(
                {"code": "CANCELLED-1", "category_slug": "general"}
            )
            entered.set()
            await hold.wait()

    task = asyncio.create_task(cancellable_tx())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert raw_pool.get_idle_size() >= idle_before
    assert (
        await Query.table("test_tx_items").where("code", "CANCELLED-1").first() is None
    )
