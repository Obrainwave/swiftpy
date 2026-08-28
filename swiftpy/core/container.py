from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar, cast, get_type_hints

from swiftpy.core.context import Context

from .container_exceptions import (
    BindingNotFoundError,
    CircularDependencyError,
    PrimitiveResolutionError,
    ResolutionError,
)

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
        self._resolving: list[type[Any]] = []

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

        self._bindings[interface] = Binding(
            interface=interface,
            factory=resolved_factory,
            scope=scope,
        )

    def singleton(
        self,
        interface: type[T],
        factory: Factory | None = None,
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
        factory: Callable[[Container], T] | None = None,
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

        self._singletons[interface] = instance_obj

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
            return cast(T, item)

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
                    cached = self._singletons.get(target)

                    if cached is not None:
                        return cast(T, cached)

                    instance = binding.factory(self)
                    self._singletons[target] = instance

                    return cast(T, instance)

                if binding.scope is Scope.SCOPED:
                    context_key = (
                        f"__swiftpy_di__:{target.__module__}.{target.__qualname__}"
                    )

                    if Context.has(context_key):
                        return cast(T, Context.get(context_key))

                    instance = binding.factory(self)
                    Context.set(context_key, instance)

                    return cast(T, instance)

                return cast(T, binding.factory(self))

            if inspect.isclass(target):
                return self._auto_wire(target)

            raise BindingNotFoundError(f"No binding registered for '{target}'.")

        finally:
            Context.pop(_DI_RESOLVING_KEY)

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

        signature = inspect.signature(func)
        type_hints = get_type_hints(func)

        resolved_kwargs: dict[str, Any] = {}

        parameters = list(signature.parameters.items())

        if skip_first and parameters:
            parameters = parameters[1:]

        for name, parameter in parameters:
            if name in extra_kwargs:
                resolved_kwargs[name] = extra_kwargs[name]
                continue

            parameter_type = type_hints.get(
                name,
                parameter.annotation,
            )

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
                    if parameter.default is not inspect.Parameter.empty:
                        resolved_kwargs[name] = parameter.default
                        continue

            if parameter.default is not inspect.Parameter.empty:
                resolved_kwargs[name] = parameter.default
                continue

            raise ResolutionError(
                f"Cannot resolve parameter '{name}' "
                f"of type '{parameter_type}' "
                f"for callable '{func.__qualname__}'."
            )

        return resolved_kwargs
