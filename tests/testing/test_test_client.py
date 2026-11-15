from __future__ import annotations

import asyncio

import pytest

from swiftpy.core.application import Application
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.testing.http_client import TestClient
from swiftpy.testing.test_case import TestCase


@pytest.mark.asyncio
async def test_test_client_http_methods_and_assertions() -> None:
    app = Application()
    client = TestClient(app)

    @app.get("/items")
    async def list_items(request: Request) -> Response:
        category = request.query_params.get("category", "all")
        return json_response(
            {"items": ["item1", "item2"], "category": category},
            headers={"X-Total-Count": "2"},
        )

    @app.post("/items")
    async def create_item(request: Request) -> Response:
        payload = await request.json()
        return json_response({"created": True, "item": payload}, status_code=201)

    @app.put("/items/{item_id}")
    async def update_item(request: Request, item_id: int) -> Response:
        payload = await request.json()
        return json_response({"updated_id": item_id, "data": payload})

    @app.patch("/items/{item_id}")
    async def patch_item(request: Request, item_id: int) -> Response:
        payload = await request.json()
        return json_response({"patched_id": item_id, "fields": payload})

    @app.delete("/items/{item_id}")
    async def delete_item(request: Request, item_id: int) -> Response:
        return json_response({"deleted": item_id}, status_code=200)

    # 1. GET with query params & header assertions
    res_get = await client.get("/items", query={"category": "electronics"})
    res_get.assert_ok()
    res_get.assert_header("X-Total-Count", "2")
    res_get.assert_json_fragment({"category": "electronics"})
    assert res_get.json()["items"] == ["item1", "item2"]

    # 2. POST with json body
    res_post = await client.post("/items", json={"name": "Widget", "price": 19.99})
    res_post.assert_created()
    res_post.assert_json_fragment({"created": True})
    assert res_post.json()["item"]["name"] == "Widget"

    # 3. PUT with path param and json body
    res_put = await client.put("/items/42", json={"name": "Widget Updated"})
    res_put.assert_ok()
    res_put.assert_json_fragment({"updated_id": 42})

    # 4. PATCH with path param
    res_patch = await client.patch("/items/42", json={"price": 24.99})
    res_patch.assert_ok()
    res_patch.assert_json_fragment({"patched_id": 42})

    # 5. DELETE
    res_delete = await client.delete("/items/42")
    res_delete.assert_ok()
    res_delete.assert_json({"deleted": 42})


@pytest.mark.asyncio
async def test_test_client_header_chaining() -> None:
    app = Application()

    @app.get("/auth-check")
    async def auth_check(request: Request) -> Response:
        auth_header = request.headers.get("authorization", "")
        api_key = request.headers.get("x-api-key", "")
        custom_header = request.headers.get("x-custom-tenant", "")
        return json_response(
            {
                "auth": auth_header,
                "api_key": api_key,
                "tenant": custom_header,
            }
        )

    base_client = TestClient(app)

    # Chain Bearer token
    token_client = base_client.with_token("test-jwt-token-123")
    res = await token_client.get("/auth-check")
    res.assert_ok()
    assert res.json()["auth"] == "Bearer test-jwt-token-123"

    # Chain API key
    key_client = base_client.with_api_key("secret-api-key-xyz")
    res_key = await key_client.get("/auth-check")
    res_key.assert_ok()
    assert res_key.json()["api_key"] == "secret-api-key-xyz"

    # Chain custom headers
    custom_client = base_client.with_headers({"X-Custom-Tenant": "tenant_abc"})
    res_custom = await custom_client.get("/auth-check")
    res_custom.assert_ok()
    assert res_custom.json()["tenant"] == "tenant_abc"


@pytest.mark.asyncio
async def test_test_case_integration() -> None:
    class MyTestCase(TestCase):
        def create_app(self) -> Application:
            app = Application()

            @app.get("/ping")
            async def ping() -> Response:
                return json_response({"pong": True})

            return app

    case = MyTestCase()
    res = await case.get("/ping")
    res.assert_ok()
    res.assert_json({"pong": True})


