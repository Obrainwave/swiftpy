import asyncio

import pytest

from swiftpy.core.container import Container
from swiftpy.core.context import Context


class ScopedDatabaseConnection:
    pass


@pytest.mark.asyncio
async def test_concurrent_scoped_resolution_isolation() -> None:
    """
    Scoped services should behave as:

    - Same instance within a task.
    - Different instance across tasks.
    """

    container = Container()

    container.scoped(
        ScopedDatabaseConnection,
        ScopedDatabaseConnection,
    )

    async def worker(
        index: int,
    ) -> tuple[
        ScopedDatabaseConnection,
        ScopedDatabaseConnection,
    ]:
        Context.clear()

        first = container.resolve(
            ScopedDatabaseConnection,
        )

        second = container.resolve(
            ScopedDatabaseConnection,
        )

        assert first is second

        return first, second

    results = await asyncio.gather(*[worker(i) for i in range(200)])

    assert len(results) == 200

    first_instances = [result[0] for result in results]

    unique_ids = {id(instance) for instance in first_instances}

    assert len(unique_ids) == 200


@pytest.mark.asyncio
async def test_scoped_resolution_reuses_instance_in_same_task() -> None:
    container = Container()

    container.scoped(
        ScopedDatabaseConnection,
        ScopedDatabaseConnection,
    )

    first = container.resolve(
        ScopedDatabaseConnection,
    )

    second = container.resolve(
        ScopedDatabaseConnection,
    )

    assert first is second


def test_singleton_resolution_returns_same_instance() -> None:
    container = Container()

    container.singleton(
        ScopedDatabaseConnection,
        ScopedDatabaseConnection,
    )

    first = container.resolve(
        ScopedDatabaseConnection,
    )

    second = container.resolve(
        ScopedDatabaseConnection,
    )

    assert first is second
