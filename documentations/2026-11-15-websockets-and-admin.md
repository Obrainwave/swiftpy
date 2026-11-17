# WebSockets & Admin Dashboard

## Introduction

SwiftPY provides built-in real-time messaging and a zero-boilerplate administrative panel integrated directly into `Application`:

- **WebSockets (`swiftpy.websockets`):** Define WebSocket endpoints with `@app.websocket(...)`, manage rooms and topic-style broadcasts with `WebSocketManager` (`ws_manager()`), enforce Decision `D-14` origin checks, authenticate handshakes with HTTP middleware (`JWTGuard` / `require_api_key`), bound frame sizes and idle timeouts, and fan out broadcasts across workers via `RedisEventBus`.
- **Admin Dashboard (`swiftpy.admin`):** Register ORM `Model` classes with `AdminSite` (`admin`) and `ModelAdmin`, enforce RBAC permissions and HMAC-signed cookie/CSRF sessions (`AdminUser`, `AdminSessionStore`, `AdminAuthenticator`), and mount full HTML + JSON CRUD views (plus AI observability stats) onto your application with `admin.mount(app)`.

```python
from swiftpy import Application
from swiftpy.admin import (
    AdminAuthenticator,
    AdminSite,
    AdminUser,
    ModelAdmin,
    admin,
    get_admin_authenticator,
)
from swiftpy.helpers import websocket_manager, ws_manager
from swiftpy.websockets import (
    WebSocket,
    WebSocketConnection,
    WebSocketDisconnect,
    WebSocketManager,
)
```

---

## Creating & Organizing Files

- **WebSocket handlers & config:**
  - Define `@app.websocket(...)` routes alongside HTTP routes (for example in `routes/web.py`, `routes/api.py`, or dedicated `Router` instances included on `app`).
  - `Application` creates a `WebSocketManager` singleton on `app.websocket_manager`, registers it in `app.container`, subscribes it to `RedisEventBus` (when configured) on startup, and disconnects/cleans up active connections on shutdown.
  - WebSocket runtime settings can be supplied directly on `Application(...)` (`allowed_origins`, `max_ws_message_size`, `ws_idle_timeout`) or via config keys under `websockets.*` (`websockets.allowed_origins`, `websockets.max_message_size`, `websockets.idle_timeout`).
- **Admin registration & starter views:**
  - Put `AdminSite` registrations (`@admin.register(...)` / `admin.register(...)`) in an imported module (e.g., `app/admin.py`) and mount with `admin.mount(app, prefix="/admin")` during app bootstrap in `main.py`.
  - Newly scaffolded projects also include reference HTML templates in `views/admin/login.html` and `views/admin/dashboard.html`; `AdminSite.mount()` generates responsive HTML and JSON endpoints dynamically from your registered `ModelAdmin` classes.
  - Configure admin credentials and signing secret via `config={"admin": {...}}` (or a `config/admin.py` file / double-underscore environment variables such as `ADMIN__SECRET_KEY`, `ADMIN__USERNAME`, `ADMIN__PASSWORD`):
    - `admin.username` & `admin.password`
    - `admin.secret_key` (falls back to `app.key`; required outside local debug mode)
    - `admin.cookie_secure` (optional boolean override; otherwise enabled automatically on HTTPS / `production` env)

---

## Step-by-Step Usage Guides & Practical Examples

### 1. Defining WebSocket Routes & Connection Lifecycle

`@app.websocket(path)` registers a WebSocket endpoint isolated from HTTP routes even when sharing the same path (HTTP `405` `Allow` headers never advertise `WEBSOCKET`):

```python
from swiftpy import Application
from swiftpy.websockets import WebSocket, WebSocketDisconnect

app = Application()

@app.websocket("/ws/echo/{room}")
async def room_echo(ws: WebSocket, room: str) -> None:
    await ws.accept()
    await ws.send_json({"joined": room})

    try:
        while True:
            payload = await ws.receive_json()
            await ws.send_json({"room": room, "echo": payload})
    except WebSocketDisconnect:
        # Client disconnected normally or hit idle timeout / size limit
        pass
```

