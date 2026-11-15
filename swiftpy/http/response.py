from __future__ import annotations

import inspect
import json
from collections.abc import (
    AsyncIterator,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID


@runtime_checkable
class _SupportsKeysAndGetItem(Protocol):
    def keys(self) -> Iterable[str]: ...
    def __getitem__(self, key: str, /) -> str: ...


def _validate_header_pair(name: str, value: str) -> tuple[str, str]:
    if not isinstance(name, str) or not isinstance(value, str):
        raise TypeError("Header names and values must be strings.")
    if not name or not name.strip():
        raise ValueError("Header name must not be empty.")
    if "\r" in name or "\n" in name or "\x00" in name:
        raise ValueError(
            "Header names and values must not contain CR or LF characters."
        )
    if "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError(
            "Header names and values must not contain CR or LF characters."
        )
    return name, value


class ResponseHeaders(MutableMapping[str, str]):
    """
    Case-insensitive HTTP header container backed by an ordered list of (name, value) pairs.
    Supports dict-style access while preserving duplicate headers such as Set-Cookie via .add() / .getlist().
    """

    def __init__(
        self,
        headers: (
            Mapping[str, str]
            | Sequence[tuple[str, str]]
            | ResponseHeaders
            | None
        ) = None,
    ) -> None:
        self._items: list[tuple[str, str]] = []
        if headers is not None:
            if isinstance(headers, ResponseHeaders):
                self._items = list(headers._items)
            elif isinstance(headers, Mapping):
                for k, v in headers.items():
                    self[k] = v
            else:
                for k, v in headers:
                    self.add(k, v)

    def add(self, name: str, value: str) -> None:
        """Append a header pair without removing existing headers of the same name (e.g. Set-Cookie)."""
        safe_name, safe_val = _validate_header_pair(name, value)
        self._items.append((safe_name, safe_val))

    def append(self, name: str, value: str) -> None:
        """Alias for add()."""
        self.add(name, value)

    def getlist(self, name: str) -> list[str]:
        """Return all values for a header name (case-insensitive)."""
        target = name.lower()
        return [v for k, v in self._items if k.lower() == target]

    def raw_items(self) -> list[tuple[str, str]]:
        """Return all (name, value) pairs in order, including repeated headers."""
        return list(self._items)

    def __getitem__(self, name: str) -> str:
        target = name.lower()
        for k, v in reversed(self._items):
            if k.lower() == target:
                return v
        raise KeyError(name)

    def __setitem__(self, name: str, value: str) -> None:
        safe_name, safe_val = _validate_header_pair(name, value)
        target = safe_name.lower()
        replaced = False
        new_items: list[tuple[str, str]] = []
        for k, v in self._items:
            if k.lower() == target:
                if not replaced:
                    new_items.append((safe_name, safe_val))
                    replaced = True
            else:
                new_items.append((k, v))
        if not replaced:
            new_items.append((safe_name, safe_val))
        self._items = new_items

    def __delitem__(self, name: str) -> None:
        target = name.lower()
        new_items = [(k, v) for k, v in self._items if k.lower() != target]
        if len(new_items) == len(self._items):
            raise KeyError(name)
        self._items = new_items

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        target = name.lower()
        return any(k.lower() == target for k, _ in self._items)

    def __iter__(self) -> Iterator[str]:
        seen: set[str] = set()
        for k, _ in self._items:
            low = k.lower()
            if low not in seen:
                seen.add(low)
                yield k

    def __len__(self) -> int:
        return len({k.lower() for k, _ in self._items})

    def update(
        self,
        other: _SupportsKeysAndGetItem | Iterable[tuple[str, str]] | None = None,
        /,
        **kwargs: str,
    ) -> None:
        if other is not None:
            if isinstance(other, ResponseHeaders):
                keys_to_replace = {k.lower() for k, _ in other._items}
                self._items = [
                    (k, v)
                    for k, v in self._items
                    if k.lower() not in keys_to_replace
                ]
                for k, v in other._items:
                    self.add(k, v)
            elif isinstance(other, Mapping):
                for k, v in other.items():
                    self[k] = v
            elif isinstance(other, _SupportsKeysAndGetItem):
                keys = other.keys()
                for k in keys:
                    self[k] = other[k]
            else:
                pairs = list(other)
                keys_to_replace = {k.lower() for k, _ in pairs}
                self._items = [
                    (k, v)
                    for k, v in self._items
                    if k.lower() not in keys_to_replace
                ]
                for k, v in pairs:
                    self.add(k, v)
        for k, v in kwargs.items():
            self[k] = v

    def __repr__(self) -> str:
        return f"ResponseHeaders({self._items!r})"


def _json_default(obj: Any) -> Any:
    """Default JSON serializer for framework response payloads."""
    if isinstance(obj, (bytes, bytearray, memoryview)):
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    if isinstance(obj, (UUID, Decimal)):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        raw_dict = obj.to_dict()
        if isinstance(raw_dict, dict):
            hidden = set(getattr(obj, "__hidden__", ()) or ())
            if hidden:
                return {k: v for k, v in raw_dict.items() if k not in hidden}
            return raw_dict
        return raw_dict
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def encode_json(data: Any) -> bytes:
    """Serialize data to UTF-8 JSON bytes using SwiftPY's strict encoder."""
    return json.dumps(data, default=_json_default, allow_nan=False).encode("utf-8")


