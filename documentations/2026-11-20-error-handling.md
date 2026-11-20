# Error Handling & Exceptions

## Introduction

SwiftPY handles errors centrally through **`ExceptionHandler`** (`app.exception_handler`) inside the ASGI dispatch pipeline. Route handlers, `FormRequest` validators, middleware, and domain services can raise framework or custom exceptions anywhere without manually threading error `Response` objects back up the call stack.

Quick imports:

```python
from swiftpy import HttpException, abort
from swiftpy.core.exceptions import (
    BadRequestError,       # 400
    ForbiddenError,        # 403
    InternalServerError,   # 500
    MethodNotAllowedError, # 405
    NotFoundError,         # 404
    PayloadTooLargeError,  # 413
    UnauthorizedError,     # 401
    ValidationError,       # 422
)
from swiftpy.validation import ValidationException
```

---

## Built-In Framework Exceptions & Automatic Mapping

Without registering any custom handlers, `ExceptionHandler.handle()` maps framework exceptions automatically:

| Exception Class | Status | Default JSON Body | Typical Source |
|---|---:|---|---|
| `BadRequestError` | `400` | `{"detail": "..."}` | Bad client parameters / explicit raise |
| `UnauthorizedError` | `401` | `{"detail": "..."}` | Missing/invalid auth credentials |
| `AuthenticationError`, `TokenRevokedError`, `InvalidApiKeyError` | `401` | `{"detail": "..."}` + `WWW-Authenticate` | JWT / API key guards |
| `ForbiddenError` | `403` | `{"detail": "..."}` | Insufficient permissions |
| `NotFoundError` | `404` | `{"detail": "..."}` | Resource missing or route 404 |
| `ModelNotFoundError`, `RecordNotFoundError` | `404` | `{"detail": "..."}` | ORM `.find_or_fail()`, `.first_or_fail()`, `Query.first_or_fail()` |
| `MethodNotAllowedError` | `405` | `{"detail": "..."}` + `Allow` | Router method mismatch |
| `PayloadTooLargeError` | `413` | `{"detail": "..."}` | Request exceeds `max_body_size` / `max_content_length` |
| `ValidationException`, `ValidationError`, `FileValidationError` | `422` | `{"errors": ...}` (or single-error shape below) | Validation / upload validation failure |
| `InternalServerError` | `500` | Masked `"Internal Server Error"` when `debug=False` | Server failure |
| `AuthenticationServiceUnavailableError` | `503` | `{"detail": "..."}` + `Retry-After` | Redis/DB down in auth guard |
| `HttpException` (any subclass or `abort()`) | `exc.status_code` | `{"detail": exc.message}` (masked if `>= 500` and `not debug`) | Custom HTTP errors |

---

## Quick Errors with `abort()`

Use `abort(status_code, message=None, *, headers=None)` when you need an immediate HTTP error response inside a route, helper, or guard without defining a new exception class:

```python
from swiftpy import Request, abort
from swiftpy.http.router import Router

router = Router(prefix="/orders")


@router.post("/{order_id}/cancel")
async def cancel_order(order_id: int, request: Request) -> dict[str, bool]:
    if not order_already_shipped(order_id):
        return {"cancelled": True}

    abort(409, "Shipped orders cannot be cancelled")
```

- Calling `abort(404)` with no `message` defaults to the standard HTTP reason phrase (`"Not Found"`, `"Forbidden"`, `"Conflict"`, etc.) via `http.HTTPStatus`.
- Calling `abort(status_code)` with an integer outside `100..599` (or a boolean) raises `ValueError`.
- Passing optional `headers={"Retry-After": "60"}` attaches those headers to the generated JSON response.

---

## Declarative Custom HTTP Exceptions

Subclass **`HttpException`** with class-level `status_code` and `message` attributes—no `__init__` constructor and no custom `@app.exception_handler` registration required:

```python
# app/http/handler/exceptions.py
from __future__ import annotations

from swiftpy import HttpException
from swiftpy.core.application import Application


class ResourceConflictException(HttpException):
    """Raised when an operation conflicts with existing server state."""

    status_code = 409
    message = "Resource conflict"


class PaymentRequiredException(HttpException):
    status_code = 402
    message = "Payment required"


def register_exception_handlers(app: Application) -> None:
    """Register application-specific non-HTTP exception renderers when needed."""
    _ = app
```

`HttpException.__init__(message=None, status_code=None, headers=None)` falls back automatically to `type(self).message` and `type(self).status_code`:

