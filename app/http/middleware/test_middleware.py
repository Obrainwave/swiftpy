from __future__ import annotations

from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response


class TestMiddleware(Middleware):
    async def handle(self, request: Request, next_handler: Handler) -> Response:
        request.state["middleware_ran"] = True
        return await next_handler(request)
