from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, ClassVar

import pytest

from swiftpy.core.application import Application
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.schemas import BaseSchema, Field, model_validator
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


@pytest.mark.asyncio
async def test_form_request_and_schema_runtime_agreement_and_mass_assignment() -> None:
    """Verify FormRequest[BaseSchema], schema_model, ValidatedData parity helpers, and request.validate(Schema)."""

    class DocVersionCreate(BaseSchema):
        version: str = Field(..., strip_whitespace=True, min_length=2)
        title: str = Field(..., strip_whitespace=True, min_length=2)
        release_date: date
        notes: str | None = Field(default=None, max_length=200)
        is_published: bool = False

        @model_validator
        def check_title_not_reserved(self) -> None:
            if self.title.lower() == "forbidden":
                raise ValueError("Reserved document title.")

    class DocVersionCreateRequest(FormRequest[DocVersionCreate]):
        def rules(self) -> dict[str, Any]:
            return {
                "version": "required|str",
                "title": "required|str",
                "release_date": "required|str",
                "notes": "nullable|str",
            }

    class ChildDocVersionRequest(DocVersionCreateRequest):
        pass

    class DirectSchemaOnlyRequest(FormRequest[DocVersionCreate]):
        pass

    class UnparameterizedRequest(FormRequest):
        def rules(self) -> dict[str, Any]:
            return {
                "version": "required|str",
                "title": "required|str",
                "release_date": "required|str",
                "notes": "nullable|str",
            }

    class _Service:
        def __init__(self) -> None:
            self.received: list[Any] = []

        async def create_doc_version(self, data: DocVersionCreate) -> dict[str, Any]:
            self.received.append(data)
            return {
                "is_schema": isinstance(data, DocVersionCreate),
                "version": data["version"],
                "title": data.get("title"),
                "release_date": str(data.release_date),
                "notes": data.notes,
                "is_published": data.is_published,
                "dumped": data.to_dict(exclude_unset=True),
                "has_version": "version" in data,
                "is_set_notes": data.is_set("notes"),
                "is_set_title": data.is_set("title"),
            }

    service = _Service()
    app = Application(auto_discover_providers=False)

    @app.post("/docs/typed")
    async def store_typed(req: DocVersionCreateRequest) -> Response:
        validated = req.validated()
        assert isinstance(validated, DocVersionCreate)
        return json_response(await service.create_doc_version(validated), status_code=201)

    @app.post("/docs/child")
    async def store_child(req: ChildDocVersionRequest) -> Response:
        return json_response(await service.create_doc_version(req.validated()), status_code=201)

    @app.post("/docs/schema-only")
    async def store_schema_only(req: DirectSchemaOnlyRequest) -> Response:
        return json_response(await service.create_doc_version(req.validated()), status_code=201)

    @app.post("/docs/inline-generic")
    async def store_inline_generic(req: FormRequest[DocVersionCreate]) -> Response:
        return json_response(await service.create_doc_version(req.validated()), status_code=201)

    @app.post("/docs/untyped")
    async def store_untyped(req: UnparameterizedRequest) -> Response:
        raw_validated = req.validated()
        assert isinstance(raw_validated, ValidatedData)
        # Omitted declared optional rule fields resolve to None via dot access
        assert raw_validated.notes is None
        with pytest.raises(AttributeError):
            _ = raw_validated.undeclared_attr
        return json_response(
            {
                "version": raw_validated.version,
                "to_dict": raw_validated.to_dict(exclude_unset=True),
                "model_dump": raw_validated.model_dump(exclude_none=True),
                "fields_set": sorted(raw_validated.fields_set),
                "has_title": raw_validated.is_set("title"),
                "schema_title": req.validated(DocVersionCreate).title,
            },
            status_code=201,
        )

    @app.post("/docs/request-validate-schema")
    async def store_via_request_validate(request: Request) -> Response:
        dto = await request.validate(
            {
                "version": "required|str",
                "title": "required|str",
                "release_date": "required|str",
                "notes": "nullable|str",
            },
            schema_cls=DocVersionCreate,
        )
        assert isinstance(dto, DocVersionCreate)
        return json_response(await service.create_doc_version(dto), status_code=201)

    @app.post("/docs/request-validate-direct")
    async def store_via_direct_schema(request: Request) -> Response:
        dto = await request.validate(DocVersionCreate)
        assert isinstance(dto, DocVersionCreate)
        return json_response(dto.validated(), status_code=201)

    client = TestClient(app)

    # 1. Typed FormRequest strips unvalidated is_published (mass assignment) AND hydrates BaseSchema
    res_typed = await client.post(
        "/docs/typed",
        json={
            "version": "  1.0.0  ",
            "title": "  Initial Release ",
            "release_date": "2026-11-28",
            "is_published": True,  # NOT in DocVersionCreateRequest.rules(), so stripped before schema
        },
    )
    assert res_typed.status_code == 201
    assert res_typed.json() == {
        "is_schema": True,
        "version": "1.0.0",
        "title": "Initial Release",
        "release_date": "2026-11-28",
        "notes": None,
        "is_published": False,
        "dumped": {
            "version": "1.0.0",
            "title": "Initial Release",
            "release_date": "2026-11-28",
        },
        "has_version": True,
        "is_set_notes": False,
        "is_set_title": True,
    }

    # 2. Schema-level constraint / model_validator fails with HTTP 422 before handler runs
    bad_schema_res = await client.post(
        "/docs/typed",
        json={
            "version": "v2",
            "title": "forbidden",
            "release_date": "2026-11-28",
        },
    )
    assert bad_schema_res.status_code == 422
    assert bad_schema_res.json()["errors"] == {"__root__": ["Reserved document title."]}

    # 3. Subclass inheritance, schema-only FormRequest[Schema], and inline FormRequest[Schema]
    for path in ("/docs/child", "/docs/schema-only", "/docs/inline-generic", "/docs/request-validate-schema"):
        ok_res = await client.post(
            path,
            json={"version": "2.0", "title": "Docs v2", "release_date": "2026-11-28"},
        )
        assert ok_res.status_code == 201, path
        assert ok_res.json()["is_schema"] is True
        assert ok_res.json()["release_date"] == "2026-11-28"

    # 4. Unparameterized FormRequest returns ValidatedData with BaseSchema parity helpers
    untyped_res = await client.post(
        "/docs/untyped",
        json={"version": "1.0", "title": "Manual", "release_date": "2026-11-28"},
    )
    assert untyped_res.status_code == 201
    assert untyped_res.json()["fields_set"] == ["release_date", "title", "version"]
    assert untyped_res.json()["has_title"] is True
    assert untyped_res.json()["schema_title"] == "Manual"

    # 5. request.validate(BaseSchemaClass) validates directly and serializes BaseSchema cleanly
    direct_res = await client.post(
        "/docs/request-validate-direct",
        json={"version": "3.0", "title": "Direct", "release_date": "2026-11-28"},
    )
    assert direct_res.status_code == 201
    assert direct_res.json()["version"] == "3.0"

    # 6. Manual construction & explicit invalid schema_model guard
    manual_req = DocVersionCreateRequest(
        data={"version": "1.1", "title": "Unit", "release_date": "2026-11-28"}
    )
    manual_validated = manual_req.validated()
    assert isinstance(manual_validated, DocVersionCreate)
    assert manual_validated.release_date == date(2026, 11, 28)

    with pytest.raises(TypeError, match="BaseSchema subclass"):
        type("_BadBoundRequest", (FormRequest,), {"schema_model": dict})

    # 7. Alias cleanup, unset schema defaults omitted from form.data, and cache invalidation on form.data mutation
    class _AliasedSchema(BaseSchema):
        release_date: date = Field(..., alias="releaseDate")
        title: str
        is_admin: bool = False

    class _AliasedFormRequest(FormRequest[_AliasedSchema]):
        def rules(self) -> dict[str, Any]:
            return {
                "releaseDate": "required|str",
                "title": "required|str",
            }

    @app.post("/docs/aliased")
    async def store_aliased(req: _AliasedFormRequest) -> Response:
        # Unset schema default is NOT injected into req.data (preventing accidental mass-assignment overwrites)
        assert "is_admin" not in req.data
        # Raw alias key is replaced by canonical schema field key in req.data
        assert "releaseDate" not in req.data
        assert req.data["release_date"] == date(2026, 11, 28)
        # Attribute fallback still resolves defaulted schema attribute via _validated_schema
        assert req.is_admin is False
        return json_response({"keys": sorted(req.data.keys()), "is_admin": req.is_admin})

    aliased_res = await client.post(
        "/docs/aliased",
        json={"releaseDate": "2026-11-28", "title": "Aliased"},
    )
    assert aliased_res.status_code == 200
    assert aliased_res.json() == {
        "keys": ["release_date", "title"],
        "is_admin": False,
    }

    # Cache invalidation when form.data is mutated after form.validated()
    mut_req = DocVersionCreateRequest(
        data={"version": "1.0", "title": "First", "release_date": "2026-11-28"}
    )
    first_dto = mut_req.validated()
    assert first_dto.title == "First"
    mut_req.data["title"] = "Second"
    second_dto = mut_req.validated()
    assert second_dto.title == "Second"

    # stop_on_first_error propagates into schema_cls validation in Request.validate / ValidationResult.validated
    @app.post("/docs/single-schema-err")
    async def store_single_schema_err(request: Request) -> Response:
        await request.validate(
            {"version": "required|str", "title": "required|str", "release_date": "required|str"},
            stop_on_first_error=True,
            schema_cls=DocVersionCreate,
        )
        return json_response({"ok": True})

    single_schema_res = await client.post(
        "/docs/single-schema-err",
        json={"version": "x", "title": "y", "release_date": "not-a-date"},
    )
    assert single_schema_res.status_code == 422
    assert "field" in single_schema_res.json()
    assert "message" in single_schema_res.json()

    # ValidatedData.to_dict() unwraps nested BaseSchema instances and tuples
    nested_vd = ValidatedData(
        {
            "doc": first_dto,
            "pair": (first_dto,),
        }
    )
    dumped_nested = nested_vd.to_dict(exclude_unset=True)
    assert isinstance(dumped_nested["doc"], dict)
    assert dumped_nested["doc"]["title"] == "First"
    assert isinstance(dumped_nested["pair"][0], dict)
