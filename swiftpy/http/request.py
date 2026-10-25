from __future__ import annotations

import json
from typing import Any, cast


class Request:
    def __init__(self, scope: dict[str, Any], receive: Any) -> None:
        self.scope = scope
        self._receive = receive

        self._body: bytes | None = None
        self._json: Any = None

        self.state: dict[str, Any] = {}

    @property
    def method(self) -> str:
        return cast(str, self.scope["method"])

    @property
    def path(self) -> str:
        return cast(str, self.scope["path"])

    @property
    def headers(self) -> dict[str, str]:
        return {k.decode(): v.decode() for k, v in self.scope.get("headers", [])}

    @property
    def query_params(self) -> dict[str, str]:
        """Parsed dictionary of URL query string parameters (first value for each key)."""
        raw_qs = self.scope.get("query_string", b"")
        if not raw_qs:
            return {}
        from urllib.parse import parse_qs

        parsed = parse_qs(raw_qs.decode("latin-1"), keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    @property
    def query_param_list(self) -> dict[str, list[str]]:
        """Parsed dictionary of URL query string parameters with all values as lists."""
        raw_qs = self.scope.get("query_string", b"")
        if not raw_qs:
            return {}
        from urllib.parse import parse_qs

        return parse_qs(raw_qs.decode("latin-1"), keep_blank_values=True)

    async def body(self) -> bytes:
        if self._body is None:
            chunks: list[bytes] = []

            while True:
                message = await self._receive()

                chunks.append(message.get("body", b""))

                if not message.get(
                    "more_body",
                    False,
                ):
                    break

            self._body = b"".join(chunks)

        return self._body

    async def json(self) -> Any:
        if self._json is None:
            raw = (await self.body()).decode()
            self._json = json.loads(raw) if raw else {}

        return self._json
