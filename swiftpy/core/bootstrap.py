from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from swiftpy.core.application import Application
from swiftpy.core.container import Container, get_container, set_container
from swiftpy.core.providers.base import ServiceProvider

__all__ = [
    "Container",
    "create_app",
    "get_container",
    "set_container",
]


def create_app(
    providers: Sequence[type[ServiceProvider]] | None = None,
    config: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Application:
    """
    Bootstrap and initialize the SwiftPY application instance.

    Convenience factory function wrapping Application instantiation and boot.
    """
    app = Application(providers=providers, config=config, **kwargs)
    app.boot()
    return app
