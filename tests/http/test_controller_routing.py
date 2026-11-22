from __future__ import annotations

import asyncio
from typing import Any

import pytest

from swiftpy.core.application import Application
from swiftpy.core.container import Container
from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.http.router import RouteGroup, Router, RouteRegistrationError
from swiftpy.testing.http_client import TestClient
from swiftpy.validation.form_request import FormRequest
from swiftpy.websockets.connection import WebSocketConnection


class _TraceMiddleware(Middleware):
    async def handle(self, request: Request, next_handler: Handler) -> Response:
        request.state["mw_ran"] = True
        response = await next_handler(request)
        response.headers["X-Controller-MW"] = "yes"
        return response


class _ScopedAuditContext:
    def __init__(self) -> None:
        self.events: list[str] = []


class _SamplePostService:
    def __init__(self, audit: _ScopedAuditContext) -> None:
        self.audit = audit

    async def list_posts(self) -> list[dict[str, object]]:
        self.audit.events.append("list")
        return [{"id": 1, "title": "Hello SwiftPY"}]

    async def get_post(self, post_id: int) -> dict[str, object]:
        self.audit.events.append(f"get:{post_id}")
        return {"id": post_id, "title": f"Post #{post_id}"}

    async def create_post(self, title: str) -> dict[str, object]:
        self.audit.events.append(f"create:{title}")
        return {"id": 2, "title": title}


class _CreatePostForm(FormRequest):
    def rules(self) -> dict[str, str]:
        return {"title": "required|string|min:3"}


class _SamplePostController:
    def __init__(self, service: _SamplePostService) -> None:
        self.service = service

    async def index(self, request: Request) -> Response:
        _ = request
        posts = await self.service.list_posts()
        return json_response(posts)

    async def show(self, request: Request, post_id: int) -> Response:
        _ = request
        post = await self.service.get_post(post_id)
        return json_response(
            {
                "post": post,
                "controller_id": id(self),
                "audit_events": list(self.service.audit.events),
            }
        )

    async def store(self, request: Request, form: _CreatePostForm) -> Response:
        _ = request
        data = form.validated()
        created = await self.service.create_post(str(data["title"]))
        return json_response(created, status_code=201)

    async def update(self, post_id: int, service: _SamplePostService) -> Response:
        return json_response(
            {
                "updated": post_id,
                "same_scoped_service": service is self.service,
                "same_audit": service.audit is self.service.audit,
            }
        )

    async def destroy(self, post_id: int) -> dict[str, object]:
        return {"deleted": post_id}

    async def upload(self, request: Request) -> dict[str, object]:
        has_img = bool(request.has_file("image"))
        return {"has_image": has_img}

    @staticmethod
    async def health(request: Request) -> dict[str, str]:
        _ = request
        return {"status": "ok"}

    @classmethod
    async def cls_info(cls, request: Request) -> dict[str, str]:
        _ = request
        return {"controller": cls.__name__}


class _PositionalSelfController:
    def __init__(self, service: _SamplePostService) -> None:
        self.service = service

    async def index(self, /, request: Request) -> dict[str, object]:
        _ = request
        return {"items": await self.service.list_posts()}


class _UnsatisfiableController:
    def __init__(self, missing_secret: str) -> None:
        self.missing_secret = missing_secret

    async def index(self, request: Request) -> Response:
        _ = request
        return json_response({"secret": self.missing_secret})


class _SampleWsController:
    def __init__(self, service: _SamplePostService) -> None:
        self.service = service

    async def handle(self, ws: WebSocketConnection, room: str) -> None:
        await ws.accept()
        posts = await self.service.list_posts()
        await ws.send_json({"room": room, "count": len(posts)})
        await ws.close()


