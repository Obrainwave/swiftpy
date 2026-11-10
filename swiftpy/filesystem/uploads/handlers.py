from __future__ import annotations

import asyncio
import os
import re
import tempfile
from collections.abc import AsyncIterator, Callable
from typing import Any

from swiftpy.filesystem.exceptions import (
    FileTooLargeError,
    MalformedMultipartError,
)
from swiftpy.filesystem.uploads.uploaded_file import UploadedFile

_HEADER_SPLIT = b"\r\n\r\n"
_LINE_SPLIT = b"\r\n"


class FormData:
    """Container for parsed multipart form fields and uploaded files."""

    def __init__(self) -> None:
        self.fields: dict[str, str] = {}
        self.fields_list: dict[str, list[str]] = {}
        self.files: dict[str, UploadedFile] = {}
        self.files_list: dict[str, list[UploadedFile]] = {}

    def add_field(self, name: str, value: str) -> None:
        self.fields[name] = value
        self.fields_list.setdefault(name, []).append(value)

    def add_file(self, name: str, file: UploadedFile) -> None:
        self.files[name] = file
        self.files_list.setdefault(name, []).append(file)

    def get(self, name: str, default: Any = None) -> Any:
        if name in self.fields:
            return self.fields[name]
        if name in self.files:
            return self.files[name]
        return default

    def get_file(self, name: str) -> UploadedFile | None:
        return self.files.get(name)

    def get_files(self, name: str) -> list[UploadedFile]:
        return list(self.files_list.get(name, []))

    def all_files(self) -> list[UploadedFile]:
        result: list[UploadedFile] = []
        for flist in self.files_list.values():
            result.extend(flist)
        return result

    async def close(self) -> None:
        """Close and discard all spooled uploaded files."""
        for flist in self.files_list.values():
            for f in flist:
                try:
                    await f.close()
                except OSError:
                    pass
        self.files.clear()
        self.files_list.clear()


