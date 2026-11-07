from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from swiftpy.core.container import get_container

EventHandler = Callable[..., Any]


@dataclass(slots=True)
class _ListenerRegistration:
    pattern: str
    regex: re.Pattern[str] | None
    handler: EventHandler
    order: int


class EventDispatcher:
    """
    In-process event dispatcher supporting synchronous and asynchronous listeners,
    exact and segment-scoped wildcard event patterns, global registration-order
    execution, and fail-fast exception propagation.
    """

    _instance: ClassVar[EventDispatcher | None] = None

    def __init__(self) -> None:
        self._registry: list[_ListenerRegistration] = []
        self._seq: int = 0

    @classmethod
    def get_instance(cls) -> EventDispatcher:
        """Retrieve the active application's EventDispatcher from the container, or fallback instance."""
        try:
            container = get_container()
            if container.has(cls):
                return container.resolve(cls)
        except RuntimeError:
            pass

        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def set_instance(cls, dispatcher: EventDispatcher | None) -> None:
        """Override or reset the EventDispatcher instance in the active container and fallback."""
        cls._instance = dispatcher
        try:
            container = get_container()
            if container.has(cls):
                if dispatcher is not None:
                    container.instance(cls, dispatcher)
                else:
                    container.singleton(cls, lambda _: cls())
        except RuntimeError:
            pass

    def _compile_pattern(self, pattern: str) -> re.Pattern[str] | None:
        """
        Compile an event pattern into regex.
        - '*' alone matches all events.
        - Single '*' within a segmented pattern matches exactly one segment ([^.]+).
        - Double '**' within a pattern matches zero or more segments (.*).
        - Patterns without '*' return None for O(1) exact string matching.
        """
        if "*" not in pattern:
            return None

        if pattern == "*":
            return re.compile(r"^.*$")

        # Protect double star '**' before escaping
        token = "__SWIFTPY_DOUBLE_STAR__"
        p = pattern.replace("**", token)
        escaped = re.escape(p)
        # Convert single star to one segment
        escaped = escaped.replace(r"\*", r"[^.]+")
        # Restore double star to any characters
        escaped = escaped.replace(token, r".*")
        return re.compile(f"^{escaped}$")

    def listen(self, event: str, handler: EventHandler) -> None:
        """
        Register an event listener for a specific event or wildcard pattern.
        Example patterns: 'user.created', 'user.*', '*.created', 'user.**', '*'
        """
        regex = self._compile_pattern(event)
        reg = _ListenerRegistration(
            pattern=event,
            regex=regex,
            handler=handler,
            order=self._seq,
        )
        self._seq += 1
        self._registry.append(reg)

    def has_listeners(self, event: str) -> bool:
        """Determine whether any listeners match the given event."""
        for reg in self._registry:
            if reg.regex is None:
                if reg.pattern == event:
                    return True
            elif reg.regex.match(event):
                return True
        return False

    def get_listeners(self, event: str) -> list[EventHandler]:
        """
        Retrieve all matching handlers for an event in global registration order,
        interleaving exact and wildcard registrations deterministically.
        """
        matching: list[tuple[int, EventHandler]] = []
        for reg in self._registry:
            if reg.regex is None:
                if reg.pattern == event:
                    matching.append((reg.order, reg.handler))
            elif reg.regex.match(event):
                matching.append((reg.order, reg.handler))

        matching.sort(key=lambda item: item[0])
        return [handler for _, handler in matching]

    async def dispatch(self, event: str, payload: Any = None) -> list[Any]:
        """
        Dispatch an event to all matching registered listeners in registration order.
        Supports both synchronous functions and async coroutine handlers.

        Policy: Fail-Fast. If any handler raises an exception, dispatch immediately
        halts, subsequent handlers are not invoked, and the exception is propagated
        to the caller without modifying dispatcher registration state.
        """
        handlers = self.get_listeners(event)
        if not handlers:
            return []

        results: list[Any] = []
        for handler in handlers:
            res = self._invoke_handler(handler, event, payload)
            if inspect.isawaitable(res):
                res = await res
            results.append(res)

        return results

    def _invoke_handler(self, handler: EventHandler, event: str, payload: Any) -> Any:
        """Invoke a handler with appropriate arguments based on its signature."""
        try:
            sig = inspect.signature(handler)
        except (ValueError, TypeError):
            # Fallback for callables whose signature cannot be inspected
            try:
                return handler(payload)
            except TypeError:
                return handler()

        params = list(sig.parameters.values())

        # Check for *args or **kwargs
        has_varargs = any(
            p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            for p in params
        )
        if has_varargs:
            return handler(payload)

        # Check for keyword-only parameters
        kw_only = [p for p in params if p.kind == inspect.Parameter.KEYWORD_ONLY]
        if kw_only:
            kwargs: dict[str, Any] = {}
            for p in kw_only:
                if p.name == "payload":
                    kwargs["payload"] = payload
                elif p.name == "event":
                    kwargs["event"] = event

            pos_params = [
                p
                for p in params
                if p.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
            ]
            if len(pos_params) == 0:
                return handler(**kwargs)
            if len(pos_params) == 1:
                return handler(payload, **kwargs)
            return handler(event, payload, **kwargs)

        required_params = [p for p in params if p.default is inspect.Parameter.empty]

        if len(required_params) == 0:
            if len(params) >= 1:
                return handler(payload)
            return handler()
        if len(required_params) == 1:
            return handler(payload)
        # If 2 or more required arguments, pass event name and payload
        return handler(event, payload)

    def unsubscribe(self, event: str, handler: EventHandler) -> bool:
        """Unsubscribe a specific handler from an event or pattern."""
        initial_len = len(self._registry)
        self._registry = [
            reg
            for reg in self._registry
            if not (reg.pattern == event and reg.handler == handler)
        ]
        return len(self._registry) < initial_len

    def forget(self, event: str | None = None) -> None:
        """
        Forget listeners for a specific event or wildcard pattern,
        or clear all registered listeners if event is None.
        """
        if event is None:
            self._registry.clear()
        else:
            self._registry = [reg for reg in self._registry if reg.pattern != event]
