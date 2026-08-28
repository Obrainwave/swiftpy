from __future__ import annotations

from swiftpy.core.container import Container
from swiftpy.core.providers.base import ServiceProvider


class Application:
    """
    Root SwiftPY application.
    """

    def __init__(self) -> None:
        self.container = Container()

        self._providers: list[ServiceProvider] = []

    def register(
        self,
        provider_class: type[ServiceProvider],
    ) -> ServiceProvider:
        """Instantiate and register a service provider."""
        provider = provider_class(self.container)

        provider.register()

        self._providers.append(provider)

        return provider

    def boot(self) -> None:
        """
        Boot all registered service providers once all bindings are configured.
        """
        for provider in self._providers:
            provider.boot()
