from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any, cast
from urllib.parse import parse_qs

from swiftpy.http.exceptions import (
    ClientDisconnect,
    HttpException,
    PayloadTooLargeError,
)

DEFAULT_MAX_BODY_SIZE: int = 2 * 1024 * 1024  # 2 MiB


class RequestHeaders(Mapping[str, str]):
    """
    Case-insensitive, immutable HTTP request header container decoded as latin-1 per ASGI spec.
    Preserves duplicate headers via `.getlist(name)` while using last-wins semantics for
    dict-style single-value lookups (`headers[name]`, `headers.get(name)`).
    """

    __slots__ = ("_by_lower", "_items", "_list_by_lower")

    def __init__(self, raw_headers: list[tuple[bytes, bytes]] | None = None) -> None:
        items: list[tuple[str, str]] = []
        by_lower: dict[str, str] = {}
        list_by_lower: dict[str, list[str]] = {}

        for raw_k, raw_v in raw_headers or ():
            key = raw_k.decode("latin-1")
            val = raw_v.decode("latin-1")
            lower_key = key.lower()
            items.append((key, val))
            by_lower[lower_key] = val
            list_by_lower.setdefault(lower_key, []).append(val)

        self._items: list[tuple[str, str]] = items
        self._by_lower: dict[str, str] = by_lower
        self._list_by_lower: dict[str, list[str]] = list_by_lower

    def __getitem__(self, name: str) -> str:
        lower = name.lower()
        if lower not in self._by_lower:
            raise KeyError(name)
        return self._by_lower[lower]

    def __iter__(self) -> Iterator[str]:
        return iter(self._by_lower)

    def __len__(self) -> int:
        return len(self._by_lower)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        return name.lower() in self._by_lower

    def getlist(self, name: str) -> list[str]:
        """Return all values for `name` (case-insensitive) in arrival order."""
        return list(self._list_by_lower.get(name.lower(), ()))

    def raw_items(self) -> list[tuple[str, str]]:
        """Return all `(name, value)` header pairs in arrival order."""
        return list(self._items)


class Request:
    def __init__(
        self,
        scope: dict[str, Any],
        receive: Any,
        *,
        max_body_size: int = DEFAULT_MAX_BODY_SIZE,
    ) -> None:
        self.scope = scope
        self._receive = receive
        self.max_body_size = max_body_size

        self._headers: RequestHeaders | None = None
        self._query_params: dict[str, str] | None = None
        self._query_param_list: dict[str, list[str]] | None = None
        self._body: bytes | None = None
        self._json: Any = None
        self._json_parsed: bool = False

        self.state: dict[str, Any] = {}

    @property
    def method(self) -> str:
        return cast(str, self.scope["method"])

    @property
    def path(self) -> str:
        return cast(str, self.scope["path"])

    @property
    def headers(self) -> RequestHeaders:
        if self._headers is None:
            self._headers = RequestHeaders(self.scope.get("headers", []))
        return self._headers

    @property
    def query_param_list(self) -> dict[str, list[str]]:
        """Parsed dictionary of URL query string parameters with all values as lists (cached)."""
        if self._query_param_list is None:
            raw_qs: bytes = self.scope.get("query_string", b"")
            if not raw_qs:
                self._query_param_list = {}
            else:
                decoded = raw_qs.decode("utf-8", errors="replace")
                self._query_param_list = parse_qs(
                    decoded,
                    keep_blank_values=True,
                    encoding="utf-8",
                    errors="replace",
                )
        return self._query_param_list

    @property
    def query_params(self) -> dict[str, str]:
        """Parsed dictionary of URL query string parameters (first value for each key, cached)."""
        if self._query_params is None:
            self._query_params = {
                k: v[0] for k, v in self.query_param_list.items() if v
            }
        return self._query_params

    async def body(self, *, max_size: int | None = None) -> bytes:
        limit = max_size if max_size is not None else self.max_body_size

        content_length_raw = self.headers.get("content-length")
        if content_length_raw is not None:
            try:
                content_length = int(content_length_raw)
            except ValueError:
                raise HttpException(
                    "Invalid Content-Length header", status_code=400
                ) from None
            if content_length < 0:
                raise HttpException(
                    "Invalid Content-Length header", status_code=400
                )
            if limit is not None and content_length > limit:
                raise PayloadTooLargeError(
                    f"Request body exceeds maximum size of {limit} bytes"
                )

        if self._body is None:
            chunks: list[bytes] = []
            total_bytes = 0

            while True:
                message = await self._receive()
                msg_type = message.get("type")

                if msg_type == "http.disconnect":
                    raise ClientDisconnect(
                        "Client disconnected before request body was completely received"
                    )

                chunk: bytes = message.get("body", b"")
                total_bytes += len(chunk)
                if limit is not None and total_bytes > limit:
                    raise PayloadTooLargeError(
                        f"Request body exceeds maximum size of {limit} bytes"
                    )

                chunks.append(chunk)

                if not message.get("more_body", False):
                    break

            self._body = b"".join(chunks)
        elif limit is not None and len(self._body) > limit:
            raise PayloadTooLargeError(
                f"Request body exceeds maximum size of {limit} bytes"
            )

        return self._body

    async def json(self) -> Any:
        if not self._json_parsed:
            raw_bytes = await self.body()
            if not raw_bytes:
                self._json = {}
            else:
                try:
                    raw_text = raw_bytes.decode("utf-8")
                    self._json = json.loads(raw_text) if raw_text else {}
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    RecursionError,
                    ValueError,
                ):
                    raise HttpException(
                        "Invalid JSON payload", status_code=400
                    ) from None
            self._json_parsed = True

        return self._json
