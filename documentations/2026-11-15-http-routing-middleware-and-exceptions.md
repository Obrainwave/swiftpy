# HTTP Routing, Requests, Responses, Middleware & Exceptions

SwiftPY's HTTP layer keeps request handling clean, typed, and modular: you declare routes on `Application` or standalone `Router` files, extract typed path parameters, JSON payloads, and file uploads from `Request`, return plain `dict`/`list` payloads or `Response` objects, compose behavior through ASGI `Middleware`, and convert errors into consistent JSON via `HttpException` and `app/http/handler/exceptions.py`.

---

## Defining Routes & Organizing Modular Route Files

### Quick Routes on `app`

In small services or prototypes, you can register endpoints directly on your `Application` instance using HTTP method decorators:

```python
# main.py
from __future__ import annotations

from swiftpy import Application, Request

app = Application()


@app.get("/health")
async def health(request: Request) -> dict[str, str]:
    return {"status": "ok"}


@app.post("/echo")
async def echo(request: Request) -> dict[str, object]:
    payload = await request.json()
    return {"received": payload}
```

`Application` provides `@app.get`, `@app.post`, `@app.put`, `@app.patch`, `@app.delete`, `@app.head`, and `@app.options` (plus `@app.websocket` for WebSocket endpoints).

> [!NOTE]
> Returning `None`, `str`, `int`, or custom objects directly from a route handler raises `TypeError`. Always return a `dict`, `list`, or `Response` (including `StreamingResponse`).

### Modular Route Files with `Router`

In a standard SwiftPY project, endpoints live inside dedicated files under `routes/` and are mounted in `main.py` with `app.include_router(...)`.

A new project already includes `routes/api.py`:

```python
# routes/api.py
from __future__ import annotations

from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.http.router import Router

from app.schemas.user_schema import UserResponseSchema

router = Router()


@router.get("/health")
async def health_check(request: Request) -> Response:
    """Health check endpoint for container orchestrators and monitoring."""
    return json_response({"status": "ok", "app": "SwiftPY"})


@router.get("/users")
async def list_users(request: Request) -> Response:
    """Example user listing endpoint returning typed schema response."""
    return json_response(
        [
            UserResponseSchema(
                id=1, name="Alice Doe", email="alice@example.com"
            ).to_dict()
        ]
    )
```

Create additional route modules as your API grows—for example `routes/orders.py`—and mount them in `main.py`:

```python
# routes/orders.py
from __future__ import annotations

from swiftpy.http.router import Router

router = Router()


@router.get("/")
async def list_orders() -> list[dict[str, object]]:
    return [{"id": 101, "status": "paid"}]


@router.get("/{order_id}")
async def get_order(order_id: int) -> dict[str, object]:
    return {"id": order_id, "status": "paid"}
```

```python
# main.py
from __future__ import annotations

from swiftpy import Application

from app.http.handler.exceptions import register_exception_handlers
from routes.api import router as api_router
from routes.orders import router as orders_router

app = Application()
register_exception_handlers(app)

app.include_router(api_router, prefix="/api")
app.include_router(orders_router, prefix="/api/orders")
```

You can also attach a base `prefix` and default `middleware` list directly on a `Router(...)` constructor, and compose routers inside other routers with `parent_router.include_router(child_router, prefix="/sub", middleware=[...])`:

```python
api = Router(prefix="/v1", middleware=[AuthMiddleware])
```

Constructor prefixes are validated eagerly against dynamic `{...}` segments (`RouteRegistrationError`), and outer `prefix`/`middleware` layers wrap child routes cleanly in declaration order.

### Route Groups (`app.group` / `router.group`)

When several endpoints share a URL prefix and/or middleware stack, group them with `@app.group(...)` or `@router.group(...)`:

```python
# routes/billing.py
from __future__ import annotations

from swiftpy.http.router import RouteGroup, Router

from app.http.middleware.auth_middleware import AuthMiddleware

router = Router()


@router.group("/billing", middleware=[AuthMiddleware])
def register_billing_routes(group: RouteGroup) -> None:
    @group.get("/invoices")
    async def list_invoices() -> list[dict[str, object]]:
        return [{"invoice_id": "inv_01", "total_cents": 4900}]

    @group.post("/refunds")
    async def create_refund() -> dict[str, str]:
        return {"status": "queued"}
```

