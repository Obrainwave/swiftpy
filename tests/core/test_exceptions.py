from __future__ import annotations

from typing import Any

import pytest

from swiftpy.core.application import Application
from swiftpy.core.exceptions import (
    BadRequestError,
    ForbiddenError,
    MethodNotAllowedError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.testing.http_client import TestClient
from swiftpy.validation.exceptions import ValidationException


@pytest.mark.asyncio
async def test_mapped_exceptions_to_http_status_codes() -> None:
    app = Application(debug=False)
    client = TestClient(app)

    @app.get("/not-found")
    async def not_found_route() -> Response:
        raise NotFoundError("Item was not found in catalog")

    @app.get("/unauthorized")
    async def unauthorized_route() -> Response:
        raise UnauthorizedError("Invalid authentication credentials")

    @app.get("/forbidden")
    async def forbidden_route() -> Response:
        raise ForbiddenError("Permission denied for this resource")

    @app.get("/bad-request")
    async def bad_request_route() -> Response:
        raise BadRequestError("Malformed syntax in request")

    @app.get("/method-not-allowed")
    async def method_not_allowed_route() -> Response:
        raise MethodNotAllowedError("The requested method is not supported")

    @app.get("/validation-error")
    async def validation_error_route() -> Response:
        raise ValidationError(
            {"email": ["The email format is invalid."], "age": ["Age is required."]}
        )

    # 1. 404 Not Found
    res = await client.get("/not-found")
    res.assert_status(404)
    assert res.json() == {"detail": "Item was not found in catalog"}

    # 2. 401 Unauthorized
    res = await client.get("/unauthorized")
    res.assert_status(401)
    assert res.json() == {"detail": "Invalid authentication credentials"}

    # 3. 403 Forbidden
    res = await client.get("/forbidden")
    res.assert_status(403)
    assert res.json() == {"detail": "Permission denied for this resource"}

    # 4. 400 Bad Request
    res = await client.get("/bad-request")
    res.assert_status(400)
    assert res.json() == {"detail": "Malformed syntax in request"}

    # 5. 405 Method Not Allowed
    res = await client.get("/method-not-allowed")
    res.assert_status(405)
    assert res.json() == {"detail": "The requested method is not supported"}

    # 6. 422 Validation Error
    res = await client.get("/validation-error")
    res.assert_validation_error("email")
    res.assert_validation_error("age")
    assert res.json()["errors"]["email"] == ["The email format is invalid."]


@pytest.mark.asyncio
async def test_development_mode_exception_returns_full_traceback() -> None:
    # Development mode: debug=True
    app = Application(debug=True)
    client = TestClient(app)

    @app.get("/crash")
    async def crash_route() -> Response:
        raise RuntimeError("Something catastrophically failed in handler")

    res = await client.get("/crash")
    res.assert_status(500)
    data = res.json()

    assert data["error"] == "Internal Server Error"
    assert data["exception"] == "RuntimeError"
    assert data["message"] == "Something catastrophically failed in handler"
    assert isinstance(data["traceback"], list)
    assert len(data["traceback"]) > 0
    assert any("crash_route" in line for line in data["traceback"])


@pytest.mark.asyncio
async def test_production_mode_exception_returns_safe_generic_message() -> None:
    # Production mode: debug=False
    app = Application(debug=False)
    client = TestClient(app)

    @app.get("/secret-db-crash")
    async def secret_db_crash() -> Response:
        raise ConnectionError(
            "Database connection failed: postgres://admin:secret_pass_123@db.internal:5432/swiftpy"
        )

    @app.get("/secret-auth-crash")
    async def secret_auth_crash() -> Response:
        raise PermissionError(
            "Authentication backend RSA private key /etc/ssl/certs/private.key unreadable"
        )

    @app.get("/secret-handler-crash")
    async def secret_handler_crash() -> Response:
        raise KeyError("MISSING_ENV_SECRET: AWS_SECRET_ACCESS_KEY_XYZ")

    # 1. Database exception leakage test
    res_db = await client.get("/secret-db-crash")
    res_db.assert_status(500)
    assert res_db.json() == {"detail": "Internal Server Error"}
    assert "postgres" not in res_db.text
    assert "secret_pass" not in res_db.text
    assert "traceback" not in res_db.json()

    # 2. Auth exception leakage test
    res_auth = await client.get("/secret-auth-crash")
    res_auth.assert_status(500)
    assert res_auth.json() == {"detail": "Internal Server Error"}
    assert "private.key" not in res_auth.text
    assert "traceback" not in res_auth.json()

    # 3. Handler exception leakage test
    res_handler = await client.get("/secret-handler-crash")
    res_handler.assert_status(500)
    assert res_handler.json() == {"detail": "Internal Server Error"}
    assert "AWS_SECRET" not in res_handler.text
    assert "traceback" not in res_handler.json()


def test_custom_exception_handler_requires_explicit_request_exception_order() -> None:
    app = Application(auto_discover_providers=False)

    class AmbiguousError(Exception):
        pass

    with pytest.raises(TypeError, match="Unsupported exception handler signature"):

        @app.exception_handler_for(AmbiguousError)
        def ambiguous_handler(a: Any, b: Any) -> Response:
            return json_response({"ok": True})


@pytest.mark.asyncio
async def test_custom_exception_handler_registration() -> None:
    app = Application()
    client = TestClient(app)

    class PaymentRequiredException(Exception):
        def __init__(self, invoice_id: str) -> None:
            self.invoice_id = invoice_id

    @app.exception_handler_for(PaymentRequiredException)
    async def handle_payment_required(
        exc: PaymentRequiredException, request: Any
    ) -> Response:
        return json_response(
            {"payment_required": True, "invoice": exc.invoice_id},
            status_code=402,
        )

    @app.get("/checkout")
    async def checkout() -> Response:
        raise PaymentRequiredException("INV-12345")

    res = await client.get("/checkout")
    res.assert_status(402)
    assert res.json() == {"payment_required": True, "invoice": "INV-12345"}


@pytest.mark.asyncio
async def test_router_returns_405_method_not_allowed() -> None:
    """Verify that sending a disallowed HTTP method to a known route returns HTTP 405."""
    app = Application()
    client = TestClient(app)

    @app.post("/items")
    async def create_item() -> Response:
        return json_response({"created": True}, status_code=201)

    # Sending GET to POST-only route must return 405 Method Not Allowed
    res = await client.get("/items")
    res.assert_status(405)
    assert "Method 'GET' not allowed" in res.json()["detail"]
    res.assert_header("Allow", "POST")

    # Sending DELETE to POST-only route must also return 405
    res_del = await client.delete("/items")
    res_del.assert_status(405)
    res_del.assert_header("Allow", "POST")


@pytest.mark.asyncio
async def test_unknown_route_returns_404_not_found() -> None:
    """Verify that unmatched URL paths return 404 Not Found."""
    app = Application()
    client = TestClient(app)

    res = await client.get("/completely-unregistered-route-xyz")
    res.assert_not_found()
    assert res.json() == {"detail": "Not Found"}


@pytest.mark.asyncio
async def test_validation_exception_mapping() -> None:
    """Verify that ValidationException (from validation subsystem) maps to 422 with structured errors."""
    app = Application()
    client = TestClient(app)

    @app.get("/validate-check")
    async def validate_route() -> Response:
        raise ValidationException({"username": ["The username is already taken."]})

    res = await client.get("/validate-check")
    res.assert_validation_error("username")
    assert res.json()["errors"]["username"] == ["The username is already taken."]


@pytest.mark.asyncio
async def test_failing_custom_exception_handler_falls_back_safely() -> None:
    """Verify that if a custom exception handler itself raises an unhandled error, the framework safely returns 500."""
    app = Application(debug=False)
    client = TestClient(app)

    class CustomCrashException(Exception):
        pass

    @app.exception_handler_for(CustomCrashException)
    async def broken_handler(exc: CustomCrashException, request: Any) -> Response:
        raise RuntimeError("Bug inside the custom exception handler itself!")

    @app.get("/trigger-broken-handler")
    async def trigger() -> Response:
        raise CustomCrashException("Original failure")

    # Must safely return 500 without crashing or hanging the ASGI pipeline
    res = await client.get("/trigger-broken-handler")
    res.assert_status(500)
    assert res.json() == {"detail": "Internal Server Error"}


class ParentDomainError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ChildDomainError(ParentDomainError):
    pass


class UnhandledChildDomainError(ParentDomainError):
    pass


@pytest.mark.asyncio
async def test_custom_exception_handler_signatures_and_subclass_hierarchy() -> None:
    """
    Verify:
    1. Decorator syntax @app.exception_handler(ExcClass)
    2. (request, exc) parameter convention
    3. (exc, request) parameter convention
    4. Synchronous and asynchronous custom handlers
    5. Subclass matching and MRO fallback behavior
    6. Dict return auto-wrapping into json_response
    """
    app = Application(debug=False)
    client = TestClient(app)

    # 1. Async handler with (request, exc) signature
    @app.exception_handler(ParentDomainError)
    async def handle_parent(request: Request, exc: ParentDomainError) -> Response:
        return json_response(
            {"handler": "parent", "path": request.path, "error": exc.message},
            status_code=400,
        )

    # 2. Sync handler with (exc, request) signature and dict return
    @app.exception_handler(ChildDomainError)
    def handle_child(exc: ChildDomainError, request: Request) -> dict[str, Any]:
        return {
            "handler": "child",
            "path": request.path,
            "error": exc.message,
        }

    @app.get("/trigger-child")
    async def route_child() -> Response:
        raise ChildDomainError("Child domain specific error")

    @app.get("/trigger-unhandled-child")
    async def route_unhandled_child() -> Response:
        raise UnhandledChildDomainError("Falls back to parent domain handler")

    @app.get("/trigger-parent")
    async def route_parent() -> Response:
        raise ParentDomainError("General parent domain error")

    # Specific child handler matched
    res_child = await client.get("/trigger-child")
    assert res_child.status_code == 200  # Dict return defaults to 200 json response
    assert res_child.json() == {
        "handler": "child",
        "path": "/trigger-child",
        "error": "Child domain specific error",
    }

    # Subclass without explicit handler falls back to parent handler via MRO
    res_fallback = await client.get("/trigger-unhandled-child")
    assert res_fallback.status_code == 400
    assert res_fallback.json() == {
        "handler": "parent",
        "path": "/trigger-unhandled-child",
        "error": "Falls back to parent domain handler",
    }

    # Parent handler matched
    res_parent = await client.get("/trigger-parent")
    assert res_parent.status_code == 400
    assert res_parent.json() == {
        "handler": "parent",
        "path": "/trigger-parent",
        "error": "General parent domain error",
    }