Key `WebSocket` (`WebSocketConnection`) APIs:

- **Handler injection:** Name your connection parameter `websocket`, `ws`, `conn`, or `connection` typed as `WebSocket` / `WebSocketConnection` alongside coerced path parameters (`user_id: int`, `room: str`) and container-resolvable dependencies. Invalid path parameter coercion closes with code `1002`; unknown paths close with `1000`.
- **Handshake & messaging:** Call `await ws.accept(subprotocol=..., headers=...)` before reading frames. Send/receive helpers include `receive()`, `receive_text()`, `receive_bytes()`, `receive_json()`, `send()`, `send_text()`, `send_bytes()`, `send_json()`, and `close(code=1000, reason="")`.
- **Error semantics:** If a handler raises before `ws.accept()`, SwiftPY closes with `1008` (`Policy Violation`). Exceptions raised after `accept()` close with `1011` (`Internal Server Error`), while the `finally` block always unregisters the connection from `app.websocket_manager`.

### 2. Rooms, Topic Broadcasts & Multi-Worker Fanout (`WebSocketManager`)

Use `ws_manager()` (or `websocket_manager()` / `app.websocket_manager`) to track rooms/topics and broadcast messages safely with bounded concurrency (`max_concurrency=100`, per-client `timeout=5.0`):

```python
from swiftpy.helpers import ws_manager
from swiftpy.websockets import WebSocketConnection, WebSocketDisconnect

@app.websocket("/ws/topics/{topic}")
async def topic_feed(ws: WebSocketConnection, topic: str) -> None:
    manager = ws_manager()
    await ws.accept()
    await manager.connect(ws, room=topic)

    try:
        while True:
            data = await ws.receive_json()
            action = data.get("action")

            if action == "subscribe" and isinstance(data.get("topic"), str):
                await manager.join_room(data["topic"], ws)
                await ws.send_json({"subscribed": data["topic"]})
            elif action == "unsubscribe" and isinstance(data.get("topic"), str):
                await manager.leave_room(data["topic"], ws)
                await ws.send_json({"unsubscribed": data["topic"]})
            elif action == "publish":
                await manager.broadcast(
                    {"topic": topic, "event": data.get("payload")},
                    room=topic,
                    distribute=True,  # Also fans out to other workers via RedisEventBus
                )
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)
```

Key `WebSocketManager` features:

- **Single-pass serialization & slow-client isolation:** Dict/list payloads are serialized once before fanning out across recipients. Stalled or failing clients hit the per-client `timeout` and are automatically disconnected without blocking fast clients.
- **Selective filtering:** Pass `filter_fn=lambda conn: conn.state.get("tenant_id") == 7` to `broadcast(...)` to target a subset of active connections.
- **Cross-worker distribution (`distribute=True`):** When `RedisEventBus` is registered in `app.container`, `Application` subscribes `WebSocketManager` to `"websocket.broadcast"` on startup and `broadcast(..., distribute=True)` relays messages to other ASGI workers (ignoring the originating worker).

### 3. Heartbeats, Idle Timeouts, Size Limits & Handshake Auth

#### Idle Timeouts & Application Heartbeats

Configure server-enforced frame limits and inactivity timeouts:

```python
app = Application(
    max_ws_message_size=1_048_576,  # Default 1 MiB; oversized frames close with 1009
    ws_idle_timeout=60.0,           # Closes silent connections cleanly with 1000 ("Idle timeout")
)
```

Equivalent `config` dictionary / `config/websockets.py` keys:

```python
CONFIG = {
    "allowed_origins": ["https://app.example.com"],
    "max_message_size": 1_048_576,
    "idle_timeout": 60.0,
}
```

Pair `ws_idle_timeout` with an application-level ping/pong heartbeat so active clients stay alive while dead TCP sockets time out reliably:

