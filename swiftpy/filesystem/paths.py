from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from urllib.parse import unquote

from swiftpy.filesystem.exceptions import PathTraversalError


class PathNormalizer:
    """Security-focused path normalization and collision-resistant name generation."""

    _SUSPICIOUS_ENCODED = re.compile(r"%2e|%2f|%5c", re.IGNORECASE)
    _SAFE_EXT = re.compile(r"^[a-zA-Z0-9]+$")
    _WINDOWS_DRIVE = re.compile(r"^[a-zA-Z]:(?:[/\\]|$)", re.IGNORECASE)

    @classmethod
    def normalize(cls, path: str) -> str:
        """
        Normalize a relative storage path.
        Strips leading/trailing slashes, normalizes separators to '/',
        and rejects path traversal attempts.
        """
        if "\x00" in path:
            raise PathTraversalError("Null byte detected in storage path.")

        # Reject Windows drive letters (e.g., C:\, C:/, D:)
        if cls._WINDOWS_DRIVE.match(path):
            raise PathTraversalError(f"Windows drive letter detected in storage path: '{path}'.")

        # Check for URL-encoded and double-encoded traversal indicators
        decoded = unquote(path)
        double_decoded = unquote(decoded)
        if "\x00" in decoded or "\x00" in double_decoded:
            raise PathTraversalError("Null byte detected in storage path.")

        if ".." in decoded or ".." in double_decoded:
            raise PathTraversalError(f"Encoded path traversal sequence detected in '{path}'.")

        if (cls._SUSPICIOUS_ENCODED.search(path) or cls._SUSPICIOUS_ENCODED.search(decoded)) and (
            "/" in decoded or "\\" in decoded or "/" in double_decoded or "\\" in double_decoded
        ):
            raise PathTraversalError("Encoded path traversal characters detected.")

        clean = path.replace("\\", "/")
        parts = [p for p in clean.split("/") if p and p != "."]

        for part in parts:
            if part == "..":
                raise PathTraversalError(f"Path traversal sequence '..' detected in '{path}'.")
            if re.match(r"^[a-zA-Z]:$", part, re.IGNORECASE):
                raise PathTraversalError(f"Windows drive letter detected in storage path: '{path}'.")

        return "/".join(parts)

    @classmethod
    def resolve_safe_path(cls, root: str | Path, path: str) -> Path:
        """
        Resolve a relative path against a storage root and strictly ensure
        it does not escape the root via symlinks or relative references.
        """
        clean_rel = cls.normalize(path)
        root_path = Path(root).resolve()
        target = (root_path / clean_rel).resolve()

        if target != root_path and not target.is_relative_to(root_path):
            raise PathTraversalError(
                f"Storage path '{path}' escapes configured root '{root}'."
            )
        return target

    @classmethod
    def generate_name(
        cls,
        original_filename: str | None = None,
        prefix: str = "",
        extension: str | None = None,
    ) -> str:
        """
        Generate a cryptographically random, collision-resistant storage filename.
        Preserves or overrides a sanitized extension.
        """
        random_hex = uuid.uuid4().hex
        ext = extension

        if ext is None and original_filename:
            sanitized = original_filename.replace("\x00", "")
            _, raw_ext = os.path.splitext(sanitized)
            candidate = raw_ext.lstrip(".").lower()
            if cls._SAFE_EXT.match(candidate):
                ext = candidate

        filename = f"{prefix}{random_hex}"
        if ext:
            filename = f"{filename}.{ext}"
        return filename

