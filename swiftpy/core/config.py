from __future__ import annotations

from typing import Any


class Config:
    """
    Application configuration repository.
    Supports dot-notation access for nested dictionaries.
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data = data or {}

    def get(self, key: str, default: Any = None) -> Any:
        """Retrieve a configuration value using dot notation."""
        keys = key.split(".")
        val: Any = self._data

        for k in keys:
            if isinstance(val, dict) and k in val:
                val = val[k]
            else:
                return default

        return val

    def all(self) -> dict[str, Any]:
        """Return a copy of the complete configuration dictionary."""
        return self._data.copy()
