from __future__ import annotations

import asyncio
import mimetypes
import posixpath
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from swiftpy.mail.exceptions import (
    AttachmentNotFoundError,
    AttachmentTooLargeError,
    HeaderInjectionError,
    MailAttachmentError,
)

_CRLF_PATTERN = re.compile(r"[\r\n\0]")
_STREAM_CHUNK_SIZE = 65536


def _sanitize_filename(name: str) -> str:
    """Sanitize filename to prevent header injection, traversal, or quote escapes."""
    if _CRLF_PATTERN.search(name):
        raise HeaderInjectionError(
            f"Attachment filename '{name}' contains illegal CRLF or null bytes"
        )
    # Normalize Windows/POSIX separators so traversal segments never survive basename extraction
    cleaned = posixpath.basename(name.replace("\\", "/")).strip()
    if cleaned in ("", ".", ".."):
        return "attachment"
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


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
        """Read attachment bytes asynchronously with incremental size enforcement."""
        if self.content is not None:
            if max_size is not None and len(self.content) > max_size:
                raise AttachmentTooLargeError(
                    f"Attachment '{self.filename}' ({len(self.content)} bytes) exceeds size limit ({max_size} bytes)"
                )
            return self.content

        if self.path is not None:

            def _sync_read(p: Path) -> bytes:
                try:
                    if not p.is_file():
                        raise AttachmentNotFoundError(
                            f"Attachment file not found at '{p}'"
                        )
                    stat_size = p.stat().st_size
                    if max_size is not None and stat_size > max_size:
                        raise AttachmentTooLargeError(
                            f"Attachment '{self.filename}' ({stat_size} bytes) exceeds size limit ({max_size} bytes)"
                        )
                    chunks: list[bytes] = []
                    total = 0
                    with p.open("rb") as handle:
                        while True:
                            chunk = handle.read(_STREAM_CHUNK_SIZE)
                            if not chunk:
                                break
                            total += len(chunk)
                            if max_size is not None and total > max_size:
                                raise AttachmentTooLargeError(
                                    f"Attachment '{self.filename}' exceeds size limit ({max_size} bytes)"
                                )
                            chunks.append(chunk)
                    return b"".join(chunks)
                except (FileNotFoundError, AttachmentNotFoundError) as exc:
                    raise AttachmentNotFoundError(
                        f"Attachment file not found at '{p}'"
                    ) from exc
                except AttachmentTooLargeError:
                    raise
                except PermissionError as exc:
                    raise MailAttachmentError(
                        f"Permission denied reading attachment at '{p}': {exc}"
                    ) from exc
                except Exception as exc:
                    raise MailAttachmentError(
                        f"Error reading attachment file at '{p}': {exc}"
                    ) from exc

            return await asyncio.to_thread(_sync_read, self.path)

        if self.stream is not None:
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
                    return b"".join(chunks)

                bio: BinaryIO = stream_obj

                def _sync_read_bio() -> bytes:
                    if hasattr(bio, "seek"):
                        bio.seek(0)
                    bio_chunks: list[bytes] = []
                    bio_total = 0
                    while True:
                        chunk = bio.read(_STREAM_CHUNK_SIZE)
                        if not chunk:
                            break
                        bio_total += len(chunk)
                        if max_size is not None and bio_total > max_size:
                            raise AttachmentTooLargeError(
                                f"Attachment '{self.filename}' exceeds stream size limit ({max_size} bytes)"
                            )
                        bio_chunks.append(chunk)
                    return b"".join(bio_chunks)

                return await asyncio.to_thread(_sync_read_bio)
            except AttachmentTooLargeError:
                raise
            except Exception as exc:
                raise MailAttachmentError(
                    f"Failed to read attachment stream for '{self.filename}': {exc}"
                ) from exc

        raise ValueError("Attachment has no readable content source")

    @staticmethod
    def _detect_mime(filename: str) -> str:
        mime, _ = mimetypes.guess_type(filename)
        return mime or "application/octet-stream"


__all__ = ["Attachment"]
