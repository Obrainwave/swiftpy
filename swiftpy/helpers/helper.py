from __future__ import annotations

import os
from typing import Any

from swiftpy.cache.cache import Cache
from swiftpy.core.bootstrap import get_container
from swiftpy.core.config import Config
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.job import Job
from swiftpy.support.strings import pluralize, snake_case
from swiftpy.websockets.manager import WebSocketManager


def app() -> Any:
    """Return the application container."""
    return get_container()


def cache(driver_name: str | None = None) -> Any:
    """Retrieve the application Cache manager or a specific driver."""

    try:
        c = get_container().resolve(Cache)
    except RuntimeError:
        c = Cache()

    if driver_name is not None:
        return c.driver(driver_name)
    return c


def config(key: str | None = None, default: Any = None) -> Any:
    """Retrieve configuration values."""
    try:
        cfg = get_container().resolve(Config)
    except RuntimeError:
        cfg = Config.get_instance()

    if key is None:
        return cfg

    return cfg.get(key, default)


def container() -> Any:
    """Return the IoC container."""
    return get_container()


def resolve(target: type[Any]) -> Any:
    """Resolve a dependency from the container."""
    return get_container().resolve(target)


def env(key: str, default: Any = None) -> Any:
    """Retrieve environment variables."""
    return os.getenv(key, default)



def dispatcher() -> EventDispatcher:
    """Retrieve the application EventDispatcher."""
    try:
        disp: EventDispatcher = get_container().resolve(EventDispatcher)
        return disp
    except RuntimeError:
        return EventDispatcher.get_instance()


async def event(event_name: str, payload: Any = None) -> list[Any]:
    """Dispatch an event through the event dispatcher."""
    d = dispatcher()
    return await d.dispatch(event_name, payload)


def queue() -> QueueDispatcher:
    """Retrieve the application QueueDispatcher."""
    try:
        q: QueueDispatcher = get_container().resolve(QueueDispatcher)
        return q
    except RuntimeError:
        return QueueDispatcher.get_instance()


async def dispatch(
    job: Job,
    queue_name: str | None = None,
    delay: float = 0,
) -> str:
    """Dispatch a job onto the application queue."""
    q = queue()
    return await q.push(job, queue=queue_name, delay=delay)


def websocket_manager() -> WebSocketManager:
    """Retrieve the application WebSocketManager."""
    try:
        wm: WebSocketManager = get_container().resolve(WebSocketManager)
        return wm
    except RuntimeError:
        return WebSocketManager.get_instance()


def ws_manager() -> WebSocketManager:
    """Alias for websocket_manager()."""
    return websocket_manager()


__all__ = [
    "app",
    "cache",
    "config",
    "container",
    "dispatch",
    "dispatcher",
    "env",
    "event",
    "pluralize",
    "queue",
    "resolve",
    "snake_case",
    "websocket_manager",
    "ws_manager",
]
