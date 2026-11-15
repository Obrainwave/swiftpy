from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from swiftpy.filesystem.visibility import Visibility


@dataclass(slots=True, frozen=True)
class FileMetadata:
    """Unified metadata representation for a stored file."""

    path: str
    size: int
    mime_type: str
    last_modified: datetime
    visibility: Visibility | str = Visibility.PRIVATE
    etag: str | None = None
    checksum: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

