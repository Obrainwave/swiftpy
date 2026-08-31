from __future__ import annotations

from typing import Any, cast

import asyncpg

from swiftpy.core.bootstrap import get_container
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import get_active_connection


class QueryBuilder:
    def __init__(self, table_name: str) -> None:
        self._table = table_name

        self._columns: list[str] = ["*"]

        self._wheres: list[tuple[str, Any]] = []

        self._order_column: str | None = None
        self._order_direction: str = "ASC"

        self._limit_value: int | None = None

    def select(self, *columns: str) -> QueryBuilder:
        self._columns = list(columns)
        return self

    def where(self, column: str, value: Any) -> QueryBuilder:
        self._wheres.append((column, value))
        return self

    def order_by(self, column: str, direction: str = "ASC") -> QueryBuilder:
        self._order_column = column
        self._order_direction = direction.upper()
        return self

    def limit(self, value: int) -> QueryBuilder:
        self._limit_value = value
        return self

    async def _connection(self) -> asyncpg.Connection:
        tx_conn = get_active_connection()

        if tx_conn is not None:
            return tx_conn

        container = get_container()

        db_pool = container.resolve(DatabasePool)

        pool = db_pool.get_pool()

        return await pool.acquire()

    async def _release(self, conn: asyncpg.Connection) -> None:
        if get_active_connection() is not None:
            return

        container = get_container()

        db_pool = container.resolve(DatabasePool)

        await db_pool.get_pool().release(conn)

    def _compile_where(self, start: int = 1) -> tuple[str, list[Any]]:
        if not self._wheres:
            return "", []

        clauses: list[str] = []
        bindings: list[Any] = []

        idx = start

        for column, value in self._wheres:
            clauses.append(f"{column} = ${idx}")
            bindings.append(value)
            idx += 1

        return (" WHERE " + " AND ".join(clauses), bindings)

    async def get(self) -> list[dict[str, Any]]:
        where_sql, bindings = self._compile_where()

        sql = f"SELECT {', '.join(self._columns)} FROM {self._table}{where_sql}"

        if self._order_column:
            sql += f" ORDER BY {self._order_column} {self._order_direction}"

        if self._limit_value is not None:
            sql += f" LIMIT {self._limit_value}"

        conn = await self._connection()

        try:
            rows = await conn.fetch(sql, *bindings)

            return [dict(row) for row in rows]

        finally:
            await self._release(conn)

    async def first(self) -> dict[str, Any] | None:
        self.limit(1)

        rows = await self.get()

        return rows[0] if rows else None

    async def insert(self, data: dict[str, Any]) -> int:
        columns = list(data.keys())

        values = list(data.values())

        placeholders = [f"${i}" for i in range(1, len(values) + 1)]

        sql = (
            f"INSERT INTO {self._table} "
            f"({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"RETURNING id"
        )

        conn = await self._connection()

        try:
            row = await conn.fetchrow(sql, *values)

            if row is None:
                raise RuntimeError("Insert failed")

            return int(row["id"])

        finally:
            await self._release(conn)

    async def update(self, data: dict[str, Any]) -> str:
        bindings: list[Any] = []

        sets: list[str] = []

        idx = 1

        for column, value in data.items():
            sets.append(f"{column} = ${idx}")
            bindings.append(value)
            idx += 1

        where_sql, where_bindings = self._compile_where(idx)

        bindings.extend(where_bindings)

        sql = f"UPDATE {self._table} SET {', '.join(sets)}{where_sql}"

        conn = await self._connection()

        try:
            result = await conn.execute(sql, *bindings)
            return cast(str, result)

        finally:
            await self._release(conn)

    async def delete(self) -> str:
        where_sql, bindings = self._compile_where()

        sql = f"DELETE FROM {self._table}{where_sql}"

        conn = await self._connection()

        try:
            result = await conn.execute(sql, *bindings)
            return cast(str, result)

        finally:
            await self._release(conn)


class Query:
    @staticmethod
    def table(table_name: str) -> QueryBuilder:
        return QueryBuilder(table_name)
