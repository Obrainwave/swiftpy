from __future__ import annotations

import json
from typing import Any


class Request:
    def __init__(
        self,
        scope: dict[str, Any],
        receive: Any,
    ) -> None:
        self.scope = scope
        self._receive = receive

        self._body: bytes | None = None
        self._json: Any = None

    @property
    def method(self) -> str:
        return self.scope["method"]

    @property
    def path(self) -> str:
        return self.scope["path"]

    @property
    def headers(self) -> dict[str, str]:
        return {
            k.decode(): v.decode()
            for k, v in self.scope.get("headers", [])
        }

    async def body(self) -> bytes:
        if self._body is None:
            chunks: list[bytes] = []

            while True:
                message = await self._receive()

                chunks.append(
                    message.get("body", b"")
                )

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