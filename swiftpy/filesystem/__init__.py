from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from typing import Any, BinaryIO

from swiftpy.filesystem.disk import StorageDisk
from swiftpy.filesystem.drivers.base import StorageDriver
from swiftpy.filesystem.drivers.local import LocalStorageDriver
from swiftpy.filesystem.drivers.s3 import S3StorageDriver
from swiftpy.filesystem.manager import FilesystemManager
from swiftpy.filesystem.metadata import FileMetadata
from swiftpy.filesystem.paths import PathNormalizer
from swiftpy.filesystem.uploads.uploaded_file import UploadedFile
from swiftpy.filesystem.visibility import Visibility
from swiftpy.http.response import StreamingResponse


class Storage:
    """
    Class-level facade for SwiftPY Filesystem & Storage.
    Delegates all operations to the default or explicitly named StorageDisk.
    """

    @classmethod
    def _manager(cls) -> FilesystemManager:
        return FilesystemManager.get_instance()

    @classmethod
    def disk(cls, name: str | None = None) -> StorageDisk:
        """Resolve a configured storage disk instance."""
        return cls._manager().disk(name)

    @classmethod
    def extend(
        cls,
        driver_name: str,
        factory: Callable[[dict[str, Any]], StorageDriver],
    ) -> None:
        """Register a custom driver creator."""
        cls._manager().extend(driver_name, factory)

    @classmethod
    def build(cls, config: dict[str, Any], name: str = "on_demand") -> StorageDisk:
        """Build an on-demand, unmanaged storage disk."""
        return cls._manager().build(config, name=name)

    @classmethod
    async def put(
        cls,
        path: str,
        content: bytes | str,
        visibility: Visibility | str | None = None,
    ) -> int:
        return await cls.disk().put(path, content, visibility=visibility)

    @classmethod
    async def put_file(
        cls,
        directory: str,
        file: UploadedFile,
        visibility: Visibility | str | None = None,
    ) -> str:
        return await cls.disk().put_file(directory, file, visibility=visibility)

    @classmethod
    async def put_file_as(
        cls,
        directory: str,
        file: UploadedFile,
        name: str,
        visibility: Visibility | str | None = None,
    ) -> str:
        return await cls.disk().put_file_as(directory, file, name, visibility=visibility)

    @classmethod
    async def write_stream(
        cls,
        path: str,
        stream: AsyncIterator[bytes] | BinaryIO,
        chunk_size: int = 64 * 1024,
        visibility: Visibility | str | None = None,
    ) -> int:
        return await cls.disk().write_stream(
            path, stream, chunk_size=chunk_size, visibility=visibility
        )

    @classmethod
    async def get(cls, path: str) -> bytes:
        return await cls.disk().get(path)

    @classmethod
    async def get_text(cls, path: str, encoding: str = "utf-8") -> str:
        return await cls.disk().get_text(path, encoding=encoding)

    @classmethod
    async def read(cls, path: str) -> bytes:
        return await cls.disk().read(path)

    @classmethod
    async def read_text(cls, path: str, encoding: str = "utf-8") -> str:
        return await cls.disk().read_text(path, encoding=encoding)

    @classmethod
    def read_stream(
        cls,
        path: str,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        return cls.disk().read_stream(path, chunk_size=chunk_size)

    @classmethod
    def stream(
        cls,
        path: str,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        return cls.disk().stream(path, chunk_size=chunk_size)

    @classmethod
    async def exists(cls, path: str) -> bool:
        return await cls.disk().exists(path)

    @classmethod
    async def missing(cls, path: str) -> bool:
        return await cls.disk().missing(path)

    @classmethod
    async def delete(cls, path: str) -> bool:
        return await cls.disk().delete(path)

    @classmethod
    async def copy(cls, source: str, destination: str) -> bool:
        return await cls.disk().copy(source, destination)

    @classmethod
    async def move(cls, source: str, destination: str) -> bool:
        return await cls.disk().move(source, destination)

    @classmethod
    async def size(cls, path: str) -> int:
        return await cls.disk().size(path)

    @classmethod
    async def mime_type(cls, path: str) -> str:
        return await cls.disk().mime_type(path)

    @classmethod
    async def last_modified(cls, path: str) -> datetime:
        return await cls.disk().last_modified(path)

    @classmethod
    async def checksum(cls, path: str, algorithm: str = "sha256") -> str:
        return await cls.disk().checksum(path, algorithm=algorithm)

    @classmethod
    async def files(cls, directory: str = "", recursive: bool = False) -> list[str]:
        return await cls.disk().files(directory, recursive=recursive)

    @classmethod
    async def directories(cls, directory: str = "", recursive: bool = False) -> list[str]:
        return await cls.disk().directories(directory, recursive=recursive)

    @classmethod
    async def make_directory(cls, directory: str) -> bool:
        return await cls.disk().make_directory(directory)

    @classmethod
    async def delete_directory(cls, directory: str) -> bool:
        return await cls.disk().delete_directory(directory)

    @classmethod
    async def url(cls, path: str) -> str:
        return await cls.disk().url(path)

    @classmethod
    async def temporary_url(
        cls,
        path: str,
        expires_in: int | timedelta,
    ) -> str:
        return await cls.disk().temporary_url(path, expires_in=expires_in)

    @classmethod
    async def get_visibility(cls, path: str) -> Visibility:
        return await cls.disk().get_visibility(path)

    @classmethod
    async def set_visibility(cls, path: str, visibility: Visibility | str) -> bool:
        return await cls.disk().set_visibility(path, visibility=visibility)

    @classmethod
    async def metadata(cls, path: str) -> FileMetadata:
        return await cls.disk().metadata(path)

    @classmethod
    async def download(
        cls,
        path: str,
        name: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> StreamingResponse:
        return await cls.disk().download(path, name=name, headers=headers)


__all__ = [
    "FileMetadata",
    "FilesystemManager",
    "LocalStorageDriver",
    "PathNormalizer",
    "S3StorageDriver",
    "Storage",
    "StorageDisk",
    "StorageDriver",
    "UploadedFile",
    "Visibility",
]