`RouteGroup` supports `.get`, `.post`, `.put`, `.patch`, `.delete`, `.head`, `.options`, and `.websocket`.

### Route Precedence & HTTP Protocol Behavior

SwiftPY enforces strict, predictable routing rules:

- **Static routes beat dynamic routes**: Even if you register `@router.get("/users/{username}")` before `@router.get("/users/me")`, `GET /users/me` always matches the static `/users/me` handler.
- **Exact trailing-slash matching**: `/items` and `/items/` are distinct paths. Requesting `/items/` when only `/items` is registered returns `404 Not Found`.
- **Duplicate detection**: Registering two routes with the same HTTP method and normalized path raises `RouteRegistrationError` at startup.
- **Automatic `HEAD` & `OPTIONS`**: Any `GET` route automatically supports `HEAD` (returning headers with an empty body). `OPTIONS` requests on any matched path automatically return `204 No Content` with an `Allow` header listing supported methods.
- **Method Not Allowed (`405`)**: Sending an unsupported HTTP method to an existing route returns `405` JSON (`{"detail": "Method '...' not allowed for route '...'. Allowed: ..."}`) along with the `Allow` header.

---

## Path Parameters & Handler Injection

### Typed Path Parameters

Declare placeholders using `{name}` in the path string and specify the expected type using Python type annotations on the handler parameters:

```python
# routes/catalog.py
from __future__ import annotations

from uuid import UUID

from swiftpy.http.router import Router

router = Router()


@router.get("/orders/{order_id}")
async def show_order(order_id: int) -> dict[str, int]:
    return {"order_id": order_id}


@router.get("/flags/{enabled}")
async def check_flag(enabled: bool) -> dict[str, bool]:
    return {"enabled": enabled}


@router.get("/tokens/{token_id}")
async def inspect_token(token_id: UUID) -> dict[str, str]:
    return {"token_id": str(token_id)}
```

SwiftPY automatically coerces matched path segments using your annotation:

| Annotation | Accepted Input Examples | Invalid Input Result |
| :--- | :--- | :--- |
| `str` (or unannotated) | Any non-empty path segment | — |
| `int` | `"42"`, `"-7"` | Automatic **422** JSON error |
| `float` | `"19.95"` | Automatic **422** JSON error |
| `bool` | `"true"` / `"false"`, `"1"` / `"0"`, `"yes"` / `"no"` | Automatic **422** JSON error |
| Custom types (e.g. `UUID`) | Valid string accepted by type constructor | Automatic **422** JSON error |

> [!WARNING]
> Do **not** embed colons or type specifiers inside the path string like `"/orders/{order_id:int}"`. SwiftPY rejects invalid identifier syntax with `RouteRegistrationError`—always put types in the handler's Python signature.

### What Route Handlers Can Inject

SwiftPY inspects your route handler's signature and automatically binds any combination of:

