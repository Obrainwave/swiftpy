from __future__ import annotations

import asyncio
from typing import Any

import pytest

from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.helpers.helper import dispatcher, event


@pytest.fixture(autouse=True)
def reset_dispatcher() -> None:
    """Reset the global dispatcher singleton before each test."""
    disp = EventDispatcher()
    EventDispatcher.set_instance(disp)


async def test_dispatcher_sync_and_async_listeners() -> None:
    disp = EventDispatcher()
    calls: list[str] = []

    def sync_listener(payload: Any) -> str:
        calls.append(f"sync:{payload}")
        return "sync_result"

    async def async_listener(payload: Any) -> str:
        await asyncio.sleep(0.01)
        calls.append(f"async:{payload}")
        return "async_result"

    disp.listen("user.login", sync_listener)
    disp.listen("user.login", async_listener)

    assert disp.has_listeners("user.login") is True
    assert disp.has_listeners("user.logout") is False

    results = await disp.dispatch("user.login", "john_doe")

    assert calls == ["sync:john_doe", "async:john_doe"]
    assert results == ["sync_result", "async_result"]


async def test_dispatcher_execution_order() -> None:
    disp = EventDispatcher()
    execution_order: list[int] = []

    disp.listen("order.created", lambda p: execution_order.append(1))
    disp.listen("order.created", lambda p: execution_order.append(2))
    disp.listen("order.created", lambda p: execution_order.append(3))

    await disp.dispatch("order.created", {"order_id": 42})
    assert execution_order == [1, 2, 3]


async def test_dispatcher_wildcard_listeners() -> None:
    disp = EventDispatcher()
    received_user_events: list[tuple[str, Any]] = []
    received_all_events: list[tuple[str, Any]] = []

    def on_user_wildcard(event_name: str, payload: Any) -> None:
        received_user_events.append((event_name, payload))

    def on_global_wildcard(event_name: str, payload: Any) -> None:
        received_all_events.append((event_name, payload))

    disp.listen("user.*", on_user_wildcard)
    disp.listen("*", on_global_wildcard)

    assert disp.has_listeners("user.created") is True
    assert disp.has_listeners("user.updated") is True
    assert disp.has_listeners("order.placed") is True

    await disp.dispatch("user.created", {"user_id": 1})
    await disp.dispatch("order.placed", {"order_id": 99})

    assert len(received_user_events) == 1
    assert received_user_events[0] == ("user.created", {"user_id": 1})

    assert len(received_all_events) == 2
    assert received_all_events[0] == ("user.created", {"user_id": 1})
    assert received_all_events[1] == ("order.placed", {"order_id": 99})


async def test_dispatcher_argument_variations() -> None:
    disp = EventDispatcher()
    zero_arg_called = False
    single_arg_val: Any = None
    multi_arg_val: tuple[str, Any] | None = None
    varargs_val: Any = None

    def zero_arg() -> None:
        nonlocal zero_arg_called
        zero_arg_called = True

    def single_arg(payload: Any) -> None:
        nonlocal single_arg_val
        single_arg_val = payload

    def multi_arg(event_name: str, payload: Any) -> None:
        nonlocal multi_arg_val
        multi_arg_val = (event_name, payload)

    def varargs(*args: Any) -> None:
        nonlocal varargs_val
        varargs_val = args

    disp.listen("custom.event", zero_arg)
    disp.listen("custom.event", single_arg)
    disp.listen("custom.event", multi_arg)
    disp.listen("custom.event", varargs)

    await disp.dispatch("custom.event", "test_payload")

    assert zero_arg_called is True
    assert single_arg_val == "test_payload"
    assert multi_arg_val == ("custom.event", "test_payload")
    assert varargs_val == ("test_payload",)


async def test_dispatcher_unsubscribe_and_forget() -> None:
    disp = EventDispatcher()
    received: list[str] = []

    def handler_a(p: Any) -> None:
        received.append("A")

    def handler_b(p: Any) -> None:
        received.append("B")

    disp.listen("item.added", handler_a)
    disp.listen("item.added", handler_b)
    disp.listen("item.*", handler_a)

    assert disp.has_listeners("item.added") is True

    # Unsubscribe handler_a from exact event
    removed = disp.unsubscribe("item.added", handler_a)
    assert removed is True

    await disp.dispatch("item.added")
    # handler_b exact + handler_a wildcard
    assert received == ["B", "A"]

    received.clear()
    # Forget only item.added
    disp.forget("item.added")
    await disp.dispatch("item.added")
    # Only wildcard remains
    assert received == ["A"]

    received.clear()
    # Forget all listeners
    disp.forget()
    assert disp.has_listeners("item.added") is False
    await disp.dispatch("item.added")
    assert received == []


async def test_dispatcher_empty_dispatch() -> None:
    disp = EventDispatcher()
    results = await disp.dispatch("nonexistent.event", {"dummy": 123})
    assert results == []


