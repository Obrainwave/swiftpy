from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from swiftpy.filesystem.exceptions import (
    FileTooLargeError,
    MalformedMultipartError,
)
from swiftpy.filesystem.uploads.handlers import MultipartParser


@pytest.mark.asyncio
async def test_parse_multipart_stream() -> None:
    boundary = b"----WebKitFormBoundary7MA4YWxkTrZu0gW"
    content_type = f"multipart/form-data; boundary={boundary.decode()}"

    extracted_boundary = MultipartParser.extract_boundary(content_type)
    assert extracted_boundary == boundary

    body_payload = (
        b"------WebKitFormBoundary7MA4YWxkTrZu0gW\r\n"
        b'Content-Disposition: form-data; name="username"\r\n\r\n'
        b"john_doe\r\n"
        b"------WebKitFormBoundary7MA4YWxkTrZu0gW\r\n"
        b'Content-Disposition: form-data; name="avatar"; filename="avatar.png"\r\n'
        b"Content-Type: image/png\r\n\r\n"
        b"\x89PNG\r\n\x1a\nfake_image_bytes\r\n"
        b"------WebKitFormBoundary7MA4YWxkTrZu0gW--\r\n"
    )

    async def stream_generator() -> AsyncIterator[bytes]:
        chunk_size = 32
        for i in range(0, len(body_payload), chunk_size):
            yield body_payload[i : i + chunk_size]

    parser = MultipartParser(boundary)
    form_data = await parser.parse(stream_generator())

    try:
        assert form_data.get("username") == "john_doe"

        avatar = form_data.get_file("avatar")
        assert avatar is not None
        assert avatar.filename == "avatar.png"
        assert avatar.content_type == "image/png"
        assert avatar.extension == "png"
        assert await avatar.read() == b"\x89PNG\r\n\x1a\nfake_image_bytes"
    finally:
        await form_data.close()


@pytest.mark.asyncio
async def test_multipart_enforces_request_size_limit() -> None:
    boundary = b"----WebKitBoundary"
    body_payload = (
        b"------WebKitBoundary\r\n"
        b'Content-Disposition: form-data; name="data"\r\n\r\n'
        b"large_payload_exceeding_limit\r\n"
        b"------WebKitBoundary--\r\n"
    )

    async def stream_gen() -> AsyncIterator[bytes]:
        yield body_payload

    parser = MultipartParser(boundary, max_request_size=20)
    with pytest.raises(FileTooLargeError):
        await parser.parse(stream_gen())


@pytest.mark.asyncio
async def test_multipart_enforces_file_size_limit() -> None:
    boundary = b"----WebKitBoundary"
    body_payload = (
        b"------WebKitBoundary\r\n"
        b'Content-Disposition: form-data; name="doc"; filename="doc.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
        b"1234567890\r\n"
        b"------WebKitBoundary--\r\n"
    )

    async def stream_gen() -> AsyncIterator[bytes]:
        yield body_payload

    parser = MultipartParser(boundary, max_file_size=5)
    with pytest.raises(FileTooLargeError):
        await parser.parse(stream_gen())


@pytest.mark.asyncio
async def test_multipart_streaming_rejection_midstream_prevents_unlimited_consumption() -> None:
    """Verify that an oversized file upload is rejected mid-stream without consuming infinite data."""
    boundary = b"----StreamBoundary"
    header = (
        b"------StreamBoundary\r\n"
        b'Content-Disposition: form-data; name="video"; filename="large.mp4"\r\n'
        b"Content-Type: video/mp4\r\n\r\n"
    )

    chunks_yielded = 0

    async def infinite_stream() -> AsyncIterator[bytes]:
        nonlocal chunks_yielded
        yield header
        chunks_yielded += 1
        # Stream 100 KB chunks endlessly
        chunk = b"V" * 1024 * 100
        for _ in range(1000):
            chunks_yielded += 1
            yield chunk

    # Limit file size to 250 KB
    parser = MultipartParser(boundary, max_file_size=250 * 1024)

    with pytest.raises(FileTooLargeError):
        await parser.parse(infinite_stream())

    # Ensure parser aborted early: yielded chunks should be <= 5, not 1000!
    assert chunks_yielded <= 5


@pytest.mark.asyncio
async def test_multipart_missing_boundary_raises_malformed() -> None:
    with pytest.raises(MalformedMultipartError):
        MultipartParser.extract_boundary("multipart/form-data; charset=utf-8")


@pytest.mark.asyncio
async def test_multipart_truncated_body_raises_malformed() -> None:
    boundary = b"----TruncatedBoundary"
    truncated = (
        b"------TruncatedBoundary\r\n"
        b'Content-Disposition: form-data; name="avatar"; filename="photo.jpg"\r\n'
        b"Content-Type: image/jpeg\r\n\r\n"
        b"partial_bytes_without_closing_boundary"
    )

    async def stream_gen() -> AsyncIterator[bytes]:
        yield truncated

    parser = MultipartParser(boundary)
    with pytest.raises(MalformedMultipartError):
        await parser.parse(stream_gen())
