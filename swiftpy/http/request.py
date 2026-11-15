from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any, cast
from urllib.parse import parse_qs

from swiftpy.core.config import Config
from swiftpy.core.container import get_container
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
    """Validate iteratively that nested JSON structures do not exceed maximum nesting depth."""
    stack: list[tuple[Any, int]] = [(obj, current_depth)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            raise HttpException(
                f"JSON payload exceeds maximum allowed depth of {max_depth}",
                status_code=400,
            )
        if isinstance(node, dict):
            next_depth = depth + 1
            for val in node.values():
                if isinstance(val, (dict, list)):
                    stack.append((val, next_depth))
        elif isinstance(node, list):
            next_depth = depth + 1
            for item in node:
                if isinstance(item, (dict, list)):
                    stack.append((item, next_depth))


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

        content_length_values = self.headers.getlist("content-length")
        if content_length_values:
            parsed_lengths: list[int] = []
            for raw_cl in content_length_values:
                try:
                    parsed_cl = int(raw_cl)
                except ValueError:
                    raise HttpException(
                        "Invalid Content-Length header", status_code=400
                    ) from None
                if parsed_cl < 0:
                    raise HttpException(
                        "Invalid Content-Length header", status_code=400
                    )
                parsed_lengths.append(parsed_cl)
            if len(content_length_values) > 1 and (
                len(set(parsed_lengths)) > 1
                or len({val.strip() for val in content_length_values}) > 1
            ):
                raise HttpException(
                    "Conflicting Content-Length headers", status_code=400
                )
            content_length = parsed_lengths[0]
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

    def _resolve_upload_limits(self) -> tuple[int, int, int, int, str | None]:
        """Resolve upload parser limits from filesystems.uploads config and Request overrides."""
        default_max_file = 100 * 1024 * 1024
        default_max_req = 200 * 1024 * 1024
        default_mem_limit = 1024 * 1024

        uploads_cfg: Mapping[str, Any] | None = None
        try:
            container = get_container()
            if container.has(Config):
                cfg = container.resolve(Config)
                raw_cfg = cfg.get("filesystems.uploads")
                if isinstance(raw_cfg, Mapping):
                    uploads_cfg = raw_cfg
        except RuntimeError:
            uploads_cfg = None

        max_file_size = default_max_file
        memory_limit = default_mem_limit
        max_field_size = default_mem_limit
        temp_dir: str | None = None
        cfg_max_request: int | None = None

        if uploads_cfg is not None:
            raw_file_limit = uploads_cfg.get("max_file_size")
            if isinstance(raw_file_limit, int) and not isinstance(raw_file_limit, bool) and raw_file_limit > 0:
                max_file_size = raw_file_limit
            raw_mem = uploads_cfg.get("memory_limit")
            if isinstance(raw_mem, int) and not isinstance(raw_mem, bool) and raw_mem > 0:
                memory_limit = raw_mem
                max_field_size = memory_limit
            raw_field_limit = uploads_cfg.get("max_field_size")
            if isinstance(raw_field_limit, int) and not isinstance(raw_field_limit, bool) and raw_field_limit > 0:
                max_field_size = raw_field_limit
            raw_req_limit = uploads_cfg.get("max_request_size")
            if isinstance(raw_req_limit, int) and not isinstance(raw_req_limit, bool) and raw_req_limit > 0:
                cfg_max_request = raw_req_limit
            raw_temp_dir = uploads_cfg.get("temp_dir")
            if isinstance(raw_temp_dir, str) and raw_temp_dir.strip():
                temp_dir = raw_temp_dir

        if self.max_body_size != DEFAULT_MAX_BODY_SIZE and self.max_body_size is not None:
            max_request_size = (
                min(self.max_body_size, cfg_max_request)
                if cfg_max_request is not None
                else self.max_body_size
            )
        elif cfg_max_request is not None:
            max_request_size = cfg_max_request
        else:
            max_request_size = (
                self.max_body_size if self.max_body_size is not None else default_max_req
            )

        return max_file_size, max_request_size, memory_limit, max_field_size, temp_dir

    async def form(self) -> FormData:
        if self._form is None:
            content_type = self.headers.get("content-type", "")
            if "multipart/form-data" in content_type.lower():
                boundary = MultipartParser.extract_boundary(content_type)
                (
                    max_file_size,
                    max_request_size,
                    memory_limit,
                    max_field_size,
                    temp_dir,
                ) = self._resolve_upload_limits()
                parser = MultipartParser(
                    boundary,
                    max_file_size=max_file_size,
                    max_request_size=max_request_size,
                    memory_limit=memory_limit,
                    max_field_size=max_field_size,
                    temp_dir=temp_dir,
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