@pytest.mark.asyncio
async def test_test_client_immutability_and_chaining_precedence() -> None:
    """Verify that with_token() and with_headers() return new instances without mutating the original client."""
    app = Application()
    base_client = TestClient(app)

    # 1. Calling with_token returns a new instance and does NOT mutate base_client
    token_client_1 = base_client.with_token("token-alpha")
    assert base_client._default_headers == {}
    assert token_client_1._default_headers == {"Authorization": "Bearer token-alpha"}

    # 2. Chaining another with_token returns a new instance and does NOT mutate token_client_1
    token_client_2 = token_client_1.with_token("token-beta")
    assert token_client_1._default_headers == {"Authorization": "Bearer token-alpha"}
    assert token_client_2._default_headers == {"Authorization": "Bearer token-beta"}

    # 3. Header precedence in chaining: later values overwrite earlier ones, other headers preserved
    chained_client = base_client.with_headers(
        {"X-App": "swiftpy", "X-Version": "1.0"}
    ).with_headers({"X-Version": "2.0", "X-Environment": "staging"})
    assert base_client._default_headers == {}
    assert chained_client._default_headers == {
        "X-App": "swiftpy",
        "X-Version": "2.0",
        "X-Environment": "staging",
    }


@pytest.mark.asyncio
async def test_test_client_repeated_query_parameters_and_encoding() -> None:
    """Verify query encoding and repeated query parameters via doseq=True."""
    app = Application()
    client = TestClient(app)

    @app.get("/search")
    async def search(request: Request) -> Response:
        return json_response(
            {
                "tags": request.query_param_list.get("tag", []),
                "first_tag": request.query_params.get("tag"),
                "page": request.query_params.get("page"),
            }
        )

    res = await client.get(
        "/search",
        query={"tag": ["python", "asgi", "swiftpy"], "page": "1"},
    )
    res.assert_ok()
    data = res.json()
    assert data["tags"] == ["python", "asgi", "swiftpy"]
    assert data["first_tag"] == "python"
    assert data["page"] == "1"


@pytest.mark.asyncio
async def test_test_client_json_serialization_and_raw_body_handling() -> None:
    """Verify JSON array serialization and raw non-dict body handling."""
    app = Application()
    client = TestClient(app)

    @app.post("/echo")
    async def echo(request: Request) -> Response:
        data = await request.json()
        return json_response({"echo": data})

    # Non-dict JSON (array)
    res_array = await client.post("/echo", json=[10, 20, 30])
    res_array.assert_ok()
    assert res_array.json() == {"echo": [10, 20, 30]}

    # String data body
    res_raw = await client.post(
        "/echo",
        data='{"custom": "raw_payload"}',
        headers={"Content-Type": "application/json"},
    )
    res_raw.assert_ok()
    assert res_raw.json() == {"echo": {"custom": "raw_payload"}}


@pytest.mark.asyncio
async def test_test_client_independent_headers_across_concurrent_requests() -> None:
    """Verify that concurrent requests using distinct client configurations retain header isolation."""
    app = Application()

    @app.get("/echo-headers")
    async def echo_headers(request: Request) -> Response:
        await asyncio.sleep(0.002)
        return json_response(
            {
                "client_id": request.headers.get("x-client-id"),
                "tenant": request.headers.get("x-tenant"),
            }
        )

    base = TestClient(app)
    client_a = base.with_headers({"X-Client-ID": "ClientA", "X-Tenant": "TenantA"})
    client_b = base.with_headers({"X-Client-ID": "ClientB", "X-Tenant": "TenantB"})

    tasks = []
    expected_clients = []
    for i in range(40):
        if i % 2 == 0:
            tasks.append(client_a.get("/echo-headers"))
            expected_clients.append(("ClientA", "TenantA"))
        else:
            tasks.append(client_b.get("/echo-headers"))
            expected_clients.append(("ClientB", "TenantB"))

    responses = await asyncio.gather(*tasks)
    assert len(responses) == 40

    for i, res in enumerate(responses):
        res.assert_ok()
        expected_id, expected_tenant = expected_clients[i]
        assert res.json()["client_id"] == expected_id
        assert res.json()["tenant"] == expected_tenant


@pytest.mark.asyncio
async def test_test_client_error_handling_without_state_leakage() -> None:
    """Verify that an application error in one request does not leak error state into later requests."""
    app = Application(debug=False)
    client = TestClient(app)

    @app.get("/flaky")
    async def flaky(request: Request) -> Response:
        if request.query_params.get("fail") == "1":
            raise RuntimeError("Intentional server crash")
        return json_response({"status": "healthy"})

    # Request 1 fails with 500
    res_fail = await client.get("/flaky", query={"fail": "1"})
    res_fail.assert_server_error()
    assert res_fail.json() == {"detail": "Internal Server Error"}

    # Request 2 on the same client succeeds with 200 OK cleanly
    res_ok = await client.get("/flaky", query={"fail": "0"})
    res_ok.assert_ok()
    assert res_ok.json() == {"status": "healthy"}
