from __future__ import annotations

from swiftpy.database.seeder import Seeder


class DatabaseSeeder(Seeder):
    """
    Root database seeder.
    Run via `swiftpy db:seed`. Call additional seeders using `await self.call(...)`.
    """

    async def run(self) -> None:
        # Example:
        # await self.call(UserSeeder)
        pass

