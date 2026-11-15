from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import Container
from swiftpy.core.context import Context
from swiftpy.http.exceptions import ClientDisconnect
from swiftpy.http.request import Request
from swiftpy.http.response import Response

Handler = Callable[..., Awaitable[Response]]

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._\-:]{1,128}$")


class Middleware:
    async def handle(self, request: Request, next_handler: Handler) -> Response:
        return await next_handler(request)


class MiddlewarePipeline:
    def __init__(
        self, container: Container, middleware: Sequence[type[Middleware]]
    ) -> None:
        self.container = container
        self.middleware = list(middleware)

    def wrap(
        self,
        handler: Handler,
        *,
        exception_handler: Callable[[Exception, Request], Awaitable[Response]] | None = None,
    ) -> Handler:
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
                    if exception_handler is None:
                        return await current(req, *args, **kwargs)
                    try:
                        return await current(req, *args, **kwargs)
                    except ClientDisconnect:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        return await exception_handler(exc, req)

                return await middleware.handle(request, next_handler)

            wrapped = wrapped_handler

        return wrapped


class SecurityHeadersMiddleware(Middleware):
    """
    Opt-in middleware that attaches standard defensive HTTP security headers.
    """

    DEFAULT_HEADERS: tuple[tuple[str, str], ...] = (
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "strict-origin-when-cross-origin"),
        ("X-XSS-Protection", "0"),
        ("Cross-Origin-Opener-Policy", "same-origin"),
    )

    async def handle(self, request: Request, next_handler: Handler) -> Response:
        response = await next_handler(request)
        for header_name, header_val in self.DEFAULT_HEADERS:
            if header_name not in response.headers:
                response.headers[header_name] = header_val
        return response


class RequestIdMiddleware(Middleware):
    """
    Opt-in middleware that assigns or propagates a validated `X-Request-ID` header
    and stores `request_id` in `request.state` and `Context`.
    """

    HEADER_NAME: str = "X-Request-ID"

    async def handle(self, request: Request, next_handler: Handler) -> Response:
        incoming = request.headers.get(self.HEADER_NAME)
        if incoming and _REQUEST_ID_RE.fullmatch(incoming):
            request_id = incoming
        else:
            request_id = str(uuid.uuid4())

        request.state["request_id"] = request_id
        Context.set("request_id", request_id)

        response = await next_handler(request)
        response.headers[self.HEADER_NAME] = request_id
        return response


