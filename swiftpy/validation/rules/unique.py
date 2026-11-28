from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import asyncpg

from swiftpy.core.container import get_container
from swiftpy.database.identifiers import validate_identifier
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import get_active_connection

_INT_LITERAL_RE = re.compile(r"^[+-]?[0-9]+$")
_CANONICAL_INT_STR_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_NULL_IGNORE_TOKENS = frozenset({"", "null", "none"})


def _resolve_ignore_target(
    ignore_id: Any,
    id_column: str,
    *,
    id_column_explicit: bool,
) -> tuple[Any, str]:
    """Normalize explicit IDs, null-like string tokens, Model instances, and AuthUser mappings."""
    if ignore_id is None:
        return None, id_column

    if isinstance(ignore_id, bool):
        raise TypeError("UniqueRule ignore_id cannot be a boolean.")

    if isinstance(ignore_id, str):
        if ignore_id.strip().lower() in _NULL_IGNORE_TOKENS:
            return None, id_column
        return ignore_id, id_column

    if getattr(ignore_id, "__primary_key__", None) is not None and callable(
        getattr(ignore_id, "to_dict", None)
    ):
        pk_col = str(getattr(ignore_id, "__primary_key__", "id"))
        pk_val = getattr(ignore_id, pk_col, None)
        resolved_col = id_column if id_column_explicit else pk_col
        return pk_val, resolved_col

    if isinstance(ignore_id, Mapping):
        model_inst = getattr(ignore_id, "model", None)
        if (
            model_inst is not None
            and getattr(model_inst, "__primary_key__", None) is not None
            and callable(getattr(model_inst, "to_dict", None))
        ):
            pk_col = str(getattr(model_inst, "__primary_key__", "id"))
            pk_val = getattr(model_inst, pk_col, None)
            if pk_val is not None:
                resolved_col = id_column if id_column_explicit else pk_col
                return pk_val, resolved_col

        lookup_key = id_column if id_column_explicit else "id"
        pk_val = ignore_id.get(lookup_key)
        if pk_val is None and not id_column_explicit:
            pk_val = ignore_id.get("sub")
        return pk_val, id_column

    return ignore_id, id_column


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
        id_column: str | None = None,
        pool: DatabasePool | None = None,
        case_insensitive: bool = False,
    ) -> None:
        id_column_explicit = id_column is not None
        effective_id_col = id_column if id_column is not None else "id"
        resolved_ignore_id, resolved_id_column = _resolve_ignore_target(
            ignore_id,
            effective_id_col,
            id_column_explicit=id_column_explicit,
        )
        self.table = validate_identifier(table, param_name="table")
        self.column = validate_identifier(column, param_name="column")
        self.id_column = validate_identifier(resolved_id_column, param_name="column")
        self.ignore_id = resolved_ignore_id
        self.pool = pool
        self.case_insensitive = case_insensitive
        self._id_column_explicit = id_column_explicit

    def ignore(self, ignore_id: Any, id_column: str | None = None) -> UniqueRule:
        """Return a new UniqueRule ignoring the row identified by `ignore_id` or a Model."""
        next_id_col = (
            id_column
            if id_column is not None
            else (self.id_column if self._id_column_explicit else None)
        )
        return UniqueRule(
            table=self.table,
            column=self.column,
            ignore_id=ignore_id,
            id_column=next_id_col,
            pool=self.pool,
            case_insensitive=self.case_insensitive,
        )

    async def _fetchval_unique(
        self,
        conn: Any,
        col_expr: str,
        val_param: str,
        value: Any,
        *,
        use_savepoint: bool,
    ) -> Any:
        async def _run_one(query: str, *args: Any) -> Any:
            if use_savepoint and hasattr(conn, "transaction"):
                async with conn.transaction():
                    return await conn.fetchval(query, *args)
            return await conn.fetchval(query, *args)

        if self.ignore_id is None:
            query = (
                f'SELECT 1 FROM "{self.table}" WHERE {col_expr} = {val_param} LIMIT 1'
            )
            return await _run_one(query, value)

        query = (
            f'SELECT 1 FROM "{self.table}" '
            f'WHERE {col_expr} = {val_param} AND "{self.id_column}" != $2 LIMIT 1'
        )
        if isinstance(self.ignore_id, str):
            stripped_ignore = self.ignore_id.strip()
            if _CANONICAL_INT_STR_RE.fullmatch(stripped_ignore):
                try:
                    return await _run_one(query, value, int(stripped_ignore))
                except (asyncpg.DataError, TypeError):
                    return await _run_one(query, value, self.ignore_id)

        try:
            return await _run_one(query, value, self.ignore_id)
        except (asyncpg.DataError, TypeError):
            if isinstance(self.ignore_id, str) and _INT_LITERAL_RE.fullmatch(
                self.ignore_id.strip()
            ):
                return await _run_one(query, value, int(self.ignore_id.strip()))
            if isinstance(self.ignore_id, int) and not isinstance(self.ignore_id, bool):
                return await _run_one(query, value, str(self.ignore_id))
            raise

    async def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        if value is None or value == "":
            return True

        col_expr = (
            f'LOWER("{self.column}")' if self.case_insensitive else f'"{self.column}"'
        )
        val_param = "LOWER($1)" if self.case_insensitive else "$1"

        try:
            active_conn = get_active_connection()
            if active_conn is not None:
                row = await self._fetchval_unique(
                    active_conn, col_expr, val_param, value, use_savepoint=True
                )
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
                row = await self._fetchval_unique(
                    conn, col_expr, val_param, value, use_savepoint=False
                )

            return row is None
        except (asyncpg.DataError, ValueError, TypeError):
            return False

    def message(self, field: str) -> str:
        return f"The {field} has already been taken."


def unique(
    table: str,
    column: str,
    ignore_id: Any = None,
    id_column: str | None = None,
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
