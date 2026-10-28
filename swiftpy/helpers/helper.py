from __future__ import annotations

import os
import re
from typing import Any

from swiftpy.cache.cache import Cache
from swiftpy.core.bootstrap import get_container
from swiftpy.core.config import Config
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.queue.job import Job
from swiftpy.websockets.manager import WebSocketManager

_UNCOUNTABLES = {
    "aircraft",
    "deer",
    "equipment",
    "fish",
    "information",
    "money",
    "police",
    "rice",
    "series",
    "sheep",
    "species",
}

_IRREGULARS = {
    "child": "children",
    "foot": "feet",
    "goose": "geese",
    "man": "men",
    "mouse": "mice",
    "ox": "oxen",
    "person": "people",
    "quiz": "quizzes",
    "tooth": "teeth",
    "woman": "women",
}

_PLURAL_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(quiz)$", re.IGNORECASE), r"\1zes"),
    (re.compile(r"^(ox)$", re.IGNORECASE), r"\1en"),
    (re.compile(r"([ml])ouse$", re.IGNORECASE), r"\1ice"),
    (re.compile(r"(matr|vert|ind)(?:ix|ex)$", re.IGNORECASE), r"\1ices"),
    (re.compile(r"(ax|test)is$", re.IGNORECASE), r"\1es"),
    (re.compile(r"sis$", re.IGNORECASE), "ses"),
    (re.compile(r"(x|ch|ss|sh)$", re.IGNORECASE), r"\1es"),
    (re.compile(r"([^aeiouy]|qu)y$", re.IGNORECASE), r"\1ies"),
    (re.compile(r"(?:([^f])fe|([lr])f)$", re.IGNORECASE), r"\1\2ves"),
    (re.compile(r"([ti])um$", re.IGNORECASE), r"\1a"),
    (re.compile(r"(buffal|tomat)o$", re.IGNORECASE), r"\1oes"),
    (re.compile(r"(bu)s$", re.IGNORECASE), r"\1ses"),
    (re.compile(r"(alias|status)$", re.IGNORECASE), r"\1es"),
    (re.compile(r"(octop|vir)us$", re.IGNORECASE), r"\1uses"),
    (re.compile(r"s$", re.IGNORECASE), "s"),
    (re.compile(r"$"), "s"),
]


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


def snake_case(name: str) -> str:
    """Convert PascalCase or camelCase to snake_case."""
    value = re.sub(
        r"(?<!^)(?=[A-Z])",
        "_",
        name,
    )

    return value.lower()


def pluralize(word: str) -> str:
    """Pluralize an English word or snake_cased identifier, preserving capitalization."""
    if not word:
        return word

    parts = word.split("_")
    last = parts[-1]
    last_lower = last.lower()

    if last_lower in _UNCOUNTABLES:
        plural_last = last
    elif last_lower in _IRREGULARS:
        replacement = _IRREGULARS[last_lower]
        if last.isupper():
            plural_last = replacement.upper()
        elif last.istitle():
            plural_last = replacement.title()
        else:
            plural_last = replacement
    else:
        for pattern, replacement in _PLURAL_RULES:
            if pattern.search(last):
                plural_last = pattern.sub(replacement, last)
                break
        else:
            plural_last = last + "s"

        if last.isupper():
            plural_last = plural_last.upper()
        elif last.istitle():
            plural_last = plural_last[0].upper() + plural_last[1:]

    parts[-1] = plural_last
    return "_".join(parts)


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
