from __future__ import annotations

import re
from typing import Any

from swiftpy.core.container import get_container
from swiftpy.database.pool import DatabasePool

_IDENTIFIER_REGEX = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class ExistsRule:
    """
    Validates that a field value exists in a given database table and column.
    Non-blocking async query executed against DatabasePool.
    """

    def __init__(
        self,
        table: str,
        column: str,
        pool: DatabasePool | None = None,
    ) -> None:
        if not _IDENTIFIER_REGEX.match(table):
            raise ValueError(f"Invalid table identifier '{table}'")
        if not _IDENTIFIER_REGEX.match(column):
            raise ValueError(f"Invalid column identifier '{column}'")

        self.table = table
        self.column = column
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
                "DatabasePool is required for ExistsRule validation. Ensure it is registered in the container or passed explicitly."
            )

        db = active_pool.get_pool()
        async with db.acquire() as conn:
            query = f'SELECT 1 FROM "{self.table}" WHERE "{self.column}" = $1 LIMIT 1'
            row = await conn.fetchval(query, value)

        return row is not None

    def message(self, field: str) -> str:
        return f"The selected {field} is invalid."


def exists(
    table: str,
    column: str,
    pool: DatabasePool | None = None,
) -> ExistsRule:
    """Helper to instantiate ExistsRule."""
    return ExistsRule(
        table=table,
        column=column,
        pool=pool,
    )


__all__ = ["ExistsRule", "exists"]
