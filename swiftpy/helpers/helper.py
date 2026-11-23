from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any

from swiftpy.cache.cache import Cache
from swiftpy.core.config import Config, env, env_bool
from swiftpy.core.container import get_container
from swiftpy.core.container_exceptions import BindingNotFoundError, ResolutionError
from swiftpy.database.orm.model import Model
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.filesystem.manager import FilesystemManager
from swiftpy.filesystem.uploads.handlers import FormData
from swiftpy.http.request import Request
from swiftpy.mail.manager import MailManager
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.job import Job
from swiftpy.scheduling.scheduler import Scheduler
from swiftpy.support.hashing import (
    hash_password,
    hash_password_async,
    verify_password,
    verify_password_async,
)
from swiftpy.support.strings import pluralize, snake_case
from swiftpy.validation.form_request import FormRequest
from swiftpy.websockets.manager import WebSocketManager

_MISSING = object()


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


def scheduler() -> Scheduler:
    """Retrieve the active application Scheduler instance."""
    return Scheduler.get_instance()


def _resolve_isset_step(target: Any, key: Any) -> Any:
    """Resolve one lookup step for ``isset()``, returning ``_MISSING`` if absent."""
    if target is None or isinstance(
        target, (str, bytes, bytearray, memoryview, int, float, bool, complex)
    ):
        return _MISSING

    if isinstance(target, Config):
        if not isinstance(key, str):
            return _MISSING
        return target.get(key, _MISSING)

    if isinstance(target, FormData):
        if not isinstance(key, str):
            return _MISSING
        if key in target.fields_list:
            field_vals = target.fields_list[key]
            if not field_vals:
                return _MISSING
            return field_vals[0] if len(field_vals) == 1 else field_vals
        if key in target.files_list:
            file_vals = target.files_list[key]
            if not file_vals:
                return _MISSING
            return file_vals[0] if len(file_vals) == 1 else file_vals
        return _MISSING

    if isinstance(target, Request):
        if not isinstance(key, str):
            return _MISSING
        mapping = target._current_input_mapping()
        if key in mapping:
            return mapping[key]
        if target._form is not None:
            return _resolve_isset_step(target._form, key)
        return _MISSING

    if isinstance(target, FormRequest):
        if not isinstance(key, str):
            return _MISSING
        payload = (
            target.data
            if target.validation_result is not None or target.data
            else target.raw_data
        )
        if isinstance(payload, Mapping):
            return _resolve_isset_step(payload, key)
        return _MISSING

    if isinstance(target, Model):
        if not isinstance(key, str) or key.startswith("_"):
            return _MISSING
        loaded = getattr(target, "_loaded_relations", None)
        if isinstance(loaded, dict) and key in loaded:
            return loaded[key]
        static_attr = inspect.getattr_static(type(target), key, _MISSING)
        if (
            key in getattr(target, "__relations_config__", {})
            and static_attr is not _MISSING
        ):
            return _MISSING
        instance_dict = getattr(target, "__dict__", None)
        if isinstance(instance_dict, dict) and key in instance_dict:
            return instance_dict[key]
        if key in getattr(target, "__fields__", {}):
            return getattr(target, key, _MISSING)
        if isinstance(static_attr, property):
            return getattr(target, key, _MISSING)
        return _MISSING

    if isinstance(target, Mapping):
        try:
            if key in target:
                return target[key]
        except (KeyError, TypeError):
            return _MISSING
        return _MISSING

    if isinstance(target, AbstractSet):
        try:
            if key in target:
                return key
        except TypeError:
            return _MISSING
        return _MISSING

    if isinstance(target, Sequence):
        index: int | None = None
        if isinstance(key, int) and not isinstance(key, bool):
            index = key
        elif isinstance(key, str) and key.isascii() and key.isdigit():
            index = int(key)
        if index is not None and -len(target) <= index < len(target):
            return target[index]
        return _MISSING

    if not isinstance(key, str) or key.startswith("_"):
        return _MISSING
    instance_dict = getattr(target, "__dict__", None)
    if isinstance(instance_dict, dict) and key in instance_dict:
        return instance_dict[key]
    val = getattr(target, key, _MISSING)
    if val is _MISSING:
        return _MISSING
    if inspect.ismethod(val) or inspect.isbuiltin(val) or inspect.isfunction(val):
        return _MISSING
    return val


def _isset_single(data: Any, key: Any) -> bool:
    """Return True if a single key/path resolves in ``data`` to a non-None value."""
    direct = _resolve_isset_step(data, key)
    if direct is not _MISSING:
        return direct is not None

    if not isinstance(key, str) or "." not in key:
        return False

    segments = key.split(".")
    if any(seg == "" for seg in segments):
        return False

    current: Any = data
    for seg in segments:
        current = _resolve_isset_step(current, seg)
        if current is _MISSING:
            return False
    return current is not None


def isset(data: Any, *keys: Any) -> bool:
    """Determine if ``data`` (or every given key/index/attribute/dot-path) exists and is not None."""
    if data is None:
        return False
    if not keys:
        return True
    return all(_isset_single(data, key) for key in keys)


__all__ = [
    "app",
    "cache",
    "config",
    "container",
    "dispatch",
    "dispatcher",
    "env",
    "env_bool",
    "event",
    "hash_password",
    "hash_password_async",
    "isset",
    "mail",
    "pluralize",
    "queue",
    "resolve",
    "scheduler",
    "snake_case",
    "verify_password",
    "verify_password_async",
    "websocket_manager",
    "ws_manager",
]
