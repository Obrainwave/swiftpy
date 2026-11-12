from __future__ import annotations

import pytest

from swiftpy.core.application import Application
from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.http.router import RouteRegistrationError, Router
from swiftpy.testing.http_client import TestClient


class TracingMiddleware(Middleware):
    async def handle(self, request: Request, next_handler: Handler) -> Response:
        trace = request.state.setdefault("trace", [])
        trace.append("parent_mw")
        response = await next_handler(request)
        response.headers["X-Parent-Middleware"] = "active"
        return response


class ChildMiddleware(Middleware):
    async def handle(self, request: Request, next_handler: Handler) -> Response:
        trace = request.state.setdefault("trace", [])
        trace.append("child_mw")
        response = await next_handler(request)
        response.headers["X-Child-Middleware"] = "active"
        return response


@pytest.mark.asyncio
async def test_include_router_prefix_and_path_variations() -> None:
    app = Application(auto_discover_providers=False)
    router = Router()

    @router.get("/")
    async def root_child(request: Request) -> Response:
        return json_response({"page": "root_child"})

    @router.get("/users")
    async def list_users(request: Request) -> Response:
        return json_response({"page": "users"})

    @router.get("/users/{user_id}")
    async def get_user(request: Request, user_id: int) -> Response:
        return json_response({"user_id": user_id})

    @router.get("/trailing/")
    async def trailing_slash(request: Request) -> Response:
        return json_response({"page": "trailing"})

    app.include_router(router, prefix="/api")

    client = TestClient(app)
    # 1. Root route mounted under /api
    res_root = await client.get("/api")
    assert res_root.status_code == 200
    assert res_root.json() == {"page": "root_child"}

    # 2. Subpath
    res_users = await client.get("/api/users")
    assert res_users.status_code == 200
    assert res_users.json() == {"page": "users"}

    # 3. Path parameter coercion
    res_user_id = await client.get("/api/users/42")
    assert res_user_id.status_code == 200
    assert res_user_id.json() == {"user_id": 42}

    # 4. Trailing slash preservation
    res_trail = await client.get("/api/trailing/")
    assert res_trail.status_code == 200
    assert res_trail.json() == {"page": "trailing"}


def test_include_router_rejects_invalid_prefixes() -> None:
    app = Application(auto_discover_providers=False)
    router = Router()

    @router.get("/ping")
    async def ping(request: Request) -> Response:
        return json_response({"ok": True})

    with pytest.raises(RouteRegistrationError, match="whitespace"):
        app.include_router(router, prefix="api test")

    with pytest.raises(RouteRegistrationError, match="dynamic pattern"):
        app.include_router(router, prefix="/api/{id}")

    with pytest.raises(RouteRegistrationError, match="empty path segments"):
        app.include_router(router, prefix="//api")


@pytest.mark.asyncio
async def test_include_router_middleware_chaining() -> None:
    app = Application(auto_discover_providers=False)
    child_router = Router()

    @child_router.route("GET", "/profile", middleware=[ChildMiddleware])
    async def get_profile(request: Request) -> Response:
        trace: list[str] = request.state.get("trace", [])
        return json_response({"trace": trace})

    app.include_router(child_router, prefix="/api", middleware=[TracingMiddleware])

    client = TestClient(app)
    res = await client.get("/api/profile")
    assert res.status_code == 200
    data = res.json()
    # Parent middleware must execute before child middleware
    assert data["trace"] == ["parent_mw", "child_mw"]
    assert res.headers["x-parent-middleware"] == "active"
    assert res.headers["x-child-middleware"] == "active"


@pytest.mark.asyncio
async def test_include_multiple_routers() -> None:
    app = Application(auto_discover_providers=False)

    v1 = Router()
    v2 = Router()

    @v1.get("/version")
    async def get_v1(request: Request) -> Response:
        return json_response({"version": "v1"})

    @v2.get("/version")
    async def get_v2(request: Request) -> Response:
        return json_response({"version": "v2"})

    app.include_router(v1, prefix="api/v1")
    app.include_router(v2, prefix="/api/v2/")

    client = TestClient(app)
    res1 = await client.get("/api/v1/version")
    assert res1.status_code == 200
    assert res1.json() == {"version": "v1"}

    res2 = await client.get("/api/v2/version")
    assert res2.status_code == 200
    assert res2.json() == {"version": "v2"}


@pytest.mark.asyncio
async def test_include_router_without_prefix() -> None:
    app = Application(auto_discover_providers=False)
    router = Router()

    @router.get("/ping")
    async def ping_handler(request: Request) -> Response:
        return json_response({"message": "pong"})

    app.include_router(router)

    client = TestClient(app)
    res = await client.get("/ping")
    assert res.status_code == 200
    assert res.json() == {"message": "pong"}


class _AppLevelMiddleware(Middleware):
    async def handle(self, request: Request, next_handler: Handler) -> Response:
        request.state.setdefault("trace", []).append("app_mw")
        return await next_handler(request)


