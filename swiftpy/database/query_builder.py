from __future__ import annotations

import re
from typing import Any, cast

import asyncpg

from swiftpy.core.bootstrap import get_container
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import get_active_connection

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*$")
_COLUMN_RE = re.compile(
    r"^(?:"
    r"\*"
    r"|"
    r"[a-zA-Z_][a-zA-Z0-9_]*"
    r"(?:\.(?:[a-zA-Z_][a-zA-Z0-9_]*|\*))?"
    r")$"
)

_MISSING = object()


def validate_identifier(identifier: str) -> str:
    if identifier == "*":
        return identifier
    if not _IDENTIFIER_RE.match(identifier):
        raise ValueError(f"Invalid SQL identifier detected: {identifier}")
    return identifier


def validate_column_identifier(column: str) -> str:
    if not _COLUMN_RE.fullmatch(column):
        raise ValueError(f"Invalid SQL column identifier detected: {column}")

    return column


def quote_identifier(identifier: str) -> str:
    """Validate, then quote each dotted segment: t.col becomes "t"."col"."""
    if identifier == "*":
        return "*"
    validate_column_identifier(identifier)
    return ".".join(
        "*" if part == "*" else f'"{part}"' for part in identifier.split(".")
    )


class Column:
    def __init__(self, name: str, *, alias: str | None = None) -> None:
        self.name = validate_column_identifier(name)

        self.alias = validate_identifier(alias) if alias is not None else None

    def to_sql(self) -> str:
        quoted = quote_identifier(self.name)
        if self.alias is None:
            return quoted

        return f'{quoted} AS "{self.alias}"'


class Raw:
    """
    Trusted raw SQL fragment.

    SQL passed here is inserted directly into the query structure.
    Never pass untrusted/user-controlled SQL into this object.

    Dynamic values must be supplied through bindings.
    """

    def __init__(self, sql: str, bindings: list[Any] | None = None) -> None:
        self.sql = sql
        self.bindings = bindings or []


SelectExpression = str | Column | Raw


