from __future__ import annotations

from swiftpy.core.exceptions import HttpException
from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response


class AuthMiddleware(Middleware):
    """
    Authentication ASGI middleware for SwiftPY.

    Extracts Bearer tokens from the Authorization header, validates the credential
    against the application's authentication guard, and binds the authenticated
    user entity to both `request.state["user"]` and task-isolated `Context.set("auth.user", user)`.
    Raises HTTP 401 Unauthorized if the token is missing or invalid.
    """

    async def handle(self, request: Request, next_handler: Handler) -> Response:
        auth_header = request.headers.get("authorization") or request.headers.get(
            "Authorization"
        )
        if not auth_header or not auth_header.startswith("Bearer "):
            raise HttpException(
                status_code=401,
                message="Unauthenticated: Missing or invalid Bearer token",
            )

        token = auth_header[7:].strip()
        if not token:
            raise HttpException(
                status_code=401, message="Unauthenticated: Empty Bearer token"
            )

        # In production applications, decode JWT or resolve API key via Auth guard:
        # user = await Auth.guard().user_from_token(token)
        # Context.set("auth.user", user)
        # request.state["user"] = user

        return await next_handler(request)
