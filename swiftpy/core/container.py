from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Any, TypeVar, cast, get_type_hints

from swiftpy.core.context import Context

from .container_exceptions import (
    BindingNotFoundError,
    CircularDependencyError,
    PrimitiveResolutionError,
    ResolutionError,
)

logger = logging.getLogger("swiftpy.core.container")

T = TypeVar("T")

Factory = Callable[["Container"], Any]

PRIMITIVE_TYPES: tuple[type[Any], ...] = (
    str,
    int,
    float,
    bool,
    dict,
    list,
    set,
    tuple,
    bytes,
)

_DI_RESOLVING_KEY = "__swiftpy_di_resolving__"
_DI_SCOPED_INSTANCES_KEY = "__swiftpy_di_scoped_instances__"
_NONE_SENTINEL = object()


@dataclass(slots=True, frozen=True)
class _CallableParamInfo:
    name: str
    param_type: Any
    default: Any


_callable_reflection_cache: dict[tuple[Any, bool], tuple[_CallableParamInfo, ...]] = {}
_can_resolve_init_cache: dict[type[Any], tuple[tuple[str, Any], ...] | None] = {}


def _inspect_callable_specs(
    func: Callable[..., Any], *, skip_first: bool = False
) -> tuple[_CallableParamInfo, ...]:
    cache_key = (func, skip_first)
    cached = _callable_reflection_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        signature = inspect.signature(func)
        type_hints = get_type_hints(func)
    except (NameError, TypeError, ValueError) as exc:
        callable_name = getattr(
            func, "__qualname__", getattr(func, "__name__", str(func))
        )
        raise ResolutionError(
            f"Failed to resolve type hints for '{callable_name}': {exc}"
        ) from exc

    parameters = list(signature.parameters.items())
    if skip_first and parameters:
        parameters = parameters[1:]

    specs: list[_CallableParamInfo] = []
    for name, parameter in parameters:
        param_type = type_hints.get(name, parameter.annotation)
        specs.append(
            _CallableParamInfo(
                name=name,
                param_type=param_type,
                default=parameter.default,
            )
        )

    res = tuple(specs)
    _callable_reflection_cache[cache_key] = res
    return res


def _inspect_can_resolve_params(
    target: type[Any],
) -> tuple[tuple[str, Any], ...] | None:
    if target in _can_resolve_init_cache:
        return _can_resolve_init_cache[target]

    init_method = target.__init__
    if init_method is object.__init__:
        _can_resolve_init_cache[target] = ()
        return ()

    try:
        signature = inspect.signature(init_method)
        type_hints = get_type_hints(init_method)
    except (TypeError, ValueError, NameError):
        _can_resolve_init_cache[target] = None
        return None

    parameters = list(signature.parameters.items())
    if parameters:
        parameters = parameters[1:]

    required: list[tuple[str, Any]] = []
    for name, parameter in parameters:
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if parameter.default is not inspect.Parameter.empty:
            continue
        param_type = type_hints.get(name, parameter.annotation)
        required.append((name, param_type))

    res = tuple(required)
    _can_resolve_init_cache[target] = res
    return res


def _current_task_or_none() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _scoped_context_key(interface: type[Any]) -> str:
    return f"__swiftpy_di__:{interface.__module__}.{interface.__qualname__}"

_container_var: ContextVar[Container | None] = ContextVar(
    "swiftpy_container", default=None
)
_container_fallback: Container | None = None


def set_container(container: Container) -> None:
    """Set the active application container context-wide and globally."""
    global _container_fallback
    _container_var.set(container)
    _container_fallback = container


def get_container() -> Container:
    """Retrieve the currently active application container."""
    container = _container_var.get()
    if container is None:
        container = _container_fallback
    if container is None:
        raise RuntimeError("Application container not bootstrapped")
    return container


class Scope(StrEnum):
    TRANSIENT = "transient"
    SINGLETON = "singleton"
    SCOPED = "scoped"


@dataclass(slots=True, frozen=True)
class Binding:
    """Immutable metadata describing a container registration."""

    interface: type[Any]
    factory: Factory
    scope: Scope = Scope.TRANSIENT


