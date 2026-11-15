from __future__ import annotations

import re
from collections.abc import Mapping
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

_EXTENSION_MIME_MAP: dict[str, tuple[str, ...]] = {
    "jpg": ("image/jpeg", "image/jpg"),
    "jpeg": ("image/jpeg", "image/jpg"),
    "png": ("image/png",),
    "gif": ("image/gif",),
    "webp": ("image/webp",),
    "pdf": ("application/pdf",),
    "zip": ("application/zip", "application/x-zip-compressed"),
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
    """
    Verify if header bytes match known magic signatures for expected_mime.
    Fails closed (returns False) when expected_mime has no known signature.
    """
    mime = expected_mime.split(";", 1)[0].strip().lower()
    if not header:
        return False

    if mime == "image/webp":
        return len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP"

    signatures = MAGIC_BYTE_SIGNATURES.get(mime)
    if not signatures:
        return False

    return any(header.startswith(sig) for sig in signatures)


class FileValidator:
    """Upload validation engine enforcing size, extension, MIME, and magic byte constraints."""

    def __init__(self, rules: Mapping[str, Any]) -> None:
        if not isinstance(rules, Mapping):
            raise TypeError(
                f"FileValidator rules must be a mapping, got {type(rules).__name__}."
            )
        self.rules: dict[str, Any] = dict(rules)

    def _resolve_magic_byte_candidates(
        self,
        file: UploadedFile,
        allowed_extensions: list[str] | None,
        allowed_mimes: list[str] | None,
    ) -> list[str]:
        """Resolve authoritative MIME signature candidates derived from server policy."""
        file_ext = file.extension.lower()
        declared_mime = file.content_type.split(";", 1)[0].strip().lower()
        ext_mimes = list(_EXTENSION_MIME_MAP.get(file_ext, ()))

        if allowed_extensions is not None and allowed_mimes is not None:
            if not ext_mimes:
                return []
            return [m for m in ext_mimes if m in allowed_mimes]

        if allowed_extensions is not None:
            return ext_mimes

        if allowed_mimes is not None:
            if ext_mimes:
                return [m for m in ext_mimes if m in allowed_mimes]
            return allowed_mimes

        if ext_mimes:
            return ext_mimes
        return [declared_mime]

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
        ext_list: list[str] | None = None
        if extensions is not None:
            ext_list = [str(e).lstrip(".").lower() for e in extensions]
            if file.extension.lower() not in ext_list:
                allowed_str = ", ".join(ext_list)
                errors.append(f"The {field_name} must be a file of type: {allowed_str}.")

        mimes = self.rules.get("mimes")
        mime_list: list[str] | None = None
        if mimes is not None:
            mime_list = [str(m).split(";", 1)[0].strip().lower() for m in mimes]
            declared_mime = file.content_type.split(";", 1)[0].strip().lower()
            if declared_mime not in mime_list:
                allowed_str = ", ".join(mime_list)
                errors.append(f"The {field_name} must have a MIME type of: {allowed_str}.")

        magic_bytes_check = self.rules.get("magic_bytes", False)
        if magic_bytes_check:
            await file.seek(0)
            header = await file.read(32)
            await file.seek(0)
            candidates = self._resolve_magic_byte_candidates(file, ext_list, mime_list)
            declared_mime = file.content_type.split(";", 1)[0].strip().lower()
            matched_policy = bool(candidates) and any(
                verify_magic_bytes(header, mime) for mime in candidates
            )
            if ext_list is None and mime_list is None:
                matched_policy = matched_policy and verify_magic_bytes(header, declared_mime)
            if not matched_policy:
                errors.append(
                    f"The {field_name} content does not match an expected file signature."
                )

        if errors:
            error_map = {field_name: errors}
            if any("greater than" in e for e in errors):
                raise FileTooLargeError(errors[0])
            if any("file of type" in e for e in errors):
                raise InvalidFileExtensionError(error_map, message=errors[0])
            if any("MIME type" in e for e in errors):
                raise InvalidMimeTypeError(error_map, message=errors[0])
            raise FileValidationError(errors=error_map, message=errors[0])
