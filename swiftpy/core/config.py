from __future__ import annotations

import copy
import json
import logging
import math
import os
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

from swiftpy.core.container import get_container

logger = logging.getLogger("swiftpy.core.config")

_MISSING = object()


class ConfigurationError(ValueError):
    """Raised when configuration values are malformed or invalid."""


class Config:
    """
    Application configuration repository.
    Supports dot-notation access for nested dictionaries and environment variable merging.
    """

    _instance: Config | None = None

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = copy.deepcopy(data) if data else {}

    @classmethod
    def get_instance(cls) -> Config:
        """
        Retrieve the active application's Config from the current container,
        falling back to a default loaded Config when no application is active.
        """
        try:
            container = get_container()
            if container.has(cls):
                return container.resolve(cls)
        except RuntimeError:
            pass

        if cls._instance is None:
            cls._instance = cls.load()
        return cls._instance

    @classmethod
    def load(
        cls,
        config_dir: str | Path = "config",
        env: dict[str, str] | None = None,
        *,
        strict: bool = True,
        ignore_cache: bool = False,
    ) -> Config:
        """
        Load configuration files from config_dir and merge with environment variables.
        When `strict=True` (the default), broken or invalid config files raise `ConfigurationError`.
        When `strict=False`, broken config files log a warning and are skipped.
        """
        cache_path = Path("bootstrap/cache/config.json")
        if not ignore_cache and env is None and cache_path.is_file():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
                if isinstance(cached_data, dict):
                    instance = cls(cached_data)
                    cls._instance = instance
                    return instance
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load cached config %s: %s", cache_path, exc)

        data: dict[str, Any] = {}
        config_path = Path(config_dir)

        if config_path.is_dir():
            for file in sorted(config_path.glob("*.py")):
                if file.name.startswith(("_", ".")):
                    continue

                spec = spec_from_file_location(file.stem, file)
                if spec and spec.loader:
                    module = module_from_spec(spec)
                    try:
                        spec.loader.exec_module(module)
                    except Exception as exc:
                        if strict:
                            raise ConfigurationError(
                                f"Failed to load configuration file '{file}': {exc}"
                            ) from exc
                        logger.warning(
                            "Failed to load config file: %s",
                            file,
                            exc_info=True,
                        )
                        continue

                    if hasattr(module, "CONFIG"):
                        if isinstance(module.CONFIG, dict):
                            data[file.stem] = copy.deepcopy(module.CONFIG)
                        elif strict:
                            raise ConfigurationError(
                                f"Configuration file '{file}' must define CONFIG as a dict, "
                                f"got {type(module.CONFIG).__name__}."
                            )

        # Merge environment variables
        env_vars = os.environ if env is None else env
        cls._merge_env_variables(data, env_vars)

        instance = cls(data)
        cls._instance = instance
        return instance

    @classmethod
    def _merge_env_variables(
        cls,
        data: dict[str, Any],
        env_vars: dict[str, str] | Any,
    ) -> None:
        """
        Merge environment variables into the configuration dictionary.
        Supports deterministic precedence:
        1. Low tier: Standard top-level prefix and single-underscore normalized paths
        2. Medium tier: Double-underscore nested syntax (e.g. DATABASE__POOL__MAX_SIZE)
        3. High tier: Framework-specific SWIFTPY_ prefix (e.g. SWIFTPY_DATABASE__POOL__MAX_SIZE)
        """
        # First, build a map of existing normalized keys to their dotted paths
        existing_paths: dict[str, str] = {}

        def _index_paths(current: dict[str, Any], prefix: str = "") -> None:
            for k, v in current.items():
                if not k:
                    continue
                path = f"{prefix}.{k}" if prefix else k
                norm_key = path.upper().replace(".", "_")
                existing_paths[norm_key] = path
                if isinstance(v, dict):
                    _index_paths(v, path)

        _index_paths(data)

        # Precedence tiers: 1 (single-underscore/prefix) < 2 (double-underscore) < 3 (SWIFTPY_ prefix)
        def _get_tier(raw_k: str) -> int:
            if raw_k.startswith("SWIFTPY_"):
                return 3
            if "__" in raw_k:
                return 2
            return 1

        sorted_env_items = sorted(env_vars.items(), key=lambda item: _get_tier(item[0]))

        for raw_key, raw_val in sorted_env_items:
            if not raw_key:
                continue
            key = raw_key.removeprefix("SWIFTPY_")
            if not key or key.startswith("_") or key.endswith("_"):
                continue

            # 1. Double-underscore nested syntax
            if "__" in key:
                parts = [p.lower() for p in key.split("__")]
                if any(not p or p.startswith("_") or p.endswith("_") for p in parts):
                    continue
                dotted_key = ".".join(parts)
                existing_val = cls._get_from_dict(data, dotted_key, _MISSING)
                coerced = cls._coerce_value(raw_val, existing_val, key=dotted_key)
                cls._set_in_dict(data, dotted_key, coerced)
                continue

            # 2. Match against existing normalized paths
            norm_key = key.upper()
            if norm_key in existing_paths:
                dotted_key = existing_paths[norm_key]
                existing_val = cls._get_from_dict(data, dotted_key, _MISSING)
                coerced = cls._coerce_value(raw_val, existing_val, key=dotted_key)
                cls._set_in_dict(data, dotted_key, coerced)
                continue

            # 3. Standard top-level prefix mapping (e.g. APP_DEBUG -> app.debug, APP_NAME -> app.name)
            if "_" in key:
                parts = key.split("_", 1)
                top = parts[0].lower()
                rest = parts[1].lower()
                if not top or not rest:
                    continue
                if top in data and isinstance(data[top], dict):
                    dotted_key = f"{top}.{rest}"
                    existing_val = cls._get_from_dict(data, dotted_key, _MISSING)
                    coerced = cls._coerce_value(raw_val, existing_val, key=dotted_key)
                    cls._set_in_dict(data, dotted_key, coerced)

    @classmethod
    def _coerce_value(
        cls,
        val: str,
        existing: Any = _MISSING,
        key: str = "",
    ) -> Any:
        """
        Coerce raw string environment variable based on the declared type of `existing` (Decision D-8).
        When no typed default exists, strings are preserved without blind numeric/boolean coercion.
        """
        if (existing is not _MISSING and isinstance(existing, bool)) or key in (
            "app.debug",
            "debug",
        ):
            return val.strip().lower() in ("true", "1", "yes", "on")
        if existing is not _MISSING and existing is not None:
            if isinstance(existing, int):
                try:
                    return int(val)
                except ValueError as exc:
                    raise ConfigurationError(
                        f"Configuration key '{key}' expects an integer, got '{val}'"
                    ) from exc
            if isinstance(existing, float):
                try:
                    res = float(val)
                    if math.isfinite(res):
                        return res
                    raise ConfigurationError(
                        f"Configuration key '{key}' expects a finite float, got '{val}'"
                    )
                except ValueError as exc:
                    raise ConfigurationError(
                        f"Configuration key '{key}' expects a float, got '{val}'"
                    ) from exc

        # JSON arrays / objects
        if (val.startswith("{") and val.endswith("}")) or (
            val.startswith("[") and val.endswith("]")
        ):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ConfigurationError(
                    f"Configuration key '{key}' contains malformed JSON: '{val}'"
                ) from exc

        return val

    @classmethod
    def _get_from_dict(
        cls,
        data: dict[str, Any],
        key: str,
        default: Any = _MISSING,
    ) -> Any:
        if not key:
            return default
        keys = key.split(".")
        current: Any = data
        for k in keys:
            if not k:
                return default
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return default
        return current

    @classmethod
    def _set_in_dict(cls, data: dict[str, Any], key: str, value: Any) -> None:
        if not key:
            return
        keys = key.split(".")
        if any(not k for k in keys):
            return
        current = data
        for k in keys[:-1]:
            if k not in current or not isinstance(current[k], dict):
                current[k] = {}
            current = current[k]
        current[keys[-1]] = value

    def get(self, key: str, default: Any = None) -> Any:
        """
        Retrieve a configuration value using dot notation.
        Distinguishes a missing key (returns `default`) from an explicit `None` value (returns `None`).
        """
        val = self._get_from_dict(self._data, key, _MISSING)
        return default if val is _MISSING else val

    def set(self, key: str, value: Any) -> None:
        """Set a configuration value using dot notation."""
        self._set_in_dict(self._data, key, value)

    def has(self, key: str) -> bool:
        """Check if a configuration key exists."""
        return self._get_from_dict(self._data, key, _MISSING) is not _MISSING

    def all(self) -> dict[str, Any]:
        """Return a deep copy of the complete configuration dictionary."""
        return copy.deepcopy(self._data)

    def __getitem__(self, key: str) -> Any:
        if not self.has(key):
            raise KeyError(key)
        return self.get(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __contains__(self, key: str) -> bool:
        return self.has(key)

    def __call__(self, key: str | None = None, default: Any = None) -> Any:
        if key is None:
            return self
        return self.get(key, default)

    def dump_cache(
        self,
        cache_path: str | Path = "bootstrap/cache/config.json",
    ) -> Path:
        """Serialize configuration data to JSON cache for production performance."""
        target = Path(cache_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, default=str)
        return target

    @classmethod
    def clear_cache(
        cls,
        cache_path: str | Path = "bootstrap/cache/config.json",
    ) -> bool:
        """Delete serialized configuration cache file."""
        target = Path(cache_path)
        if target.is_file():
            target.unlink()
            return True
        return False


__all__ = ["Config", "ConfigurationError"]
