from __future__ import annotations

from abc import ABC, abstractmethod

from swiftpy.core.container import Container


class ServiceProvider(ABC):
    """
    Base service provider.
    """

    def __init__(self, container: Container) -> None:
        self.container = container

    @abstractmethod
    def register(self) -> None:
        """
        Register services into the container.
        """

    def boot(self) -> None:
        """
        Optional synchronous boot hook.
        """

    async def startup(self) -> None:
        """
        Optional asynchronous startup hook invoked during ASGI lifespan startup.
        """

    async def shutdown(self) -> None:
        """
        Optional asynchronous shutdown hook invoked during ASGI lifespan shutdown.
        """