class MultipartParser:
    """
    Streaming multipart/form-data parser.
    Enforces bounded memory via SpooledTemporaryFile and streaming size limits.
    """

    def __init__(
        self,
        boundary: bytes,
        *,
        max_file_size: int = 100 * 1024 * 1024,
        max_request_size: int = 200 * 1024 * 1024,
        memory_limit: int = 1024 * 1024,
    ) -> None:
        self.boundary = boundary
        self.delimiter = b"--" + boundary
        self.close_delimiter = b"--" + boundary + b"--"
        self.max_file_size = max_file_size
        self.max_request_size = max_request_size
        self.memory_limit = memory_limit

    @classmethod
    def extract_boundary(cls, content_type: str) -> bytes:
        match = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type, re.IGNORECASE)
        if not match:
            raise MalformedMultipartError("Missing or invalid multipart boundary in Content-Type header.")
        boundary_str = match.group(1) or match.group(2)
        return boundary_str.encode("latin-1")

    async def parse(
        self,
        stream: AsyncIterator[bytes] | Callable[[], Any],
    ) -> FormData:
        form_data = FormData()
        buffer = bytearray()
        total_request_bytes = 0

        # Current part state
        part_headers: dict[str, str] = {}
        part_name: str | None = None
        part_filename: str | None = None
        part_content_type: str = "application/octet-stream"
        part_spool: tempfile.SpooledTemporaryFile[bytes] | None = None
        part_field_value: bytearray | None = None
        part_file_bytes = 0

        async def get_next_chunk() -> bytes | None:
            nonlocal total_request_bytes
            chunk: bytes = b""
            if isinstance(stream, AsyncIterator):
                try:
                    chunk = await anext(stream)
                except StopAsyncIteration:
                    return None
            else:
                msg = await stream()
                if msg.get("type") == "http.disconnect":
                    return None
                chunk = msg.get("body", b"")
                if not chunk and not msg.get("more_body", False):
                    return None

            if chunk:
                total_request_bytes += len(chunk)
                if total_request_bytes > self.max_request_size:
                    raise FileTooLargeError(
                        f"Request payload exceeds maximum allowed size of {self.max_request_size} bytes."
                    )
            return chunk

        try:
            # Read until initial delimiter
            while self.delimiter not in buffer and self.close_delimiter not in buffer:
                chunk = await get_next_chunk()
                if chunk is None:
                    raise MalformedMultipartError("Stream ended before initial multipart boundary was found.")
                buffer.extend(chunk)

            init_idx = buffer.find(self.delimiter)
            buffer = buffer[init_idx + len(self.delimiter) :]

            if buffer.startswith(b"--"):
                return form_data

            if buffer.startswith(b"\r\n"):
                buffer = buffer[2:]

            boundary_marker = b"\r\n" + self.delimiter
            marker_len = len(boundary_marker)

            while True:
                # Read part headers
                while _HEADER_SPLIT not in buffer:
                    chunk = await get_next_chunk()
                    if chunk is None:
                        raise MalformedMultipartError("Unexpected end of stream while parsing part headers.")
                    buffer.extend(chunk)

                header_end = buffer.find(_HEADER_SPLIT)
                raw_headers = buffer[:header_end]
                buffer = buffer[header_end + len(_HEADER_SPLIT) :]

                part_headers.clear()
                for line in raw_headers.split(_LINE_SPLIT):
                    if not line:
                        continue
                    colon_idx = line.find(b":")
                    if colon_idx != -1:
                        h_name = line[:colon_idx].decode("latin-1").strip().lower()
                        h_val = line[colon_idx + 1 :].decode("latin-1").strip()
                        part_headers[h_name] = h_val

                cd = part_headers.get("content-disposition", "")
                part_name = None
                part_filename = None
                part_content_type = part_headers.get("content-type", "text/plain")

                name_m = re.search(r'name="([^"]+)"', cd)
                if name_m:
                    part_name = name_m.group(1)

                filename_m = re.search(r'filename="([^"]+)"', cd)
                if filename_m:
                    part_filename = os.path.basename(filename_m.group(1))

                if part_filename is not None:
                    part_spool = tempfile.SpooledTemporaryFile(max_size=self.memory_limit)  # noqa: SIM115
                    part_file_bytes = 0
                else:
                    part_field_value = bytearray()

                # Read body of current part until boundary_marker
                while True:
                    delim_idx = buffer.find(boundary_marker)
                    if delim_idx != -1:
                        # Found delimiter
                        final_chunk = bytes(buffer[:delim_idx])
                        buffer = buffer[delim_idx + marker_len :]

                        if part_spool is not None:
                            part_file_bytes += len(final_chunk)
                            if part_file_bytes > self.max_file_size:
                                raise FileTooLargeError(
                                    f"Uploaded file '{part_filename}' exceeds maximum allowed size of {self.max_file_size} bytes."
                                )
                            await asyncio.to_thread(part_spool.write, final_chunk)
                            await asyncio.to_thread(part_spool.seek, 0)
                            up_file = UploadedFile(
                                file=part_spool,
                                filename=part_filename or "unnamed",
                                content_type=part_content_type,
                                size=part_file_bytes,
                            )
                            if part_name:
                                form_data.add_file(part_name, up_file)
                            part_spool = None
                        elif part_field_value is not None:
                            part_field_value.extend(final_chunk)
                            val = part_field_value.decode("utf-8", errors="replace")
                            if part_name:
                                form_data.add_field(part_name, val)
                            part_field_value = None
                        break
                    else:
                        # Flush safe portion to spool to keep memory bounded and enforce limits mid-stream
                        if len(buffer) > marker_len:
                            safe_len = len(buffer) - marker_len
                            safe_chunk = bytes(buffer[:safe_len])
                            buffer = buffer[safe_len:]

                            if part_spool is not None:
                                part_file_bytes += len(safe_chunk)
                                if part_file_bytes > self.max_file_size:
                                    raise FileTooLargeError(
                                        f"Uploaded file '{part_filename}' exceeds maximum allowed size of {self.max_file_size} bytes."
                                    )
                                await asyncio.to_thread(part_spool.write, safe_chunk)
                            elif part_field_value is not None:
                                part_field_value.extend(safe_chunk)

                        chunk = await get_next_chunk()
                        if chunk is None:
                            raise MalformedMultipartError("Unexpected end of stream: missing closing multipart boundary.")
                        buffer.extend(chunk)

                # Check if closing boundary
                if buffer.startswith(b"--"):
                    break

                if buffer.startswith(b"\r\n"):
                    buffer = buffer[2:]

            return form_data
        except Exception:
            await form_data.close()
            if part_spool is not None:
                try:
                    await asyncio.to_thread(part_spool.close)
                except OSError:
                    pass
            raise
