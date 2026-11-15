from __future__ import annotations

from swiftpy.core.config import Config
from swiftpy.core.providers.base import ServiceProvider
from swiftpy.mail.manager import MailManager


class MailServiceProvider(ServiceProvider):
    """
    Register the MailManager singleton into the application DI container.
    """

    def register(self) -> None:
        self.container.singleton(
            MailManager,
            lambda c: MailManager(
                config=c.resolve(Config) if c.has(Config) else None
            ),
        )

    async def shutdown(self) -> None:
        manager = self.container.get_singleton(MailManager)
        if isinstance(manager, MailManager):
            await manager.close()
