from __future__ import annotations

from swiftpy.core.providers.base import ServiceProvider
from swiftpy.filesystem.manager import FilesystemManager


class FilesystemServiceProvider(ServiceProvider):
    """
    Registers the FilesystemManager singleton into the application DI container.
    """

    def register(self) -> None:
        self.container.singleton(FilesystemManager)

    def boot(self) -> None:
        pass

