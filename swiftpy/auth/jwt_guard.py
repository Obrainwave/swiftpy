from __future__ import annotations

from typing import Any

from swiftpy.auth.exceptions import (
    AuthenticationError,
    AuthenticationServiceUnavailableError,
    InvalidAlgorithmError,
    InvalidSignatureError,
    InvalidTokenError,
    TokenExpiredError,
    TokenRevokedError,
)
from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.auth.token_blacklist import TokenBlacklist
from swiftpy.core.container import get_container
from swiftpy.core.context import Context
from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response, unauthorized


async def require_jwt(
    request: Request | None = None,
    *,
    issuer: JWTIssuer | None = None,
    blacklist: TokenBlacklist | None = None,
    token_type: str = "access",
) -> dict[str, Any]:
    """
    Parse and validate a JWT from the HTTP Authorization header.

    Verifies:
      1. Presence of Authorization: Bearer <token>
      2. Valid cryptographic signature (RS256 or HS256)
      3. Token expiration (exp claim)
      4. Algorithm authenticity (rejects 'none' or unauthorized algs)
      5. Expected token type (default 'access')
      6. Token revocation state in Redis blacklist (jti claim)

    Attaches the authenticated payload to request.state and task Context.
    """
    # 1. Resolve request from argument or task Context
    req = request or Context.get("request")
    if req is None or not isinstance(req, Request):
        raise RuntimeError(
            "No active HTTP Request found. Pass 'request' explicitly or call within request pipeline."
        )

    # 2. Extract Authorization header case-insensitively and check for duplicates
    raw_auth_headers = [
        v.decode("latin1")
        for k, v in req.scope.get("headers", [])
        if k.lower() == b"authorization"
    ]
    if len(raw_auth_headers) > 1:
        raise AuthenticationError("Multiple Authorization headers provided")
    if not raw_auth_headers:
        raise AuthenticationError("Missing Authorization header")

    auth_header = raw_auth_headers[0]
    parts = auth_header.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthenticationError(
            "Invalid Authorization header format. Expected 'Bearer <token>'"
        )

    token = parts[1].strip()
    if not token:
        raise AuthenticationError("Bearer token is empty")

    # 3. Resolve JWTIssuer from parameters, container, or default
    active_issuer = issuer
    if active_issuer is None:
        try:
            container = get_container()
            if container.has(JWTIssuer):
                active_issuer = container.resolve(JWTIssuer)
        except RuntimeError:
            pass

    if active_issuer is None:
        active_issuer = JWTIssuer()

    # 4. Resolve TokenBlacklist from parameters, container, or default
    active_blacklist = blacklist
    if active_blacklist is None:
        try:
            container = get_container()
            if container.has(TokenBlacklist):
                active_blacklist = container.resolve(TokenBlacklist)
        except RuntimeError:
            pass

    # 5. Decode and cryptographically verify token
    try:
        payload = active_issuer.decode_token(
            token,
            verify_exp=True,
            expected_type=token_type,
        )
    except TokenExpiredError as exc:
        raise AuthenticationError("Token has expired") from exc
    except InvalidSignatureError as exc:
        raise AuthenticationError("Invalid token signature") from exc
    except InvalidAlgorithmError as exc:
        raise AuthenticationError("Invalid token algorithm") from exc
    except InvalidTokenError as exc:
        raise AuthenticationError(f"Invalid token: {exc}") from exc

    # 6. Check Redis revocation blacklist if jti is present
    jti = payload.get("jti")
    if jti and active_blacklist is not None:
        try:
            if await active_blacklist.is_revoked(jti):
                raise TokenRevokedError("Token has been revoked")
        except TokenRevokedError:
            raise
        except Exception as exc:
            # If Redis connection fails unexpectedly, fail closed with 503 Service Unavailable
            raise AuthenticationServiceUnavailableError(
                f"Authentication service temporarily unavailable: Redis error ({exc})"
            ) from exc

    # 7. Attach authenticated user payload to request state and task context
    req.state["user"] = payload
    req.state["auth_guard"] = "jwt"
    Context.set("__swiftpy_current_user__", payload)

    return payload


class JWTGuard(Middleware):
    """
    Middleware adapter for JWT authentication.
    Applies require_jwt() and handles AuthenticationError (401) or
    AuthenticationServiceUnavailableError (503).
    """

    def __init__(
        self,
        issuer: JWTIssuer | None = None,
        blacklist: TokenBlacklist | None = None,
        token_type: str = "access",
    ) -> None:
        self.issuer = issuer
        self.blacklist = blacklist
        self.token_type = token_type

    async def handle(self, request: Request, next_handler: Handler) -> Response:
        try:
            await require_jwt(
                request,
                issuer=self.issuer,
                blacklist=self.blacklist,
                token_type=self.token_type,
            )
        except AuthenticationError as exc:
            return unauthorized(exc.message, headers=exc.headers)
        except AuthenticationServiceUnavailableError as exc:
            from swiftpy.http.response import json_response

            return json_response(
                {"detail": exc.message},
                status_code=exc.status_code,
                headers=exc.headers,
            )

        return await next_handler(request)
