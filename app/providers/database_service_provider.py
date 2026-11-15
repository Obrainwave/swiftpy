from __future__ import annotations

from swiftpy.core.providers.base import ServiceProvider
from swiftpy.database.pool import DatabasePool


class DatabaseServiceProvider(ServiceProvider):
    def register(self) -> None:
        self.container.singleton(DatabasePool)

    def boot(self) -> None:
        pass
