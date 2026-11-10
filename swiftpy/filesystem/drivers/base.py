from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import BinaryIO

from swiftpy.filesystem.metadata import FileMetadata
from swiftpy.filesystem.visibility import Visibility


class StorageDriver(ABC):
    """Abstract contract for SwiftPY storage drivers."""

    @abstractmethod
    async def write(
        self,
        path: str,
        content: bytes | str,
        visibility: Visibility | str | None = None,
    ) -> int:
        """
        Write content to the given path.
        Returns the number of bytes written.
        """

    @abstractmethod
    async def write_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes] | BinaryIO,
        chunk_size: int = 64 * 1024,
        visibility: Visibility | str | None = None,
    ) -> int:
        """
        Stream content to the given path without loading the entire payload into RAM.
        Returns total bytes written.
        """

    @abstractmethod
    async def read(self, path: str) -> bytes:
        """
        Read the entire binary content of the file.
        Raises FileNotFoundError if the file does not exist.
        """

    @abstractmethod
    async def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """
        Read the file content decoded as text.
        Raises FileNotFoundError if the file does not exist.
        """

    @abstractmethod
    def read_stream(
        self,
        path: str,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        """
        Stream file content asynchronously in chunks.
        Raises FileNotFoundError if the file does not exist.
        """

    @abstractmethod
    async def exists(self, path: str) -> bool:
        """Determine if a file or object exists at the given path."""

    @abstractmethod
    async def missing(self, path: str) -> bool:
        """Determine if a file or object is missing at the given path."""

    @abstractmethod
    async def delete(self, path: str) -> bool:
        """
        Delete the file at the given path.
        Returns True if deleted or already absent.
        """

    @abstractmethod
    async def copy(self, source: str, destination: str) -> bool:
        """
        Copy a file from source to destination.
        Returns True on success.
        """

    @abstractmethod
    async def move(self, source: str, destination: str) -> bool:
        """
        Move/rename a file from source to destination.
        Returns True on success.
        """

    @abstractmethod
    async def size(self, path: str) -> int:
        """Get the file size in bytes."""

    @abstractmethod
    async def mime_type(self, path: str) -> str:
        """Get the detected MIME type of the file."""

    @abstractmethod
    async def last_modified(self, path: str) -> datetime:
        """Get the last modified timestamp in UTC."""

    @abstractmethod
    async def checksum(self, path: str, algorithm: str = "sha256") -> str:
        """Compute the cryptographic hex digest of the file."""

    @abstractmethod
    async def files(self, directory: str = "", recursive: bool = False) -> list[str]:
        """
        List all file paths within a directory (relative to the disk root).
        """

    @abstractmethod
    async def directories(self, directory: str = "", recursive: bool = False) -> list[str]:
        """
        List all directory paths within a directory (relative to the disk root).
        """

    @abstractmethod
    async def make_directory(self, directory: str) -> bool:
        """Create a directory if it does not exist."""

    @abstractmethod
    async def delete_directory(self, directory: str) -> bool:
        """Recursively delete a directory and all its contents."""

    @abstractmethod
    async def url(self, path: str) -> str:
        """Get the public URL for the file."""

    @abstractmethod
    async def temporary_url(
        self,
        path: str,
        expires_in: int | timedelta,
    ) -> str:
        """Generate a cryptographically signed temporary URL with expiration."""

    @abstractmethod
    async def get_visibility(self, path: str) -> Visibility:
        """Get the access visibility (public or private) for a file."""

    @abstractmethod
    async def set_visibility(self, path: str, visibility: Visibility | str) -> bool:
        """Set the access visibility for a file."""

    @abstractmethod
    async def metadata(self, path: str) -> FileMetadata:
        """Retrieve unified metadata for a file."""

    async def close(self) -> None:
        """Clean up underlying connections, pools, or open handles."""

