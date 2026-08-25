import asyncio

import pytest

from swiftpy.core.container import Container
from swiftpy.core.context import Context


class ScopedDatabaseConnection:
    pass


@pytest.mark.asyncio
async def test_concurrent_scoped_resolution_isolation():
    container = Container()
    container.scoped(ScopedDatabaseConnection, ScopedDatabaseConnection)

    async def worker(
        index: int,
    ) -> tuple[ScopedDatabaseConnection, ScopedDatabaseConnection]:
        # Reset context boundary per request/task execution
        Context.clear()

        # Resolve the same scoped dependency twice in the current task
        conn_1 = container.resolve(ScopedDatabaseConnection)
        conn_2 = container.resolve(ScopedDatabaseConnection)

        # Confirm identity matches inside the same task execution scope
        assert conn_1 is conn_2
        return conn_1, conn_2

    # Execute 200 tasks concurrently on the event loop
    tasks = [worker(i) for i in range(200)]
    results = await asyncio.gather(*tasks)

    # 1. Verify 200 tasks succeeded
    assert len(results) == 200

    # 2. Collect primary instances from each task
    first_instances = [r[0] for r in results]

    # 3. Assert every single task received its own unique memory instance ID
    unique_ids = {id(inst) for inst in first_instances}
    assert len(unique_ids) == 200
