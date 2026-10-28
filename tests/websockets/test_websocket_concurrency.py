from __future__ import annotations

import asyncio

import pytest

from swiftpy.core.application import Application
from swiftpy.testing.http_client import TestClient
from swiftpy.websockets.connection import (
    WebSocketConnection,
    WebSocketDisconnect,
)
from swiftpy.websockets.manager import WebSocketManager


@pytest.mark.asyncio
async def test_websocket_50_simultaneous_connections_broadcast() -> None:
    """
    Stress test verifying that 50 simultaneous WebSocket connections
    accept, receive, and broadcast correctly without dropped messages.
    """
    manager = WebSocketManager()
    app = Application()

    @app.websocket("/ws/concurrent_broadcast")
    async def broadcast_handler(ws: WebSocketConnection) -> None:
        await ws.accept()
        await manager.connect(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            await manager.disconnect(ws)

    client = TestClient(app)
    num_clients = 50
    connections_ready = asyncio.Event()
    ready_count = 0
    ready_lock = asyncio.Lock()
    all_received: list[str] = []
    received_lock = asyncio.Lock()

    async def client_worker(worker_id: int) -> None:
        nonlocal ready_count
        async with client.websocket_connect("/ws/concurrent_broadcast") as ws:
            async with ready_lock:
                ready_count += 1
                if ready_count == num_clients:
                    connections_ready.set()

            # Wait until all workers are connected before reading broadcast
            await connections_ready.wait()
            msg = await ws.receive_text()
            async with received_lock:
                all_received.append(msg)

    # Launch all 50 clients concurrently
    tasks = [asyncio.create_task(client_worker(i)) for i in range(num_clients)]

    try:
        # Wait for all 50 connections to be established and registered
        await asyncio.wait_for(connections_ready.wait(), timeout=10.0)
        assert manager.active_count == num_clients

        # Broadcast to all 50 connections
        broadcast_payload = "Global Event: Phase 3 Concurrent Verification"
        await manager.broadcast(broadcast_payload)

        # Wait for all client workers to receive message and complete
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10.0)

        # Verify all 50 clients received the exact message
        assert len(all_received) == num_clients
        assert all(m == broadcast_payload for m in all_received)
        assert manager.active_count == 0
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_websocket_multi_room_simultaneous_isolation() -> None:
    """
    Stress test verifying room isolation under concurrent conditions:
    20 clients in room_alpha, 20 clients in room_beta.
    """
    manager = WebSocketManager()
    app = Application()

    @app.websocket("/ws/multi_room/{room}")
    async def room_handler(ws: WebSocketConnection, room: str) -> None:
        await ws.accept()
        await manager.connect(ws, room=room)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            await manager.disconnect(ws, room=room)

    client = TestClient(app)
    clients_per_room = 20
    alpha_received: list[str] = []
    beta_received: list[str] = []
    ready_barrier = asyncio.Barrier(clients_per_room * 2 + 1)

    async def alpha_worker(cid: int) -> None:
        async with client.websocket_connect("/ws/multi_room/alpha") as ws:
            await ready_barrier.wait()
            msg = await ws.receive_text()
            alpha_received.append(msg)

    async def beta_worker(cid: int) -> None:
        async with client.websocket_connect("/ws/multi_room/beta") as ws:
            await ready_barrier.wait()
            msg = await ws.receive_text()
            beta_received.append(msg)

    alpha_tasks = [asyncio.create_task(alpha_worker(i)) for i in range(clients_per_room)]
    beta_tasks = [asyncio.create_task(beta_worker(i)) for i in range(clients_per_room)]
    all_tasks = alpha_tasks + beta_tasks

    try:
        # Wait until all 40 workers are connected with timeout
        await asyncio.wait_for(ready_barrier.wait(), timeout=10.0)
        assert manager.active_count == clients_per_room * 2
        assert len(manager.get_connections("alpha")) == clients_per_room
        assert len(manager.get_connections("beta")) == clients_per_room

        # Broadcast to alpha only
        await manager.broadcast("Message for Alpha Room", room="alpha")
        # Broadcast to beta only
        await manager.broadcast("Message for Beta Room", room="beta")

        await asyncio.wait_for(asyncio.gather(*all_tasks), timeout=10.0)

        assert len(alpha_received) == clients_per_room
        assert all(m == "Message for Alpha Room" for m in alpha_received)

        assert len(beta_received) == clients_per_room
        assert all(m == "Message for Beta Room" for m in beta_received)

        assert manager.active_count == 0
    finally:
        for t in all_tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_websocket_concurrent_bidirectional_messaging() -> None:
    """
    Stress test with 30 concurrent connections actively sending and receiving messages.
    """
    app = Application()

    @app.websocket("/ws/bidirectional/{user_id}")
    async def bidirectional_handler(ws: WebSocketConnection, user_id: int) -> None:
        await ws.accept()
        try:
            while True:
                text = await ws.receive_text()
                await ws.send_text(f"ACK user {user_id}: {text}")
        except WebSocketDisconnect:
            pass

    client = TestClient(app)
    num_workers = 30

    async def worker(uid: int) -> None:
        async with client.websocket_connect(f"/ws/bidirectional/{uid}") as ws:
            for seq in range(3):
                await ws.send_text(f"msg-{seq}")
                reply = await ws.receive_text()
                assert reply == f"ACK user {uid}: msg-{seq}"

    workers = [asyncio.create_task(worker(i)) for i in range(num_workers)]
    try:
        await asyncio.wait_for(asyncio.gather(*workers), timeout=10.0)
    finally:
        for t in workers:
            if not t.done():
                t.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


@pytest.mark.asyncio
async def test_websocket_simultaneous_send_and_receive_per_connection() -> None:
    """
    Advanced concurrency test:
    Each connection runs independent sender and receiver coroutines simultaneously,
    verifying concurrent full-duplex WebSocket messaging without interleaving or deadlock.
    """
    app = Application()

    @app.websocket("/ws/duplex/{conn_id}")
    async def duplex_handler(ws: WebSocketConnection, conn_id: int) -> None:
        await ws.accept()
        try:
            while True:
                data = await ws.receive_json()
                # Reply asynchronously with echo and sequence acknowledgment
                await ws.send_json({"ack": conn_id, "seq": data["seq"], "payload": data["payload"]})
        except WebSocketDisconnect:
            pass

    client = TestClient(app)
    num_conns = 15
    msgs_per_conn = 5

    async def run_client_duplex(cid: int) -> None:
        async with client.websocket_connect(f"/ws/duplex/{cid}") as ws:
            received_seqs: list[int] = []

            async def sender() -> None:
                for seq in range(msgs_per_conn):
                    await ws.send_json({"seq": seq, "payload": f"conn-{cid}-data-{seq}"})
                    await asyncio.sleep(0.001)

            async def receiver() -> None:
                for _ in range(msgs_per_conn):
                    msg = await ws.receive_json(timeout=5.0)
                    assert msg["ack"] == cid
                    received_seqs.append(msg["seq"])

            # Run sender and receiver simultaneously on the same active connection
            await asyncio.gather(sender(), receiver())
            assert sorted(received_seqs) == list(range(msgs_per_conn))

    tasks = [asyncio.create_task(run_client_duplex(i)) for i in range(num_conns)]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10.0)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
