from __future__ import annotations

import asyncio
import time

from app.providers.database_service_provider import (
    DatabaseServiceProvider,
)

from swiftpy.core.bootstrap import create_app
from swiftpy.database.pool import DatabasePool
from swiftpy.database.query_builder import Query
from swiftpy.database.transaction import db


async def create_table(
    db_pool: DatabasePool,
) -> None:
    pool = db_pool.get_pool()

    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS test_users (
                id SERIAL PRIMARY KEY,
                name TEXT,
                active BOOLEAN DEFAULT TRUE
            )
            """
        )


async def worker() -> None:
    await Query.table("test_users").select("id").limit(1).get()

    async with db.transaction():
        await Query.table("test_users").where("active", True).limit(1).get()


async def main() -> None:
    app = create_app(
        providers=[
            DatabaseServiceProvider,
        ]
    )

    db_pool = app.container.resolve(DatabasePool)

    pool = await db_pool.init()

    await create_table(db_pool)

    concurrency = 1000

    started = time.perf_counter()

    await asyncio.gather(*[worker() for _ in range(concurrency)])

    elapsed = time.perf_counter() - started

    print()
    print("==== LOAD TEST ====")
    print(f"Tasks: {concurrency}")
    print(f"Elapsed: {elapsed:.3f}s")
    print(f"QPS: {(concurrency * 2) / elapsed:.2f}")
    print(f"Pool Size: {pool.get_size()}")
    print(f"Idle Size: {pool.get_idle_size()}")

    if pool.get_size() == pool.get_idle_size():
        print("Leak Check: PASS")
    else:
        print("Leak Check: FAIL")

    await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
