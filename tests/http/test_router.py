from __future__ import annotations

import asyncio
import inspect
from typing import Any, ClassVar
from unittest.mock import patch

import pytest

from swiftpy.auth.auth_manager import CurrentUser
from swiftpy.auth.jwt_guard import JWTGuard
from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.core.application import Application
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.http.router import RouteGroup
from swiftpy.testing.http_client import TestClient


@pytest.mark.asyncio
async def test_regex_escaping_in_literal_path_segments() -> None:
    app = Application(auto_discover_providers=False)
    client = TestClient(app)

    @app.get("/v1.0/ping")
    async def ping() -> Response:
        return json_response({"pong": True})

    # Exact dot matches
    res_ok = await client.get("/v1.0/ping")
    res_ok.assert_ok()
    assert res_ok.json() == {"pong": True}

    # Unescaped dot would match 'x'; escaped regex must return 404
    res_bad = await client.get("/v1x0/ping")
    res_bad.assert_not_found()


@pytest.mark.asyncio
async def test_route_precedence_matrix_static_before_dynamic() -> None:
    app = Application(auto_discover_providers=False)
    client = TestClient(app)

    # Register dynamic routes BEFORE static routes
    @app.get("/users/{user_id}")
    async def user_by_id(user_id: str) -> Response:
        return json_response({"matched": "dynamic_user", "user_id": user_id})

    @app.get("/users/me")
    async def user_me() -> Response:
        return json_response({"matched": "static_me"})

    # Register 2-dynamic route BEFORE 1-dynamic route
    @app.get("/orgs/{org}/{section}")
    async def org_section(org: str, section: str) -> Response:
        return json_response({"matched": "two_dynamic", "org": org, "section": section})

    @app.get("/orgs/{org}/settings")
    async def org_settings(org: str) -> Response:
        return json_response({"matched": "one_dynamic_settings", "org": org})

    # Equal dynamic count (1), but static segment appears earlier in second route
    @app.get("/items/{item_id}/export")
    async def item_export(item_id: str) -> Response:
        return json_response({"matched": "dynamic_first", "item_id": item_id})

    @app.get("/items/featured/{fmt}")
    async def featured_export(fmt: str) -> Response:
        return json_response({"matched": "static_first", "fmt": fmt})

    res_me = await client.get("/users/me")
    res_me.assert_ok()
    assert res_me.json() == {"matched": "static_me"}

    res_id = await client.get("/users/alice")
    res_id.assert_ok()
    assert res_id.json() == {"matched": "dynamic_user", "user_id": "alice"}

    res_settings = await client.get("/orgs/acme/settings")
    res_settings.assert_ok()
    assert res_settings.json() == {"matched": "one_dynamic_settings", "org": "acme"}

    res_sec = await client.get("/orgs/acme/billing")
    res_sec.assert_ok()
    assert res_sec.json() == {
        "matched": "two_dynamic",
        "org": "acme",
        "section": "billing",
    }

    res_feat = await client.get("/items/featured/export")
    res_feat.assert_ok()
    assert res_feat.json() == {"matched": "static_first", "fmt": "export"}


@pytest.mark.asyncio
async def test_inspect_signature_not_called_per_request() -> None:
    app = Application(auto_discover_providers=False)
    client = TestClient(app)

    @app.get("/calc/{value}")
    async def calc(value: int) -> Response:
        return json_response({"doubled": value * 2})

    app.boot()

    with patch.object(
        inspect, "signature", wraps=inspect.signature
    ) as mocked_signature:
        for i in range(5):
            res = await client.get(f"/calc/{i}")
            res.assert_ok()
            assert res.json() == {"doubled": i * 2}

        assert mocked_signature.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_flag", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("YES", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("NO", False),
    ],
)
async def test_strict_bool_path_param_coercion_valid(
    raw_flag: str, expected: bool
) -> None:
    app = Application(auto_discover_providers=False)
    client = TestClient(app)

    @app.get("/flags/{active}")
    async def check_flag(active: bool) -> Response:
        return json_response({"active": active})

    res = await client.get(f"/flags/{raw_flag}")
    res.assert_ok()
    assert res.json() == {"active": expected}


