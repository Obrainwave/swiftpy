from __future__ import annotations

from swiftpy.core.providers.base import ServiceProvider
from swiftpy.queue.dispatcher import QueueDispatcher


class QueueServiceProvider(ServiceProvider):
    """
    Register the QueueDispatcher singleton into the application DI container.
    """

    def register(self) -> None:
        self.container.singleton(
            QueueDispatcher, lambda _: QueueDispatcher()
        )

    def boot(self) -> None:
        pass
