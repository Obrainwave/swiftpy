from __future__ import annotations

import asyncio
from typing import Any

import pytest

from swiftpy.core.application import Application
from swiftpy.core.context import Context
from swiftpy.core.providers.base import ServiceProvider
from swiftpy.http.response import json_response
from swiftpy.testing.http_client import TestClient
from swiftpy.websockets.connection import (
    WebSocketConnection,
    WebSocketDisconnect,
)


@pytest.mark.asyncio
async def test_http_and_websocket_route_isolation_and_405_allow_header() -> None:
    """
    Verify routing isolation when HTTP and WebSocket routes share the same path.
    HTTP 405 Method Not Allowed must only include HTTP methods in 'Allow', never 'WEBSOCKET'.
    """
    app = Application()

    @app.get("/api/channel")
    async def get_channel() -> Any:
        return json_response({"status": "http ok"})

    @app.websocket("/api/channel")
    async def ws_channel(ws: WebSocketConnection) -> None:
        await ws.accept()
        await ws.send_text("ws ok")

    client = TestClient(app)

    # 1. Valid HTTP GET request succeeds
    res_get = await client.get("/api/channel")
    assert res_get.status_code == 200
    assert res_get.json() == {"status": "http ok"}

    # 2. HTTP POST receives 405 Method Not Allowed
    res_post = await client.post("/api/channel", json={})
    assert res_post.status_code == 405
    allow_header = res_post.headers.get("allow", "")
    assert "GET" in allow_header
    assert "WEBSOCKET" not in allow_header

    # 3. WebSocket connection succeeds on the same path
    async with client.websocket_connect("/api/channel") as ws:
        msg = await ws.receive_text()
        assert msg == "ws ok"


@pytest.mark.asyncio
async def test_websocket_context_cleared_on_not_found_and_invalid_param() -> None:
    """
    Verify that Context.clear() is guaranteed to execute even when
    WebSocket requests are rejected due to 404 Route Not Found or 1002 Path Param Coercion Error.
    """
    app = Application()

    @app.websocket("/ws/users/{user_id}")
    async def user_ws(ws: WebSocketConnection, user_id: int) -> None:
        await ws.accept()

    client = TestClient(app)

    # 1. 404 Route Not Found
    with pytest.raises(WebSocketDisconnect) as exc_404:
        async with client.websocket_connect("/ws/nonexistent"):
            pass
    assert exc_404.value.code == 1000
    assert Context.get("websocket") is None

    # 2. 1002 Invalid Path Param Coercion
    with pytest.raises(WebSocketDisconnect) as exc_1002:
        async with client.websocket_connect("/ws/users/invalid_number"):
            pass
    assert exc_1002.value.code == 1002
    assert Context.get("websocket") is None


@pytest.mark.asyncio
async def test_asgi_lifespan_lifecycle() -> None:
    """
    Verify application handles standard ASGI lifespan startup and shutdown messages.
    """
    app = Application()
    assert not app._booted

    to_app_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    from_app_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    scope: dict[str, Any] = {"type": "lifespan", "asgi": {"version": "3.0"}}

    async def receive() -> dict[str, Any]:
        return await to_app_queue.get()

    async def send(message: dict[str, Any]) -> None:
        await from_app_queue.put(message)

    lifespan_task = asyncio.create_task(app(scope, receive, send))

    try:
        # 1. Send startup
        await to_app_queue.put({"type": "lifespan.startup"})
        startup_res = await asyncio.wait_for(from_app_queue.get(), timeout=10.0)
        assert startup_res["type"] == "lifespan.startup.complete"
        assert app._booted is True

        # 2. Send shutdown
        await to_app_queue.put({"type": "lifespan.shutdown"})
        shutdown_res = await asyncio.wait_for(from_app_queue.get(), timeout=10.0)
        assert shutdown_res["type"] == "lifespan.shutdown.complete"

        await asyncio.wait_for(lifespan_task, timeout=10.0)
    finally:
        if not lifespan_task.done():
            lifespan_task.cancel()
            await asyncio.gather(lifespan_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_asgi_lifespan_startup_failure_reporting() -> None:
    """
    Verify application reports lifespan.startup.failed when boot fails.
    """
    class ExplodingProvider(ServiceProvider):
        def register(self) -> None:
            pass

        def boot(self) -> None:
            raise RuntimeError("Database connection unreachable at startup")

    app = Application(providers=[ExplodingProvider])

    to_app_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    from_app_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    scope: dict[str, Any] = {"type": "lifespan", "asgi": {"version": "3.0"}}

    async def receive() -> dict[str, Any]:
        return await to_app_queue.get()

    async def send(message: dict[str, Any]) -> None:
        await from_app_queue.put(message)

    lifespan_task = asyncio.create_task(app(scope, receive, send))

    try:
        await to_app_queue.put({"type": "lifespan.startup"})
        res = await asyncio.wait_for(from_app_queue.get(), timeout=2.0)
        assert res["type"] == "lifespan.startup.failed"
        assert "Database connection unreachable" in res.get("message", "")
        await asyncio.wait_for(lifespan_task, timeout=2.0)
    finally:
        if not lifespan_task.done():
            lifespan_task.cancel()
            await asyncio.gather(lifespan_task, return_exceptions=True)