```python
@app.websocket("/ws/live")
async def live_stream(ws: WebSocketConnection) -> None:
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "ping":
                await ws.send_json({"type": "pong"})
                continue
            await ws.send_json({"ack": msg})
    except WebSocketDisconnect:
        pass
```

#### Origin Validation (`D-14`) & Middleware Authentication

Every incoming WebSocket handshake passes two checks before entering your handler:

1. **Origin validation (`D-14`):**
   - By default (`allowed_origins=None`), SwiftPY enforces **same-origin**: when an `Origin` header is present (browser clients), its scheme, hostname, and port must match the request `Host` / server scheme.
   - Configure `Application(allowed_origins=["https://frontend.example.com"])` (or `["*"]` to opt into wildcard origins) when hosting browser clients on a separate origin. Disallowed or malformed `Origin` headers close immediately with code `1008` (`"Origin not allowed"`).
   - Non-browser clients (CLI, mobile, server-to-server) that omit `Origin` remain permitted.
2. **Handshake middleware:** Global middleware (`app.middleware([...])`) and route-group middleware execute during the WebSocket handshake using `JWTGuard`, `require_api_key`, or custom `Middleware`. If middleware rejects (`status_code >= 400` or `AuthenticationError` / `HttpException`), SwiftPY closes the socket with `1008` (`"Unauthorized"`); otherwise `request.state` values propagate onto `ws.state`:

```python
from swiftpy import Application, RouteGroup
from swiftpy.auth import JWTGuard
from swiftpy.websockets import WebSocketConnection

protected_ws = RouteGroup("/ws", app.router, middleware=[JWTGuard])

@protected_ws.websocket("/notifications")
async def notifications(ws: WebSocketConnection) -> None:
    await ws.accept()
    user = ws.state.get("user", {})
    await ws.send_json({"connected_sub": user.get("sub")})
```

> `JWTGuard` accepts tokens via `Authorization: Bearer <token>`, `Sec-WebSocket-Protocol: bearer, <token>`, or `?token=<token>` on WebSocket handshakes.

### 4. Registering Models in `AdminSite` & Customizing `ModelAdmin`

Register ORM `Model` classes with `admin` (default `AdminSite` singleton) or a dedicated `AdminSite(title="...")`:

```python
from typing import ClassVar

from swiftpy.admin import AdminSite, ModelAdmin, admin
from swiftpy.database.orm import Field, Model


class Post(Model):
    __table__ = "posts"
    __fillable__ = ["title", "body", "published"]

    id: int | None = None
    title: str = ""
    body: str = ""
    published: bool = Field(default=False)
    internal_notes: str | None = None


@admin.register(Post)
class PostAdmin(ModelAdmin):
    list_display: ClassVar[list[str] | tuple[str, ...] | None] = ["id", "title", "published"]
    search_fields: ClassVar[list[str]] = ["title", "body"]
    ordering: ClassVar[str | None] = "-id"
    per_page: ClassVar[int] = 25
    exclude: ClassVar[list[str]] = ["internal_notes"]
    can_delete: ClassVar[bool] = True
```

Supported registration styles:

- `admin.register(User)` — default `ModelAdmin` configuration
- `admin.register(User, UserAdmin)` — explicit `ModelAdmin` pairing
- `@admin.register` directly on a `Model` subclass
- `@admin.register(Post)` on a `ModelAdmin` subclass

Key `ModelAdmin` features:

- **Auto-introspection & N+1 avoidance:** Automatically inspects `Model` field types (`checkbox`, `number`, `textarea`, `datetime-local`, `text`), renders `<select>` dropdowns for `BelongsTo` foreign keys, and eager-loads `BelongsTo` relations on list queries (`get_queryset()`).
- **Mass-assignment & protected fields (`D-4`):** Because `AdminSite` constructs new records with `model_cls(**data)`, registered models must declare `__fillable__` (recommended) or `__guarded__` — otherwise create submissions raise `MassAssignmentError` (HTTP `422`). `exclude` removes fields from Create/Edit forms and ignores them on POST, while `primary_key`, `created_at`, and `updated_at` are always protected.
- **SQL search escaping:** `search_fields` generates case-insensitive `OR` search filters (`?q=...`) with wildcard `%` / `_` escaping.

