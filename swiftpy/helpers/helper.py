from __future__ import annotations

from typing import Any
import os

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


__all__ = [
    "app",
    "config",
    "container",
    "resolve",
]