@pytest.mark.asyncio
async def test_direct_route_registration_and_unbound_controller_di() -> None:
    app = Application(auto_discover_providers=False)
    app.container.scoped(_ScopedAuditContext)

    router = Router(middleware=[_TraceMiddleware])
    router.get("/", _SamplePostController.index)
    router.get("/health", _SamplePostController.health)
    router.get("/cls", _SamplePostController.cls_info)
    router.get("/{post_id}", _SamplePostController.show)
    router.post("/", _SamplePostController.store)
    router.put("/{post_id}", _SamplePostController.update)
    router.delete("/{post_id}", _SamplePostController.destroy)
    router.post("/upload", _SamplePostController.upload)
    router.get("/pos-self/list", _PositionalSelfController.index)

    app.include_router(router, prefix="/api/posts")

    # Direct registration on Application and RouteGroup also works
    async def direct_ping(request: Request) -> dict[str, bool]:
        _ = request
        return {"pong": True}

    app.get("/direct-ping", direct_ping)

    @app.group("/v2")
    def v2_group(group: RouteGroup) -> None:
        group.get("/posts", _SamplePostController.index)
        group.patch("/posts/{post_id}", _SamplePostController.destroy)

    client = TestClient(app)

    res_list = await client.get("/api/posts")
    res_list.assert_ok()
    assert res_list.json() == [{"id": 1, "title": "Hello SwiftPY"}]
    assert res_list.headers.get("X-Controller-MW") == "yes"

    res_health = await client.get("/api/posts/health")
    res_health.assert_ok()
    assert res_health.json() == {"status": "ok"}

    res_cls = await client.get("/api/posts/cls")
    res_cls.assert_ok()
    assert res_cls.json() == {"controller": "_SamplePostController"}

    res_show = await client.get("/api/posts/42")
    res_show.assert_ok()
    assert res_show.json()["post"] == {"id": 42, "title": "Post #42"}
    assert res_show.json()["audit_events"] == ["get:42"]

    res_store = await client.post("/api/posts", json={"title": "New Post Title"})
    res_store.assert_created()
    assert res_store.json() == {"id": 2, "title": "New Post Title"}

    # Scoped dependencies are shared within a single request across controller __init__ and action args
    res_put = await client.put("/api/posts/9")
    res_put.assert_ok()
    assert res_put.json() == {
        "updated": 9,
        "same_scoped_service": False,  # _SamplePostService is transient; _ScopedAuditContext is scoped
        "same_audit": True,
    }

    res_del = await client.delete("/api/posts/9")
    res_del.assert_ok()
    assert res_del.json() == {"deleted": 9}

    res_pos = await client.get("/api/posts/pos-self/list")
    res_pos.assert_ok()
    assert res_pos.json() == {"items": [{"id": 1, "title": "Hello SwiftPY"}]}

    res_ping = await client.get("/direct-ping")
    res_ping.assert_ok()
    assert res_ping.json() == {"pong": True}

    res_v2 = await client.get("/v2/posts")
    res_v2.assert_ok()
    assert res_v2.json() == [{"id": 1, "title": "Hello SwiftPY"}]

    res_v2_patch = await client.patch("/v2/posts/77")
    res_v2_patch.assert_ok()
    assert res_v2_patch.json() == {"deleted": 77}

    # Multipart upload with sync request.has_file detection inside unbound controller method
    boundary = "----SwiftPYControllerUploadBoundary"
    multipart_body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="image"; filename="photo.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode() + b"\x89PNG\r\n\x1a\nfake" + f"\r\n--{boundary}--\r\n".encode()
    res_upload = await client.post(
        "/api/posts/upload",
        data=multipart_body,
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )
    res_upload.assert_ok()
    assert res_upload.json() == {"has_image": True}


@pytest.mark.asyncio
async def test_concurrent_unbound_controller_requests_are_isolated() -> None:
    app = Application(auto_discover_providers=False)
    app.container.scoped(_ScopedAuditContext)
    active_ids: list[int] = []

    class ConcurrentPostController:
        def __init__(self, service: _SamplePostService) -> None:
            self.service = service

        async def show(self, request: Request, post_id: int) -> Response:
            _ = request
            # Keep a reference alive across the await so CPython does not recycle memory addresses
            active_ids.append(id(self))
            await asyncio.sleep(0.01)
            post = await self.service.get_post(post_id)
            return json_response(
                {
                    "post": post,
                    "controller_id": id(self),
                    "audit_events": list(self.service.audit.events),
                }
            )

    router = Router()
    router.get("/posts/{post_id}", ConcurrentPostController.show)
    app.include_router(router)

    client = TestClient(app)
    responses = await asyncio.gather(
        *(client.get(f"/posts/{idx}") for idx in range(1, 21))
    )
    controller_ids = {r.json()["controller_id"] for r in responses}
    assert len(controller_ids) == 20
    assert len(set(active_ids)) == 20
    for idx, resp in enumerate(responses, start=1):
        assert resp.json()["audit_events"] == [f"get:{idx}"]


@pytest.mark.asyncio
async def test_bound_controller_instance_method_and_websocket_controller_method() -> None:
    app = Application(auto_discover_providers=False)
    audit = _ScopedAuditContext()
    bound_controller = _SamplePostController(_SamplePostService(audit))

    app.get("/bound", bound_controller.index)
    app.websocket("/ws/rooms/{room}", _SampleWsController.handle)

    client = TestClient(app)
    res = await client.get("/bound")
    res.assert_ok()
    assert res.json() == [{"id": 1, "title": "Hello SwiftPY"}]

    async with client.websocket_connect("/ws/rooms/general") as ws:
        msg = await ws.receive_json()
        assert msg == {"room": "general", "count": 1}


