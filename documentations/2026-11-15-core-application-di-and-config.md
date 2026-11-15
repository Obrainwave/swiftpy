# Application Lifecycle, Configuration & Dependency Injection

SwiftPY gives your backend a clear, predictable structure: `Application` runs your ASGI server and HTTP/WebSocket traffic, `config/` files hold environment-aware settings, `ServiceProvider` classes connect your services cleanly at startup and shutdown, and the DI container wires dependencies into your routes and classes automatically.

This guide shows you how to create configuration files, generate services and service providers, inject dependencies, and safely share request-local data.

---

## The Application Entrypoint (`main.py`)

When you create a project with `swiftpy new`, `main.py` creates the `Application` instance, attaches exception handlers, and mounts your routers:

```python
# main.py
from __future__ import annotations

from swiftpy import Application

from app.http.handler.exceptions import register_exception_handlers
from routes.api import router as api_router

app = Application()
register_exception_handlers(app)

app.include_router(api_router, prefix="/api")
```

When `Application()` initializes, it automatically:

1. Loads `.env` and every Python config file in `config/`.
2. Auto-discovers and registers every `ServiceProvider` inside `app/providers/`.
3. Wires built-in framework services (`Config`, `Cache`, database pool when configured, mail, queues, events, authentication, and WebSockets).

### Customizing `Application` Settings

You can configure app metadata, request size limits, or explicit inline config/providers directly in `Application(...)` — useful for single-file services or isolated tests:

```python
from __future__ import annotations

from swiftpy import Application

app = Application(
    title="Orders API",
    version="1.2.0",
    max_body_size=5 * 1024 * 1024,  # Allow up to 5 MiB request bodies (default: 2 MiB)
    max_json_depth=32,              # Max JSON nesting depth (default: 32)
)
```

If you want to control provider loading explicitly (for instance, in a standalone script or custom entrypoint), pass `auto_discover_providers=False` and `providers=[...]`:

```python
from swiftpy.core.bootstrap import create_app

from app.providers.app_service_provider import AppServiceProvider

app = create_app(
    config={"app": {"name": "Worker Service", "env": "local"}},
    auto_discover_providers=False,
    providers=[AppServiceProvider],
)
```

> **Production Safeguards:** When `APP_ENV=production` (`app.env` is `"production"` or `"prod"`):
> - `debug=True` (`APP_DEBUG=true`) is refused and raises `ConfigurationError`.
> - `APP_KEY` must be configured and decode to at least 32 bytes (`base64:...` or a 32+ character secret), otherwise startup fails with `ConfigurationError`.

---

## Configuration (`config/`, `.env`, `config()`, `env()`)

SwiftPY stores application configuration as clean Python dictionaries inside the `config/` directory at your project root. Each file becomes a top-level namespace (for example, `config/app.py` maps to `app.*`, `config/database.py` maps to `database.*`).

### Creating a Configuration File

To add configuration for your own integrations or feature flags, create a new `.py` file under `config/` that defines a `CONFIG` dictionary and reads `.env` values using `env()` and `env_bool()`:

```python
# config/billing.py
from __future__ import annotations

from swiftpy import env, env_bool

CONFIG = {
    "enabled": env_bool("BILLING_ENABLED", True),
    "currency": env("BILLING_CURRENCY", "usd"),
    "stripe": {
        "secret_key": env("STRIPE_SECRET_KEY", ""),
        "webhook_secret": env("STRIPE_WEBHOOK_SECRET", ""),
        "timeout": float(env("STRIPE_TIMEOUT", "15.0") or "15.0"),
    },
}
```

Then add the corresponding keys to `.env` (and `.env.example`):

```dotenv
BILLING_ENABLED=true
BILLING_CURRENCY=eur
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
```

> **Why use `env()` instead of `os.getenv()`?**  
> SwiftPY reads `.env` into an isolated loading context without mutating `os.environ`. Values written only in `.env` are available to `env()` and `env_bool()`, whereas `os.getenv()` would miss them unless exported in your shell. Process environment variables still override `.env`.

