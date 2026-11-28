from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest

from swiftpy.core.application import Application
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.testing.http_client import TestClient
from swiftpy.validation import (
    FormRequest,
    ValidatedData,
    ValidationException,
    validate,
)


class PostFormRequest(FormRequest):
    schema: ClassVar[dict[str, Any]] = {
        "title": "required|str|min:3",
        "author": "required|str",
    }


class SingleErrorFormRequest(FormRequest):
    stop_on_first_error = True
    schema: ClassVar[dict[str, Any]] = {
        "email": "bail|required|email",
        "name": "required|str|min:3",
    }


def test_validated_data_dot_access_and_dict_compatibility() -> None:
    raw = {
        "title": "SwiftPY 0.2.0",
        "profile": {"handle": "ada", "links": [{"label": "docs"}]},
        "items": ["keep-dict-method-shadow-safe"],
    }
    data = ValidatedData(raw)
    assert isinstance(data, dict)
    assert data == raw
    assert data.title == "SwiftPY 0.2.0"
    assert data["title"] == "SwiftPY 0.2.0"
    assert data.profile.handle == "ada"
    assert data.profile.links[0].label == "docs"

    # Keys shadowing dict methods resolve to dict methods via attribute access, bracket for value
    assert callable(data.items)
    assert data["items"] == ["keep-dict-method-shadow-safe"]

    with pytest.raises(AttributeError):
        _ = data.nonexistent


@pytest.mark.asyncio
async def test_form_request_validated_dot_access_and_stop_on_first_error() -> None:
    app = Application(auto_discover_providers=False)
    calls = 0

    class _CountingRule:
        message = "Counting rule failed."

        def validate(self, _field: str, _value: Any) -> bool:
            nonlocal calls
            calls += 1
            return False

    class CountingSingleRequest(FormRequest):
        stop_on_first_error = True

        def rules(self) -> dict[str, Any]:
            return {
                "email": "required|email",
                "extra": [_CountingRule()],
            }

    @app.post("/posts")
    async def create_post(form: PostFormRequest) -> Response:
        data = form.validated()
        return json_response({"title": data.title, "author": data.author})

    @app.post("/single")
    async def single_form(form: SingleErrorFormRequest) -> Response:
        return json_response({"email": form.validated().email})

    @app.post("/short-circuit")
    async def short_circuit(form: CountingSingleRequest) -> Response:
        return json_response({"ok": True})

    client = TestClient(app)

    ok_res = await client.post(
        "/posts",
        json={"title": "Hello World", "author": "Ada"},
    )
    ok_res.assert_ok()
    assert ok_res.json() == {"title": "Hello World", "author": "Ada"}

    single_res = await client.post(
        "/single",
        json={"email": "not-an-email", "name": "x"},
    )
    assert single_res.status_code == 422
    assert single_res.json() == {
        "message": "The email field must be a valid email address.",
        "field": "email",
    }

    sc_res = await client.post("/short-circuit", json={"email": "bad", "extra": "x"})
    assert sc_res.status_code == 422
    assert calls == 0  # Second field did not execute once email failed


