import asyncio
import uuid

import pytest

from swiftpy.core.context import Context


@pytest.mark.asyncio
async def test_request_task_isolation():
    Context.clear()

    async def task_a():
        Context.set("req_id", "A")
        await asyncio.sleep(0.01)
        assert Context.get("req_id") == "A"
        assert not Context.has("req_id_b")

    async def task_b():
        Context.set("req_id_b", "B")
        await asyncio.sleep(0.01)
        assert Context.get("req_id_b") == "B"
        assert not Context.has("req_id")

    # Run concurrently
    await asyncio.gather(task_a(), task_b())


@pytest.mark.asyncio
async def test_child_task_inheritance_and_isolation():
    Context.clear()
    Context.set("tenant", "acme")

    async def child_task():
        # Child inherits parent's context value
        assert Context.get("tenant") == "acme"
        # Child mutates its copy
        Context.set("tenant", "globex")
        assert Context.get("tenant") == "globex"

    task = asyncio.create_task(child_task())
    await task

    # Parent context MUST remain untouched by child's mutation
    assert Context.get("tenant") == "acme"


@pytest.mark.asyncio
async def test_isolation_under_load():
    """The actual Phase 1 gate: 500 concurrent tasks, zero leaks."""
    Context.clear()

    async def worker(i: int) -> bool:
        unique_val = f"user_{i}_{uuid.uuid4()}"
        Context.set("user_id", unique_val)
        await asyncio.sleep(0.001 * (i % 10))
        return Context.get("user_id") == unique_val

    results = await asyncio.gather(*(worker(i) for i in range(500)))
    failures = results.count(False)
    assert failures == 0, (
        f"{failures} of 500 tasks observed a leaked or overwritten context"
    )


@pytest.mark.asyncio
async def test_get_default_fallback():
    """Covers DEVLOG guarantee #3, currently unverified."""
    Context.clear()
    assert Context.get("never_set") is None
    assert Context.get("never_set", "fallback") == "fallback"

    Context.set("real_key", "real_value")
    assert Context.get("still_unset") is None
    assert Context.get("real_key") == "real_value"
