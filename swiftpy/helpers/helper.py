from __future__ import annotations

import os
from typing import Any

from swiftpy.cache.cache import Cache
from swiftpy.core.config import Config
from swiftpy.core.container import get_container
from swiftpy.core.container_exceptions import BindingNotFoundError, ResolutionError
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.filesystem.manager import FilesystemManager
from swiftpy.mail.manager import MailManager
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
        c_inst = get_container()
    except RuntimeError as exc:
        raise ResolutionError("Application container not bootstrapped") from exc

    if not c_inst.has(Cache):
        raise BindingNotFoundError(
            "Cache is not registered in the container. Ensure CacheServiceProvider is registered."
        )
    c = c_inst.resolve(Cache)

    if driver_name is not None:
        return c.driver(driver_name)
    return c


def storage(disk_name: str | None = None) -> Any:
    """Retrieve the application FilesystemManager or a specific StorageDisk."""
    try:
        c_inst = get_container()
        if c_inst.has(FilesystemManager):
            manager = c_inst.resolve(FilesystemManager)
        else:
            manager = FilesystemManager.get_instance()
    except RuntimeError:
        manager = FilesystemManager.get_instance()

    if disk_name is not None:
        return manager.disk(disk_name)
    return manager


def mail(mailer_name: str | None = None) -> Any:
    """Retrieve the application MailManager or a specific MailTransport."""
    try:
        c_inst = get_container()
        if c_inst.has(MailManager):
            manager = c_inst.resolve(MailManager)
        else:
            manager = MailManager.get_instance()
    except RuntimeError:
        manager = MailManager.get_instance()

    if mailer_name is not None:
        return manager.mailer(mailer_name)
    return manager


def config(key: str | None = None, default: Any = None) -> Any:
    """Retrieve configuration values."""
    try:
        c_inst = get_container()
        cfg = c_inst.resolve(Config) if c_inst.has(Config) else Config.get_instance()
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
        c_inst = get_container()
    except RuntimeError as exc:
        raise ResolutionError("Application container not bootstrapped") from exc

    if not c_inst.has(EventDispatcher):
        raise BindingNotFoundError(
            "EventDispatcher is not registered in the container. Ensure EventServiceProvider is registered."
        )
    disp: EventDispatcher = c_inst.resolve(EventDispatcher)
    return disp


async def event(event_name: str, payload: Any = None) -> list[Any]:
    """Dispatch an event through the event dispatcher."""
    d = dispatcher()
    return await d.dispatch(event_name, payload)


def queue() -> QueueDispatcher:
    """Retrieve the application QueueDispatcher."""
    try:
        c_inst = get_container()
    except RuntimeError as exc:
        raise ResolutionError("Application container not bootstrapped") from exc

    if not c_inst.has(QueueDispatcher):
        raise BindingNotFoundError(
            "QueueDispatcher is not registered in the container. Ensure QueueServiceProvider is registered."
        )
    q: QueueDispatcher = c_inst.resolve(QueueDispatcher)
    return q


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
        c_inst = get_container()
    except RuntimeError as exc:
        raise ResolutionError("Application container not bootstrapped") from exc

    if not c_inst.has(WebSocketManager):
        raise BindingNotFoundError(
            "WebSocketManager is not registered in the container."
        )
    wm: WebSocketManager = c_inst.resolve(WebSocketManager)
    return wm


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
    "mail",
    "pluralize",
    "queue",
    "resolve",
    "snake_case",
    "websocket_manager",
    "ws_manager",
]
