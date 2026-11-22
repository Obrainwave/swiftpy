from __future__ import annotations

from swiftpy.database.migrations import Blueprint, Migration, Schema


class CreateUsersTable(Migration):
    async def up(self) -> None:
        def define(table: Blueprint) -> None:
            table.id()
            table.string("name")
            table.string("email").unique()
            table.string("password")
            table.boolean("is_admin").default(False)
            table.remember_token()
            table.timestamps()

        await Schema.create("users", define)

    async def down(self) -> None:
        await Schema.drop_if_exists("users")

