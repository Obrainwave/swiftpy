from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, BinaryIO

from swiftpy.core.container import get_container
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.filesystem.drivers.base import StorageDriver
from swiftpy.filesystem.events import FileDeleted, FileMoved, FileUploaded
from swiftpy.filesystem.exceptions import ReadOnlyFileSystemError
from swiftpy.filesystem.metadata import FileMetadata
from swiftpy.filesystem.paths import PathNormalizer
from swiftpy.filesystem.visibility import Visibility
from swiftpy.http.response import StreamingResponse

if TYPE_CHECKING:
    from swiftpy.filesystem.uploads.uploaded_file import UploadedFile


class StorageDisk:
    """
    Public API wrapper for a configured storage disk.
    Applies path normalization, read-only guards, prefix scoping,
    and storage lifecycle events.
    """

    def __init__(
        self,
        name: str,
        driver: StorageDriver,
        *,
        read_only: bool = False,
        prefix: str = "",
    ) -> None:
        self.name = name
        self.driver = driver
        self.read_only = read_only
        self.prefix = PathNormalizer.normalize(prefix) if prefix else ""

    def _apply_prefix(self, path: str) -> str:
        clean = PathNormalizer.normalize(path)
        if not self.prefix:
            return clean
        if not clean:
            return self.prefix
        return f"{self.prefix}/{clean}"

    def _strip_prefix(self, path: str) -> str:
        clean = PathNormalizer.normalize(path)
        if not self.prefix:
            return clean
        if clean == self.prefix:
            return ""
        if clean.startswith(f"{self.prefix}/"):
            return clean[len(self.prefix) + 1 :]
        return clean

    def _check_writable(self) -> None:
        if self.read_only:
            raise ReadOnlyFileSystemError(
                f"Storage disk '{self.name}' is configured as read-only."
            )

    async def _dispatch_event(self, event_instance: Any) -> None:
        try:
            container = get_container()
            if container.has(EventDispatcher):
                dispatcher = container.resolve(EventDispatcher)
                event_name = getattr(
                    event_instance, "EVENT_NAME", event_instance.__class__.__name__
                )
                await dispatcher.dispatch(event_name, event_instance)
        except (RuntimeError, KeyError, AttributeError):
            # Events should not abort underlying storage operation if dispatcher not bound
            pass

    async def put(
        self,
        path: str,
        content: bytes | str,
        visibility: Visibility | str | None = None,
    ) -> int:
        self._check_writable()
        target = self._apply_prefix(path)
        bytes_written = await self.driver.write(target, content, visibility=visibility)

        clean_path = PathNormalizer.normalize(path)
        mime = await self.driver.mime_type(target)
        await self._dispatch_event(
            FileUploaded(
                disk=self.name,
                path=clean_path,
                size=bytes_written,
                mime_type=mime,
            )
        )
        return bytes_written

    async def put_file(
        self,
        directory: str,
        file: UploadedFile,
        visibility: Visibility | str | None = None,
    ) -> str:
        name = PathNormalizer.generate_name(file.filename)
        return await self.put_file_as(directory, file, name, visibility=visibility)

    async def put_file_as(
        self,
        directory: str,
        file: UploadedFile,
        name: str,
        visibility: Visibility | str | None = None,
    ) -> str:
        self._check_writable()
        clean_dir = PathNormalizer.normalize(directory) if directory else ""
        clean_name = PathNormalizer.normalize(name)
        relative_path = f"{clean_dir}/{clean_name}" if clean_dir else clean_name

        target = self._apply_prefix(relative_path)
        await file.seek(0)
        bytes_written = await self.driver.write_stream(
            target, file.chunks(), visibility=visibility
        )

        mime = file.content_type or await self.driver.mime_type(target)
        await self._dispatch_event(
            FileUploaded(
                disk=self.name,
                path=relative_path,
                size=bytes_written,
                mime_type=mime,
            )
        )
        return relative_path

    async def write_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes] | BinaryIO,
        chunk_size: int = 64 * 1024,
        visibility: Visibility | str | None = None,
    ) -> int:
        self._check_writable()
        target = self._apply_prefix(path)
        bytes_written = await self.driver.write_stream(
            target, stream, chunk_size=chunk_size, visibility=visibility
        )
        clean_path = PathNormalizer.normalize(path)
        mime = await self.driver.mime_type(target)
        await self._dispatch_event(
            FileUploaded(
                disk=self.name,
                path=clean_path,
                size=bytes_written,
                mime_type=mime,
            )
        )
        return bytes_written

    async def get(self, path: str) -> bytes:
        target = self._apply_prefix(path)
        return await self.driver.read(target)

    async def get_text(self, path: str, encoding: str = "utf-8") -> str:
        target = self._apply_prefix(path)
        return await self.driver.read_text(target, encoding=encoding)

    async def read(self, path: str) -> bytes:
        """Alias for get()."""
        return await self.get(path)

    async def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Alias for get_text()."""
        return await self.get_text(path, encoding=encoding)

    def read_stream(
        self,
        path: str,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        target = self._apply_prefix(path)
        return self.driver.read_stream(target, chunk_size=chunk_size)

    def stream(
        self,
        path: str,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        """Alias for read_stream."""
        return self.read_stream(path, chunk_size=chunk_size)

    async def exists(self, path: str) -> bool:
        target = self._apply_prefix(path)
        return await self.driver.exists(target)

    async def missing(self, path: str) -> bool:
        target = self._apply_prefix(path)
        return await self.driver.missing(target)

    async def delete(self, path: str) -> bool:
        self._check_writable()
        target = self._apply_prefix(path)
        deleted = await self.driver.delete(target)
        if deleted:
            clean_path = PathNormalizer.normalize(path)
            await self._dispatch_event(FileDeleted(disk=self.name, path=clean_path))
        return deleted

    async def copy(self, source: str, destination: str) -> bool:
        self._check_writable()
        src = self._apply_prefix(source)
        dst = self._apply_prefix(destination)
        copied = await self.driver.copy(src, dst)
        if copied:
            size = await self.driver.size(dst)
            mime = await self.driver.mime_type(dst)
            await self._dispatch_event(
                FileUploaded(
                    disk=self.name,
                    path=PathNormalizer.normalize(destination),
                    size=size,
                    mime_type=mime,
                )
            )
        return copied

    async def move(self, source: str, destination: str) -> bool:
        self._check_writable()
        src = self._apply_prefix(source)
        dst = self._apply_prefix(destination)
        moved = await self.driver.move(src, dst)
        if moved:
            clean_src = PathNormalizer.normalize(source)
            clean_dst = PathNormalizer.normalize(destination)
            await self._dispatch_event(
                FileMoved(disk=self.name, source=clean_src, destination=clean_dst)
            )
        return moved

    async def size(self, path: str) -> int:
        target = self._apply_prefix(path)
        return await self.driver.size(target)

    async def mime_type(self, path: str) -> str:
        target = self._apply_prefix(path)
        return await self.driver.mime_type(target)

    async def last_modified(self, path: str) -> datetime:
        target = self._apply_prefix(path)
        return await self.driver.last_modified(target)

    async def checksum(self, path: str, algorithm: str = "sha256") -> str:
        target = self._apply_prefix(path)
        return await self.driver.checksum(target, algorithm=algorithm)

    async def files(self, directory: str = "", recursive: bool = False) -> list[str]:
        target = self._apply_prefix(directory)
        raw_files = await self.driver.files(target, recursive=recursive)
        return [self._strip_prefix(f) for f in raw_files]

    async def directories(self, directory: str = "", recursive: bool = False) -> list[str]:
        target = self._apply_prefix(directory)
        raw_dirs = await self.driver.directories(target, recursive=recursive)
        return [self._strip_prefix(d) for d in raw_dirs]

    async def make_directory(self, directory: str) -> bool:
        self._check_writable()
        target = self._apply_prefix(directory)
        return await self.driver.make_directory(target)

    async def delete_directory(self, directory: str) -> bool:
        self._check_writable()
        target = self._apply_prefix(directory)
        return await self.driver.delete_directory(target)

    async def url(self, path: str) -> str:
        target = self._apply_prefix(path)
        return await self.driver.url(target)

    async def temporary_url(
        self,
        path: str,
        expires_in: int | timedelta,
    ) -> str:
        target = self._apply_prefix(path)
        return await self.driver.temporary_url(target, expires_in=expires_in)

    async def get_visibility(self, path: str) -> Visibility:
        target = self._apply_prefix(path)
        return await self.driver.get_visibility(target)

    async def set_visibility(self, path: str, visibility: Visibility | str) -> bool:
        self._check_writable()
        target = self._apply_prefix(path)
        return await self.driver.set_visibility(target, visibility=visibility)

    async def metadata(self, path: str) -> FileMetadata:
        target = self._apply_prefix(path)
        meta = await self.driver.metadata(target)
        return FileMetadata(
            path=self._strip_prefix(meta.path),
            size=meta.size,
            mime_type=meta.mime_type,
            last_modified=meta.last_modified,
            visibility=meta.visibility,
            etag=meta.etag,
            checksum=meta.checksum,
            extra=meta.extra,
        )

    async def download(
        self,
        path: str,
        name: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> StreamingResponse:
        """
        Generate a StreamingResponse for downloading the file.
        Sets Content-Disposition attachment and detected Content-Type.
        """
        clean_path = PathNormalizer.normalize(path)
        filename = name or os.path.basename(clean_path)
        mime = await self.mime_type(path)
        file_size = await self.size(path)

        resp_headers: dict[str, str] = {
            "content-type": mime,
            "content-disposition": f'attachment; filename="{filename}"',
            "content-length": str(file_size),
        }
        if headers:
            resp_headers.update(headers)

        return StreamingResponse(
            iterator=self.read_stream(path),
            status_code=200,
            headers=resp_headers,
        )
