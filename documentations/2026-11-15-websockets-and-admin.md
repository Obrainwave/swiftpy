# WebSockets & Real-Time Channels

## Introduction

SwiftPY provides built-in real-time messaging integrated directly into `Application`:

- **WebSockets (`swiftpy.websockets`):** Define WebSocket endpoints with `@app.websocket(...)` (or `router.websocket(...)`), manage rooms and topic-style broadcasts with `WebSocketManager` (`ws_manager()`), enforce Decision `D-14` origin checks, authenticate handshakes with HTTP middleware (`JWTGuard` / `require_api_key`), bound frame sizes and idle timeouts, and fan out broadcasts across workers via `RedisEventBus`.

```python
from swiftpy import Application
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

---

## Important Defaults & Production Safeguards

- **Origin checks fail closed against cross-site WebSocket hijacking (`D-14`):** Leave `allowed_origins=None` for same-origin browser policies or list exact origins (never `"*"` when authenticating browser cookies/sessions).
- **Proxy-aware WebSocket client IP (`ws.ip()`):** Call `ws.ip()` (or `Request.ip()` inside WebSocket handshake middleware) to obtain the canonical proxy-verified client IP governed by `http.trusted_proxies` / `TRUSTED_PROXIES`, ignoring forged forwarding headers from untrusted peers.
- **Never trust unauthenticated WebSocket handshakes or unbounded frames:** Attach authentication middleware (`JWTGuard` / API keys) to sensitive WebSocket routes/groups and set `ws_idle_timeout` alongside `max_ws_message_size` (`1 MiB` default) to prevent resource exhaustion.

---

## Next Step

Continue to [AI Client, Embeddings, Vector Stores, RAG, Agents & Prompts](2026-11-15-ai-subsystem.md) to work with SwiftPY's native `swiftpy.ai` subsystem.
