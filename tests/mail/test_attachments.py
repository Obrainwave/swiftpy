from __future__ import annotations

import io
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from swiftpy.mail.attachments import Attachment
from swiftpy.mail.exceptions import AttachmentNotFoundError, HeaderInjectionError


@pytest.mark.asyncio
async def test_attachment_from_bytes() -> None:
    att = Attachment.from_bytes(b"hello world", filename="hello.txt")
    assert att.filename == "hello.txt"
    assert att.content_type == "text/plain"
    assert att.disposition == "attachment"
    assert att.size == 11
    data = await att.read_bytes()
    assert data == b"hello world"


@pytest.mark.asyncio
async def test_attachment_from_path(tmp_path: Path) -> None:
    file_path = tmp_path / "document.pdf"
    file_path.write_bytes(b"%PDF-1.4 sample content")

    att = Attachment.from_path(file_path)
    assert att.filename == "document.pdf"
    assert att.content_type == "application/pdf"
    assert att.disposition == "attachment"

    data = await att.read_bytes()
    assert data == b"%PDF-1.4 sample content"


@pytest.mark.asyncio
async def test_attachment_from_path_missing() -> None:
    att = Attachment.from_path("non_existent_file_xyz_123.bin")
    with pytest.raises(AttachmentNotFoundError):
        await att.read_bytes()


@pytest.mark.asyncio
async def test_attachment_inline_from_bytes() -> None:
    att = Attachment.inline_from_bytes(
        b"fake png", cid="header_logo", filename="logo.png"
    )
    assert att.cid == "header_logo"
    assert att.disposition == "inline"
    assert att.content_type == "image/png"
    data = await att.read_bytes()
    assert data == b"fake png"


@pytest.mark.asyncio
async def test_attachment_inline_from_path(tmp_path: Path) -> None:
    file_path = tmp_path / "logo.png"
    file_path.write_bytes(b"png bytes")
    att = Attachment.inline_from_path(file_path, cid="logo")
    assert att.cid == "logo"
    assert att.disposition == "inline"
    data = await att.read_bytes()
    assert data == b"png bytes"


@pytest.mark.asyncio
async def test_attachment_from_stream() -> None:
    bio = io.BytesIO(b"streaming bytes payload")
    att = Attachment.from_stream(bio, filename="stream.dat")
    data = await att.read_bytes()
    assert data == b"streaming bytes payload"


@pytest.mark.asyncio
async def test_attachment_from_async_iterator() -> None:
    async def _async_gen() -> AsyncIterator[bytes]:
        yield b"chunk1"
        yield b"chunk2"

    att = Attachment.from_stream(_async_gen(), filename="async.dat")
    data = await att.read_bytes()
    assert data == b"chunk1chunk2"


def test_attachment_filename_sanitization() -> None:
    att = Attachment.from_bytes(b"content", filename="../../../etc/passwd")
    assert att.filename == "passwd"

    with pytest.raises(HeaderInjectionError):
        Attachment.from_bytes(b"content", filename="test.txt\r\nBcc: evil")

    with pytest.raises(HeaderInjectionError):
        Attachment.inline_from_bytes(b"content", cid="logo\r\n", filename="logo.png")
