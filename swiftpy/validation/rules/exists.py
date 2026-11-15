from __future__ import annotations

from typing import Any

import asyncpg

from swiftpy.core.container import get_container
from swiftpy.database.identifiers import validate_identifier
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import get_active_connection


class ExistsRule:
    """
    Validates that a field value exists in a given database table and column.
    Non-blocking async query executed against the active transaction or DatabasePool.
    """

    requires_db: bool = True

    def __init__(
        self,
        table: str,
        column: str,
        pool: DatabasePool | None = None,
    ) -> None:
        self.table = validate_identifier(table, param_name="table")
        self.column = validate_identifier(column, param_name="column")
        self.pool = pool

    async def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        if value is None or value == "":
            return True

        query = f'SELECT 1 FROM "{self.table}" WHERE "{self.column}" = $1 LIMIT 1'

        try:
            active_conn = get_active_connection()
            if active_conn is not None:
                if hasattr(active_conn, "transaction"):
                    async with active_conn.transaction():
                        row = await active_conn.fetchval(query, value)
                else:
                    row = await active_conn.fetchval(query, value)
                return row is not None

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
                row = await conn.fetchval(query, value)

            return row is not None
        except (asyncpg.DataError, ValueError, TypeError):
            return False

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