### Reading Configuration in Routes and Services

You can read configuration values with dot notation either through the `config()` helper or by injecting `Config` directly into any class or route handler:

```python
# routes/api.py
from __future__ import annotations

from swiftpy import Config, Request, Response, config, json_response
from swiftpy.http.router import Router

router = Router()


@router.get("/billing/status")
async def billing_status(request: Request, cfg: Config) -> Response:
    # Option 1: Using the injected Config instance
    enabled = cfg.get("billing.enabled", False)
    currency = cfg.get("billing.currency", "usd")

    # Option 2: Using the global config() helper anywhere in your app
    app_name = config("app.name", "SwiftPY")

    return json_response(
        {
            "app": app_name,
            "billing_enabled": enabled,
            "currency": currency,
        }
    )
```

Useful `Config` operations:

- `config("billing.stripe.timeout", 15.0)` — read a nested key with a fallback default.
- `cfg.has("billing.stripe.secret_key")` or `"billing.stripe.secret_key" in cfg` — check whether a key exists (even if its value is `None`).
- `cfg.set("billing.currency", "gbp")` — update a key at runtime.

### Overriding Config with Environment Variables

Even if a config file doesn't read a specific key via `env()`, SwiftPY merges environment variables automatically when loading configuration:

- **Top-level section keys:** `APP_NAME="Shop API"` overrides `app.name`.
- **Nested double-underscore paths:** `DATABASE__POOL__MAX_SIZE=50` overrides `database.pool.max_size`; `BILLING__STRIPE__CURRENCY=cad` sets `billing.stripe.currency`.
- **`SWIFTPY_` prefix (highest precedence):** `SWIFTPY_APP__ENV=production` takes priority over unprefixed variables.

When a key already has a default value in `config/*.py`, SwiftPY coerces the environment string to match that default's type (`bool`, `int`, finite `float`, or `list` from JSON arrays / comma-separated values). Keys without an existing typed default remain strings so values like `"0012"` are never accidentally altered.

### Caching Configuration for Production

In production, you can precompile configuration into `bootstrap/cache/config.json` (and prompt templates into `bootstrap/cache/prompts.json`) so Python config modules are not re-evaluated on startup:

```bash
swiftpy cache:build
```

If you change `.env` or any file in `config/` later, rebuild or clear the cache before restarting the server:

```bash
swiftpy cache:clear
```

---

## Services & Dependency Injection

SwiftPY's DI container injects typed dependencies into your route handlers and services automatically — in most cases without writing a single line of wiring code.

### 1. Generate a Service Class

Use the CLI to generate a service in `app/services/`:

```bash
swiftpy make:service InvoiceService
```

This creates `app/services/invoice_service.py`:

```python
# app/services/invoice_service.py
from __future__ import annotations

from swiftpy import Config


class InvoiceService:
    """Create and format customer invoices."""

    def __init__(self, cfg: Config) -> None:
        self.currency = str(cfg.get("billing.currency", "usd"))

    async def create_preview(self, amount_cents: int) -> dict[str, object]:
        return {
            "amount_cents": amount_cents,
            "currency": self.currency.upper(),
        }
```

Pass `--path custom/dir` to save in another directory, or `--force` to overwrite an existing file.

### 2. Inject Services Directly (Zero-Config Auto-Wiring)

Notice `InvoiceService.__init__` expects `cfg: Config`. Because `InvoiceService` is a concrete class with typed constructor parameters, SwiftPY auto-wires `Config` and injects `InvoiceService` directly into any route handler or another service:

```python
# routes/api.py
from __future__ import annotations

from swiftpy import Request, Response, json_response
from swiftpy.http.router import Router

from app.services.invoice_service import InvoiceService

router = Router()


@router.get("/invoices/preview")
async def preview_invoice(request: Request, invoices: InvoiceService) -> Response:
    preview = await invoices.create_preview(amount_cents=2500)
    return json_response(preview)
```

