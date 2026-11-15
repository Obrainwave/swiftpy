from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from swiftpy.core.application import Application
from swiftpy.core.context import Context
from swiftpy.core.exceptions import HttpException
from swiftpy.filesystem.exceptions import FileValidationError
from swiftpy.http.exceptions import ClientDisconnect
from swiftpy.http.middleware import CORS, Handler, Middleware
from swiftpy.http.request import Request, _validate_json_depth
from swiftpy.http.response import Response, StreamingResponse, json_response
from swiftpy.http.router import RouteGroup, Router
from swiftpy.testing.http_client import TestClient
from swiftpy.validation import FormRequest, ValidationException


class _CreateItemForm(FormRequest):
    def rules(self) -> dict[str, str]:
        return {"name": "required|string|min:2"}


class _GroupHeaderMiddleware(Middleware):
    async def handle(self, request: Request, next_handler: Handler) -> Response:
        response = await next_handler(request)
        response.headers["X-Group-Header"] = "group-active"
        return response


class _ScopedResource:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def _run_raw_http(
    app: Application,
    method: str,
    path: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
    on_body_chunk: Any = None,
) -> tuple[int, dict[str, str], bytes]:
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }
    body_sent = False
    status_code = 0
    resp_headers: dict[str, str] = {}
    resp_body = bytearray()

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = int(message["status"])
            for k_bytes, v_bytes in message.get("headers", []):
                resp_headers[k_bytes.decode("latin-1").lower()] = v_bytes.decode("latin-1")
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            if on_body_chunk is not None and chunk:
                on_body_chunk(chunk)
            resp_body.extend(chunk)

    await app(scope, receive, send)
    return status_code, resp_headers, bytes(resp_body)


