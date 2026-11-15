from __future__ import annotations

import asyncio
import warnings

import pytest

from swiftpy.auth.token_blacklist import TokenBlacklist
from swiftpy.cache.cache import Cache
from swiftpy.core.application import Application
from swiftpy.core.providers.base import ServiceProvider
from swiftpy.database.pool import DatabasePool
from swiftpy.events.redis_bus import RedisEventBus
from swiftpy.http.response import Response, json_response
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.testing.http_client import TestClient
from swiftpy.websockets.connection import WebSocketConnection, WebSocketDisconnect


@pytest.mark.asyncio
async def test_lifespan_provider_startup_and_shutdown_ordering() -> None:
    events: list[str] = []

    class ProviderA(ServiceProvider):
        def register(self) -> None:
            events.append("register:A")

        def boot(self) -> None:
            events.append("boot:A")

        async def startup(self) -> None:
            events.append("startup:A")

        async def shutdown(self) -> None:
            events.append("shutdown:A")

    class ProviderB(ServiceProvider):
        def register(self) -> None:
            events.append("register:B")

        def boot(self) -> None:
            events.append("boot:B")

        async def startup(self) -> None:
            events.append("startup:B")

        async def shutdown(self) -> None:
            events.append("shutdown:B")

    app = Application(
        config={},
        auto_discover_providers=False,
        providers=[ProviderA, ProviderB],
    )

    @app.get("/ping")
    async def ping() -> Response:
        events.append("handle:ping")
        return json_response({"ok": True})

    async with TestClient(app) as client:
        res = await client.get("/ping")
        res.assert_ok()

    assert events == [
        "register:A",
        "register:B",
        "boot:A",
        "boot:B",
        "startup:A",
        "startup:B",
        "handle:ping",
        "shutdown:B",
        "shutdown:A",
    ]


@pytest.mark.asyncio
async def test_lifespan_startup_failure_rolls_back_started_providers() -> None:
    events: list[str] = []

    class Provider1(ServiceProvider):
        def register(self) -> None:
            pass

        async def startup(self) -> None:
            events.append("startup:1")

        async def shutdown(self) -> None:
            events.append("shutdown:1")

    class Provider2Fails(ServiceProvider):
        def register(self) -> None:
            pass

        async def startup(self) -> None:
            events.append("startup:2")
            raise RuntimeError("Provider 2 failed to start")

        async def shutdown(self) -> None:
            events.append("shutdown:2")

    class Provider3NeverStarts(ServiceProvider):
        def register(self) -> None:
            pass

        async def startup(self) -> None:
            events.append("startup:3")

        async def shutdown(self) -> None:
            events.append("shutdown:3")

    app = Application(
        config={},
        auto_discover_providers=False,
        providers=[Provider1, Provider2Fails, Provider3NeverStarts],
    )

    with pytest.raises(RuntimeError, match="Provider 2 failed to start"):
        async with TestClient(app):
            pass

    assert events == [
        "startup:1",
        "startup:2",
        "shutdown:1",
    ]


@pytest.mark.asyncio
async def test_lifespan_shutdown_continues_when_one_provider_raises() -> None:
    events: list[str] = []

    class Provider1(ServiceProvider):
        def register(self) -> None:
            pass

        async def startup(self) -> None:
            events.append("startup:1")

        async def shutdown(self) -> None:
            events.append("shutdown:1")

    class Provider2FailsShutdown(ServiceProvider):
        def register(self) -> None:
            pass

        async def startup(self) -> None:
            events.append("startup:2")

        async def shutdown(self) -> None:
            events.append("shutdown:2")
            raise RuntimeError("Shutdown error in Provider 2")

    app = Application(
        config={},
        auto_discover_providers=False,
        providers=[Provider1, Provider2FailsShutdown],
    )

    async with TestClient(app):
        pass

    assert events == [
        "startup:1",
        "startup:2",
        "shutdown:2",
        "shutdown:1",
    ]


@pytest.mark.asyncio
async def test_lifespan_initializes_and_closes_db_pool_redis_bus_and_websockets() -> None:
    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always", ResourceWarning)

        app = Application()

        @app.get("/db-check")
        async def db_check(pool: DatabasePool) -> Response:
            async with pool.get_pool().acquire() as conn:
                val = await conn.fetchval("SELECT 1")
            return json_response({"val": val})

        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocketConnection) -> None:
            await websocket.accept()
            await app.websocket_manager.connect(websocket)
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass

        async with TestClient(app) as client:
            # 1. DatabasePool was initialized automatically by lifespan startup
            pool = app.container.resolve(DatabasePool)
            assert pool._pool is not None

            res = await client.get("/db-check")
            res.assert_ok()
            assert res.json() == {"val": 1}

            # 2. Start RedisEventBus, Cache, QueueDispatcher, and TokenBlacklist
            bus = app.container.resolve(RedisEventBus)
            await bus.start_listening()
            await bus.wait_until_ready()
            assert bus._listener_task is not None
            assert not bus._listener_task.done()

            cache_mgr = app.container.resolve(Cache)
            await cache_mgr.driver("redis").get("nonexistent_lifespan_key")

            queue_disp = app.container.resolve(QueueDispatcher)
            await queue_disp.get_client()

            blacklist = app.container.resolve(TokenBlacklist)
            await blacklist.get_client()

            # 3. Open a WebSocket connection and keep it active until shutdown
            ws_session = await client.websocket_connect("/ws")
            await asyncio.sleep(0.02)
            assert app.websocket_manager.active_count >= 1

        # After exiting TestClient context (lifespan.shutdown):
        # - DatabasePool is closed
        assert pool._pool is None
        # - RedisEventBus listener task is stopped and client closed
        assert bus._listener_task is None
        assert bus._client is None
        # - Cache drivers, QueueDispatcher, and TokenBlacklist clients are closed
        assert cache_mgr._drivers == {}
        assert queue_disp._client is None
        assert blacklist._client is None
        # - Active WebSocket connection received close code 1001
        with pytest.raises(WebSocketDisconnect) as exc_info:
            await ws_session.receive_raw(timeout=1.0)
        assert exc_info.value.code == 1001

        resource_warnings = [
            w for w in caught_warnings if issubclass(w.category, ResourceWarning)
        ]
        assert resource_warnings == []


@pytest.mark.asyncio
async def test_app_without_database_config_starts_and_stops_cleanly() -> None:
    app = Application(
        config={"app": {"name": "StatelessApp", "env": "testing", "debug": True}},
        auto_discover_providers=False,
    )

    @app.get("/health")
    async def health() -> Response:
        return json_response({"status": "ok"})

    async with TestClient(app) as client:
        res = await client.get("/health")
        res.assert_ok()
        assert res.json() == {"status": "ok"}
        assert not app.container.has(DatabasePool)
