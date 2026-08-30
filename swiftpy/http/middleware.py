from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from swiftpy.core.container import Container
from swiftpy.http.request import Request
from swiftpy.http.response import Response

Handler = Callable[..., Awaitable[Response]]


class Middleware:
    async def handle(self, request: Request, next_handler: Handler) -> Response:
        return await next_handler(request)


class MiddlewarePipeline:
    def __init__(self, container: Container, middleware: list[type[Middleware]]) -> None:
        self.container = container
        self.middleware = middleware

    def wrap(self, handler: Handler) -> Handler:
        wrapped = handler

        for middleware_cls in reversed(self.middleware):
            current = wrapped

            async def wrapped_handler(
                request: Request,
                *args: Any,
                middleware_cls: type[Middleware] = middleware_cls,
                current: Handler = current,
                **kwargs: Any,
            ) -> Response:
                middleware = self.container.resolve(middleware_cls)

                async def next_handler(req: Request) -> Response:
                    return await current(req, *args, **kwargs)

                return await middleware.handle(request, next_handler)

            wrapped = wrapped_handler

        return wrapped
