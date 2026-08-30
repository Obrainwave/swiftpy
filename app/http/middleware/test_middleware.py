from swiftpy.http.middleware import Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response


class TestMiddleware(Middleware):
    async def handle(self, request: Request, next_handler) -> Response:
        request.state["middleware_ran"] = True
        return await next_handler(request)
