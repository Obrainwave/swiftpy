from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode


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
                    resp_headers[k.decode("latin-1")] = v.decode("latin-1")
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


__all__ = ["TestClient", "TestResponse"]
