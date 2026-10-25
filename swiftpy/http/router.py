from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

RouteHandler = Any
F = TypeVar("F", bound=Callable[..., Any])


class PathParamCoercionError(Exception):
    def __init__(self, param: str, value: str, expected_type: type) -> None:
        self.param = param
        self.value = value
        self.expected_type = expected_type
        super().__init__(
            f"Path parameter '{param}' with value '{value}' could not be "
            f"converted to {expected_type.__name__}"
        )


@dataclass(slots=True)
class Route:
    method: str
    path: str
    handler: RouteHandler
    middleware: list[type]
    pattern: re.Pattern[str] = field(init=False)
    compiled_handler: RouteHandler | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        regex = re.sub(
            r"\{(\w+)\}",
            r"(?P<\1>[^/]+)",
            self.path,
        )

        self.pattern = re.compile(f"^{regex}$")


class RouteGroup:
    def __init__(
        self,
        prefix: str,
        router: Router,
        middleware: list[type] | None = None,
    ) -> None:
        self.prefix = "/" + prefix.strip("/")
        self.router = router
        self.middleware = middleware or []

    def _add_route(
        self,
        method: str,
        path: str,
        handler: RouteHandler,
    ) -> None:
        clean_path = "/" + path.strip("/") if path != "/" else ""
        full_path = self.prefix + clean_path
        self.router.add_route(
            method=method,
            path=full_path,
            handler=handler,
            middleware=list(self.middleware),
        )

    def get(self, path: str) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            self._add_route("GET", path, func)
            return func

        return decorator

    def post(self, path: str) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            self._add_route("POST", path, func)
            return func

        return decorator

    def put(self, path: str) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            self._add_route("PUT", path, func)
            return func

        return decorator

    def patch(self, path: str) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            self._add_route("PATCH", path, func)
            return func

        return decorator

    def delete(self, path: str) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            self._add_route("DELETE", path, func)
            return func

        return decorator


class Router:
    def __init__(self) -> None:
        self.routes: list[Route] = []

    def add_route(
        self,
        method: str,
        path: str,
        handler: RouteHandler,
        middleware: list[type] | None = None,
    ) -> None:
        self.routes.append(
            Route(
                method=method,
                path=path,
                handler=handler,
                middleware=middleware or [],
            )
        )

    def route(self, method: str, path: str) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            self.add_route(
                method,
                path,
                func,
            )

            return func

        return decorator

    def get(self, path: str) -> Callable[[F], F]:
        return self.route(
            "GET",
            path,
        )

    def post(self, path: str) -> Callable[[F], F]:
        return self.route(
            "POST",
            path,
        )

    def put(self, path: str) -> Callable[[F], F]:
        return self.route(
            "PUT",
            path,
        )

    def patch(self, path: str) -> Callable[[F], F]:
        return self.route(
            "PATCH",
            path,
        )

    def delete(self, path: str) -> Callable[[F], F]:
        return self.route(
            "DELETE",
            path,
        )

    def match(
        self,
        method: str,
        path: str,
    ) -> tuple[
        Route | None,
        dict[str, Any],
    ]:
        allowed_methods: list[str] = []

        for route in self.routes:
            match = route.pattern.match(path)

            if not match:
                continue

            if route.method != method:
                allowed_methods.append(route.method)
                continue

            params = match.groupdict()
            sig = inspect.signature(route.handler)

            for name, value in params.items():
                if name in sig.parameters:
                    annotation = sig.parameters[name].annotation
                    if isinstance(annotation, str):
                        builtins_map = {
                            "int": int,
                            "str": str,
                            "float": float,
                            "bool": bool,
                        }
                        annotation = builtins_map.get(
                            annotation,
                            getattr(route.handler, "__globals__", {}).get(
                                annotation, annotation
                            ),
                        )

                    if annotation is not inspect.Parameter.empty and callable(
                        annotation
                    ):
                        try:
                            params[name] = annotation(value)
                        except (ValueError, TypeError):
                            raise PathParamCoercionError(
                                name, value, annotation
                            ) from None

            return route, params

        if allowed_methods:
            from swiftpy.core.exceptions import MethodNotAllowedError

            unique_methods = sorted(set(allowed_methods))
            raise MethodNotAllowedError(
                f"Method '{method}' not allowed for path '{path}'. Allowed: {', '.join(unique_methods)}",
                headers={"Allow": ", ".join(unique_methods)},
            )

        return None, {}

    def group(
        self,
        prefix: str,
        middleware: list[type[Any]] | None = None,
    ) -> Callable[[Callable[[RouteGroup], None]], Callable[[RouteGroup], None]]:

        def decorator(
            func: Callable[[RouteGroup], None],
        ) -> Callable[[RouteGroup], None]:

            group = RouteGroup(
                router=self,
                prefix=prefix,
                middleware=middleware,
            )

            func(group)

            return func

        return decorator
