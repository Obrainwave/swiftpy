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
