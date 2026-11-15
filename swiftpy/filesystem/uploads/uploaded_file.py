from __future__ import annotations

import asyncio
import os
import posixpath
import shutil
import tempfile
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swiftpy.filesystem.exceptions import PathTraversalError
from swiftpy.filesystem.manager import FilesystemManager
from swiftpy.filesystem.paths import PathNormalizer
from swiftpy.filesystem.uploads.validators import FileValidator

if TYPE_CHECKING:
    from swiftpy.filesystem.disk import StorageDisk


def _sanitize_uploaded_filename(filename: str) -> str:
    """Normalize '/' and '\\' separators, extract basename, and reject dot/null traversal."""
    if "\x00" in filename:
        raise PathTraversalError("Invalid null byte in uploaded filename.")
    normalized = filename.replace("\\", "/")
    cleaned = posixpath.basename(normalized).strip("\r\n")
    if cleaned in {".", ".."}:
        raise PathTraversalError(f"Path traversal detected in filename '{filename}'.")
    return cleaned or "unnamed"


def _write_spooled_file_sync(
    spool: tempfile.SpooledTemporaryFile[bytes],
    dest_path: Path,
) -> None:
    """Synchronously copy a spooled upload to disk (executed in a worker thread)."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as out:
        shutil.copyfileobj(spool, out)


def _resolve_contained_destination(
    destination: str | Path,
    filename: str,
    base_dir: str | Path | None = None,
) -> Path:
    """Resolve destination path while rejecting null bytes, traversal segments, and escapes."""
    dest_raw = str(destination)
    if "\x00" in dest_raw:
        raise PathTraversalError("Invalid null byte in upload save destination.")

    raw_parts = dest_raw.replace("\\", "/").split("/")
    if ".." in raw_parts:
        raise PathTraversalError(
            f"Path traversal segment '..' not allowed in destination '{destination}'."
        )

    candidate = Path(destination)
    if base_dir is not None:
        base_raw = str(base_dir)
        if "\x00" in base_raw:
            raise PathTraversalError("Invalid null byte in base directory.")
        base_resolved = Path(base_dir).resolve()
        target_resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (base_resolved / candidate).resolve()
        )
        if not target_resolved.is_relative_to(base_resolved) or target_resolved == base_resolved:
            raise PathTraversalError(
                f"Save destination '{destination}' escapes target directory '{base_dir}'."
            )
        raw_check = candidate if candidate.is_absolute() else (base_resolved / candidate)
        if raw_check.is_symlink():
            raise PathTraversalError(
                f"Save destination '{destination}' cannot target a symlink."
            )
        return target_resolved

    resolved = candidate.resolve()
    if not candidate.is_absolute():
        cwd = Path.cwd().resolve()
        if not resolved.is_relative_to(cwd):
            raise PathTraversalError(
                f"Relative save destination '{destination}' escapes working directory."
            )
    if candidate.is_symlink():
        raise PathTraversalError(f"Save destination '{destination}' cannot target a symlink.")

    if resolved.exists() and resolved.is_dir():
        safe_name = _sanitize_uploaded_filename(filename)
        joined = (resolved / safe_name).resolve()
        if not joined.is_relative_to(resolved) or joined == resolved:
            raise PathTraversalError(
                f"Save filename '{filename}' escapes target directory '{destination}'."
            )
        return joined

    return resolved


class UploadedFile:
    """
    Represents an uploaded file received via an HTTP multipart request.
    Backed by a SpooledTemporaryFile with bounded memory.
    """

    def __init__(
        self,
        file: tempfile.SpooledTemporaryFile[bytes],
        filename: str,
        content_type: str,
        size: int,
    ) -> None:
        self._file = file
        self._filename = filename
        self._filename = _sanitize_uploaded_filename(filename) if filename else ""
        self._content_type = content_type
        self._size = size
        self._extension = os.path.splitext(filename)[1].lstrip(".").lower()
        self._extension = os.path.splitext(self._filename)[1].lstrip(".").lower()

    @property
    def filename(self) -> str:
        """Original client-supplied filename (sanitized)."""
        return self._filename

    @property
    def content_type(self) -> str:
        """Content-Type declared in the HTTP multipart header."""
        return self._content_type

    @property
    def size(self) -> int:
        """Total size of the upload in bytes."""
        return self._size

    @property
    def extension(self) -> str:
        """Extracted file extension."""
        return self._extension

    async def get_real_size(self) -> int:
        """
        Return the actual byte size of the underlying spooled file,
        preventing client metadata spoofing.
        """
        pos = await self.seek(0, os.SEEK_END)
        await self.seek(0)
        return pos

    async def read(self, size: int = -1) -> bytes:
        """Read bytes asynchronously from the spooled file."""
        return await asyncio.to_thread(self._file.read, size)

    async def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        """Seek asynchronously within the spooled file."""
        return await asyncio.to_thread(self._file.seek, offset, whence)

    async def chunks(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        """Stream the file content in chunks asynchronously."""
        await self.seek(0)
        while True:
            chunk = await asyncio.to_thread(self._file.read, chunk_size)
            if not chunk:
                break
            yield chunk

    async def save(
        self,
        destination: str | Path,
        base_dir: str | Path | None = None,
    ) -> Path:
        """
        Save the uploaded file directly to a local filesystem path while enforcing
        traversal and target-directory containment checks.
        """
        dest_path = _resolve_contained_destination(
            destination,
            filename=self._filename,
            base_dir=base_dir,
        )
        await self.seek(0)
        await asyncio.to_thread(_write_spooled_file_sync, self._file, dest_path)
        return dest_path

    async def store(self, directory: str = "", disk: str | None = None) -> str:
        """
        Store the uploaded file on the given disk with a generated random name.
        Returns the resulting storage path.
        """
        name = PathNormalizer.generate_name(self.filename)
        return await self.store_as(directory, name, disk=disk)

    async def store_as(
        self,
        directory: str,
        filename: str,
        disk: str | None = None,
    ) -> str:
        """
        Store the uploaded file on the given disk with an explicit filename.
        Returns the resulting storage path.
        """
        clean_dir = PathNormalizer.normalize(directory) if directory else ""
        clean_name = PathNormalizer.normalize(filename)
        full_path = f"{clean_dir}/{clean_name}" if clean_dir else clean_name

        await self.seek(0)
        manager = FilesystemManager.get_instance()
        target_disk: StorageDisk = manager.disk(disk)
        await target_disk.write_stream(full_path, self.chunks())
        return full_path

    async def validate(self, rules: Mapping[str, Any]) -> None:
        """Validate the upload against FileValidator rules."""
        validator = FileValidator(rules)
        await validator.validate(self)

    async def close(self) -> None:
        """Close and discard the spooled temporary file."""
        await asyncio.to_thread(self._file.close)
