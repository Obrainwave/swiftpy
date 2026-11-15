from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from swiftpy.core.application import Application
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.validation import FormRequest, ValidationException


class CreateUserRequest(FormRequest):
    schema: ClassVar[dict[str, Any]] = {
        "name": "required|str|min:3",
        "email": "required|email",
        "age": "required|int|between:18,100",
    }


class UpdateUserRequest(FormRequest):
    schema: ClassVar[dict[str, Any]] = {
        "name": "required|str",
        "bio": "nullable|str",
    }


def _make_http_scope(
    method: str = "GET",
    path: str = "/",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "query_string": b"",
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 50000),
    }


async def _simulate_request(
    app: Application,
    method: str,
    path: str,
    body: dict[str, Any] | bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    if isinstance(body, dict):
        raw_body = json.dumps(body).encode("utf-8")
        raw_headers = [(b"content-type", b"application/json")]
    elif isinstance(body, bytes):
        raw_body = body
        raw_headers = [(b"content-type", b"application/json")]
    else:
        raw_body = b""
        raw_headers = []

    scope = _make_http_scope(method=method, path=path, headers=raw_headers)

    status_code = 0
    resp_body = bytearray()
    body_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": raw_body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code, resp_body
        if message["type"] == "http.response.start":
            status_code = message["status"]
        elif message["type"] == "http.response.body":
            resp_body.extend(message.get("body", b""))

    await app(scope, receive, send)

    try:
        data = json.loads(resp_body.decode("utf-8")) if resp_body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = {"raw": resp_body.decode("utf-8", errors="replace")}

    return status_code, data


@pytest.mark.asyncio
async def test_form_request_automatic_422_and_success() -> None:
    app = Application()

    @app.post("/users")
    async def store(request: Request, body: CreateUserRequest) -> Response:
        return json_response(
            {"created": True, "user": body.validated()},
            status_code=201,
        )

    # 1. Successful request with valid payload
    valid_payload = {
        "name": "Alice Cooper",
        "email": "alice@example.com",
        "age": "30",
    }
    status, res = await _simulate_request(app, "POST", "/users", body=valid_payload)
    assert status == 201
    assert res["created"] is True
    assert res["user"]["name"] == "Alice Cooper"
    assert res["user"]["age"] == 30  # Coerced to int

    # 2. Failed request with missing fields -> automatic 422 response
    status, res = await _simulate_request(app, "POST", "/users", body={})
    assert status == 422
    assert "errors" in res
    assert "name" in res["errors"]
    assert "email" in res["errors"]
    assert "age" in res["errors"]

    # 3. Failed request with invalid field formats -> automatic 422 response
    invalid_payload = {
        "name": "Al",
        "email": "not-an-email",
        "age": 12,
    }
    status, res = await _simulate_request(app, "POST", "/users", body=invalid_payload)
    assert status == 422
    assert "errors" in res
    assert any("at least 3" in msg for msg in res["errors"]["name"])
    assert any("valid email" in msg for msg in res["errors"]["email"])
    assert any("between 18 and 100" in msg for msg in res["errors"]["age"])


@pytest.mark.asyncio
async def test_form_request_without_explicit_request_parameter() -> None:
    app = Application()

    @app.post("/direct")
    async def direct_store(body: CreateUserRequest) -> Response:
        return json_response(
            {"created": True, "name": body.name, "email": body["email"]},
            status_code=201,
        )

    valid_payload = {
        "name": "Bob Marley",
        "email": "bob@example.com",
        "age": 45,
    }
    status, res = await _simulate_request(app, "POST", "/direct", body=valid_payload)
    assert status == 201
    assert res["name"] == "Bob Marley"
    assert res["email"] == "bob@example.com"

    # Invalid payload returns 422
    status, res = await _simulate_request(app, "POST", "/direct", body={"name": "B"})
    assert status == 422
    assert "errors" in res


@pytest.mark.asyncio
async def test_form_request_with_path_parameters() -> None:
    app = Application()

    @app.put("/users/{user_id}")
    async def update_user(
        request: Request, user_id: int, form: UpdateUserRequest
    ) -> Response:
        return json_response({"updated": user_id, "data": form.all()})

    status, res = await _simulate_request(
        app,
        "PUT",
        "/users/42",
        body={"name": "Alice Updated", "bio": "New bio"},
    )
    assert status == 200
    assert res["updated"] == 42
    assert res["data"]["name"] == "Alice Updated"

    # Validation failure returns 422
    status, res = await _simulate_request(
        app,
        "PUT",
        "/users/42",
        body={"name": ""},
    )
    assert status == 422
    assert "name" in res["errors"]


@pytest.mark.asyncio
async def test_manual_validation_exception() -> None:
    app = Application()

    @app.post("/manual")
    async def manual(request: Request) -> Response:
        raise ValidationException({"security": ["Account is temporarily locked."]})

    status, res = await _simulate_request(app, "POST", "/manual", body={})
    assert status == 422
    assert res["errors"]["security"] == ["Account is temporarily locked."]


@pytest.mark.asyncio
async def test_malformed_json_returns_422() -> None:
    app = Application()
    handler_called = False

    @app.post("/submit")
    async def submit(body: CreateUserRequest) -> Response:
        nonlocal handler_called
        handler_called = True
        return json_response({"ok": True})

    status, res = await _simulate_request(
        app, "POST", "/submit", body=b"this is not valid json {"
    )
    assert not handler_called
    assert status == 422
    assert "errors" in res
    assert "body" in res["errors"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "non_object_payload",
    [
        b"[]",
        b'[{"name": "Alice"}]',
        b'"just a string"',
        b"12345",
        b"true",
        b"null",
    ],
)
async def test_non_object_json_payloads_rejected_before_handler(
    non_object_payload: bytes,
) -> None:
    app = Application()
    handler_called = False

    @app.post("/submit")
    async def submit(body: CreateUserRequest) -> Response:
        nonlocal handler_called
        handler_called = True
        return json_response({"ok": True})

    status, res = await _simulate_request(
        app, "POST", "/submit", body=non_object_payload
    )
    assert not handler_called
    assert status == 422
    assert "errors" in res
    assert "body" in res["errors"]
    assert any("JSON object" in msg for msg in res["errors"]["body"])


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_payload", [b"", b"   \n\t  "])
async def test_empty_body_rejected_before_handler(empty_payload: bytes) -> None:
    app = Application()
    handler_called = False

    @app.post("/submit")
    async def submit(body: CreateUserRequest) -> Response:
        nonlocal handler_called
        handler_called = True
        return json_response({"ok": True})

    status, res = await _simulate_request(app, "POST", "/submit", body=empty_payload)
    assert not handler_called
    assert status == 422
    assert "errors" in res
    assert "body" in res["errors"]
    assert any("required" in msg.lower() for msg in res["errors"]["body"])


@pytest.mark.asyncio
async def test_form_request_mass_assignment_protection() -> None:
    app = Application()

    @app.post("/users")
    async def store(body: CreateUserRequest) -> Response:
        validated = body.validated()
        raw_all = body.all()

        # Guard against mass assignment
        assert "is_admin" not in validated
        assert "role" not in validated
        assert "is_admin" in raw_all
        assert "role" in raw_all

        with pytest.raises(AttributeError):
            _ = body.is_admin

        return json_response({"validated": validated, "all": raw_all})

    payload = {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "age": 28,
        "is_admin": True,
        "role": "superuser",
    }
    status, res = await _simulate_request(app, "POST", "/users", body=payload)
    assert status == 200
    assert "is_admin" not in res["validated"]
    assert "role" not in res["validated"]
    assert res["validated"]["name"] == "Jane Doe"
    assert res["all"]["is_admin"] is True
    assert res["all"]["role"] == "superuser"
