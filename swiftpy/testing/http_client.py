from __future__ import annotations

import asyncio
import json
import logging
from types import TracebackType
from typing import Any
from urllib.parse import urlencode

from swiftpy.websockets.connection import WebSocketDisconnect

logger = logging.getLogger("swiftpy.testing")


class TestResponse:
    """
    Encapsulates the HTTP response returned by TestClient.
    Provides fluent assertion helpers for testing responses.
    """

    __test__ = False

    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self.body = body
        self._text: str | None = None
        self._json: Any = None
        self._json_parsed = False

    @property
    def text(self) -> str:
        """Decoded response body string."""
        if self._text is None:
            self._text = self.body.decode("utf-8", errors="replace")
        return self._text

    def json(self) -> Any:
        """Parse JSON response body."""
        if not self._json_parsed:
            if not self.body:
                self._json = None
            else:
                self._json = json.loads(self.text)
            self._json_parsed = True
        return self._json

    def assert_status(self, expected_status: int) -> TestResponse:
        """Assert that the response status code matches expected status."""
        if self.status_code != expected_status:
            raise AssertionError(
                f"Expected status {expected_status}, but received {self.status_code}. Response: {self.text}"
            )
        return self

    def assert_ok(self) -> TestResponse:
        """Assert status code is 200 OK."""
        return self.assert_status(200)

    def assert_created(self) -> TestResponse:
        """Assert status code is 201 Created."""
        return self.assert_status(201)

    def assert_no_content(self) -> TestResponse:
        """Assert status code is 204 No Content."""
        return self.assert_status(204)

    def assert_bad_request(self) -> TestResponse:
        """Assert status code is 400 Bad Request."""
        return self.assert_status(400)

    def assert_unauthorized(self) -> TestResponse:
        """Assert status code is 401 Unauthorized."""
        return self.assert_status(401)

    def assert_forbidden(self) -> TestResponse:
        """Assert status code is 403 Forbidden."""
        return self.assert_status(403)

    def assert_not_found(self) -> TestResponse:
        """Assert status code is 404 Not Found."""
        return self.assert_status(404)

    def assert_method_not_allowed(self) -> TestResponse:
        """Assert status code is 405 Method Not Allowed."""
        return self.assert_status(405)

    def assert_validation_error(self, field: str | None = None) -> TestResponse:
        """Assert status code is 422 Unprocessable Entity, optionally checking for a field error."""
        self.assert_status(422)
        if field is not None:
            data = self.json()
            if not isinstance(data, dict) or "errors" not in data:
                raise AssertionError(f"Expected structured field errors, got: {data}")
            errors = data["errors"]
            if field not in errors:
                raise AssertionError(
                    f"Expected validation error for field '{field}', but errors were: {errors}"
                )
        return self

    def assert_server_error(self) -> TestResponse:
        """Assert status code is 500 Internal Server Error."""
        return self.assert_status(500)

    def assert_json(self, expected: Any) -> TestResponse:
        """Assert exact JSON equality."""
        actual = self.json()
        if actual != expected:
            raise AssertionError(f"Expected JSON {expected}, but got {actual}")
        return self

    def assert_json_fragment(self, fragment: dict[str, Any]) -> TestResponse:
        """Assert that fragment key-values exist in the response JSON."""
        actual = self.json()
        if not isinstance(actual, dict):
            raise AssertionError(  # noqa: TRY004
                f"Expected JSON object, got {type(actual)}"
            )

        for k, v in fragment.items():
            if k not in actual or actual[k] != v:
                raise AssertionError(
                    f"Fragment mismatch: expected '{k}': {v}, but actual had '{k}': {actual.get(k)}. Full: {actual}"
                )
        return self

    def assert_header(
        self, header: str, expected_val: str | None = None
    ) -> TestResponse:
        """Assert header presence and optionally its exact value."""
        norm_key = header.lower()
        matched = {k.lower(): v for k, v in self.headers.items()}
        if norm_key not in matched:
            raise AssertionError(f"Expected header '{header}' was not found.")
        if expected_val is not None and matched[norm_key] != expected_val:
            raise AssertionError(
                f"Expected header '{header}' to be '{expected_val}', got '{matched[norm_key]}'."
            )
        return self


