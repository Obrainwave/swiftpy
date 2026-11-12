from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from swiftpy.core.container import get_container

EventHandler = Callable[..., Any]


def _build_invoker(handler: EventHandler) -> Callable[[str, Any], Any]:
    """Inspect and precompile an invocation strategy for the given handler."""
    try:
        sig = inspect.signature(handler)
    except (ValueError, TypeError):
        def _fallback(event: str, payload: Any) -> Any:
            try:
                return handler(payload)
            except TypeError:
                return handler()

        return _fallback

    params = list(sig.parameters.values())

    # Check for *args or **kwargs
    has_varargs = any(
        p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        for p in params
    )
    if has_varargs:
        return lambda event, payload: handler(payload)

    kw_only = [p for p in params if p.kind == inspect.Parameter.KEYWORD_ONLY]
    if kw_only:
        kw_names = tuple(p.name for p in kw_only)
        pos_params = [
            p
            for p in params
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        num_pos = len(pos_params)

        def _kw_invoker(event: str, payload: Any) -> Any:
            kwargs: dict[str, Any] = {}
            for name in kw_names:
                if name == "payload":
                    kwargs["payload"] = payload
                elif name == "event":
                    kwargs["event"] = event
            if num_pos == 0:
                return handler(**kwargs)
            if num_pos == 1:
                return handler(payload, **kwargs)
            return handler(event, payload, **kwargs)

        return _kw_invoker

    required_params = [p for p in params if p.default is inspect.Parameter.empty]
    num_req = len(required_params)

    if num_req == 0:
        if len(params) >= 1:
            return lambda event, payload: handler(payload)
        return lambda event, payload: handler()
    if num_req == 1:
        return lambda event, payload: handler(payload)
    return lambda event, payload: handler(event, payload)


@dataclass(slots=True)
class _ListenerRegistration:
    pattern: str
    regex: re.Pattern[str] | None
    handler: EventHandler
    order: int
    invoker: Callable[[str, Any], Any]


def _normalize_event_name(event: str | type[Any] | Any) -> str:
    if isinstance(event, str):
        return event
    if isinstance(event, type):
        module = getattr(event, "__module__", "")
        qualname = getattr(event, "__qualname__", event.__name__)
        return f"{module}.{qualname}" if module and module != "__main__" else qualname

    event_type = type(event)
    module = getattr(event_type, "__module__", "")
    qualname = getattr(event_type, "__qualname__", event_type.__name__)
    return f"{module}.{qualname}" if module and module != "__main__" else qualname


class EventDispatcher:
    """
    In-process event dispatcher supporting synchronous and asynchronous listeners,
    exact and segment-scoped wildcard event patterns, global registration-order
    execution, signature caching, and fail-fast exception propagation.
    """

    _instance: ClassVar[EventDispatcher | None] = None

    def __init__(self) -> None:
        self._exact_registry: dict[str, list[_ListenerRegistration]] = {}
        self._wildcard_registry: list[_ListenerRegistration] = []
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

    def listen(self, event: str | type[Any], handler: EventHandler) -> None:
        """
        Register an event listener for a specific event or wildcard pattern.
        Example patterns: 'user.created', 'user.*', '*.created', 'user.**', '*'
        """
        event_str = _normalize_event_name(event)
        regex = self._compile_pattern(event_str)
        invoker = _build_invoker(handler)
        reg = _ListenerRegistration(
            pattern=event_str,
            regex=regex,
            handler=handler,
            order=self._seq,
            invoker=invoker,
        )
        self._seq += 1
        self._registry.append(reg)
        if regex is None:
            self._exact_registry.setdefault(event_str, []).append(reg)
        else:
            self._wildcard_registry.append(reg)

    def has_listeners(self, event: str | type[Any]) -> bool:
        """Determine whether any listeners match the given event."""
        event_str = _normalize_event_name(event)
        if self._exact_registry.get(event_str):
            return True
        for reg in self._wildcard_registry:
            if reg.regex is not None and reg.regex.match(event_str):
                return True
        return False

    def get_matching_registrations(self, event: str | type[Any]) -> list[_ListenerRegistration]:
        """
        Retrieve all matching registrations for an event in global registration order.
        """
        event_str = _normalize_event_name(event)
        matching: list[_ListenerRegistration] = []
        exact = self._exact_registry.get(event_str)
        if exact:
            matching.extend(exact)
        for reg in self._wildcard_registry:
            if reg.regex is not None and reg.regex.match(event_str):
                matching.append(reg)

        if len(matching) > 1:
            matching.sort(key=lambda r: r.order)
        return matching

    def get_listeners(self, event: str | type[Any]) -> list[EventHandler]:
        """
        Retrieve all matching handlers for an event in global registration order,
        interleaving exact and wildcard registrations deterministically.
        """
        return [reg.handler for reg in self.get_matching_registrations(event)]

    async def dispatch(self, event: str | type[Any] | Any, payload: Any = None) -> list[Any]:
        """
        Dispatch an event to all matching registered listeners in registration order.
        Supports both synchronous functions and async coroutine handlers.

        Policy: Fail-Fast. If any handler raises an exception, dispatch immediately
        halts, subsequent handlers are not invoked, and the exception is propagated
        to the caller without modifying dispatcher registration state.
        """
        if isinstance(event, str):
            event_name = event
        else:
            event_name = _normalize_event_name(event)
            if payload is None and not isinstance(event, type):
                payload = event

        matching = self.get_matching_registrations(event_name)
        if not matching:
            return []

        results: list[Any] = []
        for reg in matching:
            res = reg.invoker(event_name, payload)
            if inspect.isawaitable(res):
                res = await res
            results.append(res)

        return results

    def _invoke_handler(self, handler: EventHandler, event: str, payload: Any) -> Any:
        """Invoke a handler with appropriate arguments based on its signature."""
        invoker = _build_invoker(handler)
        return invoker(event, payload)

    def remove_listener(self, event: str | type[Any], handler: EventHandler) -> bool:
        """Alias for unsubscribe."""
        return self.unsubscribe(event, handler)

    def unsubscribe(self, event: str | type[Any], handler: EventHandler) -> bool:
        """Unsubscribe a specific handler from an event or pattern."""
        event_str = _normalize_event_name(event)
        initial_len = len(self._registry)
        self._registry = [
            reg
            for reg in self._registry
            if not (reg.pattern == event_str and reg.handler == handler)
        ]
        if event_str in self._exact_registry:
            self._exact_registry[event_str] = [
                reg for reg in self._exact_registry[event_str] if reg.handler != handler
            ]
            if not self._exact_registry[event_str]:
                del self._exact_registry[event_str]
        self._wildcard_registry = [
            reg
            for reg in self._wildcard_registry
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
            self._exact_registry.clear()
            self._wildcard_registry.clear()
        else:
            self._registry = [reg for reg in self._registry if reg.pattern != event]
            self._exact_registry.pop(event, None)
            self._wildcard_registry = [
                reg for reg in self._wildcard_registry if reg.pattern != event
            ]
