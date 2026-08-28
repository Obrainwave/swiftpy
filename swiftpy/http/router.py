from __future__ import annotations

import inspect
import re

from dataclasses import dataclass
from typing import Any

RouteHandler = Any


@dataclass(slots=True)
class Route:
    method: str
    path: str
    handler: RouteHandler
    middleware: list[type]

    pattern: re.Pattern[str] = field(init=False)

    def __post_init__(self) -> None:
        regex = re.sub(
            r"\{(\w+)\}",
            r"(?P<\1>[^/]+)",
            self.path,
        )

        self.pattern = re.compile(
            f"^{regex}$"
        )

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

    def get(self, path: str):
        def decorator(func: RouteHandler):
            self._add_route("GET", path, func)
            return func
        return decorator

    def post(self, path: str):
        def decorator(func: RouteHandler):
            self._add_route("POST", path, func)
            return func
        return decorator

    def put(self, path: str):
        def decorator(func: RouteHandler):
            self._add_route("PUT", path, func)
            return func
        return decorator

    def patch(self, path: str):
        def decorator(func: RouteHandler):
            self._add_route("PATCH", path, func)
            return func
        return decorator

    def delete(self, path: str):
        def decorator(func: RouteHandler):
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
                middleware=middleware
                or [],
            )
        )

    def route(
        self,
        method: str,
        path: str,
    ):
        def decorator(
            func: RouteHandler,
        ):
            self.add_route(
                method,
                path,
                func,
            )

            return func

        return decorator

    def get(self, path: str):
        return self.route(
            "GET",
            path,
        )

    def post(self, path: str):
        return self.route(
            "POST",
            path,
        )

    def put(self, path: str):
        return self.route(
            "PUT",
            path,
        )

    def patch(self, path: str):
        return self.route(
            "PATCH",
            path,
        )

    def delete(self, path: str):
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
        for route in self.routes:

            if route.method != method:
                continue

            match = route.pattern.match(path)

            if not match:
                continue

            params = match.groupdict()
            sig = inspect.signature(route.handler)

            for name, value in params.items():
                if name in sig.parameters:
                    annotation = sig.parameters[name].annotation
                    if annotation is not inspect.Parameter.empty and callable(annotation):
                        try:
                            params[name] = annotation(value)
                        except (ValueError, TypeError):
                            return None, {}

            return route, params

        return None, {}