### 5. Mounting Admin Routes, Sessions, CSRF & RBAC

Mount your `AdminSite` onto `Application` during app startup:

```python
from swiftpy import Application
from swiftpy.admin import AdminUser, admin, get_admin_authenticator

app = Application()

# Optional: plug in your own user database / hash verification
def verify_admin(username: str, password: str) -> AdminUser | None:
    if username == "alice" and password == "strong-passphrase":
        return AdminUser(
            username="alice",
            is_admin=True,
            permissions=["models:read", "posts:create", "posts:edit"],
        )
    return None

get_admin_authenticator().set_authenticator(verify_admin)
admin.mount(app, prefix="/admin")
```

Mounted endpoints (for `prefix="/admin"`):

- `GET` / `POST /admin/login` — issues HMAC-SHA256-signed `HttpOnly; SameSite=Lax` cookie `swiftpy_admin_session`
- `POST /admin/logout` — requires CSRF token and revokes the active `session_id`
- `GET /admin` and `GET /admin/` — admin overview dashboard
- `GET /admin/ai-usage` — LLM observability dashboard
- `GET /admin/{slug}` — paginated, searchable model list (`?q=...&page=1&per_page=25&format=json`)
- `GET` / `POST /admin/{slug}/create` — create record form / handler
- `GET` / `POST /admin/{slug}/{id}/edit` — edit record form / handler
- `POST /admin/{slug}/{id}/delete` and `DELETE /admin/{slug}/{id}` — delete record handler

RBAC and permission enforcement:

- `AdminUser(username, is_admin=True, permissions=[...])` controls authorization (`"all"` or `"*"` grants full access; otherwise granular scopes apply).
- Default permission checks on `ModelAdmin`:
  - **View:** `required_view_permission`, or any of `("all", "models:read", f"{slug}:read")`
  - **Create:** `can_create=True` plus `required_create_permission`, or any of `("all", "models:write", f"{slug}:create")`
  - **Edit:** `can_edit=True` plus `required_edit_permission`, or any of `("all", "models:write", f"{slug}:edit")`
  - **Delete:** `can_delete=True` plus `required_delete_permission`, or any of `("all", "models:write", "models:delete", f"{slug}:delete")`
- **CSRF protection:** Every state-changing `POST` / `DELETE` admin action (create, edit, delete, logout) verifies `_csrf_token` (form/JSON body) or `X-CSRF-Token` header bound to the user's `session_id` and returns HTTP `403` on mismatch.

---

## Important Defaults & Production Safeguards

- **Origin checks fail closed against cross-site WebSocket hijacking (`D-14`):** Leave `allowed_origins=None` for same-origin browser policies or list exact origins (never `"*"` when authenticating browser cookies/sessions).
- **Never trust unauthenticated WebSocket handshakes or unbounded frames:** Attach authentication middleware (`JWTGuard` / API keys) to sensitive WebSocket routes/groups and set `ws_idle_timeout` alongside `max_ws_message_size` (`1 MiB` default) to prevent resource exhaustion.
- **Default admin credentials (`admin`/`admin123`) and ephemeral secrets are blocked outside local debug mode:** `AdminAuthenticator` and `AdminSessionStore` raise `ConfigurationError` in production or non-debug apps unless you configure strong `admin.username`/`admin.password` (or `set_authenticator(...)`) and `admin.secret_key` / `app.key`.
- **Use `exclude` on `ModelAdmin` to hide sensitive columns:** Always exclude password hashes, secrets, or internal fields from `ModelAdmin` forms (`exclude = ["password"]`), and ensure HTTPS / `admin.cookie_secure=True` in production so `swiftpy_admin_session` carries the `Secure` flag.

---

## Next Step

Continue to [AI Client, Embeddings, Vector Stores, RAG, Agents & Prompts](2026-11-15-ai-subsystem.md) to work with SwiftPY's native `swiftpy.ai` subsystem.
