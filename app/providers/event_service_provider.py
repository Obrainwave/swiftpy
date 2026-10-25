from __future__ import annotations

from swiftpy.core.providers.base import ServiceProvider
from swiftpy.events import EventDispatcher, RedisEventBus


class EventServiceProvider(ServiceProvider):
    """
    Register the EventDispatcher and RedisEventBus singletons into the application DI container.
    """

    def register(self) -> None:
        self.container.singleton(
            EventDispatcher, lambda c: EventDispatcher.get_instance()
        )
        self.container.singleton(
            RedisEventBus,
            lambda c: RedisEventBus(
                dispatcher=c.resolve(EventDispatcher),
            ),
        )

    def boot(self) -> None:
        pass
