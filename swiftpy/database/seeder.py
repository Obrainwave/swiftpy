from __future__ import annotations

import inspect
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger("swiftpy.database.seeder")


class Seeder(ABC):
    """
    Abstract base class for all SwiftPY database seeders.
    Subclasses implement the async `run()` method to populate database records.
    """

    @abstractmethod
    async def run(self) -> None:
        """Execute the database seed operations."""
        raise NotImplementedError

    async def call(self, *seeder_classes: type[Seeder]) -> None:
        """Execute a sequence of child seeders."""
        for seeder_cls in seeder_classes:
            if (
                not isinstance(seeder_cls, type)
                or not issubclass(seeder_cls, Seeder)
                or inspect.isabstract(seeder_cls)
            ):
                cls_name = getattr(seeder_cls, "__name__", str(seeder_cls))
                raise TypeError(
                    f"Invalid seeder class '{cls_name}': Must be a non-abstract subclass of Seeder."
                )
            logger.info("Running seeder: %s", seeder_cls.__name__)
            seeder = seeder_cls()
            await seeder.run()


__all__ = ["Seeder"]

