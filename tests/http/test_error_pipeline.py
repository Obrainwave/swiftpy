from __future__ import annotations

import logging
from typing import Any

import pytest

from swiftpy.core.application import Application
from swiftpy.core.config import ConfigurationError
from swiftpy.core.exceptions import (
    BadRequestError,
    ExceptionHandler,
    InternalServerError,
)
from swiftpy.http.middleware import (
    CORS,
    Handler,
    Middleware,
    RequestId,
    SecurityHeaders,
)
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.http.router import RouteGroup
from swiftpy.testing.http_client import TestClient


@pytest.mark.asyncio
async def test_global_middleware_sees_200_404_405_and_500_in_correct_order() -> None:
    events: list[str] = []

    class GlobalTracingMiddleware(Middleware):
        async def handle(self, request: Request, next_handler: Handler) -> Response:
            events.append(f"global:before:{request.path}")
            response = await next_handler(request)
            events.append(f"global:after:{response.status_code}")
            response.headers["X-Global-Middleware"] = str(response.status_code)
            return response

    class GroupTracingMiddleware(Middleware):
        async def handle(self, request: Request, next_handler: Handler) -> Response:
            events.append("group:before")
            response = await next_handler(request)
            events.append("group:after")
            response.headers["X-Group-Middleware"] = "1"
            return response

    app = Application(auto_discover_providers=False, debug=False)
    app.middleware([GlobalTracingMiddleware])

    @app.group("/api", middleware=[GroupTracingMiddleware])
    def api_group(group: RouteGroup) -> None:
        @group.get("/ok")
        async def ok_route() -> Response:
            events.append("handler:ok")
            return json_response({"ok": True})

        @group.get("/boom")
        async def boom_route() -> Response:
            events.append("handler:boom")
            raise RuntimeError("explode")

    client = TestClient(app)

    # 1. 200 OK: global runs first, then group, then handler
    events.clear()
    res_ok = await client.get("/api/ok")
    res_ok.assert_ok()
    res_ok.assert_header("X-Global-Middleware", "200")
    res_ok.assert_header("X-Group-Middleware", "1")
    assert events == [
        "global:before:/api/ok",
        "group:before",
        "handler:ok",
        "group:after",
        "global:after:200",
    ]

    # 2. 404 Not Found: global middleware still wraps and post-processes 404 response
    events.clear()
    res_404 = await client.get("/nonexistent")
    res_404.assert_not_found()
    res_404.assert_header("X-Global-Middleware", "404")
    assert events == ["global:before:/nonexistent", "global:after:404"]

    # 3. 405 Method Not Allowed: global middleware post-processes 405 response
    events.clear()
    res_405 = await client.post("/api/ok")
    res_405.assert_method_not_allowed()
    res_405.assert_header("X-Global-Middleware", "405")
    assert events == ["global:before:/api/ok", "global:after:405"]

    # 4. 500 Internal Server Error: global middleware sees the 500 response produced by ExceptionHandler
    events.clear()
    res_500 = await client.get("/api/boom")
    res_500.assert_server_error()
    res_500.assert_header("X-Global-Middleware", "500")
    assert events == [
        "global:before:/api/boom",
        "group:before",
        "handler:boom",
        "global:after:500",
    ]


@pytest.mark.asyncio
async def test_exception_handler_mro_specificity_and_no_errors_duck_typing() -> None:
    app = Application(auto_discover_providers=False, debug=False)
    client = TestClient(app)

    class DomainError(Exception):
        pass

    class SpecificDomainError(DomainError):
        pass

    class ThirdPartyLibraryException(Exception):
        def __init__(self) -> None:
            # Should NOT be duck-typed as a 422 validation error!
            self.errors = {"internal_sql": ["secret table structure"]}
            super().__init__("internal driver error")

    # Register base class BEFORE subclass
    @app.exception_handler_for(DomainError)
    async def handle_base(exc: DomainError, request: Any) -> Response:
        return json_response({"handled_by": "base"}, status_code=400)

    @app.exception_handler_for(SpecificDomainError)
    async def handle_specific(exc: SpecificDomainError, request: Any) -> Response:
        return json_response({"handled_by": "specific"}, status_code=409)

    @app.get("/specific")
    async def specific_route() -> Response:
        raise SpecificDomainError("conflict")

    @app.get("/base")
    async def base_route() -> Response:
        raise DomainError("general")

    @app.get("/third-party-errors-attr")
    async def third_party_route() -> Response:
        raise ThirdPartyLibraryException()

    # Subclass handler wins by MRO specificity despite base being registered first
    res_spec = await client.get("/specific")
    res_spec.assert_status(409)
    assert res_spec.json() == {"handled_by": "specific"}

    res_base = await client.get("/base")
    res_base.assert_status(400)
    assert res_base.json() == {"handled_by": "base"}

    # Non-framework exception with .errors dict must return 500, NOT 422 leaking .errors
    res_tp = await client.get("/third-party-errors-attr")
    res_tp.assert_server_error()
    assert res_tp.json() == {"detail": "Internal Server Error"}
    assert "internal_sql" not in res_tp.text


