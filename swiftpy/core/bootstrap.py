from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from swiftpy.core.application import Application
from swiftpy.core.config import Config
from swiftpy.core.container import Container
from swiftpy.core.providers.base import ServiceProvider
from swiftpy.http.router import Router

_container: Container | None = None


def set_container(container: Container) -> None:
    global _container
    _container = container


def get_container() -> Container:
    if _container is None:
        raise RuntimeError("Application container not bootstrapped")
    return _container


def create_app(
    providers: Sequence[type[ServiceProvider]] | None = None,
    config: dict[str, Any] | None = None,
) -> Application:
    """
    Bootstrap and initialize the SwiftPY application instance.

    Registers application singletons into the container, registers provided
    service providers, loads optional runtime configuration, and triggers the
    boot phase across all registered services.
    """
    app = Application()

    if config is None:
        app_config = Config.load("config")
    else:
        app_config = Config(config)

    app.container.instance(Application, app)
    app.container.instance(Container, app.container)
    app.container.instance(Router, app.router)
    app.container.instance(Config, app_config)

    set_container(app.container)

    if providers:
        for provider_cls in providers:
            app.register(provider_cls)

    app.boot()

    return app