async def test_dispatcher_global_helpers() -> None:
    calls: list[Any] = []

    disp = dispatcher()
    disp.listen("notification.sent", lambda p: calls.append(p))

    results = await event("notification.sent", {"msg": "hello"})

    assert calls == [{"msg": "hello"}]
    assert len(results) == 1


async def test_dispatcher_sync_handler_exception_fail_fast() -> None:
    disp = EventDispatcher()
    log: list[str] = []

    def handler_1(p: Any) -> None:
        log.append("step_1")

    def failing_handler(p: Any) -> None:
        log.append("failing")
        raise ValueError("Synchronous handler failed")

    def handler_3(p: Any) -> None:
        log.append("step_3")

    disp.listen("order.placed", handler_1)
    disp.listen("order.placed", failing_handler)
    disp.listen("order.placed", handler_3)

    with pytest.raises(ValueError, match="Synchronous handler failed"):
        await disp.dispatch("order.placed", {"id": 101})

    # Handler 1 executed, failing handler ran, Handler 3 was NOT executed
    assert log == ["step_1", "failing"]

    # Internal state remains healthy and can dispatch subsequent events
    log.clear()
    disp.listen("order.shipped", handler_1)
    await disp.dispatch("order.shipped", {"id": 101})
    assert log == ["step_1"]


async def test_dispatcher_async_handler_exception_fail_fast() -> None:
    disp = EventDispatcher()
    log: list[str] = []

    async def async_handler_1(p: Any) -> None:
        log.append("async_1")

    async def async_failing_handler(p: Any) -> None:
        log.append("async_failing")
        raise RuntimeError("Async handler failed")

    async def async_handler_3(p: Any) -> None:
        log.append("async_3")

    disp.listen("user.registered", async_handler_1)
    disp.listen("user.registered", async_failing_handler)
    disp.listen("user.registered", async_handler_3)

    with pytest.raises(RuntimeError, match="Async handler failed"):
        await disp.dispatch("user.registered", {"user_id": 99})

    assert log == ["async_1", "async_failing"]


async def test_dispatcher_cancellation_propagation() -> None:
    disp = EventDispatcher()

    async def cancelled_handler(p: Any) -> None:
        raise asyncio.CancelledError("Handler was cancelled")

    disp.listen("job.run", cancelled_handler)

    with pytest.raises(asyncio.CancelledError):
        await disp.dispatch("job.run")


async def test_dispatcher_global_registration_ordering() -> None:
    disp = EventDispatcher()
    execution_sequence: list[str] = []

    disp.listen("user.created", lambda p: execution_sequence.append("exact_1"))
    disp.listen("user.*", lambda p: execution_sequence.append("wildcard_user"))
    disp.listen("*", lambda p: execution_sequence.append("global_star"))
    disp.listen("user.created", lambda p: execution_sequence.append("exact_2"))

    await disp.dispatch("user.created", {"id": 1})

    # Exact global registration order preserved across exact and wildcard listeners
    assert execution_sequence == ["exact_1", "wildcard_user", "global_star", "exact_2"]


async def test_dispatcher_segment_scoped_wildcards() -> None:
    disp = EventDispatcher()
    events_a: list[str] = []
    events_b: list[str] = []
    events_c: list[str] = []

    # user.* matches single-segment child (e.g. user.created)
    disp.listen("user.*", lambda e, p: events_a.append(e))
    # *.created matches single-segment parent (e.g. user.created, order.created)
    disp.listen("*.created", lambda e, p: events_b.append(e))
    # user.** matches recursive multi-segment (e.g. user.profile.created)
    disp.listen("user.**", lambda e, p: events_c.append(e))

    await disp.dispatch("user.created")
    assert "user.created" in events_a
    assert "user.created" in events_b
    assert "user.created" in events_c

    await disp.dispatch("order.created")
    assert "order.created" not in events_a
    assert "order.created" in events_b
    assert "order.created" not in events_c

    # user.profile.created has two child segments:
    # user.* should NOT match
    # *.created should NOT match
    # user.** SHOULD match
    await disp.dispatch("user.profile.created")
    assert "user.profile.created" not in events_a
    assert "user.profile.created" not in events_b
    assert "user.profile.created" in events_c


async def test_dispatcher_complex_callable_signatures() -> None:
    disp = EventDispatcher()
    results_map: dict[str, Any] = {}

    class CallableListener:
        def __call__(self, payload: Any) -> str:
            results_map["class_callable"] = payload
            return "from_class"

    def default_arg_fn(payload: Any, flag: bool = True) -> bool:
        results_map["default_arg"] = (payload, flag)
        return flag

    def keyword_only_fn(*, payload: Any = None) -> Any:
        results_map["keyword_only"] = payload
        return payload

    disp.listen("test.complex", CallableListener())
    disp.listen("test.complex", default_arg_fn)
    disp.listen("test.complex", keyword_only_fn)

    results = await disp.dispatch("test.complex", "sample_payload")

    assert results == ["from_class", True, "sample_payload"]
    assert results_map["class_callable"] == "sample_payload"
    assert results_map["default_arg"] == ("sample_payload", True)
    assert results_map["keyword_only"] == "sample_payload"