class Container:
    """
    Lightweight dependency injection container for SwiftPY.

    Supports transient and singleton bindings, constructor auto-wiring,
    callable dependency injection, and circular dependency detection.
    """

    def __init__(self) -> None:
        self._bindings: dict[type[Any], Binding] = {}
        self._singletons: dict[type[Any], Any] = {}

        self.instance(Container, self)

    def bind(
        self,
        interface: type[T],
        factory: Factory | type[T] | None = None,
        scope: Scope = Scope.TRANSIENT,
    ) -> None:
        """
        Register a binding.
        """

        if factory is None:
            factory = interface

        resolved_factory: Factory

        if inspect.isclass(factory):
            target_cls = factory

            def class_factory(container: Container) -> Any:
                return container._auto_wire(target_cls)

            resolved_factory = class_factory

        else:
            resolved_factory = factory

        if interface in self._singletons:
            self._singletons.pop(interface, None)
            logger.debug(
                "Rebinding '%s' (%s); invalidated cached singleton instance.",
                getattr(interface, "__name__", str(interface)),
                scope,
            )

        scoped_key = _scoped_context_key(interface)
        if Context.has(scoped_key):
            Context.set(scoped_key, None)

        self._bindings[interface] = Binding(
            interface=interface,
            factory=resolved_factory,
            scope=scope,
        )

    def singleton(
        self,
        interface: type[T],
        factory: Factory | type[T] | None = None,
    ) -> None:
        """Register a singleton binding."""

        self.bind(
            interface=interface,
            factory=factory,
            scope=Scope.SINGLETON,
        )

    def scoped(
        self,
        interface: type[T],
        factory: Callable[[Container], T] | type[T] | None = None,
    ) -> None:
        """Register a task-scoped binding."""

        self.bind(
            interface=interface,
            factory=factory,
            scope=Scope.SCOPED,
        )

    def instance(
        self,
        interface: type[T],
        instance_obj: T,
    ) -> None:
        """Register an already-created singleton instance."""

        self._bindings[interface] = Binding(
            interface=interface,
            factory=lambda _: instance_obj,
            scope=Scope.SINGLETON,
        )

        self._singletons[interface] = (
            _NONE_SENTINEL if instance_obj is None else instance_obj
        )

    def get_singleton(self, interface: type[T]) -> T | None:
        """Return an already-instantiated singleton if present, or None."""
        if interface not in self._singletons:
            return None
        val = self._singletons[interface]
        return None if val is _NONE_SENTINEL else cast(T, val)

    def has(self, interface: type[Any]) -> bool:
        """Determine whether an interface or service has been registered in the container."""
        return interface in self._bindings or interface in self._singletons

    def can_resolve(
        self,
        target: Any,
        _seen: frozenset[type[Any]] | None = None,
    ) -> bool:
        """
        Determine whether `target` can be satisfied by this container
        (either via an explicit binding or constructor auto-wiring).
        """
        if target is inspect.Parameter.empty or target is Any or target is object:
            return False

        try:
            if self.has(target):
                return True
        except TypeError:
            return False

        if target in PRIMITIVE_TYPES or not inspect.isclass(target):
            return False

        if inspect.isabstract(target):
            return False

        seen = _seen or frozenset()
        if target in seen:
            return False

        required_params = _inspect_can_resolve_params(target)
        if required_params is None:
            return False

        next_seen = seen | {target}
        for _, param_type in required_params:
            if not self.can_resolve(param_type, next_seen):
                return False

        return True

    def resolve(self, target: type[T]) -> T:
        """
        Resolve a dependency from the container.

        Registered bindings are resolved according to their configured
        lifetime. Unregistered concrete classes are auto-wired.
        """
        resolving_stack = Context.get(_DI_RESOLVING_KEY, [])

        if target in PRIMITIVE_TYPES:
            raise PrimitiveResolutionError(
                f"Cannot auto-wire primitive type "
                f"'{target.__name__}'. "
                f"Provide a default value or explicit binding."
            )

        if target in self._singletons:
            item = self._singletons[target]
            return cast(T, None if item is _NONE_SENTINEL else item)

        if target in resolving_stack:
            chain = " -> ".join(dependency.__name__ for dependency in resolving_stack)

            raise CircularDependencyError(
                f"Circular dependency detected: {chain} -> {target.__name__}"
            )

        Context.push(_DI_RESOLVING_KEY, target)

        try:
            binding = self._bindings.get(target)

            if binding is not None:
                if binding.scope is Scope.SINGLETON:
                    if target in self._singletons:
                        cached = self._singletons[target]
                        return cast(T, None if cached is _NONE_SENTINEL else cached)

                    instance = binding.factory(self)
                    self._singletons[target] = (
                        _NONE_SENTINEL if instance is None else instance
                    )

                    return cast(T, instance)

                if binding.scope is Scope.SCOPED:
                    context_key = _scoped_context_key(target)
                    cur_task = _current_task_or_none()

                    if Context.has(context_key):
                        cached_entry = Context.get(context_key)
                        if (
                            isinstance(cached_entry, tuple)
                            and len(cached_entry) == 2
                            and cached_entry[0] is cur_task
                        ):
                            return cast(T, cached_entry[1])

                    instance = binding.factory(self)
                    if getattr(instance, "_cache_in_scope", True) is not False:
                        Context.set(context_key, (cur_task, instance))
                        scoped_list: list[
                            tuple[asyncio.Task[Any] | None, str, Any]
                        ] = list(Context.get(_DI_SCOPED_INSTANCES_KEY, []))
                        scoped_list.append((cur_task, context_key, instance))
                        Context.set(_DI_SCOPED_INSTANCES_KEY, scoped_list)

                    return cast(T, instance)

                return cast(T, binding.factory(self))

            if inspect.isclass(target):
                return self._auto_wire(target)

            raise BindingNotFoundError(f"No binding registered for '{target}'.")

        finally:
            Context.pop(_DI_RESOLVING_KEY)

    async def aclose_scoped(
        self,
        exc_type: type[BaseException] | None = None,
        exc_val: BaseException | None = None,
        exc_tb: TracebackType | None = None,
    ) -> None:
        """
        Dispose all task-scoped instances created in the current task in reverse creation order.
        Supports `aclose()`, `__aexit__()`, `close()`, and `__exit__()`.
        """
        raw_list = Context.get(_DI_SCOPED_INSTANCES_KEY)
        if not raw_list or not isinstance(raw_list, list):
            return

        cur_task = _current_task_or_none()
        owned = [
            entry
            for entry in raw_list
            if isinstance(entry, tuple) and len(entry) == 3 and entry[0] is cur_task
        ]
        remaining = [
            entry
            for entry in raw_list
            if not (
                isinstance(entry, tuple) and len(entry) == 3 and entry[0] is cur_task
            )
        ]
        Context.set(_DI_SCOPED_INSTANCES_KEY, remaining)

        for _, context_key, inst in reversed(owned):
            Context.set(context_key, None)
            if inst is None:
                continue
            try:
                if hasattr(inst, "aclose") and callable(inst.aclose):
                    res = inst.aclose()
                    if inspect.isawaitable(res):
                        await res
                elif hasattr(inst, "__aexit__") and callable(inst.__aexit__):
                    await inst.__aexit__(exc_type, exc_val, exc_tb)
                elif hasattr(inst, "close") and callable(inst.close):
                    res = inst.close()
                    if inspect.isawaitable(res):
                        await res
                elif hasattr(inst, "__exit__") and callable(inst.__exit__):
                    inst.__exit__(exc_type, exc_val, exc_tb)
            except Exception:
                logger.exception("Error disposing scoped instance %r", inst)

    @asynccontextmanager
    async def scope(self) -> AsyncIterator[Container]:
        """Async context manager that disposes task-scoped dependencies on exit."""
        exc_info: tuple[
            type[BaseException] | None,
            BaseException | None,
            TracebackType | None,
        ] = (None, None, None)
        try:
            yield self
        except BaseException as exc:
            exc_info = (type(exc), exc, exc.__traceback__)
            raise
        finally:
            await self.aclose_scoped(*exc_info)

    def call(
        self,
        func: Callable[..., T],
        **extra_kwargs: Any,
    ) -> T:
        """Invoke a callable with dependency injection."""

        kwargs = self._resolve_callable_dependencies(
            func,
            extra_kwargs=extra_kwargs,
        )

        return func(**kwargs)

    def _auto_wire(self, target_cls: type[T]) -> T:
        """Construct a class using constructor injection."""

        init_method = target_cls.__init__

        if init_method is object.__init__:
            return target_cls()

        kwargs = self._resolve_callable_dependencies(
            init_method,
            skip_first=True,
        )

        return target_cls(**kwargs)

    def _resolve_callable_dependencies(
        self,
        func: Callable[..., Any],
        *,
        skip_first: bool = False,
        extra_kwargs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve dependencies required by a callable."""

        if extra_kwargs is None:
            extra_kwargs = {}

        specs = _inspect_callable_specs(func, skip_first=skip_first)
        resolved_kwargs: dict[str, Any] = {}

        for spec in specs:
            name = spec.name
            if name in extra_kwargs:
                resolved_kwargs[name] = extra_kwargs[name]
                continue

            parameter_type = spec.param_type

            if (
                parameter_type is not inspect.Parameter.empty
                and parameter_type not in PRIMITIVE_TYPES
            ):
                try:
                    resolved_kwargs[name] = self.resolve(parameter_type)
                    continue
                except CircularDependencyError:
                    raise
                except ResolutionError:
                    if spec.default is not inspect.Parameter.empty:
                        resolved_kwargs[name] = spec.default
                        continue

            if spec.default is not inspect.Parameter.empty:
                resolved_kwargs[name] = spec.default
                continue

            callable_name = getattr(
                func, "__qualname__", getattr(func, "__name__", str(func))
            )
            raise ResolutionError(
                f"Cannot resolve parameter '{name}' "
                f"of type '{parameter_type}' "
                f"for callable '{callable_name}'."
            )

        return resolved_kwargs
