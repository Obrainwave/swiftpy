from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any, ClassVar, Self, cast
from uuid import UUID

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from swiftpy.core.config import ConfigurationError
from swiftpy.core.container import get_container
from swiftpy.events.dispatcher import EventDispatcher, EventHandler
from swiftpy.support.keys import (
    decode_signing_key,
    resolve_configured_app_key,
    resolve_configured_previous_keys,
    validate_signing_key,
)
from swiftpy.support.redis_config import resolve_redis_params

logger = logging.getLogger("swiftpy.events")


class RedisEventBus:
    """
    Cross-process event delivery bus backed by Redis Pub/Sub.
    Allows workers in different processes or containers to publish and listen for events.

    Delivery Semantics:
      Pub/sub messages operate with at-most-once delivery semantics.
      If a subscriber is offline or disconnected when an event is published, the event is not retained.

    Payload Contract:
      - Local dispatch (local=True): Listeners in the current process receive the exact Python object.
      - Remote dispatch: Payloads are serialized to JSON (converting datetimes, UUIDs, and models to dicts/strings).
        Subscribers in remote processes receive the deserialized dicts/primitives.
      - When APP_KEY is configured, envelopes are signed with HMAC-SHA256 to ensure authenticity.
    """

    _instance: ClassVar[RedisEventBus | None] = None
    _app_key: ClassVar[str | None] = None
    _previous_keys: ClassVar[list[str]] = []
    _allow_unsigned: ClassVar[bool] = False

    @classmethod
    def set_allow_unsigned(cls, enabled: bool) -> None:
        """Explicit opt-in to allow unsigned event bus messages in local tests."""
        cls._allow_unsigned = bool(enabled)

    @classmethod
    def get_allow_unsigned(cls) -> bool:
        """Return whether unsigned event bus messages are explicitly opted in."""
        return cls._allow_unsigned

    @classmethod
    def set_app_key(cls, key: str | None) -> None:
        """Set the active application key for signing bus messages."""
        cls._app_key = key

    @classmethod
    def get_app_key(cls) -> str | None:
        """Return the active application key for signing bus messages."""
        if cls._app_key is not None:
            return cls._app_key
        return resolve_configured_app_key()

    @classmethod
    def set_previous_keys(cls, keys: list[str]) -> None:
        """Set previous keys for rotation verification."""
        cls._previous_keys = list(keys)

    @classmethod
    def get_previous_keys(cls) -> list[str]:
        """Return previous keys for key rotation verification."""
        if cls._previous_keys:
            return cls._previous_keys
        return resolve_configured_previous_keys()

    @classmethod
    def _compute_signature(cls, envelope: dict[str, Any], key: str) -> str:
        """Compute an HMAC-SHA256 signature over the canonical representation of the envelope."""
        canonical_fields = {k: v for k, v in envelope.items() if k != "signature"}
        canonical_json = json.dumps(
            canonical_fields,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        secret_bytes = decode_signing_key(key)
        return hmac.new(
            secret_bytes,
            canonical_json.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def __init__(
        self,
        dispatcher: EventDispatcher | None = None,
        redis_client: aioredis.Redis | None = None,
        connection_pool: aioredis.ConnectionPool | None = None,
        redis_url: str | None = None,
        host: str | None = None,
        port: int | None = None,
        password: str | None = None,
        database: int = 0,
        channel_prefix: str = "swiftpy_events:",
        ignore_self: bool = True,
        max_connections: int | None = None,
        timeout: float = 30.0,
        max_concurrency: int = 50,
        allow_unsigned: bool | None = None,
    ) -> None:
        self.bus_id: str = uuid.uuid4().hex
        self.dispatcher: EventDispatcher = (
            dispatcher if dispatcher is not None else EventDispatcher()
        )
        self._client: aioredis.Redis | None = redis_client
        self._pool: aioredis.ConnectionPool | None = connection_pool
        self._redis_url = redis_url
        self.host = host
        self.port = port
        self.password = password
        self.database = database
        self.channel_prefix = channel_prefix
        self.ignore_self = ignore_self
        self.max_connections = max_connections or 20
        self.timeout = timeout
        self.max_concurrency = max_concurrency
        self._allow_unsigned_override = allow_unsigned
        self._owns_client = redis_client is None
        self._owns_pool = connection_pool is None and redis_client is None
        self._client_lock = asyncio.Lock()

        self._running: bool = False
        self._listener_task: asyncio.Task[None] | None = None
        self._pubsub: aioredis.client.PubSub | None = None
        self._ready_event: asyncio.Event = asyncio.Event()
        self._dispatch_semaphore = asyncio.Semaphore(self.max_concurrency)
        self._active_dispatch_tasks: set[asyncio.Task[Any]] = set()

    @classmethod
    def get_instance(cls) -> RedisEventBus:
        """Retrieve the active application's RedisEventBus from the container, or fallback instance."""
        try:
            container = get_container()
            if container.has(cls):
                return container.resolve(cls)
            disp = (
                container.resolve(EventDispatcher)
                if container.has(EventDispatcher)
                else None
            )
            inst = cls(dispatcher=disp)
            container.instance(cls, inst)
            return inst
        except RuntimeError:
            pass

        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def set_instance(cls, bus: RedisEventBus | None) -> None:
        """Override or reset the RedisEventBus instance in the active container and fallback."""
        cls._instance = bus
        try:
            container = get_container()
            if container.has(cls):
                if bus is not None:
                    container.instance(cls, bus)
                else:
                    container.singleton(
                        cls,
                        lambda c: cls(
                            dispatcher=c.resolve(EventDispatcher)
                            if c.has(EventDispatcher)
                            else None
                        ),
                    )
        except RuntimeError:
            pass

    async def get_client(self) -> aioredis.Redis:
        """Lazily initialize and return the async Redis client."""
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is not None:
                return self._client
            if self._redis_url:
                self._client = aioredis.from_url(
                    self._redis_url,
                    decode_responses=True,
                )
            else:
                if self._pool is None:
                    params = resolve_redis_params(
                        section="events",
                        host=self.host,
                        port=self.port,
                        password=self.password,
                        database=self.database if self.database != 0 else None,
                    )

                    self._pool = aioredis.BlockingConnectionPool(
                        host=params["host"],
                        port=params["port"],
                        db=params["database"],
                        password=params["password"] or None,
                        decode_responses=True,
                        max_connections=self.max_connections,
                        timeout=self.timeout,
                    )
                self._client = aioredis.Redis(
                    connection_pool=self._pool,
                    decode_responses=True,
                )
            self._owns_client = True
            return self._client

    def subscribe(self, event: str, handler: EventHandler) -> None:
        """Subscribe a listener to events received by this worker."""
        self.dispatcher.listen(event, handler)

    def listen(self, event: str, handler: EventHandler) -> None:
        """Alias for subscribe."""
        self.subscribe(event, handler)

    def unsubscribe(self, event: str, handler: EventHandler) -> bool:
        """Unsubscribe a listener from events."""
        return self.dispatcher.unsubscribe(event, handler)

    def _serialize_value(self, val: Any, seen: set[int] | None = None) -> Any:
        """
        Convert arbitrary objects to JSON-serializable structures.
        Strictly raises TypeError on unsupported types (e.g. set) or circular references.
        """
        if seen is None:
            seen = set()

        if val is None or isinstance(val, (int, float, str, bool)):
            return val
        if isinstance(val, (datetime, date)):
            return val.isoformat()
        if isinstance(val, UUID):
            return str(val)

        obj_id = id(val)
        if obj_id in seen:
            raise TypeError("Circular reference detected in event payload")
        seen.add(obj_id)

        try:
            hidden = set(getattr(val, "__hidden__", ()) or ())
            if hasattr(val, "to_dict") and callable(val.to_dict):
                raw_dict = val.to_dict()
                if hidden and isinstance(raw_dict, dict):
                    raw_dict = {k: v for k, v in raw_dict.items() if k not in hidden}
                return self._serialize_value(raw_dict, seen)
            if isinstance(val, (list, tuple)):
                return [self._serialize_value(item, seen) for item in val]
            if isinstance(val, dict):
                return {str(k): self._serialize_value(v, seen) for k, v in val.items()}
            if hasattr(val, "__dict__"):
                return {
                    k: self._serialize_value(v, seen)
                    for k, v in val.__dict__.items()
                    if not k.startswith("_") and k not in hidden
                }
        finally:
            seen.remove(obj_id)

        raise TypeError(
            f"Unsupported payload type for serialization: {type(val).__name__}"
        )

    def _resolve_validated_key(self) -> str | None:
        """Resolve and validate active signing key, failing closed unless explicitly opted into unsigned mode."""
        key = self.get_app_key()
        unsigned_allowed = (
            self._allow_unsigned_override
            if self._allow_unsigned_override is not None
            else self.get_allow_unsigned()
        )
        if key is None or not str(key).strip():
            if not unsigned_allowed:
                raise ConfigurationError(
                    "APP_KEY (>=32 bytes) is required for RedisEventBus HMAC signing. "
                    "Configure APP_KEY or pass allow_unsigned=True for local tests."
                )
            return None
        validate_signing_key(str(key), purpose="RedisEventBus APP_KEY")
        return str(key)

    async def publish(
        self, event: str, payload: Any = None, *, local: bool = False
    ) -> int:
        """
        Publish an event across processes via Redis Pub/Sub.
        Optionally dispatches locally in the current process if local=True.
        Returns the number of subscribers that received the message.
        """
        key = self._resolve_validated_key()
        serialized_payload = self._serialize_value(payload)
        envelope: dict[str, Any] = {
            "event": event,
            "payload": serialized_payload,
            "sender_id": self.bus_id,
            "timestamp": time.time(),
        }

        if key:
            envelope["signature"] = self._compute_signature(envelope, key)

        channel = f"{self.channel_prefix}{event}"
        raw_message = json.dumps(envelope)

        client = await self.get_client()
        subscribers_count = await client.publish(channel, raw_message)

        if local:
            await self.dispatcher.dispatch(event, payload)

        return subscribers_count

    async def start_listening(self) -> asyncio.Task[None]:
        """
        Start the background worker task that consumes incoming pub/sub messages
        and dispatches them to registered listeners.
        """
        if self._listener_task is not None and not self._listener_task.done():
            return self._listener_task

        self._resolve_validated_key()

        self._running = True
        self._ready_event.clear()
        client = await self.get_client()
        self._pubsub = client.pubsub()

        pattern = f"{self.channel_prefix}*"
        await self._pubsub.psubscribe(pattern)
        self._ready_event.set()

        self._listener_task = asyncio.create_task(
            self._listener_loop(self._pubsub, pattern)
        )

        def _on_listener_done(t: asyncio.Task[None]) -> None:
            if not t.cancelled() and t.exception() is not None:
                logger.exception(
                    "RedisEventBus listener task crashed", exc_info=t.exception()
                )

        self._listener_task.add_done_callback(_on_listener_done)
        return self._listener_task

    async def wait_until_ready(self, timeout: float = 5.0) -> None:
        """Wait until the pub/sub connection and pattern subscription are established."""
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    async def _close_pubsub(self, pubsub: aioredis.client.PubSub) -> None:
        """Close pubsub connection cleanly using aclose."""
        aclose_fn = cast(Callable[[], Awaitable[None]], pubsub.aclose)
        await aclose_fn()

    async def _listener_loop(
        self, pubsub: aioredis.client.PubSub, pattern: str
    ) -> None:
        """Background loop reading Redis pub/sub messages with backoff and reconnection."""
        backoff = 0.1
        last_error_log = 0.0
        try:
            while self._running:
                try:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=0.5
                    )
                    backoff = 0.1
                except (RedisError, OSError) as exc:
                    if not self._running:
                        break
                    now = time.monotonic()
                    if now - last_error_log > 10.0:
                        logger.warning(
                            "Redis bus connection error: %s (backing off)", exc
                        )
                        last_error_log = now
                    jitter = 0.8 + 0.4 * random.random()
                    await asyncio.sleep(backoff * jitter)
                    backoff = min(30.0, backoff * 2.0)
                    continue

                if message is None:
                    await asyncio.sleep(0.01)
                    continue

                msg_type = message.get("type")
                if msg_type in ("pmessage", "message"):
                    data = message.get("data")
                    if isinstance(data, (str, bytes)):
                        await self._handle_raw_message(data)
        finally:
            try:
                await pubsub.punsubscribe(pattern)
                await self._close_pubsub(pubsub)
            except (RedisError, OSError) as exc:
                logger.debug("Error closing pubsub subscriber: %s", exc)

    async def _handle_raw_message(self, data: str | bytes) -> None:
        """Deserialize message envelope, verify signature, and route event to dispatcher concurrently."""
        try:
            if isinstance(data, bytes):
                text = data.decode("utf-8")
            else:
                text = data
            envelope = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Failed to decode Redis bus message: %s", exc)
            return

        if not isinstance(envelope, dict):
            return

        # HMAC signature verification (fail closed unless allow_unsigned is explicitly opted in)
        try:
            key = self._resolve_validated_key()
        except ConfigurationError as exc:
            logger.error(
                "Dropping Redis bus message for event '%s' due to invalid APP_KEY config: %s",
                envelope.get("event"),
                exc,
            )
            return

        if key:
            sig = envelope.get("signature")
            if not sig or not isinstance(sig, str):
                logger.warning(
                    "Dropping unsigned Redis bus message for event '%s'",
                    envelope.get("event"),
                )
                return
            expected_sig = self._compute_signature(envelope, key)
            if not hmac.compare_digest(sig, expected_sig):
                matched = False
                for prev_key in self.get_previous_keys():
                    if hmac.compare_digest(
                        sig, self._compute_signature(envelope, prev_key)
                    ):
                        matched = True
                        break
                if not matched:
                    logger.warning(
                        "Dropping tampered Redis bus message for event '%s'",
                        envelope.get("event"),
                    )
                    return

        sender_id = envelope.get("sender_id")
        if self.ignore_self and sender_id == self.bus_id:
            return

        event = envelope.get("event")
        payload = envelope.get("payload")

        if isinstance(event, str):
            await self._dispatch_semaphore.acquire()

            async def _dispatch_task(evt: str, pay: Any) -> None:
                try:
                    await self.dispatcher.dispatch(evt, pay)
                except Exception:
                    logger.exception(
                        "Error executing listener for cross-process event '%s'",
                        evt,
                    )
                finally:
                    self._dispatch_semaphore.release()

            try:
                task = asyncio.create_task(_dispatch_task(event, payload))
            except BaseException:
                self._dispatch_semaphore.release()
                raise

            self._active_dispatch_tasks.add(task)

            def _on_task_done(t: asyncio.Task[Any]) -> None:
                self._active_dispatch_tasks.discard(t)
                if not t.cancelled() and t.exception() is not None:
                    logger.exception(
                        "Event dispatch task failed", exc_info=t.exception()
                    )

            task.add_done_callback(_on_task_done)

    async def stop_listening(self) -> None:
        """Stop background message consumer and release subscriptions."""
        self._running = False
        self._ready_event.clear()

        if self._listener_task is not None:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            except (RedisError, OSError, RuntimeError) as exc:
                logger.debug("Error during listener task shutdown: %s", exc)
            self._listener_task = None

        if self._pubsub is not None:
            try:
                await self._close_pubsub(self._pubsub)
            except (RedisError, OSError) as exc:
                logger.debug("Error closing pubsub: %s", exc)
            self._pubsub = None

        if self._active_dispatch_tasks:
            await asyncio.gather(*self._active_dispatch_tasks, return_exceptions=True)
            self._active_dispatch_tasks.clear()

    async def close(self) -> None:
        """Stop listening and close Redis connections."""
        await self.stop_listening()

        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

        if self._owns_pool and self._pool is not None:
            await self._pool.disconnect()
            self._pool = None

    async def __aenter__(self) -> Self:
        await self.start_listening()
        await self.wait_until_ready()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
