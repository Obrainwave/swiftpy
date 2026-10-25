from __future__ import annotations

import re
from typing import Any

from swiftpy.core.container import get_container
from swiftpy.database.pool import DatabasePool

_IDENTIFIER_REGEX = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class UniqueRule:
    """
    Validates that a field value is unique in a given database table and column.
    Non-blocking async query executed against DatabasePool.
    """

    def __init__(
        self,
        table: str,
        column: str,
        ignore_id: Any = None,
        id_column: str = "id",
        pool: DatabasePool | None = None,
    ) -> None:
        if not _IDENTIFIER_REGEX.match(table):
            raise ValueError(f"Invalid table identifier '{table}'")
        if not _IDENTIFIER_REGEX.match(column):
            raise ValueError(f"Invalid column identifier '{column}'")
        if not _IDENTIFIER_REGEX.match(id_column):
            raise ValueError(f"Invalid column identifier '{id_column}'")

        self.table = table
        self.column = column
        self.ignore_id = ignore_id
        self.id_column = id_column
        self.pool = pool

    async def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        if value is None or value == "":
            return True

        active_pool = self.pool
        if active_pool is None:
            try:
                container = get_container()
                if container.has(DatabasePool):
                    active_pool = container.resolve(DatabasePool)
            except RuntimeError:
                pass

        if active_pool is None:
            raise RuntimeError(
                "DatabasePool is required for UniqueRule validation. Ensure it is registered in the container or passed explicitly."
            )

        db = active_pool.get_pool()
        async with db.acquire() as conn:
            if self.ignore_id is not None:
                query = (
                    f'SELECT 1 FROM "{self.table}" '
                    f'WHERE "{self.column}" = $1 AND "{self.id_column}" != $2 LIMIT 1'
                )
                row = await conn.fetchval(query, value, self.ignore_id)
            else:
                query = (
                    f'SELECT 1 FROM "{self.table}" WHERE "{self.column}" = $1 LIMIT 1'
                )
                row = await conn.fetchval(query, value)

        return row is None

    def message(self, field: str) -> str:
        return f"The {field} has already been taken."


def unique(
    table: str,
    column: str,
    ignore_id: Any = None,
    id_column: str = "id",
    pool: DatabasePool | None = None,
) -> UniqueRule:
    """Helper to instantiate UniqueRule."""
    return UniqueRule(
        table=table,
        column=column,
        ignore_id=ignore_id,
        id_column=id_column,
        pool=pool,
    )


__all__ = ["UniqueRule", "unique"]