@pytest.mark.asyncio
async def test_http_exception_logging_levels_and_exception_handler_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = Application(auto_discover_providers=False, debug=False)
    client = TestClient(app)

    @app.get("/bad-req")
    async def bad_req() -> Response:
        raise BadRequestError("Missing query param")

    @app.get("/srv-err")
    async def srv_err() -> Response:
        raise InternalServerError("Upstream timeout")

    with caplog.at_level(logging.INFO, logger="swiftpy.core.exceptions"):
        res_400 = await client.get("/bad-req")
        res_400.assert_bad_request()
        assert any(
            r.levelno == logging.INFO and "400" in r.getMessage()
            for r in caplog.records
        )

        caplog.clear()
        res_500 = await client.get("/srv-err")
        res_500.assert_server_error()
        assert any(
            r.levelno == logging.ERROR and "500" in r.getMessage()
            for r in caplog.records
        )

    # If ExceptionHandler.handle itself raises unexpectedly, Application still sends a minimal 500
    class ExplodingExceptionHandler(ExceptionHandler):
        async def handle(
            self, exc: Exception, request: Request | None = None
        ) -> Response:
            raise RuntimeError("ExceptionHandler itself crashed")

    app.exception_handler = ExplodingExceptionHandler(debug=False)
    res_fallback = await client.get("/bad-req")
    res_fallback.assert_server_error()
    assert res_fallback.json() == {"detail": "Internal Server Error"}


def test_refuse_debug_true_when_app_env_is_production() -> None:
    with pytest.raises(ConfigurationError, match=r"(?i)debug.*production"):
        Application(
            auto_discover_providers=False,
            debug=True,
            config={"app": {"env": "production", "debug": True}},
        )

    with pytest.raises(ConfigurationError, match=r"(?i)debug.*production"):
        Application(
            auto_discover_providers=False,
            config={"app": {"env": "production", "debug": True}},
        )


@pytest.mark.asyncio
async def test_builtin_security_headers_request_id_and_cors_middleware() -> None:
    # 1. Default CORS is deny-all; SecurityHeaders and RequestId attach expected headers
    app_deny = Application(
        auto_discover_providers=False,
        config={"cors": {"allowed_origins": []}},
    )
    app_deny.middleware([RequestId, SecurityHeaders, CORS])

    @app_deny.get("/data")
    async def get_data(request: Request) -> Response:
        return json_response({"request_id": request.state.get("request_id")})

    client_deny = TestClient(app_deny)

    res = await client_deny.get(
        "/data", headers={"Origin": "https://evil.example.com"}
    )
    res.assert_ok()
    res.assert_header("X-Content-Type-Options", "nosniff")
    res.assert_header("X-Frame-Options", "DENY")
    res.assert_header("Referrer-Policy", "strict-origin-when-cross-origin")
    assert "x-request-id" in res.headers
    assert res.json()["request_id"] == res.headers["x-request-id"]
    assert "access-control-allow-origin" not in res.headers

    # Preserves client-supplied X-Request-ID when valid
    res_custom_id = await client_deny.get(
        "/data", headers={"X-Request-ID": "req-abc-123"}
    )
    res_custom_id.assert_header("X-Request-ID", "req-abc-123")

    # Denied preflight does not emit Access-Control-Allow-Origin
    res_preflight_deny = await client_deny.options(
        "/data",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in res_preflight_deny.headers

    # 2. Configured CORS allows matching Origin on preflight and actual requests
    app_allow = Application(
        auto_discover_providers=False,
        config={
            "cors": {
                "allowed_origins": ["https://app.example.com"],
                "allowed_methods": ["GET", "POST"],
                "allowed_headers": ["Authorization", "Content-Type"],
                "expose_headers": ["X-Request-ID"],
                "allow_credentials": True,
                "max_age": 3600,
            }
        },
    )
    app_allow.middleware([CORS])

    @app_allow.get("/items")
    async def items() -> Response:
        return json_response({"items": []})

    client_allow = TestClient(app_allow)

    # Allowed preflight
    res_pre_ok = await client_allow.options(
        "/items",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    res_pre_ok.assert_no_content()
    res_pre_ok.assert_header(
        "Access-Control-Allow-Origin", "https://app.example.com"
    )
    res_pre_ok.assert_header("Access-Control-Allow-Credentials", "true")
    res_pre_ok.assert_header("Access-Control-Max-Age", "3600")
    assert "POST" in res_pre_ok.headers["Access-Control-Allow-Methods"]

    # Disallowed origin on app_allow
    res_pre_bad = await client_allow.options(
        "/items",
        headers={
            "Origin": "https://other.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in res_pre_bad.headers

    # Allowed actual GET request
    res_get_ok = await client_allow.get(
        "/items",
        headers={"Origin": "https://app.example.com"},
    )
    res_get_ok.assert_ok()
    res_get_ok.assert_header(
        "Access-Control-Allow-Origin", "https://app.example.com"
    )
    res_get_ok.assert_header("Access-Control-Expose-Headers", "X-Request-ID")
    res_get_ok.assert_header("Vary", "Origin")