class TestClient:
    """
    In-process ASGI test client.
    Executes HTTP requests directly through the application's ASGI interface
    without binding to network ports or running an external server.
    """

    __test__ = False

    def __init__(
        self,
        app: Any,
        base_url: str = "http://localhost",
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self.app = app
        self.base_url = base_url.rstrip("/")
        self._default_headers: dict[str, str] = dict(default_headers or {})

    def with_headers(self, headers: dict[str, str]) -> TestClient:
        """Return a new TestClient with additional default headers."""
        merged = dict(self._default_headers)
        merged.update(headers)
        return TestClient(self.app, base_url=self.base_url, default_headers=merged)

    def with_token(self, token: str) -> TestClient:
        """Return a new TestClient with a Bearer Authorization header."""
        return self.with_headers({"Authorization": f"Bearer {token}"})

    def with_api_key(self, key: str, header: str = "X-API-Key") -> TestClient:
        """Return a new TestClient with an API key header."""
        return self.with_headers({header: key})

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_data: Any = None,
        data: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestResponse:
        """Execute an in-process HTTP request against the ASGI application."""
        req_headers: dict[str, str] = dict(self._default_headers)
        if headers:
            req_headers.update(headers)

        # Prepare body
        body_bytes = b""
        if json_data is not None:
            body_bytes = json.dumps(json_data).encode("utf-8")
            if "content-type" not in {k.lower() for k in req_headers}:
                req_headers["content-type"] = "application/json"
        elif isinstance(data, str):
            body_bytes = data.encode("utf-8")
        elif isinstance(data, bytes):
            body_bytes = data

        if body_bytes and "content-length" not in {k.lower() for k in req_headers}:
            req_headers["content-length"] = str(len(body_bytes))

        # Query string
        parsed_path = path
        query_string = b""
        if query:
            query_string = urlencode(query, doseq=True).encode("ascii")
        elif "?" in path:
            parsed_path, raw_qs = path.split("?", 1)
            query_string = raw_qs.encode("ascii")

        raw_headers = [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in req_headers.items()
        ]

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "path": parsed_path,
            "raw_path": parsed_path.encode("ascii"),
            "query_string": query_string,
            "headers": raw_headers,
            "server": ("127.0.0.1", 80),
            "client": ("127.0.0.1", 50000),
            "scheme": "http",
        }

        status_code = 0
        resp_headers: dict[str, str] = {}
        resp_body = bytearray()
        body_sent = False

        async def receive() -> dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {
                    "type": "http.request",
                    "body": body_bytes,
                    "more_body": False,
                }
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            nonlocal status_code, resp_headers, resp_body
            if message["type"] == "http.response.start":
                status_code = message["status"]
                for k, v in message.get("headers", []):
                    h_key = k.decode("latin-1")
                    h_val = v.decode("latin-1")
                    resp_headers[h_key] = h_val
                    resp_headers[h_key.lower()] = h_val
            elif message["type"] == "http.response.body":
                resp_body.extend(message.get("body", b""))

        await self.app(scope, receive, send)

        return TestResponse(
            status_code=status_code,
            headers=resp_headers,
            body=bytes(resp_body),
        )

    async def get(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestResponse:
        return await self.request("GET", path, headers=headers, query=query)

    async def post(
        self,
        path: str,
        *,
        json: Any = None,
        data: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestResponse:
        return await self.request(
            "POST",
            path,
            json_data=json,
            data=data,
            headers=headers,
            query=query,
        )

    async def put(
        self,
        path: str,
        *,
        json: Any = None,
        data: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestResponse:
        return await self.request(
            "PUT", path, json_data=json, data=data, headers=headers, query=query
        )

    async def patch(
        self,
        path: str,
        *,
        json: Any = None,
        data: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestResponse:
        return await self.request(
            "PATCH",
            path,
            json_data=json,
            data=data,
            headers=headers,
            query=query,
        )

    async def delete(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
    ) -> TestResponse:
        return await self.request("DELETE", path, headers=headers, query=query)

    def websocket_connect(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        subprotocols: list[str] | None = None,
    ) -> WebSocketConnectCM:
        """Establish an in-process WebSocket connection."""
        return WebSocketConnectCM(
            self, path, headers=headers, subprotocols=subprotocols
        )


class WebSocketTestSession:
    """
    In-process WebSocket test session for testing ASGI WebSocket endpoints.
    """

    __test__ = False

    def __init__(
        self,
        app: Any,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self.app = app
        self.default_headers = default_headers or {}
        self._to_app_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._app_task: asyncio.Task[None] | None = None
        self.is_closed = False
        self.accepted_subprotocol: str | None = None
        self.accepted_headers: list[tuple[bytes, bytes]] = []

    async def connect(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        subprotocols: list[str] | None = None,
    ) -> None:
        parsed_path = path
        query_string = b""
        if "?" in path:
            parsed_path, raw_qs = path.split("?", 1)
            query_string = raw_qs.encode("ascii")

        req_headers = dict(self.default_headers)
        if headers:
            req_headers.update(headers)

        raw_headers = [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in req_headers.items()
        ]

        scope: dict[str, Any] = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": parsed_path,
            "raw_path": parsed_path.encode("ascii"),
            "query_string": query_string,
            "headers": raw_headers,
            "subprotocols": subprotocols or [],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 80),
        }

        await self._to_app_queue.put({"type": "websocket.connect"})

        async def receive() -> dict[str, Any]:
            return await self._to_app_queue.get()

        async def send(message: dict[str, Any]) -> None:
            await self._from_app_queue.put(message)

        self._app_task = asyncio.create_task(self.app(scope, receive, send))

        def _on_done(t: asyncio.Task[None]) -> None:
            if not t.cancelled() and t.exception() is not None:
                logger.debug("WebSocket app task ended with exception", exc_info=t.exception())

        self._app_task.add_done_callback(_on_done)

        try:
            msg = await asyncio.wait_for(self._from_app_queue.get(), timeout=5.0)
        except TimeoutError:
            self.is_closed = True
            if self._app_task and not self._app_task.done():
                self._app_task.cancel()
            raise TimeoutError("Timed out waiting for WebSocket handshake response") from None

        if msg["type"] == "websocket.close":
            self.is_closed = True
            code = msg.get("code", 1000)
            reason = msg.get("reason", "")
            raise WebSocketDisconnect(code=code, reason=reason)

        if msg["type"] == "websocket.accept":
            self.accepted_subprotocol = msg.get("subprotocol")
            self.accepted_headers = msg.get("headers", [])

    async def send_text(self, text: str) -> None:
        if self.is_closed:
            raise WebSocketDisconnect(1000, "Connection already closed")
        await self._to_app_queue.put({"type": "websocket.receive", "text": text})

    async def send_bytes(self, data: bytes) -> None:
        if self.is_closed:
            raise WebSocketDisconnect(1000, "Connection already closed")
        await self._to_app_queue.put({"type": "websocket.receive", "bytes": data})

    async def send_json(self, data: Any) -> None:
        await self.send_text(json.dumps(data))

    async def receive_raw(self, timeout: float = 5.0) -> dict[str, Any]:
        if self.is_closed:
            raise WebSocketDisconnect(1000, "Connection already closed")
        try:
            msg = await asyncio.wait_for(self._from_app_queue.get(), timeout=timeout)
        except TimeoutError:
            raise TimeoutError("Timed out waiting for message from WebSocket") from None

        if msg["type"] == "websocket.close":
            self.is_closed = True
            code = msg.get("code", 1000)
            reason = msg.get("reason", "")
            raise WebSocketDisconnect(code=code, reason=reason)

        return msg

    async def receive_text(self, timeout: float = 5.0) -> str:
        msg = await self.receive_raw(timeout=timeout)
        if "text" in msg and msg["text"] is not None:
            return str(msg["text"])
        if "bytes" in msg and msg["bytes"] is not None:
            return bytes(msg["bytes"]).decode("utf-8")
        return ""

    async def receive_bytes(self, timeout: float = 5.0) -> bytes:
        msg = await self.receive_raw(timeout=timeout)
        if "bytes" in msg and msg["bytes"] is not None:
            return bytes(msg["bytes"])
        if "text" in msg and msg["text"] is not None:
            return str(msg["text"]).encode("utf-8")
        return b""

    async def receive_json(self, timeout: float = 5.0) -> Any:
        text = await self.receive_text(timeout=timeout)
        return json.loads(text)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.is_closed:
            return
        self.is_closed = True
        await self._to_app_queue.put(
            {"type": "websocket.disconnect", "code": code, "reason": reason}
        )
        if self._app_task and not self._app_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._app_task), timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                self._app_task.cancel()


class WebSocketConnectCM:
    """Async context manager and awaitable for connecting to an ASGI WebSocket endpoint."""

    __test__ = False

    def __init__(
        self,
        client: TestClient,
        path: str,
        headers: dict[str, str] | None = None,
        subprotocols: list[str] | None = None,
    ) -> None:
        self.client = client
        self.path = path
        self.headers = headers
        self.subprotocols = subprotocols
        self.session: WebSocketTestSession | None = None

    async def __aenter__(self) -> WebSocketTestSession:
        session = WebSocketTestSession(
            self.client.app, default_headers=self.client._default_headers
        )
        await session.connect(
            self.path, headers=self.headers, subprotocols=self.subprotocols
        )
        self.session = session
        return session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self.session is not None:
            await self.session.close()

    def __await__(self) -> Any:
        async def _connect() -> WebSocketTestSession:
            session = WebSocketTestSession(
                self.client.app, default_headers=self.client._default_headers
            )
            await session.connect(
                self.path, headers=self.headers, subprotocols=self.subprotocols
            )
            return session

        return _connect().__await__()


__all__ = [
    "TestClient",
    "TestResponse",
    "WebSocketConnectCM",
    "WebSocketTestSession",
]
