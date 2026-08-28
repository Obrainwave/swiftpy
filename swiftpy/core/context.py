from __future__ import annotations

from contextvars import ContextVar
from typing import Any

# Use None as default to avoid shared mutable default reference
_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "swiftpy_context",
    default=None,
)


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
        return ctx.copy() if ctx is not None else {}

    @staticmethod
    def clear() -> None:
        """Reset the current task context."""
        _context.set(None)

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