class CORSMiddleware(Middleware):
    """
    Opt-in Cross-Origin Resource Sharing (CORS) middleware configured via `config/cors.py`.
    Defaults to deny-all (`allowed_origins = []`).
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._parse_cors_settings()

    def _get_cors_config(self) -> dict[str, Any]:
        raw = self._config.get("cors", {})
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _normalize_list(
        value: Any,
        *,
        default: Sequence[str] = (),
        field: str = "",
    ) -> list[str]:
        if value is None:
            return list(default)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return list(default) if default else []
            if stripped.startswith("[") and stripped.endswith("]"):
                try:
                    parsed = json.loads(stripped)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise ConfigurationError(
                        f"CORS configuration field '{field}' contains invalid JSON list: '{value}'"
                    ) from exc
                if not isinstance(parsed, list):
                    raise ConfigurationError(
                        f"CORS configuration field '{field}' expects a JSON array"
                    )
                items = [str(item).strip() for item in parsed if str(item).strip()]
            else:
                items = [item.strip() for item in stripped.split(",") if item.strip()]
            return items if items or not default else list(default)
        if isinstance(value, (list, tuple, set)):
            items = [str(item).strip() for item in value if str(item).strip()]
            return items if items or not default else list(default)
        raise ConfigurationError(
            f"CORS configuration field '{field}' must be a sequence or comma-separated string, got {type(value).__name__}"
        )

    @staticmethod
    def _normalize_bool(value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)

    def _parse_cors_settings(
        self,
    ) -> tuple[list[str], list[str], list[str], list[str], bool, int | None]:
        cfg = self._get_cors_config()
        allowed_origins = self._normalize_list(
            cfg.get("allowed_origins"),
            field="allowed_origins",
        )
        allowed_methods = self._normalize_list(
            cfg.get("allowed_methods"),
            default=("GET", "HEAD", "POST"),
            field="allowed_methods",
        )
        allowed_headers = self._normalize_list(
            cfg.get("allowed_headers"),
            field="allowed_headers",
        )
        expose_headers = self._normalize_list(
            cfg.get("expose_headers"),
            field="expose_headers",
        )
        allow_credentials = self._normalize_bool(
            cfg.get("allow_credentials"),
            default=False,
        )
        if "*" in allowed_origins and allow_credentials:
            raise ConfigurationError(
                "Invalid CORS configuration: 'allowed_origins' cannot include '*' when 'allow_credentials' is True."
            )
        max_age: int | None = (
            int(cfg["max_age"]) if cfg.get("max_age") is not None else 600
        )
        return (
            allowed_origins,
            allowed_methods,
            allowed_headers,
            expose_headers,
            allow_credentials,
            max_age,
        )

    @staticmethod
    def _is_origin_allowed(origin: str, allowed_origins: Sequence[str]) -> bool:
        if not allowed_origins:
            return False
        if "*" in allowed_origins:
            return True
        return origin in allowed_origins

    async def handle(self, request: Request, next_handler: Handler) -> Response:
        (
            allowed_origins,
            allowed_methods,
            allowed_headers,
            expose_headers,
            allow_credentials,
            max_age,
        ) = self._parse_cors_settings()

        origin = request.headers.get("origin")
        if not origin:
            return await next_handler(request)

        is_preflight = (
            request.method.upper() == "OPTIONS"
            and "access-control-request-method" in request.headers
        )
        origin_allowed = self._is_origin_allowed(origin, allowed_origins)

        if is_preflight:
            if not origin_allowed:
                return Response(b"", status_code=204)

            headers: dict[str, str] = {}
            if "*" in allowed_origins and not allow_credentials:
                headers["Access-Control-Allow-Origin"] = "*"
            else:
                headers["Access-Control-Allow-Origin"] = origin
                headers["Vary"] = "Origin"

            headers["Access-Control-Allow-Methods"] = ", ".join(allowed_methods)
            if allowed_headers:
                if "*" in allowed_headers:
                    req_hdrs = request.headers.get("access-control-request-headers")
                    if req_hdrs:
                        headers["Access-Control-Allow-Headers"] = req_hdrs
                else:
                    headers["Access-Control-Allow-Headers"] = ", ".join(allowed_headers)

            if allow_credentials:
                headers["Access-Control-Allow-Credentials"] = "true"
            if max_age is not None:
                headers["Access-Control-Max-Age"] = str(max_age)

            return Response(b"", status_code=204, headers=headers)

        response = await next_handler(request)
        if not origin_allowed:
            return response

        if "*" in allowed_origins and not allow_credentials:
            response.headers["Access-Control-Allow-Origin"] = "*"
        else:
            response.headers["Access-Control-Allow-Origin"] = origin
            existing_vary = response.headers.get("Vary")
            if not existing_vary:
                response.headers["Vary"] = "Origin"
            elif "Origin" not in [v.strip() for v in existing_vary.split(",")]:
                response.headers["Vary"] = f"{existing_vary}, Origin"

        if allow_credentials:
            response.headers["Access-Control-Allow-Credentials"] = "true"
        if expose_headers:
            response.headers["Access-Control-Expose-Headers"] = ", ".join(
                expose_headers
            )

        return response


SecurityHeaders = SecurityHeadersMiddleware
RequestId = RequestIdMiddleware
CORS = CORSMiddleware

__all__ = [
    "CORS",
    "CORSMiddleware",
    "Handler",
    "Middleware",
    "MiddlewarePipeline",
    "RequestId",
    "RequestIdMiddleware",
    "SecurityHeaders",
    "SecurityHeadersMiddleware",
]