- `raise ResourceConflictException()` -> `HTTP 409` `{"detail": "Resource conflict"}`
- `raise ResourceConflictException("Email already taken")` -> `HTTP 409` `{"detail": "Email already taken"}`
- `raise ResourceConflictException(headers={"X-Conflict-Key": "email"})` -> attaches response headers cleanly.

---

## Custom Domain Exception Renderers

Only register handlers in `app/http/handler/exceptions.py` when:

1. You want to translate a **non-HTTP Python/domain exception** (not subclassing `HttpException`) into an HTTP response, OR
2. You need a **custom JSON structure** (not `{"detail": ...}`).

Both `@app.exception_handler(ExcType)` and `@app.exception_handler_for(ExcType)` work identically, and `register_exception_handlers(app)` is wired automatically from scaffolded `main.py`:

```python
# app/http/handler/exceptions.py
from __future__ import annotations

from swiftpy.core.application import Application
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response


class RateLimitExceededError(Exception):
    def __init__(self, retry_after: int = 60) -> None:
        self.retry_after = retry_after
        super().__init__("Rate limit exceeded")


def register_exception_handlers(app: Application) -> None:
    @app.exception_handler(RateLimitExceededError)
    async def handle_rate_limit(
        request: Request, exc: RateLimitExceededError
    ) -> Response:
        return json_response(
            {"error": "too_many_requests", "retry_after": exc.retry_after},
            status_code=429,
            headers={"Retry-After": str(exc.retry_after)},
        )
```

Supported handler conventions:

- Handlers may be `async def` or regular `def`.
- Both `(request, exc)` and `(exc, request)` parameter orderings are supported (inferred from parameter types/names; ambiguous signatures raise `TypeError` at registration time so mistakes fail early).
- Returning a `Response` sends it as-is; returning any JSON-serializable value (such as a `dict`) wraps it with `json_response(...)`.
- Lookup walks `type(exc).__mro__`, so more specific exception subclasses match before base classes.
- If a custom handler itself raises an unexpected exception, `ExceptionHandler` logs the traceback and falls back to `500 {"detail": "Internal Server Error"}`.

---

## Validation Errors: Full Map vs. Single Error (`422`)

Two `422` validation exception types exist in SwiftPY:

- **`swiftpy.validation.ValidationException`** — Use this in application handlers and validation flows (`result.raise_if_failed()`, `.single()`, `.first_error()`, `.first_errors()`, `.to_dict()`, and `FormRequest`).
- **`swiftpy.core.exceptions.ValidationError`** — Legacy/internal core exception used by router path parameter coercion (`PathParamCoercionError`) or manual `raise ValidationError(errors=...)` without helper methods.

When `ValidationException` is raised normally, SwiftPY returns:

```json
{
  "errors": {
    "email": ["The email field is required."],
    "password": ["The password field is required."]
  }
}
```

When configured for single-error mode—via `FormRequest.stop_on_first_error = True`, `await validate(..., stop_on_first_error=True)`, or `raise exc.single()`—SwiftPY renders:

```json
{
  "message": "The email field is required.",
  "field": "email"
}
```

---

## Debug vs. Production Behavior & Error Logging

- **Production (`debug=False`, default):**
  - Any unhandled exception returns `500 {"detail": "Internal Server Error"}` with **no exception class name, traceback, or file path**.
  - Any `HttpException` with `status_code >= 500` also masks `exc.message` to `"Internal Server Error"` in the HTTP response.
- **Development (`debug=True`):**
  - Unhandled exceptions return `{"error": "Internal Server Error", "exception": "...", "message": "...", "traceback": [...]}`.
  - SwiftPY refuses to start with `ConfigurationError` if `debug=True` while `APP_ENV=production`.
- **Where errors are logged:**
  - `swiftpy.core.exceptions` logs unhandled exceptions via `logger.exception(...)`, `HttpException` (`>= 500`) via `logger.error(...)`, and `HttpException` (`< 500`), `ModelNotFoundError`, `RecordNotFoundError`, and `422` validation errors via `logger.info(...)`.
  - With `config/logging.py` active, these records flow asynchronously into `storage/logs/swiftpy.log` (see [Logging](2026-11-20-logging.md)).

---

## Important Defaults & Safeguards

- **Never catch bare `HttpException` inside broad service `try/except Exception:` blocks:** Doing so turns explicit `404`/`409`/`422` errors into `500` responses unless you re-raise `HttpException`.
- **`ClientDisconnect` passes through unlogged as an error:** Dropped client sockets during body reads or response streaming exit cleanly so healthy servers are not flooded with spurious `500` alerts.

---

## Next Step

Continue to [Validation DSL & Form Requests](2026-11-15-validation-and-form-requests.md) to validate payloads declaratively with `FormRequest` or `await validate(request, rules)`.
