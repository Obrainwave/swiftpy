from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any, ClassVar, Self, cast
from uuid import UUID

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from swiftpy.events.dispatcher import EventDispatcher, EventHandler

logger = logging.getLogger("swiftpy.events")


class RedisEventBus:
    """
    Cross-process event delivery bus backed by Redis Pub/Sub.
    Allows workers in different processes or containers to publish and listen for events.
    """

    _instance: ClassVar[RedisEventBus | None] = None

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
        self._owns_client = redis_client is None

        self._running: bool = False
        self._listener_task: asyncio.Task[None] | None = None
        self._pubsub: aioredis.client.PubSub | None = None
        self._ready_event: asyncio.Event = asyncio.Event()

    @classmethod
    def get_instance(cls) -> RedisEventBus:
        """Retrieve the global or fallback RedisEventBus instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def set_instance(cls, bus: RedisEventBus | None) -> None:
        """Override or reset the global RedisEventBus instance."""
        cls._instance = bus

    async def get_client(self) -> aioredis.Redis:
        """Lazily initialize and return the async Redis client."""
        if self._client is None:
            if self._redis_url:
                self._client = aioredis.from_url(
                    self._redis_url,
                    decode_responses=True,
                )
            else:
                if self._pool is None:
                    host = self.host or os.getenv("REDIS_HOST", "127.0.0.1")
                    port = (
                        self.port
                        if self.port is not None
                        else int(os.getenv("REDIS_PORT", "6379"))
                    )
                    password = (
                        self.password
                        if self.password is not None
                        else os.getenv("REDIS_PASSWORD", None)
                    )

                    self._pool = aioredis.BlockingConnectionPool(
                        host=host,
                        port=port,
                        db=self.database,
                        password=password or None,
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
            if hasattr(val, "to_dict") and callable(val.to_dict):
                return self._serialize_value(val.to_dict(), seen)
            if isinstance(val, (list, tuple)):
                return [self._serialize_value(item, seen) for item in val]
            if isinstance(val, dict):
                return {str(k): self._serialize_value(v, seen) for k, v in val.items()}
            if hasattr(val, "__dict__"):
                return {
                    k: self._serialize_value(v, seen)
                    for k, v in val.__dict__.items()
                    if not k.startswith("_")
                }
        finally:
            seen.remove(obj_id)

        raise TypeError(
            f"Unsupported payload type for serialization: {type(val).__name__}"
        )

    async def publish(
        self, event: str, payload: Any = None, *, local: bool = False
    ) -> int:
        """
        Publish an event across processes via Redis Pub/Sub.
        Optionally dispatches locally in the current process if local=True.
        Returns the number of subscribers that received the message.
        """
        serialized_payload = self._serialize_value(payload)
        envelope = {
            "event": event,
            "payload": serialized_payload,
            "sender_id": self.bus_id,
            "timestamp": time.time(),
        }

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
        """Background loop reading Redis pub/sub messages."""
        try:
            while self._running:
                try:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=0.5
                    )
                except (RedisError, OSError) as exc:
                    if not self._running:
                        break
                    logger.warning("Redis bus connection error: %s", exc)
                    await asyncio.sleep(0.1)
                    continue

                if message is None:
                    await asyncio.sleep(0.01)
                    continue

                msg_type = message.get("type")
                if msg_type in ("pmessage", "message"):
                    data = message.get("data")
                    if isinstance(data, (str, bytes)):
                        await self._handle_raw_message(data)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await pubsub.punsubscribe(pattern)
                await self._close_pubsub(pubsub)
            except (RedisError, OSError) as exc:
                logger.debug("Error closing pubsub subscriber: %s", exc)

    async def _handle_raw_message(self, data: str | bytes) -> None:
        """Deserialize message envelope and route event to dispatcher."""
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

        sender_id = envelope.get("sender_id")
        if self.ignore_self and sender_id == self.bus_id:
            return

        event = envelope.get("event")
        payload = envelope.get("payload")

        if isinstance(event, str):
            try:
                await self.dispatcher.dispatch(event, payload)
            except Exception:
                logger.exception(
                    "Error executing listener for cross-process event '%s'",
                    event,
                )

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
            except (RedisError, OSError) as exc:
                logger.debug("Error during listener task shutdown: %s", exc)
            self._listener_task = None

        if self._pubsub is not None:
            try:
                await self._close_pubsub(self._pubsub)
            except (RedisError, OSError) as exc:
                logger.debug("Error closing pubsub: %s", exc)
            self._pubsub = None

    async def close(self) -> None:
        """Stop listening and close Redis connections."""
        await self.stop_listening()

        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

        if self._pool is not None:
            await self._pool.disconnect()
            self._pool = None

    async def __aenter__(self) -> Self:
        await self.start_listening()
        await self.wait_until_ready()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