If you ever need to resolve a service manually (outside a route handler), use `resolve()` or `app.container.resolve(...)`:

```python
from swiftpy import resolve
from app.services.invoice_service import InvoiceService

service = resolve(InvoiceService)
```

### 3. Choosing Binding Lifetimes & Using Interfaces

When you want to program against a `Protocol`/interface, use a custom factory function, or share a single instance across requests, register bindings in a **ServiceProvider**:

| Method | Lifetime | Typical Use Case |
|---|---|---|
| `self.container.bind(Key, ImplOrFactory)` | **Transient** (default) | Fresh instance created every time it is resolved. |
| `self.container.singleton(Key, ImplOrFactory)` | **Singleton** | Created once and shared across the entire app (API pools, clients, managers). |
| `self.container.scoped(Key, ImplOrFactory)` | **Request / Task Scoped** | Created once per HTTP request or WebSocket connection and automatically closed when the request ends. |
| `self.container.instance(Key, obj)` | **Pre-built Instance** | Register an already-constructed object as a singleton. |

---

## Service Providers (`app/providers/`)

Service providers are the central place to bind interfaces, configure third-party clients, and run async startup or shutdown logic cleanly.

### Creating a Service Provider

Generate a provider in `app/providers/` using the CLI:

```bash
swiftpy make:provider PaymentServiceProvider
```

This creates `app/providers/payment_service_provider.py`. Because it lives in `app/providers/`, SwiftPY discovers and loads it automatically when `Application()` starts (`AppServiceProvider` is always loaded first, followed by remaining providers in filename order).

### `register`, `boot`, `startup`, and `shutdown`

A `ServiceProvider` gives you four lifecycle methods:

1. **`register(self) -> None`** *(required, sync)*: Bind services and interfaces into `self.container`. Keep this strictly to container registrations so other providers have finished registering before services are resolved.
2. **`boot(self) -> None`** *(optional, sync)*: Runs after **all** providers have finished `register()`. Safe place to resolve services, register event listeners, or warm sync state.
3. **`async def startup(self) -> None`** *(optional, async)*: Awaited when the ASGI server starts (`lifespan.startup`). Ideal for opening async connection pools or verifying upstream connections. If any provider fails during `startup()`, already-started providers have their `shutdown()` hooks rolled back automatically.
4. **`async def shutdown(self) -> None`** *(optional, async)*: Awaited in reverse registration order when the server shuts down (`lifespan.shutdown`).

### Complete Provider Example (External Async Client + Scoped Session)

Here is a practical provider that opens a shared `httpx.AsyncClient` on startup, closes it on shutdown, binds a `PaymentGateway` `Protocol`, and registers a request-scoped audit tracker that flushes at the end of each request:

```python
# app/services/payment_gateway.py
from __future__ import annotations

from typing import Protocol

import httpx


class PaymentGateway(Protocol):
    async def charge(self, amount_cents: int, currency: str) -> dict[str, str]: ...


class StripeGateway:
    def __init__(self, client: httpx.AsyncClient, secret_key: str) -> None:
        self._client = client
        self._secret_key = secret_key

    async def charge(self, amount_cents: int, currency: str) -> dict[str, str]:
        return {
            "status": "authorized",
            "amount": str(amount_cents),
            "currency": currency,
        }


class RequestAuditLog:
    """Scoped per HTTP request; SwiftPY calls aclose() automatically at response end."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, message: str) -> None:
        self.events.append(message)

    async def aclose(self) -> None:
        if self.events:
            # Flush per-request audit trail cleanly once the request finishes
            self.events.clear()
```

Now wire it inside `app/providers/payment_service_provider.py`:

