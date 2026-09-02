from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any


class Config:
    """
    Application configuration repository.
    Supports dot-notation access for nested dictionaries.
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data = data or {}
        
    @classmethod
    def load(cls, config_dir: str = "config") -> "Config":
        data: dict[str, Any] = {}

        for file in Path(config_dir).glob("*.py"):
            if file.name.startswith("__"):
                continue

            spec = spec_from_file_location(file.stem, file)

            if spec and spec.loader:
                module = module_from_spec(spec)
                spec.loader.exec_module(module)

                if hasattr(module, "CONFIG"):
                    data[file.stem] = module.CONFIG

        return cls(data)

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