@pytest.mark.asyncio
async def test_automatic_options_with_braces_in_path_and_group_middleware() -> None:
    """HTTP-1: OPTIONS on path containing '{' or '}' must not crash with 500 and preserves group middleware."""
    app = Application(
        auto_discover_providers=False,
        config={"cors": {"allowed_origins": ["https://app.example.com"]}},
    )

    @app.group("/api", middleware=[_GroupHeaderMiddleware, CORS])
    def api_routes(group: RouteGroup) -> None:
        @group.get("/items/{item_id}")
        async def get_item(item_id: str) -> Response:
            return json_response({"item_id": item_id})

    client = TestClient(app)

    res_group = await client.options("/api/items/{bad")
    res_group.assert_no_content()
    allow = sorted(m.strip() for m in res_group.headers["Allow"].split(","))
    assert allow == ["GET", "HEAD", "OPTIONS"]
    res_group.assert_header("X-Group-Header", "group-active")

    res_cors = await client.options(
        "/api/items/{unclosed",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    res_cors.assert_no_content()
    res_cors.assert_header("Access-Control-Allow-Origin", "https://app.example.com")
    res_cors.assert_header("X-Group-Header", "group-active")


@pytest.mark.asyncio
async def test_streaming_response_runs_inside_request_scope_closes_on_disconnect_and_head() -> None:
    """HTTP-3: StreamingResponse executes within request Context/DI scope, closes generator, and skips body on HEAD."""
    observations: list[tuple[str | None, bool]] = []
    tracked_resources: list[_ScopedResource] = []
    closed_events: list[str] = []
    head_iterations: list[int] = []

    class _ContextTraceMiddleware(Middleware):
        async def handle(self, request: Request, next_handler: Handler) -> Response:
            Context.set("trace_id", "req-trace-99")
            return await next_handler(request)

    app = Application(auto_discover_providers=False)
    app.container.scoped(_ScopedResource)
    app.middleware([_ContextTraceMiddleware])
    created_generators: list[Any] = []

    @app.get("/stream")
    async def stream_items(res: _ScopedResource) -> StreamingResponse:
        tracked_resources.append(res)

        async def _generate() -> AsyncIterator[bytes]:
            try:
                head_iterations.append(1)
                for idx in range(3):
                    observations.append((Context.get("trace_id"), res.closed))
                    yield f"chunk-{idx}|".encode()
            finally:
                closed_events.append("closed")

        gen = _generate()
        created_generators.append(gen)
        return StreamingResponse(
            gen,
            headers={"content-type": "text/plain"},
        )

    client = TestClient(app)

    # 1. Normal stream executes inside Context & active ScopedResource, then closes both
    res_ok = await client.get("/stream")
    res_ok.assert_ok()
    assert res_ok.body == b"chunk-0|chunk-1|chunk-2|"
    assert observations == [
        ("req-trace-99", False),
        ("req-trace-99", False),
        ("req-trace-99", False),
    ]
    assert tracked_resources[0].closed is True
    assert closed_events == ["closed"]
    assert Context.get("trace_id") is None

    # 2. Client disconnect mid-stream triggers generator aclose() and scoped cleanup
    closed_events.clear()
    tracked_resources.clear()

    def _disconnect_on_first_chunk(_chunk: bytes) -> None:
        raise ClientDisconnect("peer closed")

    status, _headers, body = await _run_raw_http(
        app,
        "GET",
        "/stream",
        on_body_chunk=_disconnect_on_first_chunk,
    )
    assert status == 200
    assert body == b""
    assert closed_events == ["closed"]
    assert len(tracked_resources) == 1
    assert tracked_resources[0].closed is True
    assert Context.get("trace_id") is None

    # 3. HEAD request does not consume stream chunks or emit body bytes, yet still closes generator
    head_iterations.clear()
    status_head, headers_head, body_head = await _run_raw_http(app, "HEAD", "/stream")
    assert status_head == 200
    assert headers_head["content-type"] == "text/plain"
    assert body_head == b""
    assert head_iterations == []
    assert created_generators[-1].ag_frame is None


@pytest.mark.asyncio
async def test_dict_list_returns_and_cors_headers_on_422_and_500() -> None:
    """HTTP-4 / G-16: dict/list returns convert to JSON and outer/group CORS decorates 4xx/5xx responses."""

    class _InnerFailingMiddleware(Middleware):
        async def handle(self, request: Request, next_handler: Handler) -> Response:
            if request.headers.get("x-fail-mw") == "401":
                raise HttpException("Blocked by inner middleware", status_code=401)
            return await next_handler(request)

    app_global = Application(
        auto_discover_providers=False,
        debug=False,
        config={"cors": {"allowed_origins": ["https://app.example.com"]}},
    )
    app_global.middleware([CORS, _InnerFailingMiddleware])

    @app_global.get("/dict-sync")
    def dict_sync() -> dict[str, Any]:
        return {"mode": "sync", "items": [1, 2]}

    @app_global.get("/list-async")
    async def list_async() -> list[Any]:
        return ["alpha", "beta"]

    @app_global.post("/validated")
    async def validated_route(form: _CreateItemForm) -> dict[str, Any]:
        return {"accepted": form.validated()["name"]}

    @app_global.get("/crash")
    async def crash_route() -> Response:
        raise RuntimeError("boom")

    client = TestClient(app_global)

    res_dict = await client.get("/dict-sync", headers={"Origin": "https://app.example.com"})
    res_dict.assert_ok()
    assert res_dict.json() == {"mode": "sync", "items": [1, 2]}
    res_dict.assert_header("Access-Control-Allow-Origin", "https://app.example.com")

    res_list = await client.get("/list-async")
    res_list.assert_ok()
    assert res_list.json() == ["alpha", "beta"]

    res_422 = await client.post(
        "/validated",
        json={"name": ""},
        headers={"Origin": "https://app.example.com"},
    )
    assert res_422.status_code == 422
    res_422.assert_header("Access-Control-Allow-Origin", "https://app.example.com")

    res_500 = await client.get("/crash", headers={"Origin": "https://app.example.com"})
    assert res_500.status_code == 500
    res_500.assert_header("Access-Control-Allow-Origin", "https://app.example.com")

    res_401 = await client.get(
        "/dict-sync",
        headers={"Origin": "https://app.example.com", "X-Fail-MW": "401"},
    )
    assert res_401.status_code == 401
    res_401.assert_header("Access-Control-Allow-Origin", "https://app.example.com")

    # Group-level CORS also wraps route / FormRequest 422 & 500 exceptions
    app_group = Application(
        auto_discover_providers=False,
        debug=False,
        config={"cors": {"allowed_origins": ["https://app.example.com"]}},
    )

    @app_group.group("/grouped", middleware=[CORS])
    def grouped_routes(group: RouteGroup) -> None:
        @group.post("/form")
        async def grouped_form(form: _CreateItemForm) -> dict[str, Any]:
            return {"name": form.validated()["name"]}

        @group.get("/boom")
        async def grouped_boom() -> Response:
            raise ValidationException({"field": ["invalid"]})

    group_client = TestClient(app_group)
    res_g422 = await group_client.post(
        "/grouped/form",
        json={"name": "a"},
        headers={"Origin": "https://app.example.com"},
    )
    assert res_g422.status_code == 422
    res_g422.assert_header("Access-Control-Allow-Origin", "https://app.example.com")

    res_gboom = await group_client.get(
        "/grouped/boom",
        headers={"Origin": "https://app.example.com"},
    )
    assert res_gboom.status_code == 422
    res_gboom.assert_header("Access-Control-Allow-Origin", "https://app.example.com")


@pytest.mark.asyncio
async def test_route_group_root_prefix_and_trailing_slash_normalization() -> None:
    """HTTP-5: RouteGroup('/') avoids '//path' and preserves trailing slashes consistently with include_router."""
    app = Application(auto_discover_providers=False)

    @app.group("/")
    def root_group(group: RouteGroup) -> None:
        @group.get("/")
        async def root_index() -> dict[str, str]:
            return {"route": "root"}

        @group.get("/ping")
        async def root_ping() -> dict[str, str]:
            return {"route": "ping"}

        @group.get("/trailing/")
        async def root_trailing() -> dict[str, str]:
            return {"route": "trailing"}

    @app.group("/v1")
    def v1_group(group: RouteGroup) -> None:
        @group.get("/")
        async def v1_index() -> dict[str, str]:
            return {"route": "v1"}

        @group.get("/users/")
        async def v1_users_trailing() -> dict[str, str]:
            return {"route": "v1_users_trailing"}

    included = Router()

    @included.get("/mounted")
    async def mounted_route() -> dict[str, str]:
        return {"route": "mounted"}

    app.include_router(included, prefix="/")

    client = TestClient(app)

    (await client.get("/")).assert_ok()
    assert (await client.get("/ping")).json() == {"route": "ping"}
    assert (await client.get("/trailing/")).json() == {"route": "trailing"}
    (await client.get("/trailing")).assert_not_found()

    assert (await client.get("/v1")).json() == {"route": "v1"}
    assert (await client.get("/v1/users/")).json() == {"route": "v1_users_trailing"}
    (await client.get("/v1/users")).assert_not_found()
    assert (await client.get("/mounted")).json() == {"route": "mounted"}


@pytest.mark.asyncio
async def test_iterative_depth_conflicting_cl_file_validation_and_form_request_400() -> None:
    """HTTP-6: Iterative depth validation, conflicting Content-Length 400, FileValidationError 422, FormRequest non-JSON 400."""
    # 1. Iterative _validate_json_depth handles >1000 nested structures without RecursionError
    nested: dict[str, Any] = {"leaf": 1}
    for _ in range(1500):
        nested = {"child": nested}

    with pytest.raises(HttpException) as depth_exc:
        _validate_json_depth(nested, max_depth=1200)
    assert depth_exc.value.status_code == 400

    # Within higher explicit max_depth limit succeeds without hitting call stack recursion limit
    _validate_json_depth(nested, max_depth=2000)

    app = Application(auto_discover_providers=False, debug=False)

    @app.post("/raw-echo")
    async def raw_echo(request: Request) -> Response:
        return Response(await request.body(), status_code=200)

    @app.post("/upload-fail")
    async def upload_fail() -> Response:
        raise FileValidationError(
            errors={"avatar": ["The avatar size must not exceed 1024 kilobytes."]}
        )

    @app.post("/validated", max_json_depth=4)
    async def validated_submit(form: _CreateItemForm) -> dict[str, Any]:
        return {"name": form.validated()["name"]}

    # 2. Conflicting Content-Length headers -> 400 Bad Request
    status_cl, _hdrs, body_cl = await _run_raw_http(
        app,
        "POST",
        "/raw-echo",
        headers=[
            (b"content-length", b"999999999"),
            (b"content-length", b"2"),
        ],
        body=b"hi",
    )
    assert status_cl == 400
    assert b"Conflicting Content-Length" in body_cl

    # 3. FileValidationError preserves structured `.errors` map in 422 response
    client = TestClient(app)
    res_file = await client.post("/upload-fail")
    assert res_file.status_code == 422
    assert res_file.json() == {
        "errors": {"avatar": ["The avatar size must not exceed 1024 kilobytes."]}
    }

    # 4. Non-JSON 400 transport/security failures on FormRequest remain 400 (not converted to 422)
    status_form_cl, _h1, _b1 = await _run_raw_http(
        app,
        "POST",
        "/validated",
        headers=[
            (b"content-type", b"application/json"),
            (b"content-length", b"10"),
            (b"content-length", b"2"),
        ],
        body=b"{}",
    )
    assert status_form_cl == 400

    res_bad_cl = await client.post(
        "/validated",
        data=b'{"name": "Al"}',
        headers={"Content-Type": "application/json", "Content-Length": "-5"},
    )
    res_bad_cl.assert_bad_request()

    res_deep_form = await client.post(
        "/validated",
        json={"name": "Alice", "nested": [[[[1]]]]},
    )
    res_deep_form.assert_bad_request()
