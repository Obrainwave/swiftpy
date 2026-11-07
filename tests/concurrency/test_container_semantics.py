from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any, Self

import pytest

from swiftpy.core.application import Application
from swiftpy.core.container import Container
from swiftpy.core.container_exceptions import ResolutionError
from swiftpy.http.response import Response, json_response
from swiftpy.testing.http_client import TestClient


class BaseRepo:
    def name(self) -> str:
        return "real"


class FakeRepo(BaseRepo):
    def name(self) -> str:
        return "fake"


class AnotherFakeRepo(BaseRepo):
    def name(self) -> str:
        return "another_fake"


class BrokenForwardRefService:
    def __init__(self, dep: Any) -> None:
        self.dep = dep


BrokenForwardRefService.__init__.__annotations__["dep"] = "NonExistentForwardType"


class AsyncClosableResource:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        await asyncio.sleep(0)
        self.closed = True


class AsyncContextScopedResource:
    def __init__(self) -> None:
        self.exited = False
        self.exc_type: type[BaseException] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await asyncio.sleep(0)
        self.exited = True
        self.exc_type = exc_type


def test_rebind_invalidates_cached_singleton_and_scoped_instances() -> None:
    container = Container()
    container.singleton(BaseRepo, BaseRepo)

    first = container.resolve(BaseRepo)
    assert isinstance(first, BaseRepo)
    assert first.name() == "real"

    # Re-bind with a transient/singleton override after first resolution (D-7)
    container.bind(BaseRepo, FakeRepo)
    second = container.resolve(BaseRepo)
    assert isinstance(second, FakeRepo)
    assert second.name() == "fake"

    # Re-bind as singleton
    container.singleton(BaseRepo, AnotherFakeRepo)
    third = container.resolve(BaseRepo)
    assert isinstance(third, AnotherFakeRepo)
    assert third.name() == "another_fake"

    # Re-bind as scoped and then re-bind again within same task
    container.scoped(BaseRepo, FakeRepo)
    scoped_first = container.resolve(BaseRepo)
    assert scoped_first.name() == "fake"

    container.scoped(BaseRepo, AnotherFakeRepo)
    scoped_second = container.resolve(BaseRepo)
    assert scoped_second.name() == "another_fake"


def test_none_singleton_factory_is_cached_via_sentinel() -> None:
    container = Container()
    calls = 0

    class OptionalConfig:
        pass

    def none_factory(_: Container) -> Any:
        nonlocal calls
        calls += 1
        return None

    container.singleton(OptionalConfig, none_factory)

    res1 = container.resolve(OptionalConfig)
    res2 = container.resolve(OptionalConfig)
    res3 = container.resolve(OptionalConfig)

    assert res1 is None
    assert res2 is None
    assert res3 is None
    assert calls == 1


def test_unresolved_type_hint_wrapped_as_resolution_error_with_class_name() -> None:
    container = Container()

    with pytest.raises(ResolutionError, match="BrokenForwardRefService"):
        container.resolve(BrokenForwardRefService)


@pytest.mark.asyncio
async def test_scoped_cache_is_not_inherited_by_child_tasks() -> None:
    container = Container()
    container.scoped(BaseRepo, BaseRepo)

    # Resolve in parent task first (populating parent task's Context)
    parent_inst_1 = container.resolve(BaseRepo)
    parent_inst_2 = container.resolve(BaseRepo)
    assert parent_inst_1 is parent_inst_2

    async def child_worker() -> tuple[BaseRepo, BaseRepo]:
        # Do NOT call Context.clear() here: child task inherits parent ContextVar
        c1 = container.resolve(BaseRepo)
        c2 = container.resolve(BaseRepo)
        assert c1 is c2
        return c1, c2

    child_a, _ = await asyncio.create_task(child_worker())
    child_b, _ = await asyncio.create_task(child_worker())

    # Child tasks must NOT inherit parent's scoped instance or share with each other
    assert child_a is not parent_inst_1
    assert child_b is not parent_inst_1
    assert child_a is not child_b

    # Parent task still sees its own cached scoped instance
    assert container.resolve(BaseRepo) is parent_inst_1


@pytest.mark.asyncio
async def test_scoped_disposal_on_normal_exit_exception_and_cancellation() -> None:
    container = Container()
    container.scoped(AsyncClosableResource)
    container.scoped(AsyncContextScopedResource)

    # 1. Normal scope exit disposes both aclose() and __aexit__()
    async with container.scope():
        r_close = container.resolve(AsyncClosableResource)
        r_ctx = container.resolve(AsyncContextScopedResource)
        assert not r_close.closed
        assert not r_ctx.exited

    assert r_close.closed is True
    assert r_ctx.exited is True
    assert r_ctx.exc_type is None

    # 2. Exception inside scope still disposes both and passes exc_type to __aexit__
    r_close_exc: AsyncClosableResource | None = None
    r_ctx_exc: AsyncContextScopedResource | None = None
    with pytest.raises(ValueError, match="boom"):
        async with container.scope():
            r_close_exc = container.resolve(AsyncClosableResource)
            r_ctx_exc = container.resolve(AsyncContextScopedResource)
            raise ValueError("boom")

    assert r_close_exc is not None and r_close_exc.closed is True
    assert r_ctx_exc is not None and r_ctx_exc.exited is True
    assert r_ctx_exc.exc_type is ValueError

    # 3. Task cancellation inside scope still disposes scoped resources
    started = asyncio.Event()
    r_close_cancel: AsyncClosableResource | None = None

    async def cancellable_worker() -> None:
        nonlocal r_close_cancel
        async with container.scope():
            r_close_cancel = container.resolve(AsyncClosableResource)
            started.set()
            await asyncio.sleep(10.0)

    task = asyncio.create_task(cancellable_worker())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert r_close_cancel is not None and r_close_cancel.closed is True


@pytest.mark.asyncio
async def test_http_request_disposes_scoped_dependencies_after_handler_and_on_error() -> None:
    app = Application(config={}, auto_discover_providers=False)
    app.container.scoped(AsyncClosableResource)

    created: list[AsyncClosableResource] = []

    @app.get("/ok")
    async def ok_route(res: AsyncClosableResource) -> Response:
        created.append(res)
        assert not res.closed
        return json_response({"closed_in_handler": res.closed})

    @app.get("/fail")
    async def fail_route(res: AsyncClosableResource) -> Response:
        created.append(res)
        raise RuntimeError("handler failed")

    client = TestClient(app)
    resp1 = await client.get("/ok")
    resp1.assert_ok()
    assert len(created) == 1
    assert created[0].closed is True

    resp2 = await client.get("/fail")
    resp2.assert_server_error()
    assert len(created) == 2
    assert created[1].closed is True
    assert created[0] is not created[1]
