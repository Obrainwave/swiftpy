from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from swiftpy.filesystem.exceptions import (
    FileTooLargeError,
    FileValidationError,
    InvalidFileExtensionError,
    InvalidMimeTypeError,
)

if TYPE_CHECKING:
    from swiftpy.filesystem.uploads.uploaded_file import UploadedFile

_SIZE_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-zA-Z]+)?$")

_SIZE_MULTIPLIERS = {
    "b": 1,
    "bytes": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024 * 1024,
    "mb": 1024 * 1024,
    "mib": 1024 * 1024,
    "g": 1024 * 1024 * 1024,
    "gb": 1024 * 1024 * 1024,
    "gib": 1024 * 1024 * 1024,
}

MAGIC_BYTE_SIGNATURES: dict[str, list[bytes]] = {
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/jpg": [b"\xff\xd8\xff"],
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/gif": [b"GIF87a", b"GIF89a"],
    "application/pdf": [b"%PDF-"],
    "application/zip": [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"],
    "application/x-zip-compressed": [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"],
}


def parse_size_to_bytes(size: int | str) -> int:
    """Parse integer or string size (e.g. '5MB', '500KB') into integer bytes."""
    if isinstance(size, int):
        return size
    match = _SIZE_PATTERN.match(size.strip())
    if not match:
        raise ValueError(f"Invalid size string: '{size}'")
    value = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    mult = _SIZE_MULTIPLIERS.get(unit)
    if mult is None:
        raise ValueError(f"Unsupported size unit: '{unit}' in '{size}'")
    return int(value * mult)


def verify_magic_bytes(header: bytes, expected_mime: str) -> bool:
    """Verify if header bytes match known magic signatures for expected_mime."""
    mime = expected_mime.lower()
    if mime == "image/webp":
        return len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP"

    signatures = MAGIC_BYTE_SIGNATURES.get(mime)
    if not signatures:
        return True  # If no signature known for MIME, pass through

    if not header:
        return False

    for sig in signatures:
        if header.startswith(sig):
            return True
    return False


class FileValidator:
    """Upload validation engine enforcing size, extension, MIME, and magic byte constraints."""

    def __init__(self, rules: dict[str, Any] | Sequence[Any]) -> None:
        self.rules: dict[str, Any] = dict(rules) if isinstance(rules, dict) else {}

    async def validate(self, file: UploadedFile, field_name: str = "file") -> None:
        errors: list[str] = []

        # Derive actual real byte size from underlying file to prevent metadata spoofing
        real_size = await file.get_real_size()

        max_size_raw = self.rules.get("max_size")
        if max_size_raw is not None:
            max_bytes = parse_size_to_bytes(max_size_raw)
            if file.size > max_bytes or real_size > max_bytes:
                errors.append(f"The {field_name} must not be greater than {max_size_raw}.")

        min_size_raw = self.rules.get("min_size")
        if min_size_raw is not None:
            min_bytes = parse_size_to_bytes(min_size_raw)
            if file.size < min_bytes or real_size < min_bytes:
                errors.append(f"The {field_name} must not be less than {min_size_raw}.")

        extensions = self.rules.get("extensions")
        if extensions is not None:
            ext_list = [str(e).lstrip(".").lower() for e in extensions]
            if file.extension.lower() not in ext_list:
                allowed_str = ", ".join(ext_list)
                errors.append(f"The {field_name} must be a file of type: {allowed_str}.")

        mimes = self.rules.get("mimes")
        if mimes is not None:
            mime_list = [str(m).lower() for m in mimes]
            if file.content_type.lower() not in mime_list:
                allowed_str = ", ".join(mime_list)
                errors.append(f"The {field_name} must have a MIME type of: {allowed_str}.")

        magic_bytes_check = self.rules.get("magic_bytes", False)
        if magic_bytes_check:
            await file.seek(0)
            header = await file.read(32)
            await file.seek(0)
            if not verify_magic_bytes(header, file.content_type):
                errors.append(f"The {field_name} content does not match its declared type '{file.content_type}'.")

        if errors:
            if any("greater than" in e for e in errors):
                raise FileTooLargeError(errors[0])
            if any("file of type" in e for e in errors):
                raise InvalidFileExtensionError(errors[0])
            if any("MIME type" in e for e in errors):
                raise InvalidMimeTypeError(errors[0])
            raise FileValidationError(errors={field_name: errors}, message=errors[0])
