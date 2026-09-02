# tests/db/load_test_database2.py

from __future__ import annotations

import asyncio
import time

from app.providers.app_service_provider import AppServiceProvider
from app.providers.database_service_provider import DatabaseServiceProvider
from swiftpy.core.bootstrap import create_app
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query
from swiftpy.database.transaction import db


async def create_table(db_pool: DatabasePool) -> None:
    pool = db_pool.get_pool()

    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS test_users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """
        )

        await conn.execute(
            """
            TRUNCATE TABLE test_users RESTART IDENTITY CASCADE
            """
        )


async def seed_data() -> None:
    for i in range(100):
        await Query.table("test_users").insert(
            {
                "name": f"user_{i}",
                "active": True,
            }
        )


async def read_worker() -> None:
    await (
        Query.table("test_users")
        .select("id", "name")
        .where("active", True)
        .limit(5)
        .get()
    )

    async with db.transaction():
        await (
            Query.table("test_users")
            .where("active", True)
            .limit(1)
            .first()
        )


async def write_worker(worker_id: int) -> None:
    await Query.table("test_users").insert(
        {
            "name": f"concurrent_user_{worker_id}",
            "active": True,
        }
    )


async def rollback_test() -> None:
    try:
        async with db.transaction():
            await Query.table("test_users").insert(
                {
                    "name": "rollback_user",
                    "active": True,
                }
            )

            raise RuntimeError("force rollback")

    except RuntimeError:
        pass

    user = (
        await Query.table("test_users")
        .where("name", "rollback_user")
        .first()
    )

    assert user is None, "Rollback failed"


async def nested_transaction_test() -> None:
    async with db.transaction():
        await Query.table("test_users").insert(
            {
                "name": "parent_user",
                "active": True,
            }
        )

        try:
            async with db.transaction():
                await Query.table("test_users").insert(
                    {
                        "name": "child_user",
                        "active": True,
                    }
                )

                raise RuntimeError("force nested rollback")

        except RuntimeError:
            pass

    parent = (
        await Query.table("test_users")
        .where("name", "parent_user")
        .first()
    )

    child = (
        await Query.table("test_users")
        .where("name", "child_user")
        .first()
    )

    print()
    print("==== NESTED TRANSACTION TEST ====")
    print("Parent Exists:", parent is not None)
    print("Child Exists :", child is not None)


async def warm_pool(db_pool: DatabasePool) -> None:
    pool = db_pool.get_pool()

    async def touch():
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")

    await asyncio.gather(*(touch() for _ in range(pool.get_max_size())))


async def load_test_reads() -> None:
    concurrency = 1000

    started = time.perf_counter()

    await asyncio.gather(
        *(read_worker() for _ in range(concurrency))
    )

    elapsed = time.perf_counter() - started

    print()
    print("==== READ LOAD TEST ====")
    print(f"Tasks: {concurrency}")
    print(f"Elapsed: {elapsed:.3f}s")
    print(f"QPS: {(concurrency * 2) / elapsed:.2f}")


async def load_test_writes() -> None:
    concurrency = 1000

    started = time.perf_counter()

    await asyncio.gather(
        *(write_worker(i) for i in range(concurrency))
    )

    elapsed = time.perf_counter() - started

    print()
    print("==== WRITE LOAD TEST ====")
    print(f"Tasks: {concurrency}")
    print(f"Elapsed: {elapsed:.3f}s")
    print(f"Writes/sec: {concurrency / elapsed:.2f}")


async def verify_pool(db_pool: DatabasePool) -> None:
    pool = db_pool.get_pool()

    print()
    print("==== POOL HEALTH ====")
    print(f"Pool Size: {pool.get_size()}")
    print(f"Idle Size: {pool.get_idle_size()}")

    if pool.get_size() == pool.get_idle_size():
        print("Leak Check: PASS")
    else:
        print("Leak Check: FAIL")


async def verify_row_count() -> None:
    rows = await Query.table("test_users").get()

    print()
    print("==== DATA CHECK ====")
    print(f"Total Rows: {len(rows)}")


async def main() -> None:
    # 1. Bootstrap the application natively (injects config, container, router, and boots providers)
    app = create_app(
        providers=[
            AppServiceProvider,
            DatabaseServiceProvider,
        ]
    )

    # 2. Resolve the DatabasePool to control its async execution lifecycle
    db_pool = app.container.resolve(DatabasePool)

    await db_pool.init()
    
    await warm_pool(db_pool)
    await create_table(db_pool)
    await seed_data()
    
    await rollback_test()
    await nested_transaction_test()
    
    await load_test_reads()
    await load_test_writes()
    
    await verify_row_count()
    await verify_pool(db_pool)
    
    await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())