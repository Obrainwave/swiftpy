from __future__ import annotations

from swiftpy.database.migrations import Blueprint, Migration, Schema


class CreateProfilesTable(Migration):
    async def up(self) -> None:
        def define(table: Blueprint) -> None:
            table.id()
            table.big_integer("user_id").unsigned().index()
            table.string("username", 128).nullable()
            table.timestamps()

        await Schema.create("profiles", define)

    async def down(self) -> None:
        await Schema.drop_if_exists("profiles")
