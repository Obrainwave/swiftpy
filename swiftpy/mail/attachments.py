from __future__ import annotations

import asyncio
import mimetypes
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from swiftpy.mail.exceptions import (
    AttachmentNotFoundError,
    AttachmentTooLargeError,
    HeaderInjectionError,
    MailAttachmentError,
)

_CRLF_PATTERN = re.compile(r"[\r\n\0]")


def _sanitize_filename(name: str) -> str:
    """Sanitize filename to prevent header injection or directory traversal in headers."""
    if _CRLF_PATTERN.search(name):
        raise HeaderInjectionError(
            f"Attachment filename '{name}' contains illegal CRLF or null bytes"
        )
    # Take basename only to prevent directory traversal in Content-Disposition
    cleaned = Path(name).name.strip()
    return cleaned or "attachment"


@dataclass(frozen=True, slots=True)
class Attachment:
    """Represents an email attachment or inline embedded resource."""

    filename: str
    content_type: str
    disposition: str = "attachment"  # 'attachment' or 'inline'
    cid: str | None = None
    path: Path | None = None
    content: bytes | None = None
    stream: AsyncIterator[bytes] | BinaryIO | None = None
    size: int | None = None
    _cache: list[bytes] = field(default_factory=list, repr=False, compare=False)

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> Attachment:
        """Create an attachment referencing a file on the filesystem."""
        p = Path(path).resolve()
        name = _sanitize_filename(filename or p.name)
        detected_type = content_type or cls._detect_mime(name)
        return cls(
            filename=name,
            content_type=detected_type,
            disposition="attachment",
            path=p,
        )

    @classmethod
    def from_bytes(
        cls,
        content: bytes,
        filename: str,
        content_type: str | None = None,
    ) -> Attachment:
        """Create an attachment from in-memory bytes."""
        name = _sanitize_filename(filename)
        detected_type = content_type or cls._detect_mime(name)
        return cls(
            filename=name,
            content_type=detected_type,
            disposition="attachment",
            content=content,
            size=len(content),
        )

    @classmethod
    def from_stream(
        cls,
        stream: AsyncIterator[bytes] | BinaryIO,
        filename: str,
        content_type: str | None = None,
        size: int | None = None,
    ) -> Attachment:
        """Create an attachment from an async byte iterator or file-like binary stream."""
        name = _sanitize_filename(filename)
        detected_type = content_type or cls._detect_mime(name)
        return cls(
            filename=name,
            content_type=detected_type,
            disposition="attachment",
            stream=stream,
            size=size,
        )

    @classmethod
    def inline_from_path(
        cls,
        path: str | Path,
        cid: str,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> Attachment:
        """Create an inline attachment embedded within HTML via cid:."""
        if _CRLF_PATTERN.search(cid):
            raise HeaderInjectionError(
                "Attachment Content-ID contains illegal CRLF or null bytes"
            )
        p = Path(path).resolve()
        name = _sanitize_filename(filename or p.name)
        detected_type = content_type or cls._detect_mime(name)
        return cls(
            filename=name,
            content_type=detected_type,
            disposition="inline",
            cid=cid.strip("<> "),
            path=p,
        )

    @classmethod
    def inline_from_bytes(
        cls,
        content: bytes,
        cid: str,
        filename: str,
        content_type: str | None = None,
    ) -> Attachment:
        """Create an inline attachment from bytes embedded within HTML via cid:."""
        if _CRLF_PATTERN.search(cid):
            raise HeaderInjectionError(
                "Attachment Content-ID contains illegal CRLF or null bytes"
            )
        name = _sanitize_filename(filename)
        detected_type = content_type or cls._detect_mime(name)
        return cls(
            filename=name,
            content_type=detected_type,
            disposition="inline",
            cid=cid.strip("<> "),
            content=content,
            size=len(content),
        )

    async def read_bytes(self, max_size: int | None = None) -> bytes:
        """Read attachment bytes asynchronously without blocking the event loop."""
        if self._cache:
            data = self._cache[0]
            if max_size is not None and len(data) > max_size:
                raise AttachmentTooLargeError(
                    f"Attachment '{self.filename}' ({len(data)} bytes) exceeds size limit ({max_size} bytes)"
                )
            return data

        if self.content is not None:
            data = self.content
        elif self.path is not None:

            def _sync_read(p: Path) -> bytes:
                try:
                    if not p.is_file():
                        raise AttachmentNotFoundError(
                            f"Attachment file not found at '{p}'"
                        )
                    return p.read_bytes()
                except (FileNotFoundError, AttachmentNotFoundError) as exc:
                    raise AttachmentNotFoundError(
                        f"Attachment file not found at '{p}'"
                    ) from exc
                except PermissionError as exc:
                    raise MailAttachmentError(
                        f"Permission denied reading attachment at '{p}': {exc}"
                    ) from exc
                except Exception as exc:
                    raise MailAttachmentError(
                        f"Error reading attachment file at '{p}': {exc}"
                    ) from exc

            data = await asyncio.to_thread(_sync_read, self.path)
        elif self.stream is not None:
            stream_obj = self.stream
            try:
                if isinstance(stream_obj, AsyncIterator):
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in stream_obj:
                        chunks.append(chunk)
                        total += len(chunk)
                        if max_size is not None and total > max_size:
                            raise AttachmentTooLargeError(
                                f"Attachment '{self.filename}' exceeds stream size limit ({max_size} bytes)"
                            )
                    data = b"".join(chunks)
                else:
                    bio: BinaryIO = stream_obj

                    def _sync_read_bio() -> bytes:
                        if hasattr(bio, "seek"):
                            bio.seek(0)
                        return bio.read()

                    data = await asyncio.to_thread(_sync_read_bio)
            except AttachmentTooLargeError:
                raise
            except Exception as exc:
                raise MailAttachmentError(
                    f"Failed to read attachment stream for '{self.filename}': {exc}"
                ) from exc
        else:
            raise ValueError("Attachment has no readable content source")

        if max_size is not None and len(data) > max_size:
            raise AttachmentTooLargeError(
                f"Attachment '{self.filename}' ({len(data)} bytes) exceeds size limit ({max_size} bytes)"
            )

        self._cache.append(data)
        return data

    @staticmethod
    def _detect_mime(filename: str) -> str:
        mime, _ = mimetypes.guess_type(filename)
        return mime or "application/octet-stream"


__all__ = ["Attachment"]
