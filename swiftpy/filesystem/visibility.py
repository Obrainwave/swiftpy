from __future__ import annotations

from enum import StrEnum


class Visibility(StrEnum):
    """File access visibility."""

    PUBLIC = "public"
    PRIVATE = "private"


DEFAULT_FILE_PERMISSIONS: dict[str, int] = {
    Visibility.PUBLIC: 0o644,
    Visibility.PRIVATE: 0o600,
}

DEFAULT_DIR_PERMISSIONS: dict[str, int] = {
    Visibility.PUBLIC: 0o755,
    Visibility.PRIVATE: 0o700,
}