@pytest.mark.asyncio
async def test_invalid_bool_int_float_path_params_return_422() -> None:
    app = Application(auto_discover_providers=False)
    client = TestClient(app)

    @app.get("/typed/{flag}/{count}/{ratio}")
    async def typed_route(flag: bool, count: int, ratio: float) -> Response:
        return json_response({"flag": flag, "count": count, "ratio": ratio})

    # Invalid bool
    res_bool = await client.get("/typed/maybe/10/1.5")
    res_bool.assert_validation_error("flag")

    # Invalid int
    res_int = await client.get("/typed/true/not_an_int/1.5")
    res_int.assert_validation_error("count")

    # Invalid float
    res_float = await client.get("/typed/false/10/not_a_float")
    res_float.assert_validation_error("ratio")

    # Non-finite float (nan / inf)
    res_nan = await client.get("/typed/false/10/nan")
    res_nan.assert_validation_error("ratio")


@pytest.mark.asyncio
async def test_websocket_route_kind_remains_internal_only() -> None:
    app = Application(auto_discover_providers=False)
    client = TestClient(app)
    ws_called = False

    @app.websocket("/ws-only")
    async def ws_only(ws: Any) -> None:
        nonlocal ws_called
        ws_called = True

    @app.get("/dual")
    async def dual_http() -> Response:
        return json_response({"http": True})

    @app.websocket("/dual")
    async def dual_ws(ws: Any) -> None:
        nonlocal ws_called
        ws_called = True

    # HTTP request with method WEBSOCKET to WS-only route returns 404
    res_404 = await client.request("WEBSOCKET", "/ws-only")
    res_404.assert_not_found()
    assert not ws_called

    # HTTP request with method WEBSOCKET to dual route returns 405 with Allow: GET, HEAD
    res_405 = await client.request("WEBSOCKET", "/dual")
    res_405.assert_method_not_allowed()
    assert "WEBSOCKET" not in res_405.headers.get("Allow", "")
    assert "GET" in res_405.headers.get("Allow", "")
    assert not ws_called

    # Direct HTTP route registration with method "WEBSOCKET" is rejected
    with pytest.raises((ValueError, TypeError)):
        app.router.add_route("WEBSOCKET", "/bad-ws", dual_http)


def test_duplicate_route_registration_raises() -> None:
    app = Application(auto_discover_providers=False)

    @app.get("/users/{id}")
    async def get_user_1(id: int) -> Response:
        return json_response({"id": id})

    with pytest.raises((ValueError, TypeError)):

        @app.get("/users/{user_id}")
        async def get_user_2(user_id: int) -> Response:
            return json_response({"id": user_id})


@pytest.mark.asyncio
async def test_automatic_head_options_and_trailing_slash_exact_match() -> None:
    app = Application(auto_discover_providers=False)
    client = TestClient(app)

    @app.get("/articles")
    async def list_articles() -> Response:
        return json_response({"articles": ["a", "b"]})

    @app.post("/articles")
    async def create_article() -> Response:
        return json_response({"created": True}, status_code=201)

    # Automatic HEAD falls back to GET, keeps Content-Length, sends empty body
    res_get = await client.get("/articles")
    res_head = await client.request("HEAD", "/articles")
    res_head.assert_ok()
    assert res_head.body == b""
    assert res_head.headers["content-length"] == res_get.headers["content-length"]

    # Automatic OPTIONS returns 204 with Allow header
    res_opt = await client.request("OPTIONS", "/articles")
    res_opt.assert_no_content()
    allow = [m.strip() for m in res_opt.headers["Allow"].split(",")]
    assert sorted(allow) == ["GET", "HEAD", "OPTIONS", "POST"]

    # 405 includes Allow header with HEAD when GET is registered
    res_del = await client.delete("/articles")
    res_del.assert_method_not_allowed()
    allow_405 = [m.strip() for m in res_del.headers["Allow"].split(",")]
    assert sorted(allow_405) == ["GET", "HEAD", "POST"]

    # Redirect-free exact-match trailing slash policy: /articles/ is 404
    res_slash = await client.get("/articles/")
    res_slash.assert_not_found()