```python
# app/providers/payment_service_provider.py
from __future__ import annotations

import httpx

from swiftpy import Config, ServiceProvider

from app.services.payment_gateway import (
    PaymentGateway,
    RequestAuditLog,
    StripeGateway,
)


class PaymentServiceProvider(ServiceProvider):
    """Register payment client, gateway binding, and per-request audit tracking."""

    def register(self) -> None:
        self.container.singleton(
            httpx.AsyncClient,
            lambda c: httpx.AsyncClient(
                timeout=float(c.resolve(Config).get("billing.stripe.timeout", 15.0))
            ),
        )
        self.container.singleton(
            PaymentGateway,
            lambda c: StripeGateway(
                client=c.resolve(httpx.AsyncClient),
                secret_key=str(
                    c.resolve(Config).get("billing.stripe.secret_key", "")
                ),
            ),
        )
        self.container.scoped(RequestAuditLog)

    async def startup(self) -> None:
        # Ensure the shared AsyncClient is created at server boot
        self.container.resolve(httpx.AsyncClient)

    async def shutdown(self) -> None:
        client = self.container.get_singleton(httpx.AsyncClient)
        if client is not None:
            await client.aclose()
```

Now any route handler can type-hint `PaymentGateway` and `RequestAuditLog` cleanly:

```python
# routes/api.py
from __future__ import annotations

from swiftpy import Request, Response, json_response
from swiftpy.http.router import Router

from app.services.payment_gateway import PaymentGateway, RequestAuditLog

router = Router()


@router.post("/checkout")
async def checkout(
    request: Request,
    gateway: PaymentGateway,
    audit: RequestAuditLog,
) -> Response:
    result = await gateway.charge(amount_cents=4900, currency="usd")
    audit.record("checkout_authorized")
    return json_response(result)
```

Because `RequestAuditLog` is registered with `self.container.scoped(...)`:
- Resolving `RequestAuditLog` multiple times during the **same** HTTP request (across middleware, services, and route handlers) returns the **same** instance.
- Concurrent requests each get their own isolated instance.
- When the HTTP response finishes, SwiftPY automatically awaits `aclose()` (or calls `close()`, `__aexit__()`, or `__exit__()` if defined) on every scoped instance.

### Scoped Cleanup in CLI Commands or Background Jobs

Outside HTTP/WebSocket requests — such as inside a standalone script or queue worker — wrap work in `async with container.scope():` so scoped bindings are isolated and closed when the block exits:

```python
from swiftpy import container

async def run_batch_job() -> None:
    async with container().scope() as scoped_container:
        audit = scoped_container.resolve(RequestAuditLog)
        audit.record("batch_step_1")
    # audit.aclose() runs automatically here
```

---

## Storing Request-Local Data with `Context`

Because an async ASGI server runs many requests concurrently inside the same Python process, **never** store per-request values (current tenant, correlation ID, locale) in module-level global variables or mutable class attributes.

Use SwiftPY's `Context` helper instead. It stores request-local values safely per async task and clears automatically at the end of every HTTP request and WebSocket connection:

```python
# app/http/middleware/tenant_middleware.py
from __future__ import annotations

from swiftpy.core.context import Context
from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response


class TenantMiddleware(Middleware):
    async def handle(self, request: Request, next_handler: Handler) -> Response:
        tenant_id = request.headers.get("x-tenant-id", "default")
        Context.set("tenant_id", tenant_id)
        return await next_handler(request)
```

Then read `"tenant_id"` cleanly anywhere downstream — in services, repositories, or route handlers — without passing `request` into every method:

```python
# app/services/tenant_service.py
from __future__ import annotations

from swiftpy.core.context import Context


def current_tenant_id() -> str:
    return str(Context.get("tenant_id", "default"))
```

Other handy `Context` helpers:

- `Context.has("tenant_id")` — check if a key is set in the current task.
- `Context.all()` — get a shallow dictionary copy of all active context keys.
- `Context.push("tags", "vip")` / `Context.pop("tags")` — append to and pop from task-local stacks safely.
- `Context.clear()` — manually reset the current context if running custom background tasks.

---

## Next Step

Continue with **#3 — HTTP Routing, Request/Response, Middleware & Exceptions**: [`docs/2026-11-14_documentation_plan.md`](../docs/2026-11-14_documentation_plan.md).
