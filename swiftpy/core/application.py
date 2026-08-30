from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from swiftpy.core.container import Container
from swiftpy.core.providers.base import ServiceProvider
from swiftpy.http.middleware import (
    MiddlewarePipeline,
)
from swiftpy.http.request import Request
from swiftpy.http.response import json_response, not_found
from swiftpy.http.router import RouteGroup, Router

logger = logging.getLogger("swiftpy")


class Application:
    """
    Root SwiftPY application.
    """

    def __init__(self) -> None:
        self.container = Container()
        self.router = Router()

        self._providers: list[ServiceProvider] = []

    def register(self, provider_class: type[ServiceProvider]) -> ServiceProvider:
        """Instantiate and register a service provider."""
        provider = provider_class(self.container)

        provider.register()

        self._providers.append(provider)

        return provider

    def boot(self) -> None:
        """
        Boot all registered service providers once all bindings are configured.
        """
        for provider in self._providers:
            provider.boot()

    def get(
        self, path: str
    ) -> Callable[[Callable[[RouteGroup], None]], Callable[[RouteGroup], None]]:
        return self.router.get(path)

    def post(
        self, path: str
    ) -> Callable[[Callable[[RouteGroup], None]], Callable[[RouteGroup], None]]:
        return self.router.post(path)

    def put(
        self, path: str
    ) -> Callable[[Callable[[RouteGroup], None]], Callable[[RouteGroup], None]]:
        return self.router.put(path)

    def patch(
        self, path: str
    ) -> Callable[[Callable[[RouteGroup], None]], Callable[[RouteGroup], None]]:
        return self.router.patch(path)

    def delete(
        self, path: str
    ) -> Callable[[Callable[[RouteGroup], None]], Callable[[RouteGroup], None]]:
        return self.router.delete(path)

    def group(
        self,
        prefix: str,
        middleware: list[type[Any]] | None = None,
    ) -> Callable[[Callable[[RouteGroup], None]], Callable[[RouteGroup], None]]:
        return self.router.group(prefix, middleware=middleware)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        
        try:

            if scope["type"] != "http":
                return

            request = Request(
                scope,
                receive,
            )

            route, params = self.router.match(
                request.method,
                request.path,
            )

            if route is None:
                response = not_found()

                await response.send(send)

                return

            pipeline = MiddlewarePipeline(self.container, route.middleware)

            handler = pipeline.wrap(route.handler)

            response = await handler(
                request,
                **params,
            )
            
        except Exception:
            logger.exception("Unhandled exception during request processing")

            response = json_response(
                {
                    "detail": "Internal Server Error",
                },
                status_code=500,
            )

        await response.send(send)
