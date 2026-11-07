from __future__ import annotations

from typing import Any

import asyncpg

from swiftpy.core.container import get_container
from swiftpy.database.identifiers import validate_identifier
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import get_active_connection


class UniqueRule:
    """
    Validates that a field value is unique in a given database table and column.
    Non-blocking async query executed against the active transaction or DatabasePool.
    """

    requires_db: bool = True

    def __init__(
        self,
        table: str,
        column: str,
        ignore_id: Any = None,
        id_column: str = "id",
        pool: DatabasePool | None = None,
        case_insensitive: bool = False,
    ) -> None:
        self.table = validate_identifier(table, param_name="table")
        self.column = validate_identifier(column, param_name="column")
        self.id_column = validate_identifier(id_column, param_name="column")
        self.ignore_id = ignore_id
        self.pool = pool
        self.case_insensitive = case_insensitive

    async def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        if value is None or value == "":
            return True

        col_expr = f'LOWER("{self.column}")' if self.case_insensitive else f'"{self.column}"'
        val_param = "LOWER($1)" if self.case_insensitive else "$1"

        try:
            active_conn = get_active_connection()
            if active_conn is not None and self.pool is None:
                if self.ignore_id is not None:
                    query = (
                        f'SELECT 1 FROM "{self.table}" '
                        f'WHERE {col_expr} = {val_param} AND "{self.id_column}" != $2 LIMIT 1'
                    )
                    row = await active_conn.fetchval(query, value, self.ignore_id)
                else:
                    query = (
                        f'SELECT 1 FROM "{self.table}" WHERE {col_expr} = {val_param} LIMIT 1'
                    )
                    row = await active_conn.fetchval(query, value)
                return row is None

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
                        f'WHERE {col_expr} = {val_param} AND "{self.id_column}" != $2 LIMIT 1'
                    )
                    row = await conn.fetchval(query, value, self.ignore_id)
                else:
                    query = (
                        f'SELECT 1 FROM "{self.table}" WHERE {col_expr} = {val_param} LIMIT 1'
                    )
                    row = await conn.fetchval(query, value)

            return row is None
        except (asyncpg.DataError, ValueError, TypeError):
            return False

    def message(self, field: str) -> str:
        return f"The {field} has already been taken."


def unique(
    table: str,
    column: str,
    ignore_id: Any = None,
    id_column: str = "id",
    pool: DatabasePool | None = None,
    case_insensitive: bool = False,
) -> UniqueRule:
    """Helper to instantiate UniqueRule."""
    return UniqueRule(
        table=table,
        column=column,
        ignore_id=ignore_id,
        id_column=id_column,
        pool=pool,
        case_insensitive=case_insensitive,
    )


__all__ = ["UniqueRule", "unique"]