def test_invalid_direct_handlers_and_unsatisfiable_controller_fail_at_registration() -> None:
    app = Application(auto_discover_providers=False)

    bad_handler: Any = "not_callable"
    with pytest.raises(RouteRegistrationError, match="must be callable"):
        app.get("/bad", bad_handler)

    with pytest.raises(RouteRegistrationError, match="not class"):
        app.get("/bad-cls", _SamplePostController)

    with pytest.raises(RouteRegistrationError, match="cannot be resolved"):
        app.get("/bad-controller", _UnsatisfiableController.index)

    # Deferred standalone router still validates once mounted onto Application
    standalone = Router()
    standalone.get("/deferred-bad", _UnsatisfiableController.index)
    with pytest.raises(RouteRegistrationError, match="cannot be resolved"):
        app.include_router(standalone)


def test_local_controller_class_without_deferred_annotations_resolves() -> None:
    app = Application(auto_discover_providers=False)

    class LocalController:
        def __init__(self, container: Container) -> None:
            self.container = container

        async def ping(self, request: Request) -> dict[str, Any]:
            _ = request
            return {"has_container": isinstance(self.container, Container)}

    app.get("/local", LocalController.ping)

    async def _run() -> None:
        client = TestClient(app)
        res = await client.get("/local")
        res.assert_ok()
        assert res.json() == {"has_container": True}

    asyncio.run(_run())


def _build_dynamic_factory_controller(tag: str) -> type[Any]:
    class FactoryController:
        def __init__(self, service: _SamplePostService) -> None:
            self.service = service

        async def index(self, request: Request) -> Response:
            _ = request
            posts = await self.service.list_posts()
            return json_response({"tag": tag, "count": len(posts)})

    return FactoryController


@pytest.mark.asyncio
async def test_app_and_routegroup_middleware_order_and_factory_controller_resolution() -> None:
    """Fixes 4 & 7: Application and RouteGroup middleware= support + factory controller localns resolution."""
    app = Application(auto_discover_providers=False)
    app.container.scoped(_ScopedAuditContext)

    class _OrderMW1(Middleware):
        async def handle(self, request: Request, next_handler: Handler) -> Response:
            order: list[str] = request.state.setdefault("mw_order", [])
            order.append("mw1")
            return await next_handler(request)

    class _OrderMW2(Middleware):
        async def handle(self, request: Request, next_handler: Handler) -> Response:
            order: list[str] = request.state.setdefault("mw_order", [])
            order.append("mw2")
            return await next_handler(request)

    class _OrderMW3(Middleware):
        async def handle(self, request: Request, next_handler: Handler) -> Response:
            order: list[str] = request.state.setdefault("mw_order", [])
            order.append("mw3")
            return await next_handler(request)

    app.middleware([_OrderMW1])

    # 1. Application.get(..., middleware=[...]) with unbound controller method
    class _EchoOrderController:
        def __init__(self, service: _SamplePostService) -> None:
            self.service = service

        async def index(self, request: Request) -> dict[str, Any]:
            return {"order": list(request.state.get("mw_order", []))}

    app.get("/app-mw", _EchoOrderController.index, middleware=[_OrderMW2, _OrderMW3])

    # 2. RouteGroup.get / post / websocket with middleware=[...] (deterministic order, no duplicate execution)
    @app.group("/grp", middleware=[_OrderMW2])
    def _grp(group: RouteGroup) -> None:
        group.get("/items", _EchoOrderController.index, middleware=[_OrderMW3])

        @group.websocket("/ws", middleware=[_OrderMW3])
        async def _ws_echo(ws: WebSocketConnection) -> None:
            await ws.accept()
            await ws.send_json({"order": list(ws.state.get("mw_order", []))})

    # 3. Fix 7: Controller class returned by a factory function that has already exited
    # and stored in a differently named local variable (`aliased_ctrl`)
    aliased_ctrl = _build_dynamic_factory_controller("from_factory")
    app.get("/factory", aliased_ctrl.index, middleware=[_OrderMW2])

    client = TestClient(app)

    r_app = await client.get("/app-mw")
    r_app.assert_ok()
    assert r_app.json() == {"order": ["mw1", "mw2", "mw3"]}

    r_grp = await client.get("/grp/items")
    r_grp.assert_ok()
    assert r_grp.json() == {"order": ["mw1", "mw2", "mw3"]}

    async with client.websocket_connect("/grp/ws") as ws:
        ws_msg = await ws.receive_json()
        assert ws_msg == {"order": ["mw1", "mw2", "mw3"]}

    r_factory = await client.get("/factory")
    r_factory.assert_ok()
    assert r_factory.json() == {"tag": "from_factory", "count": 1}

