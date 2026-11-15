from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

# Use None as default to avoid shared mutable default reference
_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "swiftpy_context",
    default=None,
)

_WATCHERS_KEY = "__swiftpy_context_watchers__"


def _current_task_or_none() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


class Context:
    """
    Request-local context storage for SwiftPY.

    Backed by Python's contextvars.ContextVar with copy-on-write task isolation.
    """

    @staticmethod
    def _get_dict() -> dict[str, Any]:
        """Internal helper to fetch or initialize the request context dict."""
        ctx = _context.get()
        if ctx is None:
            ctx = {}
            _context.set(ctx)
        return ctx

    @staticmethod
    def _notify_watchers(
        ctx_dict: dict[str, Any] | None,
        key: str | None,
        value: Any,
    ) -> None:
        if not ctx_dict:
            return
        watchers_map = ctx_dict.get(_WATCHERS_KEY)
        if not isinstance(watchers_map, dict):
            return
        cur_task = _current_task_or_none()
        target_keys = list(watchers_map.keys()) if key is None else [key]
        for watch_key in target_keys:
            entries = watchers_map.get(watch_key)
            if not entries:
                continue
            for owner_task, cb_ref in entries:
                if owner_task is not cur_task:
                    continue
                cb = cb_ref() if isinstance(cb_ref, weakref.ref) else cb_ref
                if cb is not None:
                    cb(value)

    @staticmethod
    def watch(
        key: str,
        callback: weakref.ref[Callable[[Any], None]] | Callable[[Any], None],
    ) -> None:
        """Register a task-scoped watcher invoked when `key` changes or context clears."""
        current = Context._get_dict()
        raw_watchers = current.get(_WATCHERS_KEY)
        watchers_map: dict[str, tuple[tuple[Any, Any], ...]] = (
            dict(raw_watchers) if isinstance(raw_watchers, dict) else {}
        )
        cur_task = _current_task_or_none()
        existing = watchers_map.get(key, ())
        live_entries = [
            entry
            for entry in existing
            if not (isinstance(entry[1], weakref.ref) and entry[1]() is None)
        ]
        live_entries.append((cur_task, callback))
        watchers_map[key] = tuple(live_entries)

        new_ctx = current.copy()
        new_ctx[_WATCHERS_KEY] = watchers_map
        _context.set(new_ctx)

    @staticmethod
    def set(key: str, value: Any) -> None:
        """
        Store a key-value pair in the current task context.
        Uses copy-on-write to isolate mutations across task boundaries.
        """
        current = Context._get_dict()
        # Create a shallow copy of the dict container for the write operation
        new_ctx = current.copy()
        new_ctx[key] = value
        _context.set(new_ctx)
        if key != _WATCHERS_KEY:
            Context._notify_watchers(new_ctx, key, value)

    @staticmethod
    def get(key: str, default: Any = None) -> Any:
        """Retrieve a value from the current task context."""
        ctx = _context.get()
        if ctx is None:
            return default
        return ctx.get(key, default)

    @staticmethod
    def has(key: str) -> bool:
        """Return True if the key exists in the current context."""
        ctx = _context.get()
        return key in ctx if ctx is not None else False

    @staticmethod
    def all() -> dict[str, Any]:
        """Return a shallow copy of all context variables to prevent external mutation."""
        ctx = _context.get()
        if ctx is None:
            return {}
        return {k: v for k, v in ctx.items() if k != _WATCHERS_KEY}

    @staticmethod
    def clear() -> None:
        """Reset the current task context."""
        previous = _context.get()
        _context.set(None)
        Context._notify_watchers(previous, None, None)

    @staticmethod
    def push(key: str, value: Any) -> None:
        stack = Context.get(key, [])
        Context.set(key, [*stack, value])

    @staticmethod
    def pop(key: str) -> Any:
        stack = Context.get(key, [])

        if not stack:
            return None

        value = stack[-1]
        Context.set(key, stack[:-1])

        return value
