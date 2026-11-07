from __future__ import annotations

from typing import Any

import pytest

from swiftpy.core.application import Application
from swiftpy.core.config import Config
from swiftpy.core.container import set_container
from swiftpy.core.container_exceptions import ResolutionError
from swiftpy.database.orm.model import (
    _MODEL_REGISTRY,
    Model,
    restore_model_registry,
    snapshot_model_registry,
)
from swiftpy.database.pool import DatabasePool
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.events.redis_bus import RedisEventBus
from swiftpy.helpers.helper import cache, dispatcher, queue
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.websockets.manager import WebSocketManager


class SingletonEventWidget(Model):
    __table__ = "singleton_event_widgets"
    id: int
    label: str


def test_two_applications_do_not_share_singletons() -> None:
    app1 = Application(config={"app": {"name": "AppOne", "debug": True}})
    app2 = Application(config={"app": {"name": "AppTwo", "debug": True}})

    # Config isolation
    assert app1.config is not app2.config
    assert app1.config.get("app.name") == "AppOne"
    assert app2.config.get("app.name") == "AppTwo"

    # Constructing a standalone Config does NOT overwrite app2's active Config
    standalone_cfg = Config({"app": {"name": "Standalone"}})
    assert standalone_cfg.get("app.name") == "Standalone"
    set_container(app1.container)
    assert Config.get_instance() is app1.config
    set_container(app2.container)
    assert Config.get_instance() is app2.config

    # WebSocketManager isolation
    assert app1.websocket_manager is not app2.websocket_manager
    set_container(app1.container)
    assert WebSocketManager.get_instance() is app1.websocket_manager
    set_container(app2.container)
    assert WebSocketManager.get_instance() is app2.websocket_manager

    # EventDispatcher isolation
    disp1 = app1.container.resolve(EventDispatcher)
    disp2 = app2.container.resolve(EventDispatcher)
    assert disp1 is not disp2
    set_container(app1.container)
    assert EventDispatcher.get_instance() is disp1
    set_container(app2.container)
    assert EventDispatcher.get_instance() is disp2

    # QueueDispatcher isolation
    q1 = app1.container.resolve(QueueDispatcher)
    q2 = app2.container.resolve(QueueDispatcher)
    assert q1 is not q2
    set_container(app1.container)
    assert QueueDispatcher.get_instance() is q1
    set_container(app2.container)
    assert QueueDispatcher.get_instance() is q2

    # RedisEventBus isolation
    bus1 = app1.container.resolve(RedisEventBus)
    bus2 = app2.container.resolve(RedisEventBus)
    assert bus1 is not bus2
    assert bus1.dispatcher is disp1
    assert bus2.dispatcher is disp2


def test_helpers_raise_resolution_error_when_provider_not_registered() -> None:
    app = Application(config={}, auto_discover_providers=False)
    set_container(app.container)

    with pytest.raises(ResolutionError, match="EventDispatcher"):
        dispatcher()

    with pytest.raises(ResolutionError, match="QueueDispatcher"):
        queue()

    with pytest.raises(ResolutionError, match="Cache"):
        cache()


@pytest.mark.asyncio
async def test_model_events_route_to_active_application_dispatcher_only(
    ensure_db_pool: DatabasePool,
) -> None:
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP TABLE IF EXISTS singleton_event_widgets CASCADE;
            CREATE TABLE singleton_event_widgets (
                id SERIAL PRIMARY KEY,
                label TEXT NOT NULL
            );
            """
        )

    app1 = Application()
    app1.container.instance(DatabasePool, ensure_db_pool)

    app2 = Application()
    app2.container.instance(DatabasePool, ensure_db_pool)

    events_app1: list[str] = []
    events_app2: list[str] = []

    app1.container.resolve(EventDispatcher).listen(
        "singleton_event_widget.*",
        lambda event_name, payload: events_app1.append(f"{event_name}:{payload.label}"),
    )
    app2.container.resolve(EventDispatcher).listen(
        "singleton_event_widget.*",
        lambda event_name, payload: events_app2.append(f"{event_name}:{payload.label}"),
    )

    try:
        # Execute model lifecycle under app1
        set_container(app1.container)
        w1 = await SingletonEventWidget.create(label="from_app1")
        w1.label = "from_app1_updated"
        await w1.save()
        await w1.delete()

        assert events_app1 == [
            "singleton_event_widget.created:from_app1",
            "singleton_event_widget.updated:from_app1_updated",
            "singleton_event_widget.deleted:from_app1_updated",
        ]
        assert events_app2 == []

        # Execute model lifecycle under app2
        set_container(app2.container)
        await SingletonEventWidget.create(label="from_app2")
        assert events_app2 == ["singleton_event_widget.created:from_app2"]
        assert len(events_app1) == 3
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS singleton_event_widgets CASCADE;")


def test_model_registry_snapshot_and_restore() -> None:
    snap = snapshot_model_registry()
    try:

        class EphemeralTestModel(Model):
            __table__ = "ephemeral_test_models"
            id: int

        assert "EphemeralTestModel" in _MODEL_REGISTRY
    finally:
        restore_model_registry(snap)

    assert "EphemeralTestModel" not in _MODEL_REGISTRY
    _ = Any
