from __future__ import annotations

from swiftpy.cache import Cache
from swiftpy.core.providers.base import ServiceProvider


class CacheServiceProvider(ServiceProvider):
    """
    Register the Cache manager singleton into the application DI container.
    """

    def register(self) -> None:
        self.container.singleton(Cache)

    def boot(self) -> None:
        pass