class _ApiLevelMiddleware(Middleware):
    async def handle(self, request: Request, next_handler: Handler) -> Response:
        request.state.setdefault("trace", []).append("api_mw")
        return await next_handler(request)


class _AdminLevelMiddleware(Middleware):
    async def handle(self, request: Request, next_handler: Handler) -> Response:
        request.state.setdefault("trace", []).append("admin_mw")
        return await next_handler(request)


class _RouteLevelMiddleware(Middleware):
    async def handle(self, request: Request, next_handler: Handler) -> Response:
        request.state.setdefault("trace", []).append("route_mw")
        return await next_handler(request)


@pytest.mark.asyncio
async def test_nested_router_composition_and_multi_tier_middleware_order() -> None:
    """
    Verify full hierarchical router composition:
    Application -> Router (/api) -> Nested Router (/admin) -> Route (/users)
    Strictly verifies execution order:
    Application middleware -> Router middleware -> Nested router middleware -> Route middleware -> Handler
    """
    admin_router = Router()

    @admin_router.get("/users", middleware=[_RouteLevelMiddleware])
    async def admin_users(request: Request) -> Response:
        trace: list[str] = request.state.get("trace", [])
        return json_response({"trace": trace, "users": ["admin1", "admin2"]})

    api_router = Router()
    api_router.include_router(admin_router, prefix="/admin", middleware=[_AdminLevelMiddleware])

    app = Application(middleware=[_AppLevelMiddleware], auto_discover_providers=False)
    app.include_router(api_router, prefix="/api", middleware=[_ApiLevelMiddleware])

    client = TestClient(app)
    res = await client.get("/api/admin/users")
    assert res.status_code == 200
    data = res.json()
    assert data["users"] == ["admin1", "admin2"]
    assert data["trace"] == ["app_mw", "api_mw", "admin_mw", "route_mw"]


@pytest.mark.asyncio
async def test_jit_route_recompilation_after_application_boot() -> None:
    """
    Verify JIT route recompilation when routers are included on an already booted Application.
    """
    app = Application(auto_discover_providers=False)
    base_router = Router()

    @base_router.get("/initial")
    async def initial_handler(request: Request) -> Response:
        return json_response({"status": "initial"})

    app.include_router(base_router)
    app.boot()
    assert app._booted is True

    client = TestClient(app)
    res_init = await client.get("/initial")
    assert res_init.status_code == 200
    assert res_init.json() == {"status": "initial"}

    # Include first JIT router post-boot
    jit_router_1 = Router()

    @jit_router_1.get("/dynamic")
    async def dynamic_1(request: Request) -> Response:
        return json_response({"status": "dynamic_1"})

    app.include_router(jit_router_1, prefix="/v1")

    # Send request without restarting application
    res_jit_1 = await client.get("/v1/dynamic")
    assert res_jit_1.status_code == 200
    assert res_jit_1.json() == {"status": "dynamic_1"}

    # Include second JIT router post-boot
    jit_router_2 = Router()

    @jit_router_2.get("/second")
    async def dynamic_2(request: Request) -> Response:
        return json_response({"status": "dynamic_2"})

    app.include_router(jit_router_2, prefix="/v2")

    # Verify both previously existing and newly added routes work
    assert (await client.get("/initial")).status_code == 200
    assert (await client.get("/v1/dynamic")).status_code == 200
    res_jit_2 = await client.get("/v2/second")
    assert res_jit_2.status_code == 200
    assert res_jit_2.json() == {"status": "dynamic_2"}


@pytest.mark.asyncio
async def test_router_reuse_and_state_independence() -> None:
    """
    Verify router reuse across multiple prefixes and separate applications,
    and confirm that post-mount router mutations do not pollute already mounted applications.
    """
    shared_router = Router()

    @shared_router.get("/status")
    async def status_handler(request: Request) -> Response:
        return json_response({"alive": True})

    app1 = Application(auto_discover_providers=False)
    app2 = Application(auto_discover_providers=False)

    app1.include_router(shared_router, prefix="/internal")
    app1.include_router(shared_router, prefix="/public/v1")
    app2.include_router(shared_router, prefix="/cluster")

    c1 = TestClient(app1)
    c2 = TestClient(app2)

    res_internal = await c1.get("/internal/status")
    assert res_internal.status_code == 200
    assert res_internal.json() == {"alive": True}

    res_public = await c1.get("/public/v1/status")
    assert res_public.status_code == 200

    res_cluster = await c2.get("/cluster/status")
    assert res_cluster.status_code == 200

    # Post-mount mutation on shared_router
    @shared_router.get("/subsequent_mutation")
    async def subsequent(request: Request) -> Response:
        return json_response({"mutated": True})

    # Already mounted applications remain isolated from subsequent mutations
    assert (await c1.get("/internal/subsequent_mutation")).status_code == 404
    assert (await c2.get("/cluster/subsequent_mutation")).status_code == 404
