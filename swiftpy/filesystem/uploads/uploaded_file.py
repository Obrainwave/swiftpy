from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swiftpy.filesystem.manager import FilesystemManager
from swiftpy.filesystem.paths import PathNormalizer
from swiftpy.filesystem.uploads.validators import FileValidator

if TYPE_CHECKING:
    from swiftpy.filesystem.disk import StorageDisk


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
        self._content_type = content_type
        self._size = size
        self._extension = os.path.splitext(filename)[1].lstrip(".").lower()

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

    async def save(self, destination: str | Path) -> Path:
        """Save the uploaded file directly to a local filesystem path."""
        dest_path = Path(destination).resolve()
        await asyncio.to_thread(dest_path.parent.mkdir, parents=True, exist_ok=True)
        await self.seek(0)

        def _write() -> None:
            with open(dest_path, "wb") as out:
                shutil.copyfileobj(self._file, out)

        await asyncio.to_thread(_write)
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

    async def validate(self, rules: dict[str, Any] | Sequence[Any]) -> None:
        """Validate the upload against FileValidator rules."""
        validator = FileValidator(rules)
        await validator.validate(self)

    async def close(self) -> None:
        """Close and discard the spooled temporary file."""
        await asyncio.to_thread(self._file.close)