@pytest.mark.asyncio
async def test_non_form_request_with_schema_not_treated_as_body_validator_and_ambiguous_handler_rejected() -> None:
    app = Application(auto_discover_providers=False)
    client = TestClient(app)

    class ServiceWithSchemaAttr:
        schema: ClassVar[dict[str, str]] = {"required_field": "required"}

    app.container.singleton(ServiceWithSchemaAttr)

    @app.post("/with-schema-service")
    async def handle_service(svc: ServiceWithSchemaAttr) -> Response:
        return json_response({"injected": isinstance(svc, ServiceWithSchemaAttr)})

    # Sending empty body succeeds because ServiceWithSchemaAttr is a DI service, not a FormRequest
    res = await client.post("/with-schema-service")
    res.assert_ok()
    assert res.json() == {"injected": True}

    # Ambiguous unannotated parameter fails at registration naming handler and parameter
    with pytest.raises((TypeError, ValueError), match=r"ambiguous_handler.*mystery"):

        @app.get("/ambiguous")
        async def ambiguous_handler(mystery: Any) -> Response:
            return json_response({"mystery": str(mystery)})


@pytest.mark.asyncio
async def test_di_current_user_injection_and_guard_401() -> None:
    secret = "s" * 40
    app = Application(
        auto_discover_providers=False,
        config={"auth": {"jwt": {"secret": secret}}},
    )
    issuer = JWTIssuer(secret=secret)
    app.container.instance(JWTIssuer, issuer)
    app.container.scoped(
        CurrentUser,
        lambda _: CurrentUser(),
    )

    handler_called = False

    @app.group("/api", middleware=[JWTGuard])
    def api_routes(group: RouteGroup) -> None:
        @group.get("/me")
        async def me(request: Request, user: CurrentUser) -> Response:
            nonlocal handler_called
            handler_called = True
            return json_response(
                {
                    "id": user.id,
                    "email": user.email,
                    "authenticated": user.is_authenticated,
                }
            )

    client = TestClient(app)

    # 1. Unauthenticated request returns 401 before handler runs
    res_unauth = await client.get("/api/me")
    res_unauth.assert_unauthorized()
    assert not handler_called

    # 2. Authenticated request injects CurrentUser populated by JWTGuard
    token = issuer.issue_access_token("user-99", claims={"email": "u99@example.com"})
    res_auth = await client.with_token(token).get("/api/me")
    res_auth.assert_ok()
    assert handler_called
    assert res_auth.json() == {
        "id": "user-99",
        "email": "u99@example.com",
        "authenticated": True,
    }


@pytest.mark.asyncio
async def test_di_custom_scoped_service_isolated_per_concurrent_request() -> None:
    app = Application(auto_discover_providers=False)

    class RequestScopedContext:
        pass

    app.container.scoped(RequestScopedContext)

    @app.get("/scoped-check")
    async def scoped_check(svc: RequestScopedContext) -> Response:
        # Resolving again inside the same request must return the exact same instance
        resolved_again = app.container.resolve(RequestScopedContext)
        assert svc is resolved_again
        await asyncio.sleep(0.005)
        return json_response({"instance_id": id(svc)})

    client = TestClient(app)
    responses = await asyncio.gather(
        *[client.get("/scoped-check") for _ in range(25)]
    )
    instance_ids = {r.json()["instance_id"] for r in responses}
    assert len(instance_ids) == 25


def test_unsatisfiable_handler_parameter_fails_at_registration() -> None:
    app = Application(auto_discover_providers=False)

    class UnsatisfiableDependency:
        def __init__(self, required_primitive: int) -> None:
            self.required_primitive = required_primitive

    with pytest.raises(
        (TypeError, ValueError),
        match=r"bad_primitive_handler.*missing_id",
    ):

        @app.get("/bad-primitive")
        async def bad_primitive_handler(missing_id: int) -> Response:
            return json_response({"id": missing_id})

    with pytest.raises(
        (TypeError, ValueError),
        match=r"bad_service_handler.*dep",
    ):

        @app.get("/bad-service")
        async def bad_service_handler(dep: UnsatisfiableDependency) -> Response:
            return json_response({"dep": dep.required_primitive})