1. `request: Request` — the incoming HTTP request.
2. **Path parameters** matching `{param_name}` in the route pattern.
3. **`FormRequest` subclasses** — automatically parsed and validated before your handler executes (covered in Guide #4).
4. **`BaseSchema` / `Schema` subclasses** — automatically parsed from JSON, form-urlencoded, or multipart bodies, coerced to your field annotations, and validated via `Field(...)` constraints (HTTP `422` on failure; covered in [Application Schemas](2026-11-19-application-schemas.md)).
5. **Uploaded files** — `UploadedFile` or `list[UploadedFile]` matching the multipart field name.
6. **DI container services** — any class or binding registered in the application container (such as `OrderService`, `Config`, or repository classes).
7. **Optional defaulted parameters** — parameters with default values when not satisfied by path or DI bindings.

```python
@router.post("/orders/{order_id}/receipt")
async def attach_receipt(
    request: Request,
    order_id: int,
    receipt: UploadedFile,
    orders: OrderService,
) -> dict[str, object]:
    ...
```

---

## Controllers & API Resources

Instead of putting business logic inside `routes/*.py`, generate controllers and API resources with the CLI:

```bash
uv run swiftpy make:controller OrderController
uv run swiftpy make:resource OrderResource
```

By default:
- `make:controller` creates files in `app/http/controllers/`
- `make:resource` creates files in `app/http/resources/`
- Pass `--path custom/dir` to override the destination or `--force` to overwrite an existing file.

### Writing a Resource (`app/http/resources/order_resource.py`)

Resources keep your response payloads decoupled from internal database or domain models:

```python
# app/http/resources/order_resource.py
from __future__ import annotations

from typing import Any


class OrderResource:
    """OrderResource API resource transformer."""

    def __init__(self, resource: Any) -> None:
        self.resource = resource

    def to_dict(self) -> dict[str, Any]:
        """Transform a single order into an API-safe dictionary."""
        return {
            "id": getattr(self.resource, "id", None),
            "reference": getattr(self.resource, "reference", None),
            "total_cents": getattr(self.resource, "total_cents", 0),
        }

    @classmethod
    def collection(cls, items: list[Any]) -> list[dict[str, Any]]:
        """Transform a collection of resources."""
        return [cls(item).to_dict() for item in items]
```

> [!TIP]
> SwiftPY's JSON encoder also calls `.to_dict()` automatically on any object returned inside a `dict` or `list`, while stripping any attribute listed in the object's `__hidden__` sequence.

### Writing & Wiring a Controller (`app/http/controllers/order_controller.py`)

```python
# app/http/controllers/order_controller.py
from __future__ import annotations

from swiftpy.core.exceptions import NotFoundError
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response

from app.http.resources.order_resource import OrderResource
from app.services.order_service import OrderService


class OrderController:
    """HTTP Controller for order endpoints."""

    def __init__(self, orders: OrderService) -> None:
        self.orders = orders

    async def index(self, request: Request) -> Response:
        page = int(request.query_params.get("page", "1"))
        records = await self.orders.list_page(page=page)
        return json_response(OrderResource.collection(records))

    async def show(self, order_id: int) -> Response:
        record = await self.orders.find(order_id)
        if record is None:
            raise NotFoundError(f"Order {order_id} was not found.")
        return json_response(OrderResource(record).to_dict())
```

There are three clean ways to wire a controller in `routes/orders.py`:

```python
# Option A (Recommended): Direct unbound controller method binding with automatic DI
# routes/orders.py
from __future__ import annotations

from swiftpy.http.router import Router

from app.http.controllers.order_controller import OrderController

router = Router()

router.get("/", OrderController.index)
router.get("/{order_id}", OrderController.show)
```

```python
# Option B: Decorator function injecting the controller via DI
@router.get("/")
async def index(request: Request, controller: OrderController) -> Response:
    return await controller.index(request)


@router.get("/{order_id}")
async def show(order_id: int, controller: OrderController) -> Response:
    return await controller.show(order_id)
```

Or if your controller takes no constructor dependencies, instantiate it directly and register bound methods with `router.add_route(...)`:

```python
# Option C: Direct bound method binding with router.add_route(...)
controller = StaticController()
router.add_route("GET", "/", controller.index)
router.add_route("GET", "/{order_id}", controller.show)
```

> With **Option A** (`router.get("/", OrderController.index)` or `app.get("/orders", OrderController.index)`), SwiftPY resolves `OrderController.__init__(...)` per request from the DI container cleanly without sharing instance state across concurrent requests. See [Application Architecture & Project Structure](2026-11-21-application-architecture-and-project-structure.md) for recommended layering patterns.

---

## Reading Incoming Requests (`Request`)

Inject `request: Request` into any handler or middleware to inspect headers, query parameters, bodies, state, or multipart uploads:

```python
# routes/inspect_demo.py
from __future__ import annotations

from swiftpy.core.exceptions import BadRequestError
from swiftpy.http.request import Request
from swiftpy.http.router import Router

router = Router()


@router.get("/search")
async def search(request: Request) -> dict[str, object]:
    # Headers (case-insensitive)
    user_agent = request.headers.get("user-agent", "unknown")
    accept_values = request.headers.getlist("accept")

    # Query parameters
    query = request.query_params.get("q", "")
    tags = request.query_param_list.get("tag", [])

    return {
        "method": request.method,
        "path": request.path,
        "user_agent": user_agent,
        "accept": accept_values,
        "q": query,
        "tags": tags,
    }


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> dict[str, int]:
    raw_bytes = await request.body()
    if not raw_bytes:
        raise BadRequestError("Empty webhook body.")
    payload = await request.json()
    return {"bytes": len(raw_bytes), "keys": len(payload)}
```

Common `Request` APIs:

- `request.method` / `request.path` — uppercase HTTP method and URL path.
- `request.headers` — `RequestHeaders` (case-insensitive `get()`, `in`, plus `getlist(name)` when headers appear multiple times).
- `request.query_params` — `dict[str, str]` keeping the last value per key.
- `request.query_param_list` — `dict[str, list[str]]` preserving repeated query parameters (`?tag=a&tag=b`).
- `await request.body(max_size=...)` — raw `bytes` body (cached after first read).
- `await request.json()` — parsed JSON payload (`BadRequestError` on invalid JSON).
- `await request.form()` — parsed URL-encoded or multipart text fields as `dict[str, Any]`.
- `request.input(key, default=None)` / `request.all()` / `request.<field>` — read pre-loaded JSON/form/query fields synchronously (see [Requests & Responses](2026-11-20-request-and-responses.md) for full details on pre-loading and `StreamingResponse`).
- `request.state` — per-request dictionary (`dict[str, Any]`) for passing values from middleware to handlers.

### Handling Multipart File Uploads

You can receive files either directly in the handler signature or via `await request.file(...)` / `await request.files(...)`:

```python
# routes/uploads.py
from __future__ import annotations

from swiftpy.core.exceptions import BadRequestError
from swiftpy.filesystem import UploadedFile
from swiftpy.http.request import Request
from swiftpy.http.router import Router

router = Router()


@router.post("/profile/avatar")
async def upload_avatar(avatar: UploadedFile) -> dict[str, object]:
    if avatar is None:
        raise BadRequestError("Please upload a file in the 'avatar' field.")

    await avatar.validate({"max_size": 2 * 1024 * 1024, "mimes": ["image/png", "image/jpeg"]})
    stored_path = await avatar.store("avatars")

    return {
        "filename": avatar.filename,
        "content_type": avatar.content_type,
        "size": avatar.size,
        "path": stored_path,
    }


@router.post("/documents")
async def upload_documents(attachments: list[UploadedFile], request: Request) -> dict[str, object]:
    form_fields = await request.form()
    names = [item.filename for item in attachments]
    return {"title": form_fields.get("title", ""), "uploaded": names}
```

> [!IMPORTANT]
> Annotate injected single-file parameters as `UploadedFile` (not `UploadedFile | None`, which is treated as a union/default instead of a file binding). When a client omits that multipart field, `avatar` receives `None` at runtime, so check `if avatar is None:` before calling `.read()`, `.validate()`, or `.store()`. For dynamically named or optional file fields, use `await request.file("avatar")`.

### Request Size & JSON Depth Protections

To guard against oversized payloads and deeply nested JSON bombs, SwiftPY enforces strict request limits by default:

- `max_body_size`: `2 MiB` (`2 * 1024 * 1024` bytes) -> returns **413 Payload Too Large**
- `max_content_length`: `2 MiB` (`2 * 1024 * 1024` bytes) -> returns **413 Payload Too Large**
- `max_json_depth`: `32` levels -> returns **400 Bad Request**

Override these globally when constructing `Application(...)` or per endpoint on route decorators:

```python
@router.post(
    "/imports/bulk",
    max_body_size=10 * 1024 * 1024,
    max_content_length=10 * 1024 * 1024,
    max_json_depth=64,
)
async def bulk_import(request: Request) -> dict[str, str]:
    await request.json()
    return {"status": "accepted"}
```

---

## Returning Responses (`Response` & Helpers)

### Auto-JSON Serialization for `dict`, `list`, and `BaseSchema`

Any `dict`, `list`, or `BaseSchema` / `Schema` instance returned from a handler is automatically wrapped into an `application/json` `Response`. SwiftPY's JSON encoder handles:

- `datetime`, `date`, `time` -> ISO-8601 strings
- `UUID` and `Decimal` -> strings
- `Enum` -> `.value`
- `set` and `frozenset` -> JSON arrays
- `BaseSchema` and other objects with `.to_dict()` -> dictionary output with excluded (`exclude=True`) or `__hidden__` fields removed

For safety and RFC compliance, `NaN`/`Infinity` floats and raw `bytes` values raise `TypeError` during JSON serialization.

### Response Helpers & Explicit `Response` Objects

Import `Response` and built-in helpers from `swiftpy.http.response`:

```python
from swiftpy.http.response import (
    Response,
    created,
    forbidden,
    json_response,
    not_found,
    unauthorized,
    unprocessable,
)


@router.get("/plain")
async def plain_text() -> Response:
    return Response("Hello from SwiftPY", status_code=200)


@router.post("/notes")
async def create_note() -> Response:
    return created({"id": 7, "title": "Release checklist"})


@router.get("/secret")
async def secret_area() -> Response:
    return forbidden("Administrators only")
```

Available helpers in `swiftpy.http.response`:

| Helper | Default Status | Body / Headers |
| :--- | :---: | :--- |
| `json_response(data, status_code=200, headers=None)` | `200` | Encoded JSON + `content-type: application/json` |
| `created(data)` | `201` | Encoded JSON + `201 Created` |
| `not_found(message="Not Found")` | `404` | `{"detail": message}` |
| `unprocessable(errors)` | `422` | `{"errors": errors}` |
| `unauthorized(message="Unauthorized", headers=None)` | `401` | `{"detail": message}` + `WWW-Authenticate: Bearer` |
| `forbidden(message="Forbidden")` | `403` | `{"detail": message}` |

### Custom Headers & Multiple Cookies

`Response.headers` is a `ResponseHeaders` instance that protects against CR/LF/NUL header injection while allowing repeated headers such as `Set-Cookie`:

```python
@router.post("/session/login")
async def login() -> Response:
    resp = json_response({"authenticated": True})
    resp.headers["X-API-Version"] = "2026-11"
    resp.set_cookie("session_id=tok_123; Path=/; HttpOnly; SameSite=Lax")
    resp.set_cookie("locale=en-US; Path=/")
    return resp
```

Use `response.headers[key] = value` to set or replace a single header, and `response.set_cookie(...)` (or `response.headers.add("Set-Cookie", ...)`) when sending multiple cookies.

### Streaming Responses (`stream` / `StreamingResponse`)

Use `stream(...)` with an async generator for Server-Sent Events (SSE) or large chunked payloads without buffering everything in memory:

```python
# routes/events.py
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from swiftpy.http.response import StreamingResponse, stream
from swiftpy.http.router import Router

router = Router()


@router.get("/live")
async def live_stream() -> StreamingResponse:
    async def event_chunks() -> AsyncIterator[str]:
        for step in range(3):
            yield f"data: tick {step}\n\n"
            await asyncio.sleep(0.2)

    return stream(event_chunks(), content_type="text/event-stream")
```

SwiftPY automatically awaits `aclose()` on your async iterator when the stream completes or the client disconnects.

---

## Middleware Pipeline

Middleware wraps incoming requests before they reach a route handler and inspects or modifies outgoing `Response` objects on the way back out.

### Generating Custom Middleware

Generate a middleware skeleton in `app/http/middleware/` with the CLI:

```bash
uv run swiftpy make:middleware TimingMiddleware
```

This creates `app/http/middleware/timing_middleware.py`:

```python
# app/http/middleware/timing_middleware.py
from __future__ import annotations

import time

from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response


class TimingMiddleware(Middleware):
    """Add an X-Response-Time-Ms header to every HTTP response."""

    async def handle(self, request: Request, next_handler: Handler) -> Response:
        started = time.perf_counter()
        response = await next_handler(request)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.2f}"
        return response
```

Because SwiftPY resolves middleware classes through the DI container, you can inject `Config` or registered services into your middleware's `__init__`:

```python
# app/http/middleware/version_header_middleware.py
from __future__ import annotations

from swiftpy.core.config import Config
from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response


class VersionHeaderMiddleware(Middleware):
    def __init__(self, config: Config) -> None:
        self.app_name = str(config.get("app.name", "SwiftPY"))

    async def handle(self, request: Request, next_handler: Handler) -> Response:
        response = await next_handler(request)
        response.headers["X-Powered-By"] = self.app_name
        return response
```

### Attaching Middleware & Execution Order

Register **class types** (not instances) at any of three levels:

1. **Global middleware** (runs for all HTTP requests):
   ```python
   # main.py
   app.middleware([RequestIdMiddleware, SecurityHeadersMiddleware, TimingMiddleware])
   ```
2. **Router inclusion or group middleware** (runs only for mounted/grouped routes):
   ```python
   app.include_router(admin_router, prefix="/api/admin", middleware=[AuthMiddleware, AdminMiddleware])
   ```
3. **Single-route middleware on `Router`**:
   ```python
   @router.post("/webhooks", middleware=[VerifySignatureMiddleware])
   async def receive_webhook(request: Request) -> dict[str, str]:
       return {"status": "ok"}
   ```

SwiftPY executes middleware in onion order from outer to inner:

```text
Global MW (before) -> Group/Include MW (before) -> Route MW (before)
   -> Route Handler
Route MW (after)  <- Group/Include MW (after)  <- Global MW (after)
```

### Built-In Opt-In Middleware

Import built-in middleware from `swiftpy.http.middleware` and enable only what your application needs:

#### 1. `RequestIdMiddleware` (alias `RequestId`)

Assigns a unique correlation ID to every request, preserving a valid incoming `X-Request-ID` header if sent by an upstream proxy, or generating a UUID4 otherwise. It stores the ID in:
- `response.headers["X-Request-ID"]`
- `request.state["request_id"]`
- `Context.get("request_id")` (for request-scoped logging downstream)

#### 2. `SecurityHeadersMiddleware` (alias `SecurityHeaders`)

Attaches standard defensive HTTP response headers automatically:
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `X-XSS-Protection: 0`
- `Cross-Origin-Opener-Policy: same-origin`

#### 3. `CORSMiddleware` (alias `CORS`)

Enforces Cross-Origin Resource Sharing using `config/cors.py` (which reads your `.env` settings). By default, `allowed_origins` is empty (`[]`), meaning cross-origin headers are **fail-closed** until you configure explicit origins.

Generated `config/cors.py`:

```python
# config/cors.py
from __future__ import annotations

from swiftpy.helpers.helper import env, env_bool

CONFIG = {
    "allowed_origins": [
        origin.strip()
        for origin in str(env("CORS_ALLOWED_ORIGINS", "") or "").split(",")
        if origin.strip()
    ],
    "allowed_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    "allowed_headers": ["Content-Type", "Authorization", "X-Request-ID"],
    "expose_headers": ["X-Request-ID"],
    "allow_credentials": env_bool("CORS_ALLOW_CREDENTIALS", False),
    "max_age": int(env("CORS_MAX_AGE", "600") or "600"),
}
```

Enable CORS in `main.py` and configure allowed origins in `.env`:

```python
# main.py
from swiftpy.http.middleware import CORSMiddleware

app.middleware([CORSMiddleware])
```

```dotenv
# .env
CORS_ALLOWED_ORIGINS=https://app.example.com,http://localhost:3000
CORS_ALLOW_CREDENTIALS=true
CORS_MAX_AGE=600
```

`CORSMiddleware` handles preflight `OPTIONS` requests (`Access-Control-Request-Method`) with `204 No Content` and attaches `Vary: Origin` when specific origins or credentials are enabled. Never pair `"*"` in `allowed_origins` with `CORS_ALLOW_CREDENTIALS=true`. See the dedicated [CORS Configuration & Middleware](2026-11-21-cors.md) guide for full preflight, credential, header exposure, and SPA integration details.

---

## Exception Handling & Custom Error Responses

### Built-In HTTP Exceptions

Raise semantic HTTP exceptions anywhere in middleware, services, or controllers instead of manually threading error responses back up through business logic:

```python
from swiftpy.core.exceptions import (
    BadRequestError,      # 400
    UnauthorizedError,    # 401
    ForbiddenError,       # 403
    NotFoundError,        # 404
    MethodNotAllowedError,# 405
    PayloadTooLargeError, # 413
    ValidationError,      # 422
    InternalServerError,  # 500
    HttpException,        # Any custom status_code + optional headers
)
```

SwiftPY automatically converts framework exceptions into JSON responses:

- `HttpException` subclasses -> `{"detail": exc.message}` with `exc.status_code` and `exc.headers`.
- ORM `ModelNotFoundError` / `RecordNotFoundError` -> `404` `{"detail": exc.message}`.
- `ValidationError`, `ValidationException`, and `FileValidationError` -> `422` `{"errors": exc.errors}`.

### Custom Exceptions & `app/http/handler/exceptions.py`

A generated SwiftPY project includes `app/http/handler/exceptions.py` and calls `register_exception_handlers(app)` from `main.py`. Add your own domain exceptions and renderers there:

```python
# app/http/handler/exceptions.py
from __future__ import annotations

from swiftpy.core.application import Application
from swiftpy.core.exceptions import HttpException
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response


class ResourceConflictException(HttpException):
    """Raised when an operation conflicts with existing server state (HTTP 409)."""

    def __init__(self, message: str = "Resource conflict") -> None:
        super().__init__(status_code=409, message=message)


class PaymentDeclinedError(Exception):
    """Domain exception translated into HTTP 402 JSON."""

    def __init__(self, reason: str, retryable: bool = False) -> None:
        self.reason = reason
        self.retryable = retryable
        super().__init__(reason)


def register_exception_handlers(app: Application) -> None:
    """Register custom application error renderers onto the application."""

    @app.exception_handler(ResourceConflictException)
    async def handle_conflict(
        request: Request, exc: ResourceConflictException
    ) -> Response:
        return json_response(
            {"error": "conflict", "detail": exc.message}, status_code=409
        )

    @app.exception_handler_for(PaymentDeclinedError)
    async def handle_payment_declined(
        request: Request, exc: PaymentDeclinedError
    ) -> Response:
        return json_response(
            {"error": "payment_declined", "detail": exc.reason, "retryable": exc.retryable},
            status_code=402,
        )
```

Both `@app.exception_handler(ExcType)` and `@app.exception_handler_for(ExcType)` are supported (or call `app.exception_handler.register(ExcType, fn)` directly). Handlers can be `async def` or regular `def` functions and accept `(request, exc)` or `(exc, request)`; lookup walks the exception's MRO so more specific exception subclasses take precedence over base classes.

### Production vs Debug Error Masking

SwiftPY prevents sensitive internal details from leaking in production:

- **When `debug=False` (production default)**: Unhandled Python exceptions **and** `HttpException` instances with `status_code >= 500` (such as `InternalServerError("db password...")`) automatically sanitize the public response body to `{"detail": "Internal Server Error"}` while logging full details server-side.
- **When `debug=True` (local development)**: Unhandled exceptions include `error`, `exception`, `message`, and `traceback` in the JSON payload so you can diagnose crashes immediately.

---

> [!TIP]
> For the dedicated deep-dive on `request.input()` / `request.<field>` semantics and `StreamingResponse`, see [Requests & Responses](2026-11-20-request-and-responses.md). For declarative `HttpException` subclasses (`status_code = 409`, `message = ...`) and `abort(...)`, continue to [Error Handling & Exceptions](2026-11-20-error-handling.md).

---

## Next Step

Continue to [Requests & Responses](2026-11-20-request-and-responses.md) to explore `Request` input accessors (`request.input()`, `request.all()`, `request.<field>`), JSON/redirect/file responses, and `StreamingResponse`.
