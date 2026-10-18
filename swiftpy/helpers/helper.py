from __future__ import annotations

import os
import re
from typing import Any

from swiftpy.core.bootstrap import get_container
from swiftpy.core.config import Config


def app() -> Any:
    """Return the application container."""
    return get_container()


def config(key: str | None = None, default: Any = None) -> Any:
    """Retrieve configuration values."""
    cfg = get_container().resolve(Config)

    if key is None:
        return cfg

    return cfg.get(key, default)


def container() -> Any:
    """Return the IoC container."""
    return get_container()


def resolve(target: type[Any]) -> Any:
    """Resolve a dependency from the container."""
    return get_container().resolve(target)


def env(key: str, default: Any = None) -> Any:
    """Retrieve environment variables."""
    return os.getenv(key, default)


def snake_case(name: str) -> str:
    value = re.sub(
        r"(?<!^)(?=[A-Z])",
        "_",
        name,
    )

    return value.lower()


__all__ = ["app", "config", "container", "env", "resolve", "snake_case"]
