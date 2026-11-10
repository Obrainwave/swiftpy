from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass(slots=True, frozen=True)
class FileUploaded:
    """Dispatched after a file is stored on a disk."""

    EVENT_NAME: ClassVar[str] = "storage.file.uploaded"

    disk: str
    path: str
    size: int
    mime_type: str


@dataclass(slots=True, frozen=True)
class FileDeleted:
    """Dispatched after a file is deleted from a disk."""

    EVENT_NAME: ClassVar[str] = "storage.file.deleted"

    disk: str
    path: str


@dataclass(slots=True, frozen=True)
class FileMoved:
    """Dispatched after a file is moved/renamed on a disk."""

    EVENT_NAME: ClassVar[str] = "storage.file.moved"

    disk: str
    source: str
    destination: str
