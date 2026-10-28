from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
import redis.asyncio as aioredis

from swiftpy.core.application import Application
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.helpers.helper import dispatcher, event
from swiftpy.queue.dispatcher import QueueDispatcher, push
from swiftpy.queue.job import Job
from swiftpy.queue.worker import QueueWorker
from swiftpy.testing.http_client import TestClient
from swiftpy.websockets.connection import (
    WebSocketConnection,
    WebSocketDisconnect,
)
from swiftpy.websockets.manager import WebSocketManager


class OrderCompletedJob(Job):
    """
    Background queue job that simulates order processing
    and triggers an event upon successful completion.
    """

    def __init__(
        self,
        order_id: str,
        target_room: str,
        **kwargs: object,
    ) -> None:
        super().__init__(order_id, target_room=target_room, **kwargs)
        self.order_id = order_id
        self.target_room = target_room

    async def handle(self) -> None:
        # 1. Simulate background job computation
        # 2. Fire an application event containing the result
        await event(
            "order.completed",
            {
                "order_id": self.order_id,
                "room": self.target_room,
                "status": "COMPLETED",
                "message": f"Order {self.order_id} has been processed successfully.",
            },
        )


@pytest.mark.asyncio
async def test_queue_to_event_to_websocket_end_to_end() -> None:
    """
    Phase 3 End-to-End Integration Test:
    A queued job completes via an in-process QueueWorker,
    fires an event through EventDispatcher,
    and the event listener broadcasts a WebSocket message to connected
    clients in the relevant room. All three subsystems working together in one path.
    """
    order_id = f"ORD-{uuid.uuid4().hex[:6].upper()}"
    queue_name = f"orders_{uuid.uuid4().hex[:8]}"
    prefix = f"test_integration_{uuid.uuid4().hex[:8]}:"

    # 1. Setup isolated application & WebSocket manager
    WebSocketManager.reset_instance()
    EventDispatcher.set_instance(None)
    QueueDispatcher.set_instance(None)

    app = Application()
    manager = app.websocket_manager
    event_disp = dispatcher()

    # 2. Setup WebSocket endpoint with room parameter
    @app.websocket("/ws/notifications/{room}")
    async def notification_handler(ws: WebSocketConnection, room: str) -> None:
        await ws.accept()
        await manager.connect(ws, room=room)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            await manager.disconnect(ws, room=room)

    # 3. Setup event listener that bridges event dispatch to WebSocket broadcast
    events_received: list[dict[str, Any]] = []

    async def on_order_completed(payload: dict[str, Any]) -> None:
        events_received.append(payload)
        room = payload["room"]
        # Broadcast the event payload to connected clients in the designated room
        await manager.broadcast(payload, room=room)

    event_disp.listen("order.completed", on_order_completed)

    # 4. Connect WebSocket clients to different rooms
    client = TestClient(app)
    room_orders_received_a: list[dict[str, Any]] = []
    room_orders_received_b: list[dict[str, Any]] = []
    room_billing_received: list[dict[str, Any]] = []

    queue_disp = QueueDispatcher(prefix=prefix)
    QueueDispatcher.set_instance(queue_disp)

    try:
        # Connect client A and client B to room 'orders', and client C to room 'billing'
        async with (
            client.websocket_connect("/ws/notifications/orders") as ws_orders_a,
            client.websocket_connect("/ws/notifications/orders") as ws_orders_b,
            client.websocket_connect("/ws/notifications/billing") as ws_billing,
        ):
            assert manager.active_count == 3
            assert len(manager.get_connections("orders")) == 2
            assert len(manager.get_connections("billing")) == 1

            # 5. Push job onto Redis Queue
            job = OrderCompletedJob(order_id=order_id, target_room="orders")
            job_id = await push(job, queue=queue_name)
            assert job_id is not None

            # 6. Run Queue Worker to process the job
            worker = QueueWorker(
                queues=queue_name,
                dispatcher=queue_disp,
                stop_when_empty=True,
                sleep=0.01,
            )
            await worker.work()

            # 7. Clients in room 'orders' should receive the broadcasted WebSocket message
            msg_a = await ws_orders_a.receive_json(timeout=5.0)
            msg_b = await ws_orders_b.receive_json(timeout=5.0)

            room_orders_received_a.append(msg_a)
            room_orders_received_b.append(msg_b)

            # Verify payload integrity
            expected_payload = {
                "order_id": order_id,
                "room": "orders",
                "status": "COMPLETED",
                "message": f"Order {order_id} has been processed successfully.",
            }

            assert msg_a == expected_payload
            assert msg_b == expected_payload
            assert len(events_received) == 1
            assert events_received[0] == expected_payload

            # Verify client in 'billing' did NOT receive the message
            # Send a small probe message to billing to verify its connection is intact and separate
            await manager.broadcast({"room": "billing", "probe": True}, room="billing")
            billing_msg = await ws_billing.receive_json(timeout=5.0)
            assert billing_msg == {"room": "billing", "probe": True}
            assert room_billing_received == []
    finally:
        # 8. Clean up Redis keys and singletons guaranteed even if test assertions fail
        host = os.getenv("REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("REDIS_PORT", "6379"))
        redis_client = aioredis.Redis(host=host, port=port)
        try:
            keys = await redis_client.keys(f"{prefix}*")
            if keys:
                await redis_client.delete(*keys)
        except aioredis.RedisError:
            pass
        finally:
            await redis_client.aclose()

        event_disp.forget("order.completed")
        WebSocketManager.reset_instance()
        EventDispatcher.set_instance(None)
        QueueDispatcher.set_instance(None)
        Job.set_trusted_namespaces(None)
