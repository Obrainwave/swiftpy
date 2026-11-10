from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any, cast
from urllib.parse import parse_qs

from swiftpy.filesystem.uploads.handlers import FormData, MultipartParser
from swiftpy.filesystem.uploads.uploaded_file import UploadedFile
from swiftpy.http.exceptions import (
    ClientDisconnect,
    HttpException,
    PayloadTooLargeError,
)

DEFAULT_MAX_BODY_SIZE: int = 2 * 1024 * 1024  # 2 MiB
DEFAULT_MAX_CONTENT_LENGTH: int = 2 * 1024 * 1024  # 2 MiB
DEFAULT_MAX_JSON_DEPTH: int = 32


def _validate_json_depth(
    obj: Any,
    current_depth: int = 1,
    max_depth: int = DEFAULT_MAX_JSON_DEPTH,
) -> None:
    """Validate that nested JSON structures do not exceed maximum nesting depth."""
    if current_depth > max_depth:
        raise HttpException(
            f"JSON payload exceeds maximum allowed depth of {max_depth}",
            status_code=400,
        )
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _validate_json_depth(v, current_depth + 1, max_depth)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _validate_json_depth(item, current_depth + 1, max_depth)


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
        max_content_length: int | None = None,
        max_json_depth: int = DEFAULT_MAX_JSON_DEPTH,
    ) -> None:
        self.scope = scope
        self._receive = receive
        self.max_body_size = max_body_size
        self.max_content_length = max_content_length
        self.max_json_depth = max_json_depth

        self._headers: RequestHeaders | None = None
        self._query_params: dict[str, str] | None = None
        self._query_param_list: dict[str, list[str]] | None = None
        self._body: bytes | None = None
        self._json: Any = None
        self._json_parsed: bool = False
        self._form: FormData | None = None

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
            cl_limit = (
                self.max_content_length
                if self.max_content_length is not None
                else limit
            )
            if cl_limit is not None and content_length > cl_limit:
                raise PayloadTooLargeError(
                    f"Request content length exceeds maximum size of {cl_limit} bytes"
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
                _validate_json_depth(self._json, max_depth=self.max_json_depth)
            self._json_parsed = True

        return self._json

    async def form(self) -> FormData:
        if self._form is None:
            content_type = self.headers.get("content-type", "")
            if "multipart/form-data" in content_type.lower():
                boundary = MultipartParser.extract_boundary(content_type)
                limit = self.max_body_size
                parser = MultipartParser(
                    boundary,
                    max_request_size=limit if limit is not None else 200 * 1024 * 1024,
                )
                if self._body is not None:
                    async def _body_stream(raw: bytes) -> AsyncIterator[bytes]:
                        chunk_size = 64 * 1024
                        for i in range(0, len(raw), chunk_size):
                            yield raw[i : i + chunk_size]

                    self._form = await parser.parse(_body_stream(self._body))
                else:
                    self._form = await parser.parse(self._receive)
            else:
                raw_bytes = await self.body()
                form_data = FormData()
                if raw_bytes:
                    decoded = raw_bytes.decode("utf-8", errors="replace")
                    parsed = parse_qs(decoded, keep_blank_values=True)
                    for k, vals in parsed.items():
                        for v in vals:
                            form_data.add_field(k, v)
                self._form = form_data
        return self._form

    async def file(self, name: str) -> UploadedFile | None:
        form_data = await self.form()
        return form_data.get_file(name)

    async def files(
        self, name: str | None = None
    ) -> list[UploadedFile] | dict[str, list[UploadedFile]]:
        form_data = await self.form()
        if name is not None:
            return form_data.get_files(name)
        return dict(form_data.files_list)

    async def close(self) -> None:
        if self._form is not None:
            await self._form.close()
            self._form = None
