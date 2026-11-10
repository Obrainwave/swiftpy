from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from swiftpy.filesystem.exceptions import (
    FileTooLargeError,
    FileValidationError,
    InvalidFileExtensionError,
    InvalidMimeTypeError,
)
from swiftpy.filesystem.uploads.uploaded_file import UploadedFile
from swiftpy.filesystem.uploads.validators import FileValidator


@asynccontextmanager
async def open_upload(
    content: bytes,
    filename: str,
    content_type: str,
    size: int | None = None,
) -> AsyncIterator[UploadedFile]:
    """Asynchronous context manager ensuring guaranteed cleanup of SpooledTemporaryFile."""
    spool = tempfile.SpooledTemporaryFile(max_size=max(len(content) + 10, 1024))  # noqa: SIM115
    spool.write(content)
    spool.seek(0)
    upload = UploadedFile(
        file=spool,
        filename=filename,
        content_type=content_type,
        size=size if size is not None else len(content),
    )
    try:
        yield upload
    finally:
        await upload.close()


@pytest.mark.asyncio
async def test_validator_max_size_guaranteed_cleanup() -> None:
    async with open_upload(b"x" * 200, "large.txt", "text/plain") as upload:
        validator_ok = FileValidator({"max_size": "1KB"})
        await validator_ok.validate(upload)

        validator_fail = FileValidator({"max_size": "100B"})
        with pytest.raises(FileTooLargeError):
            await validator_fail.validate(upload)


@pytest.mark.asyncio
async def test_validator_real_byte_size_prevents_spoofed_metadata() -> None:
    # Malicious upload claims to be 10 bytes, but actually contains 50,000 bytes
    payload = b"A" * 50000
    async with open_upload(payload, "exploit.bin", "application/octet-stream", size=10) as upload:
        assert upload.size == 10
        assert await upload.get_real_size() == 50000

        validator = FileValidator({"max_size": "10KB"})
        with pytest.raises(FileTooLargeError):
            await validator.validate(upload)


@pytest.mark.asyncio
async def test_validator_min_size_enforced() -> None:
    async with open_upload(b"tiny", "small.txt", "text/plain") as upload:
        validator_fail = FileValidator({"min_size": "1KB"})
        with pytest.raises(FileValidationError):
            await validator_fail.validate(upload)

        validator_ok = FileValidator({"min_size": "2B"})
        await validator_ok.validate(upload)


@pytest.mark.asyncio
async def test_validator_extensions() -> None:
    async with open_upload(b"data", "script.sh", "application/x-sh") as upload:
        validator = FileValidator({"extensions": ["jpg", "png", "pdf"]})
        with pytest.raises(InvalidFileExtensionError):
            await validator.validate(upload)


@pytest.mark.asyncio
async def test_validator_mimes() -> None:
    async with open_upload(b"data", "photo.png", "application/octet-stream") as upload:
        validator = FileValidator({"mimes": ["image/png", "image/jpeg"]})
        with pytest.raises(InvalidMimeTypeError):
            await validator.validate(upload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mime", "filename", "header_bytes"),
    [
        ("image/jpeg", "test.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"),
        ("image/png", "test.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"),
        ("image/gif", "test87.gif", b"GIF87a\x01\x00\x01\x00"),
        ("image/gif", "test89.gif", b"GIF89a\x01\x00\x01\x00"),
        ("image/webp", "test.webp", b"RIFF\x20\x00\x00\x00WEBPVP8 "),
        ("application/pdf", "test.pdf", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3"),
        ("application/zip", "test.zip", b"PK\x03\x04\x14\x00\x00\x00"),
    ],
)
async def test_validator_magic_bytes_valid_signatures(
    mime: str,
    filename: str,
    header_bytes: bytes,
) -> None:
    content = header_bytes + b"_trailing_sample_payload"
    async with open_upload(content, filename, mime) as upload:
        validator = FileValidator({"magic_bytes": True})
        await validator.validate(upload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mime", "filename"),
    [
        ("image/png", "empty.png"),
        ("image/jpeg", "empty.jpg"),
        ("image/gif", "empty.gif"),
        ("image/webp", "empty.webp"),
        ("application/pdf", "empty.pdf"),
        ("application/zip", "empty.zip"),
    ],
)
async def test_validator_magic_bytes_rejects_empty_file(mime: str, filename: str) -> None:
    async with open_upload(b"", filename, mime) as upload:
        validator = FileValidator({"magic_bytes": True})
        with pytest.raises(FileValidationError):
            await validator.validate(upload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mime", "filename", "short_bytes"),
    [
        ("image/png", "short.png", b"\x89PN"),
        ("image/jpeg", "short.jpg", b"\xff\xd8"),
        ("image/gif", "short.gif", b"GIF"),
        ("image/webp", "short.webp", b"RIFFshort"),
        ("application/pdf", "short.pdf", b"%PD"),
        ("application/zip", "short.zip", b"PK"),
    ],
)
async def test_validator_magic_bytes_rejects_truncated_signatures(
    mime: str,
    filename: str,
    short_bytes: bytes,
) -> None:
    async with open_upload(short_bytes, filename, mime) as upload:
        validator = FileValidator({"magic_bytes": True})
        with pytest.raises(FileValidationError):
            await validator.validate(upload)


@pytest.mark.asyncio
async def test_validator_magic_bytes_rejects_correct_ext_with_incorrect_content() -> None:
    # A .png extension containing plain text
    async with open_upload(b"Plain text payload pretending to be an image", "photo.png", "image/png") as upload:
        validator = FileValidator({"magic_bytes": True})
        with pytest.raises(FileValidationError):
            await validator.validate(upload)

    # A .pdf extension containing a JPEG signature
    async with open_upload(b"\xff\xd8\xff\xe0\x00\x10JFIF...", "document.pdf", "application/pdf") as upload:
        validator = FileValidator({"magic_bytes": True})
        with pytest.raises(FileValidationError):
            await validator.validate(upload)


@pytest.mark.asyncio
async def test_validator_magic_bytes_rejects_incorrect_ext_with_correct_content() -> None:
    # A .txt extension with valid PNG content but rule enforcing allowed extension
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    async with open_upload(png_bytes, "photo.txt", "image/png") as upload:
        validator = FileValidator({"extensions": ["png"], "magic_bytes": True})
        with pytest.raises(InvalidFileExtensionError):
            await validator.validate(upload)


@pytest.mark.asyncio
async def test_validator_magic_bytes_allows_unknown_formats_passthrough() -> None:
    # Custom binary format not in known magic byte dictionary
    custom_bytes = b"\xaa\xbb\xcc\xdd\xee\xff custom proprietary format"
    async with open_upload(custom_bytes, "data.xyz", "application/x-custom") as upload:
        validator = FileValidator({"magic_bytes": True})
        # Passes through because no magic signature is defined for application/x-custom
        await validator.validate(upload)
