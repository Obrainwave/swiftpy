from __future__ import annotations

from swiftpy.core.providers.base import ServiceProvider


class AppServiceProvider(ServiceProvider):
    """
    Core application service provider.

    This is the first provider loaded by the SwiftPY framework.
    Framework-level services will be registered here.
    """

    def register(self) -> None:
        pass

    def boot(self) -> None:
        pass