@pytest.mark.asyncio
async def test_validate_request_try_except_and_single_error_rendering() -> None:
    app = Application(auto_discover_providers=False)

    @app.post("/register")
    async def register(request: Request) -> Response:
        try:
            data = (
                (
                    await validate(
                        request,
                        {
                            "email": "bail|required|email",
                            "name": "nullable|str|min:2",
                        },
                    )
                )
                .raise_if_failed()
                .validated()
            )
        except ValidationException as exc:
            return json_response(exc.single().to_dict(), status_code=422)

        return json_response({"email": data.email, "name": data.get("name")})

    @app.post("/register-raise-single")
    async def register_raise_single(request: Request) -> Response:
        try:
            result = await validate(
                request,
                {"email": "bail|required|email", "name": "required|min:3"},
            )
            result.raise_if_failed()
        except ValidationException as exc:
            raise exc.single() from None
        return json_response({"ok": True})

    @app.get("/search")
    async def search(request: Request) -> Response:
        result = await validate(request, {"q": "required|str|min:2"})
        data = result.raise_if_failed().validated()
        return json_response({"q": data.q})

    client = TestClient(app)

    # 1. JSON body valid (with nullable name omitted)
    res_ok = await client.post("/register", json={"email": "ADA@EXAMPLE.COM"})
    res_ok.assert_ok()
    assert res_ok.json() == {"email": "ADA@example.com", "name": None}

    # 2. Form-urlencoded body valid
    res_form = await client.post(
        "/register",
        data={"email": "bob@example.com", "name": "Bob"},
    )
    res_form.assert_ok()
    assert res_form.json() == {"email": "bob@example.com", "name": "Bob"}

    # 3. Single error caught and returned as dict
    res_single = await client.post(
        "/register",
        json={"email": "invalid-email", "name": "A"},
    )
    assert res_single.status_code == 422
    assert res_single.json() == {
        "message": "The email field must be a valid email address.",
        "field": "email",
    }

    # 4. Raising exc.single() lets ExceptionHandler render single error
    res_raised = await client.post(
        "/register-raise-single",
        json={"email": "bad", "name": "x"},
    )
    assert res_raised.status_code == 422
    assert res_raised.json() == {
        "message": "The email field must be a valid email address.",
        "field": "email",
    }

    # 5. GET query parameters validated directly from Request
    res_query = await client.get("/search", query={"q": "swiftpy"})
    res_query.assert_ok()
    assert res_query.json() == {"q": "swiftpy"}

    # 6. Non-object JSON array payload fails cleanly with body error
    res_array = await client.post("/register", json=[1, 2, 3])
    assert res_array.status_code == 422
    assert res_array.json()["field"] == "body"


def test_validation_exception_helpers() -> None:
    exc = ValidationException(
        {
            "email": ["Email is required.", "Email must be valid."],
            "password": ["Password too short."],
        }
    )
    assert exc.first_error() == "Email is required."
    assert exc.first_error("password") == "Password too short."
    assert exc.first_error("missing") is None
    assert exc.first_errors() == {
        "email": "Email is required.",
        "password": "Password too short.",
    }
    assert exc.to_dict() == {
        "errors": {
            "email": ["Email is required.", "Email must be valid."],
            "password": ["Password too short."],
        }
    }
    single = exc.single()
    assert single is not exc
    assert single.to_dict() == {"message": "Email is required.", "field": "email"}
    # Original remains unchanged
    assert "errors" in exc.to_dict()


@pytest.mark.asyncio
async def test_validate_concurrent_requests_isolation() -> None:
    app = Application(auto_discover_providers=False)

    @app.post("/echo")
    async def echo(request: Request) -> Response:
        data = (
            (await validate(request, {"id": "required|int", "tag": "required|str"}))
            .raise_if_failed()
            .validated()
        )
        await asyncio.sleep(0.01)
        return json_response({"id": data.id, "tag": data.tag})

    client = TestClient(app)
    results = await asyncio.gather(
        *[
            client.post("/echo", json={"id": str(i), "tag": f"t-{i}"})
            for i in range(1, 9)
        ]
    )
    for i, res in enumerate(results, start=1):
        res.assert_ok()
        assert res.json() == {"id": i, "tag": f"t-{i}"}


