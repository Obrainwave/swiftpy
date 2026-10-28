from __future__ import annotations

import enum
import json
from collections.abc import Awaitable, Callable
from typing import Any, cast
from urllib.parse import parse_qs


class WebSocketState(enum.Enum):
    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2


class WebSocketException(Exception):
    """Base exception for WebSocket errors."""


class WebSocketDisconnect(WebSocketException):
    """Raised when a WebSocket connection is disconnected."""

    def __init__(self, code: int = 1000, reason: str = "") -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"WebSocket disconnected with code {code} ({reason or 'no reason given'})")


class WebSocketConnection:
    """
    Wraps an ASGI websocket connection.
    Provides standard accept(), receive(), send(), and close() methods.
    """

    def __init__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.scope = scope
        self._receive = receive
        self._send = send

        self.client_state = WebSocketState.CONNECTING
        self.close_code: int | None = None
        self.close_reason: str = ""
        self.state: dict[str, Any] = {}

    @property
    def path(self) -> str:
        """The URL path of the WebSocket request."""
        return cast(str, self.scope.get("path", ""))

    @property
    def headers(self) -> dict[str, str]:
        """Parsed request headers mapping lowercase header names to values."""
        result: dict[str, str] = {}
        for k, v in self.scope.get("headers", []):
            key = k.decode("latin-1").lower() if isinstance(k, bytes) else str(k).lower()
            val = v.decode("latin-1") if isinstance(v, bytes) else str(v)
            result[key] = val
        return result

    @property
    def query_params(self) -> dict[str, str]:
        """Parsed dictionary of URL query string parameters (first value for each key)."""
        raw_qs = self.scope.get("query_string", b"")
        if not raw_qs:
            return {}
        qs_str = raw_qs.decode("latin-1") if isinstance(raw_qs, bytes) else str(raw_qs)
        parsed = parse_qs(qs_str, keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    @property
    def query_param_list(self) -> dict[str, list[str]]:
        """Parsed dictionary of URL query string parameters with all values as lists."""
        raw_qs = self.scope.get("query_string", b"")
        if not raw_qs:
            return {}
        qs_str = raw_qs.decode("latin-1") if isinstance(raw_qs, bytes) else str(raw_qs)
        return parse_qs(qs_str, keep_blank_values=True)

    @property
    def client(self) -> tuple[str, int] | None:
        """The client's (host, port) tuple if available."""
        client_tuple = self.scope.get("client")
        if client_tuple and len(client_tuple) == 2:
            return (str(client_tuple[0]), int(client_tuple[1]))
        return None

    @property
    def subprotocols(self) -> list[str]:
        """Subprotocols requested by the client."""
        return list(self.scope.get("subprotocols", []))

    @property
    def is_connected(self) -> bool:
        """Whether the WebSocket connection is actively open."""
        return self.client_state == WebSocketState.CONNECTED

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        """
        Accept the incoming WebSocket connection handshake.
        Consumes the initial 'websocket.connect' ASGI message and transmits 'websocket.accept'.
        """
        if self.client_state == WebSocketState.CONNECTED:
            raise WebSocketException("Cannot accept an already accepted WebSocket connection")
        if self.client_state == WebSocketState.DISCONNECTED:
            raise WebSocketException("Cannot accept a closed WebSocket connection")

        message = await self._receive()
        msg_type = message.get("type")

        if msg_type == "websocket.disconnect":
            self.client_state = WebSocketState.DISCONNECTED
            self.close_code = message.get("code", 1006)
            self.close_reason = message.get("reason", "")
            raise WebSocketDisconnect(code=self.close_code, reason=self.close_reason)

        if msg_type != "websocket.connect":
            raise WebSocketException(
                f"Expected 'websocket.connect' message, but received '{msg_type}'"
            )

        accept_msg: dict[str, Any] = {"type": "websocket.accept"}
        if subprotocol is not None:
            accept_msg["subprotocol"] = subprotocol
        if headers is not None:
            accept_msg["headers"] = headers

        await self._send(accept_msg)
        self.client_state = WebSocketState.CONNECTED

    async def receive(self) -> str | bytes:
        """
        Receive raw text or bytes from the WebSocket client.
        Raises WebSocketDisconnect when the client disconnects.
        """
        if self.client_state == WebSocketState.CONNECTING:
            raise WebSocketException("Cannot receive data: WebSocket has not been accepted yet")
        if self.client_state == WebSocketState.DISCONNECTED:
            raise WebSocketDisconnect(
                code=self.close_code or 1000,
                reason=self.close_reason,
            )

        message = await self._receive()
        msg_type = message.get("type")

        if msg_type == "websocket.receive":
            if "text" in message and message["text"] is not None:
                return cast(str, message["text"])
            if "bytes" in message and message["bytes"] is not None:
                return cast(bytes, message["bytes"])
            return ""

        if msg_type == "websocket.disconnect":
            self.client_state = WebSocketState.DISCONNECTED
            self.close_code = message.get("code", 1000)
            self.close_reason = message.get("reason", "")
            raise WebSocketDisconnect(code=self.close_code, reason=self.close_reason)

        raise WebSocketException(f"Unexpected WebSocket message type: '{msg_type}'")

    async def receive_text(self) -> str:
        """Receive a text message from the WebSocket client."""
        data = await self.receive()
        if not isinstance(data, str):
            raise WebSocketException("Expected text frame, but received binary frame")
        return data

    async def receive_bytes(self) -> bytes:
        """Receive a binary message from the WebSocket client."""
        data = await self.receive()
        if not isinstance(data, bytes):
            raise WebSocketException("Expected binary frame, but received text frame")
        return data

    async def receive_json(self) -> Any:
        """Receive and parse a JSON message from the WebSocket client."""
        text = await self.receive_text()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise WebSocketException(f"Malformed JSON received: {exc.msg}") from exc

    async def send(self, data: str | bytes | dict[str, Any] | list[Any]) -> None:
        """
        Send data to the WebSocket client.
        Automatically routes str, bytes, and JSON serializable dict/list.
        """
        if self.client_state != WebSocketState.CONNECTED:
            raise WebSocketException("Cannot send message: WebSocket is not connected")

        if isinstance(data, str):
            await self.send_text(data)
        elif isinstance(data, bytes):
            await self.send_bytes(data)
        elif isinstance(data, (dict, list)):
            await self.send_json(data)
        else:
            await self.send_text(str(data))

    async def send_text(self, text: str) -> None:
        """Send a text frame to the WebSocket client."""
        if self.client_state != WebSocketState.CONNECTED:
            raise WebSocketException("Cannot send message: WebSocket is not connected")
        await self._send({"type": "websocket.send", "text": text})

    async def send_bytes(self, data: bytes) -> None:
        """Send a binary frame to the WebSocket client."""
        if self.client_state != WebSocketState.CONNECTED:
            raise WebSocketException("Cannot send message: WebSocket is not connected")
        await self._send({"type": "websocket.send", "bytes": data})

    async def send_json(self, data: Any) -> None:
        """Serialize data to JSON and send as a text frame to the WebSocket client."""
        if self.client_state != WebSocketState.CONNECTED:
            raise WebSocketException("Cannot send message: WebSocket is not connected")
        await self._send({"type": "websocket.send", "text": json.dumps(data)})

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Close the WebSocket connection."""
        if self.client_state == WebSocketState.DISCONNECTED:
            return

        self.client_state = WebSocketState.DISCONNECTED
        self.close_code = code
        self.close_reason = reason
        await self._send({"type": "websocket.close", "code": code, "reason": reason})


WebSocket = WebSocketConnection

__all__ = [
    "WebSocket",
    "WebSocketConnection",
    "WebSocketDisconnect",
    "WebSocketException",
    "WebSocketState",
]
