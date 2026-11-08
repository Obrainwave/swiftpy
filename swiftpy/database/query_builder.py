from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast, overload

import asyncpg

from swiftpy.core.container import get_container
from swiftpy.database.identifiers import (
    quote_identifier,
    validate_column_identifier,
    validate_identifier,
)
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import get_active_connection

_MISSING = object()


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


class QueryError(ValueError, RuntimeError):
    """Raised when constructing or executing an invalid or unguarded SQL query."""


def escape_like(value: str) -> str:
    """Escape SQL LIKE/ILIKE wildcard characters (\\, %, _) for literal substring matching."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class QueryBuilder:
    @classmethod
    def table(cls, table_name: str) -> QueryBuilder:
        return cls(table_name)

    def __init__(self, table_name: str) -> None:
        self._table = validate_identifier(table_name, allow_dotted=True)
        self._columns: list[SelectExpression] = ["*"]
        self._wheres: list[tuple[str, str, Any]] = []
        self._joins: list[tuple[str, str, str, str, str]] = []
        self._raw_wheres: list[Raw] = []
        self._order_column: str | None = None
        self._order_direction: str = "ASC"
        self._limit_value: int | None = None
        self._allow_all: bool = False

    def clone(self) -> QueryBuilder:
        """Create an independent copy of this QueryBuilder so terminal operations do not mutate it."""
        cloned = QueryBuilder(self._table)
        cloned._columns = list(self._columns)
        cloned._wheres = list(self._wheres)
        cloned._joins = list(self._joins)
        cloned._raw_wheres = list(self._raw_wheres)
        cloned._order_column = self._order_column
        cloned._order_direction = self._order_direction
        cloned._limit_value = self._limit_value
        cloned._allow_all = self._allow_all
        return cloned

    def allow_all(self) -> QueryBuilder:
        """Explicitly permit mass UPDATE or DELETE queries without a WHERE clause (Decision D-10)."""
        self._allow_all = True
        return self

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
        column = validate_identifier(column, allow_dotted=True, allow_wildcard=False)

        if value is _MISSING:
            operator = "="
            value = operator_or_value

        else:
            operator = " ".join(str(operator_or_value).upper().split())

        allowed = {
            "=",
            "!=",
            "<>",
            "<",
            ">",
            "<=",
            ">=",
            "LIKE",
            "ILIKE",
            "IN",
            "NOT IN",
        }

        if operator not in allowed:
            raise QueryError(f"Unsupported operator: {operator}")

        if value is None:
            if operator == "=":
                self._wheres.append((column, "IS", None))
                return self

            if operator in {"!=", "<>"}:
                self._wheres.append((column, "IS NOT", None))
                return self

            raise QueryError(
                f"Cannot compare NULL using operator '{operator}'. Use '=' / '!=' or where_null() / where_not_null()."
            )

        if operator in {"IN", "NOT IN"} and not isinstance(value, (list, QueryBuilder)):
            raise QueryError(
                f"Operator '{operator}' requires a list or QueryBuilder subquery, got {type(value).__name__}."
            )

        self._wheres.append((column, operator, value))

        return self

    def where_in(self, column: str, values: list[Any] | QueryBuilder) -> QueryBuilder:
        """Add a WHERE column IN (...) clause."""
        return self.where(column, "IN", values)

    def where_not_in(
        self, column: str, values: list[Any] | QueryBuilder
    ) -> QueryBuilder:
        """Add a WHERE column NOT IN (...) clause."""
        return self.where(column, "NOT IN", values)

    def where_like(
        self,
        column: str,
        value: str,
        *,
        case_insensitive: bool = False,
        match: str = "contains",
    ) -> QueryBuilder:
        """
        Add a safe LIKE / ILIKE clause that escapes %, _, and \\ wildcards in user input
        and emits an explicit ESCAPE '\\' clause.
        """
        escaped = escape_like(str(value))
        match_mode = match.lower()
        if match_mode == "contains":
            pattern = f"%{escaped}%"
        elif match_mode in {"starts_with", "prefix"}:
            pattern = f"{escaped}%"
        elif match_mode in {"ends_with", "suffix"}:
            pattern = f"%{escaped}"
        elif match_mode == "exact":
            pattern = escaped
        else:
            raise QueryError(f"Unsupported LIKE match mode: {match}")

        op = "ILIKE" if case_insensitive else "LIKE"
        return self.where(column, op, pattern)

    def where_ilike(
        self,
        column: str,
        value: str,
        *,
        match: str = "contains",
    ) -> QueryBuilder:
        """Add a safe case-insensitive ILIKE clause with escaped wildcards."""
        return self.where_like(column, value, case_insensitive=True, match=match)

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
        self._raw_wheres.append(
            Raw(f"{quote_identifier(column, allow_wildcard=False)} IS NULL")
        )
        return self

    def where_not_null(self, column: str) -> QueryBuilder:
        """Add an 'IS NOT NULL' clause for the specified column."""
        self._raw_wheres.append(
            Raw(f"{quote_identifier(column, allow_wildcard=False)} IS NOT NULL")
        )
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
                quote_identifier(table, allow_wildcard=False),
                quote_identifier(first, allow_wildcard=False),
                operator,
                quote_identifier(second, allow_wildcard=False),
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

        self._order_column = quote_identifier(column, allow_wildcard=False)
        self._order_direction = direction

        return self

    def limit(self, value: int) -> QueryBuilder:
        if type(value) is not int:
            raise TypeError("LIMIT must be an integer.")

        if value < 0:
            raise ValueError("LIMIT cannot be negative.")

        self._limit_value = value

        return self

    @staticmethod
    def _get_pool() -> asyncpg.Pool:
        container = get_container()
        db_pool = container.get_singleton(DatabasePool)
        if db_pool is None:
            db_pool = container.resolve(DatabasePool)
        return db_pool.get_pool()

    async def _connection(self) -> asyncpg.Connection:
        tx_conn = get_active_connection()

        if tx_conn is not None:
            return tx_conn

        pool = self._get_pool()
        return await pool.acquire()

    async def _release(self, conn: asyncpg.Connection) -> None:
        if get_active_connection() is not None:
            return

        pool = self._get_pool()
        await pool.release(conn)

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

            if operator in {"IN", "NOT IN"} and isinstance(value, list):
                if not value:
                    clauses.append("FALSE" if operator == "IN" else "TRUE")
                    continue

                placeholders: list[str] = []

                for item in value:
                    placeholders.append(f"${idx}")
                    bindings.append(item)
                    idx += 1

                clauses.append(f"{col_quoted} {operator} ({', '.join(placeholders)})")

                continue

            if value is None and operator in {"IS", "IS NOT"}:
                clauses.append(f"{col_quoted} {operator} NULL")
                continue

            if operator in {"LIKE", "ILIKE"}:
                clauses.append(f"{col_quoted} {operator} ${idx} ESCAPE '\\'")
                bindings.append(value)
                idx += 1
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
        rows = await self.clone().limit(1).get()
        return rows[0] if rows else None

    async def exists(self) -> bool:
        """Return True if at least one row matches the current query."""
        cloned = self.clone()
        cloned._columns = [Raw("1")]
        cloned._order_column = None
        row = await cloned.first()
        return row is not None

    async def _aggregate(self, function: str, column: str) -> Any:
        col_safe = (
            quote_identifier(column, allow_wildcard=False) if column != "*" else "*"
        )
        cloned = self.clone()
        cloned._columns = [Raw(f"{function}({col_safe}) AS agg_result")]
        cloned._order_column = None
        row = await cloned.first()
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

    async def insert_returning(
        self,
        data: dict[str, Any],
        *,
        returning: Sequence[str] | str | None = None,
    ) -> dict[str, Any]:
        """Insert a row and return the RETURNING row as a dictionary."""
        if not isinstance(data, dict) or not data:
            raise QueryError("Cannot execute INSERT with empty data dictionary.")

        columns = [
            quote_identifier(column, allow_dotted=False, allow_wildcard=False)
            for column in data
        ]

        values = list(data.values())

        placeholders = [f"${i}" for i in range(1, len(values) + 1)]

        if returning is None or returning == "*":
            returning_clause = "*"
        elif isinstance(returning, str):
            returning_clause = quote_identifier(
                returning, allow_dotted=False, allow_wildcard=False
            )
        else:
            returning_cols = [
                quote_identifier(col, allow_dotted=False, allow_wildcard=False)
                for col in returning
            ]
            if not returning_cols:
                raise ValueError("returning list cannot be empty")
            returning_clause = ", ".join(returning_cols)

        sql = (
            f"INSERT INTO {quote_identifier(self._table)} "
            f"({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"RETURNING {returning_clause}"
        )

        conn = await self._connection()

        try:
            row = await conn.fetchrow(sql, *values)

            if row is None:
                raise RuntimeError("Insert failed")

            return dict(row)

        finally:
            await self._release(conn)

    @overload
    async def insert(self, data: dict[str, Any], *, returning: None = None) -> int: ...

    @overload
    async def insert(
        self, data: dict[str, Any], *, returning: Sequence[str] | str
    ) -> Any: ...

    async def insert(
        self,
        data: dict[str, Any],
        *,
        returning: Sequence[str] | str | None = None,
    ) -> Any:
        row = await self.insert_returning(data, returning=returning)

        if returning is not None:
            if isinstance(returning, str) and returning != "*":
                return row.get(returning)
            if not isinstance(returning, str) and len(returning) == 1:
                return row.get(returning[0])
            return row

        if "id" in row and row["id"] is not None:
            try:
                return int(row["id"])
            except (ValueError, TypeError):
                return 0

        return 0

    async def update(self, data: dict[str, Any]) -> str:
        if not isinstance(data, dict) or not data:
            raise QueryError("Cannot execute UPDATE with empty data dictionary.")

        if not self._wheres and not self._raw_wheres and not self._allow_all:
            raise QueryError(
                "Refusing to execute UPDATE without a WHERE clause. Call .allow_all() to update all rows explicitly."
            )

        bindings: list[Any] = []

        sets: list[str] = []

        idx = 1

        for column, value in data.items():
            safe_column = quote_identifier(
                column, allow_dotted=False, allow_wildcard=False
            )
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
        if not self._wheres and not self._raw_wheres and not self._allow_all:
            raise QueryError(
                "Refusing to execute DELETE without a WHERE clause. Call .allow_all() to delete all rows explicitly."
            )

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