@pytest.mark.asyncio
async def test_request_validate_and_route_helpers_and_attr_safety() -> None:
    app = Application(auto_discover_providers=False)

    @app.put("/items/{item_id}/tags/{tag}")
    async def update_item(request: Request, item_id: int, tag: str) -> Response:
        all_params = request.route()
        assert all_params == {"item_id": item_id, "tag": tag}
        assert request.route("missing", "fallback") == "fallback"

        validated = await request.validate(
            {
                "name": "required|str|min:2",
                "qty": "required|int|min:1",
            },
            messages={"name.min": "Item name too short."},
        )
        # Reserved attributes cannot be hijacked by user JSON keys
        assert callable(request.validate)
        assert callable(request.route)
        assert isinstance(request.path_params, dict)
        return json_response(
            {
                "item_id": request.route("item_id"),
                "tag": request.route("tag"),
                "name": validated.name,
                "qty": validated.qty,
            }
        )

    @app.post("/items/single-error")
    async def single_error_validate(request: Request) -> Response:
        validated = await request.validate(
            {"email": "required|email", "name": "required|str|min:3"},
            stop_on_first_error=True,
        )
        return json_response(validated)

    client = TestClient(app)

    ok_res = await client.put(
        "/items/42/tags/books",
        json={
            "name": "Widget",
            "qty": "3",
            "validate": "shadow-attempt",
            "route": "shadow-attempt",
            "path_params": "shadow-attempt",
        },
    )
    ok_res.assert_ok()
    assert ok_res.json() == {"item_id": 42, "tag": "books", "name": "Widget", "qty": 3}

    bad_res = await client.put("/items/42/tags/books", json={"name": "X", "qty": 0})
    assert bad_res.status_code == 422
    assert bad_res.json() == {
        "errors": {
            "name": ["Item name too short."],
            "qty": ["The qty field must be at least 1."],
        }
    }

    single_res = await client.post(
        "/items/single-error", json={"email": "not-email", "name": "a"}
    )
    assert single_res.status_code == 422
    assert single_res.json() == {
        "message": "The email field must be a valid email address.",
        "field": "email",
    }

    non_dict_res = await client.post("/items/single-error", json=[1, 2])
    assert non_dict_res.status_code == 422
    assert non_dict_res.json()["field"] == "body"


def test_form_request_route_default_when_no_request() -> None:
    form = PostFormRequest()
    assert form.route() == {}
    assert form.route("id") is None
    assert form.route("id", 99) == 99


@pytest.mark.asyncio
async def test_request_validate_nested_stripping_multiple_rules_and_abort_side_effects() -> None:
    app = Application(auto_discover_providers=False)
    mutated: list[str] = []

    @app.post("/orders")
    async def create_order(request: Request) -> Response:
        validated = await request.validate(
            {
                "users": "required",
                "users.*.email": "required|email:lowercase",
                "code": "required|str|min:4|max:6|regex:^[A-Z0-9]+$",
                "items": "required",
                "items.*.sku": "required|str|min:2",
                "items.*.qty": "required|int|min:1",
                "lines": "required",
                "lines.*.qty": "required|int|min:1",
            }
        )
        mutated.append("saved")
        return json_response(
            {
                "validated": validated,
                "keys": sorted(validated.keys()),
                "first_user_email": validated.users[0].email,
                "first_qty": validated["items"][0]["qty"],
                "line_qty": validated.lines[0].qty,
            }
        )

    client = TestClient(app)

    # 1. Valid payload strips extra top-level & nested fields, normalizes email, and coerces ints
    raw_payload = {
        "users": [{"email": "BUYER@EXAMPLE.COM", "admin_override": True}],
        "code": "ORD99",
        "items": [{"sku": "SKU1", "qty": "3", "ignored": "x"}],
        "lines": [{"qty": "7"}],
        "unexpected_top_level": "drop-me",
    }
    ok_res = await client.post("/orders", json=raw_payload)
    ok_res.assert_ok()
    body = ok_res.json()
    assert body["keys"] == ["code", "items", "lines", "users"]
    assert "unexpected_top_level" not in body["validated"]
    assert body["first_user_email"] == "buyer@example.com"
    assert body["first_qty"] == 3
    assert body["line_qty"] == 7
    assert mutated == ["saved"]

    # 2. Validation failure on multiple rules reports all failures and NEVER runs handler logic after validate()
    bad_res = await client.post(
        "/orders",
        json={
            "users": [{"email": "not-email"}],
            "code": "ab!",
            "items": [{"sku": "S", "qty": "0"}],
            "lines": [{"qty": "0"}],
        },
    )
    assert bad_res.status_code == 422
    errs = bad_res.json()["errors"]
    assert "users.0.email" in errs
    assert len(errs["code"]) == 2  # min:4 AND regex failed cleanly together
    assert "items.0.sku" in errs
    assert "items.0.qty" in errs
    assert "lines.0.qty" in errs
    assert mutated == ["saved"]  # Handler side effect did not execute on failure
