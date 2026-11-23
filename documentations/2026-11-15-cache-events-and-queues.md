# Cache, Events & Background Queues

## Introduction

SwiftPY includes three Redis-aware asynchronous infrastructure subsystems that share centralized configuration through [redis_config.py](file:///home/brainwave/pyprojects/swiftpy/swiftpy/support/redis_config.py):

- **Cache (`swiftpy.cache`):** Stampede-protected key/value caching via `Cache` (`CacheManager`) with pluggable `RedisDriver` and in-memory LRU `MemoryDriver` backends.
- **Events (`swiftpy.events`):** In-process `EventDispatcher` (with sync/async listeners and wildcard patterns) and cross-process `RedisEventBus` (HMAC-signed Redis Pub/Sub).
- **Background Queues (`swiftpy.queue`):** HMAC-signed `Job` envelopes, transactional `after_commit=True` dispatching, `QueueDispatcher`, lease-protected `QueueWorker`, and dead-letter queue CLI tooling (`queue:work`, `queue:failed`, `queue:retry`, `queue:flush`).

```python
from swiftpy.cache import Cache, MemoryDriver, RedisDriver
from swiftpy.events import EventDispatcher, RedisEventBus
from swiftpy.helpers import cache, dispatch, dispatcher, event, queue
from swiftpy.queue import Job, QueueDispatcher, QueueWorker, push
from swiftpy.support.redis_config import build_redis_url, resolve_redis_params
```

> **Tip on importing `cache()` and `queue()`:** Import helper functions from `swiftpy.helpers` rather than `from swiftpy import cache, queue`. If another module has already imported the `swiftpy.cache` or `swiftpy.queue` subpackage, Python binds that module object to `swiftpy.cache` / `swiftpy.queue`.

---

## Creating & Organizing Files

In a scaffolded SwiftPY application:

- **Config files:** `config/cache.py`, `config/events.py`, and `config/queue.py` load settings from `.env` via `env()`.
- **Service providers:** `app/providers/cache_service_provider.py`, `event_service_provider.py`, and `queue_service_provider.py` register `Cache`, `EventDispatcher`, `RedisEventBus`, and `QueueDispatcher` singletons on startup.
- **Application code directories:**
  - `app/events/` — domain event dataclasses (starter: `user_registered.py`)
  - `app/listeners/` — event handlers (starter: `send_welcome_notification.py`)
  - `app/jobs/` — queued `Job` subclasses (starter: `send_notification_job.py`)

### Generating Events, Listeners & Jobs via CLI

```bash
swiftpy make:event InvoicePaid
swiftpy make:listener SendInvoiceReceipt
swiftpy make:job ProcessVideoUpload
```

Each generator accepts `--path <dir>` to override its default folder and `--force` to overwrite an existing file.

### Centralized Redis Configuration (`resolve_redis_params`)

All Redis-backed subsystems read connection settings through `resolve_redis_params()` / `build_redis_url()` (never raw `os.getenv()` in runtime code), percent-encoding credentials and isolating Redis logical databases per subsystem:

| Subsystem | Default Config File | Database Env Override | Default DB | Default Prefix |
| :--- | :--- | :--- | :---: | :--- |
| Cache | `config/cache.py` | `REDIS_CACHE_DB` | `0` | `swiftpy_cache:` |
| Queue | `config/queue.py` | `REDIS_QUEUE_DB` | `1` | `swiftpy_queue:` |
| Events | `config/events.py` | `REDIS_EVENTS_DB` | `2` | `swiftpy_events:` |

Important config keys:

```env
APP_KEY=base64:<32-byte-base64-key>   # Required (>=32 bytes) for Queue & RedisEventBus signing
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_PASSWORD=secret

CACHE_DRIVER=redis
CACHE_DEFAULT_TTL=3600
CACHE_PREFIX=swiftpy_cache:

EVENTS_DRIVER=redis
EVENTS_PREFIX=swiftpy_events:

QUEUE_DRIVER=redis
QUEUE_PREFIX=swiftpy_queue:
QUEUE_NAME=default
```

---

## Step-by-Step Usage Guides & Practical Examples

### 1. Caching with `Cache`, `MemoryDriver` & `RedisDriver`

Use the `cache()` helper inside your app (backed by `CacheServiceProvider`) or instantiate `Cache` directly. Both drivers share the same async `CacheDriver` API:

```python
from swiftpy.helpers import cache

# Basic CRUD operations (default TTL from config/cache.py if ttl is omitted)
await cache().set("user:42:profile", {"name": "Ada", "tier": "pro"}, ttl=300)
profile = await cache().get("user:42:profile")

# Set only if absent, or fetch and delete in one operation
added = await cache().add("signup_bonus:42", True, ttl=86400)
one_time_code = await cache().pull("reset_code:42")

# Atomic integer counters
hits = await cache().increment("rate:user:42", value=1, ttl=60)
remaining = await cache().decrement("credits:user:42", value=1)

# Existence checks and invalidation
if await cache().has("user:42:profile"):
    await cache().forget("user:42:profile")
```

#### Stampede Protection with `remember()`

`await cache().remember(key, ttl, callback, lock_timeout=10.0)` prevents thundering herds when an expensive cache entry expires under heavy concurrent load:

```python
async def load_dashboard_stats() -> dict[str, int]:
    async def compute() -> dict[str, int]:
        return {"active_users": 1250, "orders_today": 84}

    return await cache().remember(
        "dashboard:stats",
        ttl=120,
        callback=compute,
        lock_timeout=5.0,
    )
```

- `RedisDriver.remember()` combines per-key in-process `asyncio.Lock` coalescing with a distributed Redis `SET NX PX` lock (released atomically via Lua script).
- `MemoryDriver.remember()` uses per-key `asyncio.Lock` coalescing cleanly for single-process execution and tests.
- If `lock_timeout` expires, SwiftPY logs a warning and invokes `callback()` directly without writing back into the cache.

#### Switching Stores, Serialization & `MemoryDriver` Defaults

```python
from swiftpy.cache import Cache, MemoryDriver
from swiftpy.helpers import cache

# Use a specific named store configured in config/cache.py
mem = cache("memory")
await mem.set("ephemeral", [1, 2, 3], ttl=30)

# Or construct MemoryDriver standalone (LRU max_items=1000 by default)
local_cache = MemoryDriver(prefix="test:", default_ttl=60.0, max_items=500)
await local_cache.set("k", {"created": True})
```

- **`default_ttl` vs `ttl=0`:** Omitting `ttl` (`None`) uses the store's `default_ttl`. Explicitly passing `ttl=0` (or `<= 0`) deletes/skips storage instead of expiring never.
- **Serialization:** `RedisDriver` uses `JsonCacheSerializer`, preserving `datetime`, `date`, `time`, `UUID`, `Decimal`, `Enum`, and `Model` / `.to_dict()` objects (respecting `__hidden__`), while rejecting raw `bytes`. `MemoryDriver` deep-copies values on `set` and `get` so in-place caller mutations cannot corrupt cached data.
- **`Cache` lifecycle:** Always call `await cache_instance.close()` on custom standalone instances (application lifespan hooks handle registered singletons).

### 2. In-Process Domain Events (`EventDispatcher`)

Use `EventDispatcher` (`dispatcher()` / `await event(...)`) when multiple decoupled components in the same process should react to domain events:

```python
from dataclasses import dataclass

from swiftpy.helpers import dispatcher, event


@dataclass(slots=True, frozen=True)
class OrderPlaced:
    order_id: int
    total_cents: int


disp = dispatcher()


async def record_analytics(payload: OrderPlaced) -> None:
    print(f"Analytics logged for order {payload.order_id}")


def audit_log(event_name: str, payload: OrderPlaced) -> None:
    print(f"[{event_name}] total={payload.total_cents}")


# Subscribe by class or wildcard string pattern
disp.listen(OrderPlaced, record_analytics)
disp.listen("app.events.**", audit_log)  # Matches classes in app.events submodules
disp.listen("order.*", audit_log)       # Single '*' matches one dot segment (e.g. "order.placed")

# Dispatch class instances or string event names
await event(OrderPlaced(order_id=501, total_cents=4900))
```

Key `EventDispatcher` behavior:

- **Class event normalization:** Class listeners and dispatched instances normalize to `f"{cls.__module__}.{cls.__qualname__}"` (or `cls.__qualname__` when `__module__ == "__main__"`). Use `"app.events.**"` for module-nested classes and `"domain.*"` for dotted string event names.
- **Signature-aware invocation:** Listeners can take `()`, `(payload)`, `(event_name, payload)`, or keyword args `event=` / `payload=`.
- **Exact + wildcard interleaving:** Handlers run in deterministic global registration order regardless of whether registered via exact match (`"user.created"`), single-segment wildcard (`"user.*"`), or multi-segment wildcard (`"user.**"` / `"*"`).
- **Fail-fast policy:** If any listener raises an exception during `dispatch()`, remaining listeners are not invoked and the exception propagates directly to the caller.
- **Unsubscribing & cleanup:** Use `disp.unsubscribe(event, handler)` or `disp.forget(event=None)` in tests or dynamic plugins.

### 3. Cross-Process Events (`RedisEventBus`)

Use `RedisEventBus` when an event published in an API worker must notify separate background processes or ASGI replicas via Redis Pub/Sub:

```python
from swiftpy.events import RedisEventBus

bus = RedisEventBus.get_instance()


async def on_cache_bust(payload: dict[str, str]) -> None:
    print("Invalidating local view for:", payload["slug"])


bus.subscribe("content.updated", on_cache_bust)

async with bus:
    await bus.publish("content.updated", {"slug": "pricing"}, local=False)
```

Important `RedisEventBus` contracts:

- **At-most-once delivery:** Redis Pub/Sub does not persist messages for offline subscribers—use background `Job` queues when durable delivery is required.
- **HMAC-SHA256 signing (`APP_KEY`):** `publish()` signs every JSON envelope using `APP_KEY` (`>= 32` UTF-8 or decoded `base64:` bytes) and verifies incoming signatures (including `APP_PREVIOUS_KEYS` during key rotation) before dispatching. Without a valid `APP_KEY`, `publish()` and `start_listening()` fail closed with `ConfigurationError` unless `allow_unsigned=True` is explicitly passed for local tests.
- **`local=True` vs remote payloads:** With `local=True`, listeners in the publisher process receive the original Python object synchronously alongside remote pub/sub; remote subscribers always receive JSON-decoded primitives/dicts.
- **`ignore_self=True`:** Enabled by default so a worker does not re-handle its own published pub/sub message when subscribed to the same pattern.

### 4. Background Queues (`Job`, `QueueDispatcher` & `QueueWorker`)

#### Defining a `Job` Subclass

Important: `Job.__init__()` initializes `job_id`, `args`, `kwargs`, `attempts`, and serialization metadata. Any `Job` subclass with a custom `__init__` **must** call `super().__init__(..., **kwargs)` so worker deserialization can reconstruct your job cleanly:

```python
from __future__ import annotations

from typing import Any

from swiftpy.queue import Job


class SendReceiptJob(Job):
    queue: str = "emails"
    tries: int = 3
    backoff: list[int] = [5, 30, 120]  # Per-attempt retry delays in seconds
    timeout: float | None = 30.0

    def __init__(self, user_id: int, receipt_id: str, **kwargs: Any) -> None:
        super().__init__(user_id=user_id, receipt_id=receipt_id, **kwargs)
        self.user_id = user_id
        self.receipt_id = receipt_id

    async def handle(self) -> None:
        # Keep handle() idempotent: queues guarantee at-least-once delivery
        await send_receipt_email(self.user_id, self.receipt_id)

    async def failed(self, exception: BaseException) -> None:
        # Called when all retry attempts are exhausted before moving to DLQ
        log_permanent_failure(self.receipt_id, exception)
```

> **Note:** Job constructor arguments and `data` must be JSON-serializable (`str`, `int`, `float`, `bool`, `list`, `dict`, `None`).

#### Dispatching Jobs & Transactional `after_commit=True`

```python
from swiftpy.helpers import dispatch
from swiftpy.database.transaction import db

# Immediate or delayed push
await SendReceiptJob(user_id=42, receipt_id="rcpt_100").dispatch()
await dispatch(SendReceiptJob(user_id=42, receipt_id="rcpt_101"), queue_name="emails", delay=60)

# Transactional safety: only pushes to Redis if the database transaction commits
async with db.transaction():
    order = await Order.create(user_id=42, status="paid")
    await SendReceiptJob(user_id=42, receipt_id=order.receipt_id).dispatch(after_commit=True)
```

When `after_commit=True` is passed inside an active `db.transaction()`, SwiftPY registers an `on_commit` hook; if the transaction rolls back, the job is discarded cleanly. Outside an active transaction, `after_commit=True` dispatches immediately.

#### HMAC Envelope Signing & Trusted Namespaces

- **HMAC-SHA256 signing:** `Job.serialize()` signs the job envelope using `APP_KEY` (`>= 32` bytes), excluding mutable `signature` and `attempts` fields, and `Job.deserialize()` verifies the signature **before** importing the target Python module (also checking `APP_PREVIOUS_KEYS` during key rotation).
- **Namespace allowlist:** By default, `Job.get_trusted_namespaces()` permits only `["app", "swiftpy"]` so tampered or untrusted class paths cannot execute arbitrary imports. If you dispatch jobs defined in a custom package, explicitly whitelist them at startup:

```python
Job.set_trusted_namespaces(["app", "swiftpy", "billing_worker"])
# Or allowlist a single Job class:
Job.allow_job_class(MyCustomPackageJob)
```

#### Running Workers & Managing Failed Jobs (`DLQ`)

Run `QueueWorker` via CLI (or programmatically with `QueueWorker(queues="default", ...)`):

```bash
# Start a worker booting your app (recommended so Config, DatabasePool, & providers initialize)
swiftpy queue:work --app main:app --queue emails,default --concurrency 4 --timeout 60

# Drain pending jobs in CI/one-off tasks and exit automatically
swiftpy queue:work --app main:app --queue default --stop-when-empty

# Inspect Dead Letter Queue records (default --limit 50, max 1000)
swiftpy queue:failed --queue emails --limit 50 --app main:app

# Retry one failed job by ID or all failed jobs (re-signs payloads with active APP_KEY)
swiftpy queue:retry all --queue emails --app main:app

# Flush all failed records from a queue's DLQ
swiftpy queue:flush --queue emails --app main:app
```

When `QueueWorker` executes tasks submitted by the [Task Scheduler](2026-11-22-task-scheduling.md), it automatically records `running`, `completed`, and `failed` occurrence transitions in Redis, retains `.without_overlapping()` locks across intermediate backoff retries, and releases locks via compare-and-delete Lua upon `ack()` or final DLQ exhaustion.

---

## Important Defaults & Production Safeguards

- **`APP_KEY` (`>= 32` bytes) is mandatory in production:** Both `RedisEventBus` and `Job` serialization/workers fail closed with `ConfigurationError` if `APP_KEY` is missing or shorter than 32 decoded bytes (`allow_unsigned=True` is strictly for isolated local tests). Rotate keys smoothly using `APP_PREVIOUS_KEYS`.
- **Keep queue jobs idempotent & call `super().__init__(..., **kwargs)`:** `QueueWorker` uses Redis lease reaping to recover crashed worker jobs automatically (at-least-once delivery), so repeated execution of `handle()` must be safe; custom `Job.__init__` signatures must call `super().__init__` and accept `**kwargs`.
- **Use `--app main:app` when running queue CLI commands:** Passing `--app main:app` boots application `Config`, service providers, and `DatabasePool` lifecycle hooks cleanly before processing or inspecting queues.
- **Use `after_commit=True` inside database transactions:** Avoid queueing jobs before `db.transaction()` commits so workers never wake up to query rows that are not visible yet—or were rolled back.
- **Isolate Redis databases or prefixes per environment:** Keep `REDIS_CACHE_DB`, `REDIS_QUEUE_DB`, and `REDIS_EVENTS_DB` separate (or configure distinct `CACHE_PREFIX` / `QUEUE_PREFIX` / `EVENTS_PREFIX`) so a cache `flush()` never deletes queue keys.

---

## Next Step

Continue to [Filesystem Storage, Uploads & Mail](2026-11-15-filesystem-storage-and-mail.md) to work with `Storage` (`LocalStorageDriver` / `S3StorageDriver`), validated `UploadedFile` uploads, and `Mail` / `Mailable` transports.