class QueryBuilder:
    def __init__(self, table_name: str) -> None:
        self._table = validate_identifier(table_name)
        self._columns: list[SelectExpression] = ["*"]
        self._wheres: list[tuple[str, str, Any]] = []
        self._joins: list[tuple[str, str, str, str, str]] = []
        self._raw_wheres: list[Raw] = []
        self._order_column: str | None = None
        self._order_direction: str = "ASC"
        self._limit_value: int | None = None

    def select(self, *columns: SelectExpression) -> QueryBuilder:

        if not columns:
            self._columns = ["*"]
            return self

        selected: list[SelectExpression] = []

        for column in columns:
            if isinstance(column, (Column, Raw)):
                selected.append(column)
                continue

            selected.append(validate_column_identifier(column))

        self._columns = selected

        return self

    def where(
        self, column: str, operator_or_value: Any, value: Any = _MISSING
    ) -> QueryBuilder:
        column = validate_identifier(column)

        if value is _MISSING:
            operator = "="
            value = operator_or_value

        else:
            operator = str(operator_or_value).upper()

        if value is None:
            if operator == "=":
                self._wheres.append((column, "IS", None))
                return self

            if operator in {"!=", "<>"}:
                self._wheres.append((column, "IS NOT", None))
                return self

        allowed = {"=", "!=", "<>", "<", ">", "<=", ">=", "LIKE", "ILIKE", "IN"}

        if operator not in allowed:
            raise ValueError(f"Unsupported operator: {operator}")

        self._wheres.append((column, operator, value))

        return self

    def where_raw(self, sql: str, bindings: list[Any] | None = None) -> QueryBuilder:
        """
        Add a trusted raw WHERE expression.

        `sql` must be application-controlled.
        Dynamic values must use '?' placeholders and `bindings`.
        """
        self._raw_wheres.append(Raw(sql, bindings))
        return self

    def where_null(self, column: str) -> QueryBuilder:
        """Add an 'IS NULL' clause for the specified column."""
        self._raw_wheres.append(Raw(f"{quote_identifier(column)} IS NULL"))
        return self

    def where_not_null(self, column: str) -> QueryBuilder:
        """Add an 'IS NOT NULL' clause for the specified column."""
        self._raw_wheres.append(Raw(f"{quote_identifier(column)} IS NOT NULL"))
        return self

    def join(
        self, table: str, first: str, operator: str, second: str, type_: str = "INNER"
    ) -> QueryBuilder:
        if operator not in {"=", "!=", "<", ">", "<=", ">="}:
            raise ValueError(f"Unsupported join operator: {operator}")
        if type_.upper() not in {"INNER", "LEFT", "RIGHT", "FULL"}:
            raise ValueError(f"Unsupported join type: {type_}")

        self._joins.append(
            (
                type_.upper(),
                quote_identifier(table),
                quote_identifier(first),
                operator,
                quote_identifier(second),
            )
        )
        return self

    def left_join(
        self, table: str, first: str, operator: str, second: str
    ) -> QueryBuilder:
        return self.join(table, first, operator, second, "LEFT")

    def right_join(
        self, table: str, first: str, operator: str, second: str
    ) -> QueryBuilder:
        return self.join(table, first, operator, second, "RIGHT")

    def order_by(self, column: str, direction: str = "ASC") -> QueryBuilder:

        direction = direction.upper()

        if direction not in {"ASC", "DESC"}:
            raise ValueError("Direction must be ASC or DESC")

        self._order_column = quote_identifier(column)
        self._order_direction = direction

        return self

    def limit(self, value: int) -> QueryBuilder:
        if type(value) is not int:
            raise TypeError("LIMIT must be an integer.")

        if value < 0:
            raise ValueError("LIMIT cannot be negative.")

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

    def _compile_select(self, start_idx: int = 1) -> tuple[str, list[Any], int]:
        bindings: list[Any] = []
        select_parts: list[str] = []
        idx = start_idx

        for column in self._columns:
            if isinstance(column, Column):
                select_parts.append(column.to_sql())
                continue

            if isinstance(column, Raw):
                sql = column.sql

                for binding in column.bindings:
                    sql = sql.replace("?", f"${idx}", 1)

                    bindings.append(binding)
                    idx += 1

                select_parts.append(sql)
                continue

            select_parts.append(quote_identifier(column))

        return (", ".join(select_parts), bindings, idx)

    def _compile_joins(self) -> str:
        if not self._joins:
            return ""

        clauses = []
        for type_, table, first, operator, second in self._joins:
            clauses.append(f"{type_} JOIN {table} ON {first} {operator} {second}")
        return " " + " ".join(clauses)

    def _compile_where(self, start_idx: int = 1) -> tuple[str, list[Any], int]:
        clauses: list[str] = []
        bindings: list[Any] = []
        idx = start_idx

        for column, operator, value in self._wheres:
            col_quoted = quote_identifier(column)
            if isinstance(value, QueryBuilder):
                sub_sql, sub_bindings = value.to_sql(idx)

                clauses.append(f"{col_quoted} {operator} ({sub_sql})")

                bindings.extend(sub_bindings)

                idx += len(sub_bindings)

                continue

            if operator == "IN" and isinstance(value, list):
                placeholders: list[str] = []

                for item in value:
                    placeholders.append(f"${idx}")
                    bindings.append(item)
                    idx += 1

                clauses.append(f"{col_quoted} IN ({', '.join(placeholders)})")

                continue

            if value is None and operator in {"IS", "IS NOT"}:
                clauses.append(f"{col_quoted} {operator} NULL")
                continue

            clauses.append(f"{col_quoted} {operator} ${idx}")

            bindings.append(value)

            idx += 1

        #
        # RAW WHERES
        #

        for raw in self._raw_wheres:
            sql = raw.sql

            for binding in raw.bindings:
                sql = sql.replace(
                    "?",
                    f"${idx}",
                    1,
                )

                bindings.append(binding)

                idx += 1

            clauses.append(sql)

        if not clauses:
            return "", [], idx

        return (
            " WHERE " + " AND ".join(clauses),
            bindings,
            idx,
        )

    def to_sql(self, start_idx: int = 1) -> tuple[str, list[Any]]:
        """Compiles the entire query to raw SQL and bindings."""
        select_sql, select_bindings, idx = self._compile_select(start_idx)
        join_sql = self._compile_joins()
        where_sql, where_bindings, idx = self._compile_where(idx)

        bindings = select_bindings + where_bindings
        sql = f"SELECT {select_sql} FROM {quote_identifier(self._table)}{join_sql}{where_sql}"

        if self._order_column:
            sql += f" ORDER BY {self._order_column} {self._order_direction}"

        if self._limit_value is not None:
            sql += f" LIMIT {self._limit_value}"

        return sql, bindings

    async def get(self) -> list[dict[str, Any]]:
        sql, bindings = self.to_sql()
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

    async def _aggregate(self, function: str, column: str) -> Any:
        col_safe = quote_identifier(column) if column != "*" else "*"

        # Store original columns to restore later
        original = self._columns

        try:
            self._columns = [Raw(f"{function}({col_safe}) AS agg_result")]
            row = await self.first()
        finally:
            self._columns = original

        return row["agg_result"] if row else None

    async def count(self, column: str = "*") -> int:
        return cast(int, await self._aggregate("COUNT", column))

    async def sum(self, column: str) -> Any:
        return await self._aggregate("SUM", column)

    async def max(self, column: str) -> Any:
        return await self._aggregate("MAX", column)

    async def min(self, column: str) -> Any:
        return await self._aggregate("MIN", column)

    async def avg(self, column: str) -> Any:
        return await self._aggregate("AVG", column)

    async def insert(self, data: dict[str, Any]) -> int:
        columns = [quote_identifier(column) for column in data]

        values = list(data.values())

        placeholders = [f"${i}" for i in range(1, len(values) + 1)]

        sql = (
            f"INSERT INTO {quote_identifier(self._table)} "
            f"({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"RETURNING *"
        )

        conn = await self._connection()

        try:
            row = await conn.fetchrow(sql, *values)

            if row is None:
                raise RuntimeError("Insert failed")

            if "id" in row and row["id"] is not None:
                try:
                    return int(row["id"])
                except (ValueError, TypeError):
                    return 0

            return 0

        finally:
            await self._release(conn)

    async def update(self, data: dict[str, Any]) -> str:
        bindings: list[Any] = []

        sets: list[str] = []

        idx = 1

        for column, value in data.items():
            safe_column = quote_identifier(column)
            sets.append(f"{safe_column} = ${idx}")
            bindings.append(value)
            idx += 1

        where_sql, where_bindings, _ = self._compile_where(idx)

        bindings.extend(where_bindings)

        sql = f"UPDATE {quote_identifier(self._table)} SET {', '.join(sets)}{where_sql}"

        conn = await self._connection()

        try:
            result = await conn.execute(sql, *bindings)
            return cast(str, result)

        finally:
            await self._release(conn)

    async def delete(self) -> str:
        where_sql, bindings, _ = self._compile_where()

        sql = f"DELETE FROM {quote_identifier(self._table)}{where_sql}"

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