class Response:
    def __init__(
        self,
        content: bytes | str = b"",
        status_code: int = 200,
        headers: (
            Mapping[str, str]
            | Sequence[tuple[str, str]]
            | ResponseHeaders
            | None
        ) = None,
        *,
        _set_content_length: bool = True,
    ) -> None:
        is_str = False
        if isinstance(content, str):
            is_str = True
            self.content: bytes = content.encode("utf-8")
        elif isinstance(content, (bytes, bytearray, memoryview)):
            self.content = bytes(content)
        else:
            raise TypeError(
                f"Response content must be bytes or str, got {type(content).__name__}"
            )

        self.status_code = status_code
        self.headers = ResponseHeaders(headers)

        if is_str and "content-type" not in self.headers:
            self.headers["content-type"] = "text/plain; charset=utf-8"

        if (
            _set_content_length
            and "content-length" not in self.headers
            and self.status_code not in (204, 304)
            and not (100 <= self.status_code < 200)
        ):
            self.headers["content-length"] = str(len(self.content))

    def set_cookie(self, cookie_value: str) -> None:
        """Append a Set-Cookie header without overwriting previously added cookies."""
        self.headers.add("Set-Cookie", cookie_value)

    async def send(self, send: Any, *, send_body: bool = True) -> None:
        body_bytes = (
            self.content.encode("utf-8")
            if isinstance(self.content, str)
            else bytes(self.content)
        )
        if (
            "content-length" not in self.headers
            and self.status_code not in (204, 304)
            and not (100 <= self.status_code < 200)
        ):
            self.headers["content-length"] = str(len(body_bytes))

        raw_headers = [
            (
                _validate_header_pair(k, v)[0].encode("latin-1"),
                v.encode("latin-1"),
            )
            for k, v in self.headers.raw_items()
        ]

        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": raw_headers,
            }
        )

        await send(
            {
                "type": "http.response.body",
                "body": body_bytes if send_body else b"",
            }
        )


class StreamingResponse(Response):
    def __init__(
        self,
        iterator: AsyncIterator[bytes | str],
        status_code: int = 200,
        headers: (
            Mapping[str, str]
            | Sequence[tuple[str, str]]
            | ResponseHeaders
            | None
        ) = None,
    ) -> None:
        super().__init__(
            content=b"",
            status_code=status_code,
            headers=headers,
            _set_content_length=False,
        )

        self.iterator = iterator

    async def send(self, send: Any, *, send_body: bool = True) -> None:
        raw_headers = [
            (
                _validate_header_pair(key, value)[0].encode("latin-1"),
                value.encode("latin-1"),
            )
            for key, value in self.headers.raw_items()
        ]

        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": raw_headers,
            }
        )

        try:
            if send_body:
                async for chunk in self.iterator:
                    chunk_bytes = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
                    await send(
                        {
                            "type": "http.response.body",
                            "body": chunk_bytes,
                            "more_body": True,
                        }
                    )

            await send(
                {
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": False,
                }
            )
        finally:
            aclose = getattr(self.iterator, "aclose", None)
            if callable(aclose):
                close_res = aclose()
                if inspect.isawaitable(close_res):
                    await close_res


def json_response(
    data: Any,
    status_code: int = 200,
    headers: (
        Mapping[str, str]
        | Sequence[tuple[str, str]]
        | ResponseHeaders
        | None
    ) = None,
) -> Response:
    resp_headers = ResponseHeaders({"content-type": "application/json"})
    if headers is not None:
        resp_headers.update(headers)
    return Response(
        content=encode_json(data),
        status_code=status_code,
        headers=resp_headers,
    )


def created(data: Any) -> Response:
    return json_response(
        data,
        status_code=201,
    )


def not_found(message: str = "Not Found") -> Response:
    return json_response(
        {"detail": message},
        status_code=404,
    )


def unprocessable(
    errors: Any,
) -> Response:
    return json_response(
        {"errors": errors},
        status_code=422,
    )


def unauthorized(
    message: str = "Unauthorized",
    headers: (
        Mapping[str, str]
        | Sequence[tuple[str, str]]
        | ResponseHeaders
        | None
    ) = None,
) -> Response:
    resp_headers = ResponseHeaders(
        {"content-type": "application/json", "WWW-Authenticate": "Bearer"}
    )
    if headers is not None:
        resp_headers.update(headers)
    return Response(
        content=encode_json({"detail": message}),
        status_code=401,
        headers=resp_headers,
    )


def forbidden(message: str = "Forbidden") -> Response:
    return json_response(
        {"detail": message},
        status_code=403,
    )


def stream(
    iterator: AsyncIterator[bytes | str],
    *,
    status_code: int = 200,
    content_type: str = "application/octet-stream",
) -> StreamingResponse:
    return StreamingResponse(
        iterator=iterator,
        status_code=status_code,
        headers={
            "content-type": content_type,
        },
    )


__all__ = [
    "Response",
    "ResponseHeaders",
    "StreamingResponse",
    "created",
    "encode_json",
    "forbidden",
    "json_response",
    "not_found",
    "stream",
    "unauthorized",
    "unprocessable",
]
