from __future__ import annotations

import inspect
import math
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast, overload

import asyncpg

from swiftpy.core.container import get_container
from swiftpy.database.exceptions import RecordNotFoundError
from swiftpy.database.identifiers import (
    quote_identifier,
    validate_column_identifier,
    validate_identifier,
)
from swiftpy.database.pagination.paginator import Paginator
from swiftpy.database.pagination.simple_paginator import SimplePaginator
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import get_active_connection

_MISSING = object()

_HAVING_AGGREGATE_RE = re.compile(
    r"^(COUNT|SUM|AVG|MIN|MAX)\(\s*(DISTINCT\s+)?(\*|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*\)$",
    re.IGNORECASE,
)


def _validate_boolean(boolean: str, *, context: str = "WHERE") -> str:
    if not isinstance(boolean, str):
        raise TypeError(f"{context} boolean operator must be a string.")
    bool_op = boolean.upper().strip()
    if bool_op not in {"AND", "OR"}:
        raise ValueError(
            f"{context} boolean operator must be 'AND' or 'OR', got {boolean!r}."
        )
    return bool_op


def _parse_having_aggregate(column: str) -> str | None:
    match = _HAVING_AGGREGATE_RE.fullmatch(column.strip())
    if match is None:
        return None
    func = match.group(1).upper()
    distinct_kw = "DISTINCT " if match.group(2) else ""
    target = match.group(3)
    if target == "*":
        if func != "COUNT" or distinct_kw:
            raise ValueError(
                f"Wildcard '*' in HAVING is only allowed with COUNT(*), got {column!r}."
            )
        return "COUNT(*)"
    safe_target = validate_identifier(target, allow_dotted=True, allow_wildcard=False)
    return f"{func}({distinct_kw}{quote_identifier(safe_target, allow_dotted=True, allow_wildcard=False)})"


def _normalize_raw_bindings(
    sql: str,
    bindings: Sequence[Any] | None,
    *,
    method: str,
) -> list[Any]:
    if bindings is None:
        bindings_list: list[Any] = []
    elif isinstance(bindings, (str, bytes, bytearray)) or not isinstance(
        bindings, Sequence
    ):
        raise TypeError(
            f"{method} bindings must be a sequence (list or tuple), not {type(bindings).__name__}."
        )
    else:
        bindings_list = list(bindings)

    placeholder_count = sql.count("?")
    if placeholder_count != len(bindings_list):
        raise QueryError(
            f"Placeholder count mismatch in {method}: SQL expression contains "
            f"{placeholder_count} '?' placeholder(s), but {len(bindings_list)} binding(s) were provided."
        )
    return bindings_list


def _compile_raw_sql(raw: Raw, start_idx: int) -> tuple[str, list[Any], int]:
    if not raw.bindings:
        return raw.sql, [], start_idx
    idx = start_idx

    def _replace(_match: re.Match[str]) -> str:
        nonlocal idx
        placeholder = f"${idx}"
        idx += 1
        return placeholder

    compiled_sql = re.sub(r"\?", _replace, raw.sql, count=len(raw.bindings))
    return compiled_sql, list(raw.bindings), idx


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


@dataclass(slots=True, frozen=True)
class _WhereClause:
    boolean: str
    column: str
    operator: str
    value: Any


@dataclass(slots=True, frozen=True)
class _WhereBetweenClause:
    boolean: str
    column: str
    values: tuple[Any, Any]
    negated: bool = False


@dataclass(slots=True, frozen=True)
class _WhereRawClause:
    boolean: str
    raw: Raw


@dataclass(slots=True, frozen=True)
class _WhereGroupClause:
    boolean: str
    clauses: tuple[_WhereClauseType, ...]


_WhereClauseType = (
    _WhereClause | _WhereBetweenClause | _WhereRawClause | _WhereGroupClause
)


def _clone_where_clause(wc: _WhereClauseType) -> _WhereClauseType:
    if isinstance(wc, _WhereRawClause):
        return _WhereRawClause(wc.boolean, Raw(wc.raw.sql, list(wc.raw.bindings)))
    if isinstance(wc, _WhereGroupClause):
        return _WhereGroupClause(
            wc.boolean,
            tuple(_clone_where_clause(child) for child in wc.clauses),
        )
    return wc



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
        self._where_clauses: list[_WhereClauseType] = []
        self._joins: list[tuple[str, str, str, str, str]] = []
        self._raw_wheres: list[Raw] = []
        self._order_column: str | None = None
        self._order_direction: str = "ASC"
        self._order_random: bool = False
        self._limit_value: int | None = None
        self._offset_value: int | None = None
        self._groups: list[str] = []
        self._havings: list[tuple[str, str, str, Any]] = []
        self._raw_havings: list[tuple[str, Raw]] = []
        self._select_aliases: dict[str, str] = {}
        self._distinct: bool = False
        self._distinct_columns: list[str] = []
        self._allow_all: bool = False

    def clone(self) -> QueryBuilder:
        """Create an independent copy of this QueryBuilder so terminal operations do not mutate it."""
        cloned = QueryBuilder(self._table)
        cloned._columns = [
            Raw(col.sql, list(col.bindings)) if isinstance(col, Raw) else col
            for col in self._columns
        ]
        cloned._wheres = list(self._wheres)
        cloned._where_clauses = [_clone_where_clause(wc) for wc in self._where_clauses]
        cloned._joins = list(self._joins)
        cloned._raw_wheres = [
            Raw(rw.sql, list(rw.bindings)) for rw in self._raw_wheres
        ]
        cloned._order_column = self._order_column
        cloned._order_direction = self._order_direction
        cloned._order_random = self._order_random
        cloned._limit_value = self._limit_value
        cloned._offset_value = self._offset_value
        cloned._groups = list(self._groups)
        cloned._havings = list(self._havings)
        cloned._raw_havings = [
            (b, Raw(rh.sql, list(rh.bindings))) for b, rh in self._raw_havings
        ]
        cloned._select_aliases = dict(self._select_aliases)
        cloned._distinct = self._distinct
        cloned._distinct_columns = list(self._distinct_columns)
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

    def distinct(self, *columns: str) -> QueryBuilder:
        """
        Apply DISTINCT or PostgreSQL DISTINCT ON (...) to the query projection.
        Calling distinct() with no arguments compiles to SELECT DISTINCT.
        Calling distinct("col1", "col2") compiles to SELECT DISTINCT ON ("col1", "col2").
        """
        self._distinct = True
        validated_cols: list[str] = []
        for col in columns:
            validated_cols.append(
                validate_identifier(col, allow_dotted=True, allow_wildcard=False)
            )
        self._distinct_columns = validated_cols
        return self

    def select_raw(
        self, sql: str, bindings: Sequence[Any] | None = None
    ) -> QueryBuilder:
        """
        Add a raw SQL expression to the SELECT projection.

        SECURITY CONTRACT:
        - The `sql` parameter is trusted, application-authored SQL. Never interpolate
          or concatenate untrusted user input directly into `sql`.
        - All dynamic, user-supplied values MUST be passed as parameterized positional
          placeholders via `bindings` (using `?` in `sql`), which are safely compiled into
          numbered PostgreSQL placeholders (`$1, $2, ...`).
        """
        if not isinstance(sql, str):
            raise TypeError("select_raw SQL expression must be a string.")
        bindings_list = _normalize_raw_bindings(sql, bindings, method="select_raw")
        if self._columns == ["*"]:
            self._columns = []
        self._columns.append(Raw(sql, bindings_list))
        return self

    def select_count(
        self, column: str = "*", alias: str | None = None
    ) -> QueryBuilder:
        """Add a COUNT aggregate expression to the SELECT projection."""
        if column != "*":
            safe_col = quote_identifier(column, allow_dotted=True, allow_wildcard=False)
            expr = f"COUNT({safe_col})"
        else:
            expr = "COUNT(*)"

        if alias is not None:
            safe_alias = validate_identifier(alias, allow_dotted=False, allow_wildcard=False)
            self._select_aliases[safe_alias] = expr
            sql_expr = f'{expr} AS "{safe_alias}"'
        else:
            sql_expr = expr

        if self._columns == ["*"]:
            self._columns = []
        self._columns.append(Raw(sql_expr))
        return self

    def select_sum(
        self, column: str, alias: str | None = None
    ) -> QueryBuilder:
        """Add a SUM aggregate expression to the SELECT projection."""
        safe_col = quote_identifier(column, allow_dotted=True, allow_wildcard=False)
        expr = f"SUM({safe_col})"

        if alias is not None:
            safe_alias = validate_identifier(alias, allow_dotted=False, allow_wildcard=False)
            self._select_aliases[safe_alias] = expr
            sql_expr = f'{expr} AS "{safe_alias}"'
        else:
            sql_expr = expr

        if self._columns == ["*"]:
            self._columns = []
        self._columns.append(Raw(sql_expr))
        return self

    def select_avg(
        self, column: str, alias: str | None = None
    ) -> QueryBuilder:
        """Add an AVG aggregate expression to the SELECT projection."""
        safe_col = quote_identifier(column, allow_dotted=True, allow_wildcard=False)
        expr = f"AVG({safe_col})"

        if alias is not None:
            safe_alias = validate_identifier(alias, allow_dotted=False, allow_wildcard=False)
            self._select_aliases[safe_alias] = expr
            sql_expr = f'{expr} AS "{safe_alias}"'
        else:
            sql_expr = expr

        if self._columns == ["*"]:
            self._columns = []
        self._columns.append(Raw(sql_expr))
        return self

    def select_min(
        self, column: str, alias: str | None = None
    ) -> QueryBuilder:
        """Add a MIN aggregate expression to the SELECT projection."""
        safe_col = quote_identifier(column, allow_dotted=True, allow_wildcard=False)
        expr = f"MIN({safe_col})"

        if alias is not None:
            safe_alias = validate_identifier(alias, allow_dotted=False, allow_wildcard=False)
            self._select_aliases[safe_alias] = expr
            sql_expr = f'{expr} AS "{safe_alias}"'
        else:
            sql_expr = expr

        if self._columns == ["*"]:
            self._columns = []
        self._columns.append(Raw(sql_expr))
        return self

    def select_max(
        self, column: str, alias: str | None = None
    ) -> QueryBuilder:
        """Add a MAX aggregate expression to the SELECT projection."""
        safe_col = quote_identifier(column, allow_dotted=True, allow_wildcard=False)
        expr = f"MAX({safe_col})"

        if alias is not None:
            safe_alias = validate_identifier(alias, allow_dotted=False, allow_wildcard=False)
            self._select_aliases[safe_alias] = expr
            sql_expr = f'{expr} AS "{safe_alias}"'
        else:
            sql_expr = expr

        if self._columns == ["*"]:
            self._columns = []
        self._columns.append(Raw(sql_expr))
        return self

    def where(
        self,
        column: str,
        operator_or_value: Any,
        value: Any = _MISSING,
        *,
        boolean: str = "AND",
    ) -> QueryBuilder:
        column = validate_identifier(column, allow_dotted=True, allow_wildcard=False)
        bool_op = _validate_boolean(boolean, context="WHERE")

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
                self._where_clauses.append(_WhereClause(bool_op, column, "IS", None))
                return self

            if operator in {"!=", "<>"}:
                self._wheres.append((column, "IS NOT", None))
                self._where_clauses.append(_WhereClause(bool_op, column, "IS NOT", None))
                return self

            raise QueryError(
                f"Cannot compare NULL using operator '{operator}'. Use '=' / '!=' or where_null() / where_not_null()."
            )

        if operator in {"IN", "NOT IN"} and not isinstance(value, (list, tuple, QueryBuilder)):
            raise QueryError(
                f"Operator '{operator}' requires a list, tuple, or QueryBuilder subquery, got {type(value).__name__}."
            )

        self._wheres.append((column, operator, value))
        self._where_clauses.append(_WhereClause(bool_op, column, operator, value))

        return self

    def or_where(
        self,
        column: str,
        operator_or_value: Any,
        value: Any = _MISSING,
    ) -> QueryBuilder:
        """Add an OR WHERE clause to the query."""
        return self.where(column, operator_or_value, value, boolean="OR")

    def where_in(
        self,
        column: str,
        values: list[Any] | tuple[Any, ...] | QueryBuilder,
        *,
        boolean: str = "AND",
    ) -> QueryBuilder:
        """Add a WHERE column IN (...) clause."""
        return self.where(column, "IN", values, boolean=boolean)

    def or_where_in(
        self,
        column: str,
        values: list[Any] | tuple[Any, ...] | QueryBuilder,
    ) -> QueryBuilder:
        """Add an OR WHERE column IN (...) clause."""
        return self.where_in(column, values, boolean="OR")

    def where_not_in(
        self,
        column: str,
        values: list[Any] | tuple[Any, ...] | QueryBuilder,
        *,
        boolean: str = "AND",
    ) -> QueryBuilder:
        """Add a WHERE column NOT IN (...) clause."""
        return self.where(column, "NOT IN", values, boolean=boolean)

    def or_where_not_in(
        self,
        column: str,
        values: list[Any] | tuple[Any, ...] | QueryBuilder,
    ) -> QueryBuilder:
        """Add an OR WHERE column NOT IN (...) clause."""
        return self.where_not_in(column, values, boolean="OR")

    def where_like(
        self,
        column: str,
        value: str,
        *,
        case_insensitive: bool = False,
        match: str = "contains",
        boolean: str = "AND",
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
        return self.where(column, op, pattern, boolean=boolean)

    def where_ilike(
        self,
        column: str,
        value: str,
        *,
        match: str = "contains",
        boolean: str = "AND",
    ) -> QueryBuilder:
        """Add a safe case-insensitive ILIKE clause with escaped wildcards."""
        return self.where_like(column, value, case_insensitive=True, match=match, boolean=boolean)

    def or_where_like(
        self,
        column: str,
        value: str,
        *,
        case_insensitive: bool = False,
        match: str = "contains",
    ) -> QueryBuilder:
        """Add an OR WHERE LIKE / ILIKE clause with escaped wildcards."""
        return self.where_like(
            column,
            value,
            case_insensitive=case_insensitive,
            match=match,
            boolean="OR",
        )

    def or_where_ilike(
        self,
        column: str,
        value: str,
        *,
        match: str = "contains",
    ) -> QueryBuilder:
        """Add a safe OR case-insensitive ILIKE clause with escaped wildcards."""
        return self.where_like(
            column,
            value,
            case_insensitive=True,
            match=match,
            boolean="OR",
        )

    def where_between(
        self,
        column: str,
        values: Sequence[Any],
        *,
        boolean: str = "AND",
    ) -> QueryBuilder:
        """Add a WHERE column BETWEEN min AND max clause."""
        if isinstance(values, (str, bytes)) or len(values) != 2:
            raise QueryError("where_between expects a sequence of exactly two elements (min, max).")
        col = validate_identifier(column, allow_dotted=True, allow_wildcard=False)
        bool_op = _validate_boolean(boolean, context="WHERE")
        self._where_clauses.append(
            _WhereBetweenClause(bool_op, col, (values[0], values[1]), negated=False)
        )
        return self

    def where_not_between(
        self,
        column: str,
        values: Sequence[Any],
        *,
        boolean: str = "AND",
    ) -> QueryBuilder:
        """Add a WHERE column NOT BETWEEN min AND max clause."""
        if isinstance(values, (str, bytes)) or len(values) != 2:
            raise QueryError("where_not_between expects a sequence of exactly two elements (min, max).")
        col = validate_identifier(column, allow_dotted=True, allow_wildcard=False)
        bool_op = _validate_boolean(boolean, context="WHERE")
        self._where_clauses.append(
            _WhereBetweenClause(bool_op, col, (values[0], values[1]), negated=True)
        )
        return self

    def or_where_between(
        self,
        column: str,
        values: Sequence[Any],
    ) -> QueryBuilder:
        """Add an OR WHERE column BETWEEN min AND max clause."""
        return self.where_between(column, values, boolean="OR")

    def or_where_not_between(
        self,
        column: str,
        values: Sequence[Any],
    ) -> QueryBuilder:
        """Add an OR WHERE column NOT BETWEEN min AND max clause."""
        return self.where_not_between(column, values, boolean="OR")

    def where_group(
        self,
        callback: Callable[[QueryBuilder], Any],
        *,
        boolean: str = "AND",
    ) -> QueryBuilder:
        """
        Add a nested WHERE group with explicit boolean grouping (parentheses).

        Example:
            Query.table("users").where("status", "active").where_group(
                lambda q: q.where("role", "admin").or_where("role", "moderator")
            ).where("age", ">=", 18)
        Compiles to:
            WHERE "status" = $1 AND ("role" = $2 OR "role" = $3) AND "age" >= $4
        """
        bool_op = _validate_boolean(boolean, context="WHERE")
        sub_builder = QueryBuilder(self._table)
        callback(sub_builder)
        if sub_builder._where_clauses:
            self._where_clauses.append(
                _WhereGroupClause(bool_op, tuple(sub_builder._where_clauses))
            )
        return self

    def or_where_group(
        self,
        callback: Callable[[QueryBuilder], Any],
    ) -> QueryBuilder:
        """Add an OR nested WHERE group."""
        return self.where_group(callback, boolean="OR")

    def where_raw(
        self,
        sql: str,
        bindings: Sequence[Any] | None = None,
        *,
        boolean: str = "AND",
    ) -> QueryBuilder:
        """
        Add a trusted raw WHERE expression.

        SECURITY CONTRACT:
        - The `sql` parameter is trusted, application-controlled SQL. Never interpolate
          or concatenate untrusted user input directly into `sql`.
        - All dynamic, user-supplied values MUST be passed as parameterized positional
          placeholders via `bindings` (using `?` in `sql`), which are safely compiled into
          numbered PostgreSQL placeholders (`$1, $2, ...`).
        """
        if not isinstance(sql, str):
            raise TypeError("where_raw SQL expression must be a string.")
        bool_op = _validate_boolean(boolean, context="WHERE")
        bindings_list = _normalize_raw_bindings(sql, bindings, method="where_raw")
        raw = Raw(sql, bindings_list)
        self._raw_wheres.append(raw)
        self._where_clauses.append(_WhereRawClause(bool_op, raw))
        return self

    def or_where_raw(
        self,
        sql: str,
        bindings: Sequence[Any] | None = None,
    ) -> QueryBuilder:
        """Add an OR WHERE raw expression."""
        return self.where_raw(sql, bindings, boolean="OR")

    def where_null(self, column: str, *, boolean: str = "AND") -> QueryBuilder:
        """Add an 'IS NULL' clause for the specified column."""
        bool_op = _validate_boolean(boolean, context="WHERE")
        safe_col = validate_identifier(column, allow_dotted=True, allow_wildcard=False)
        raw = Raw(f"{quote_identifier(safe_col, allow_wildcard=False)} IS NULL")
        self._raw_wheres.append(raw)
        self._where_clauses.append(_WhereRawClause(bool_op, raw))
        return self

    def or_where_null(self, column: str) -> QueryBuilder:
        """Add an 'OR ... IS NULL' clause for the specified column."""
        return self.where_null(column, boolean="OR")

    def where_not_null(self, column: str, *, boolean: str = "AND") -> QueryBuilder:
        """Add an 'IS NOT NULL' clause for the specified column."""
        bool_op = _validate_boolean(boolean, context="WHERE")
        safe_col = validate_identifier(column, allow_dotted=True, allow_wildcard=False)
        raw = Raw(f"{quote_identifier(safe_col, allow_wildcard=False)} IS NOT NULL")
        self._raw_wheres.append(raw)
        self._where_clauses.append(_WhereRawClause(bool_op, raw))
        return self

    def or_where_not_null(self, column: str) -> QueryBuilder:
        """Add an 'OR ... IS NOT NULL' clause for the specified column."""
        return self.where_not_null(column, boolean="OR")

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

        self._order_random = False
        self._order_column = quote_identifier(column, allow_wildcard=False)
        self._order_direction = direction

        return self

    def latest(self, column: str = "created_at") -> QueryBuilder:
        """Order records descending by the specified column (default: created_at)."""
        return self.order_by(column, "DESC")

    def oldest(self, column: str = "created_at") -> QueryBuilder:
        """Order records ascending by the specified column (default: created_at)."""
        return self.order_by(column, "ASC")

    def in_random_order(self) -> QueryBuilder:
        """Order records randomly using PostgreSQL RANDOM()."""
        self._order_random = True
        return self

    def when(
        self,
        condition: Any,
        callback: Callable[[QueryBuilder, Any], Any],
        default: Callable[[QueryBuilder, Any], Any] | None = None,
    ) -> QueryBuilder:
        """Apply the callback if condition is truthy; otherwise apply default callback if provided."""
        if condition:
            res = callback(self, condition)
            return res if isinstance(res, QueryBuilder) else self
        elif default is not None:
            res = default(self, condition)
            return res if isinstance(res, QueryBuilder) else self
        return self

    def unless(
        self,
        condition: Any,
        callback: Callable[[QueryBuilder, Any], Any],
        default: Callable[[QueryBuilder, Any], Any] | None = None,
    ) -> QueryBuilder:
        """Apply the callback if condition is falsy; otherwise apply default callback if provided."""
        return self.when(not condition, callback, default)

    def limit(self, value: int) -> QueryBuilder:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("LIMIT must be an integer.")
        if value < 0:
            raise ValueError("LIMIT cannot be negative.")
        self._limit_value = value
        return self

    def offset(self, value: int) -> QueryBuilder:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("OFFSET must be an integer.")
        if value < 0:
            raise ValueError("OFFSET cannot be negative.")
        self._offset_value = value
        return self

    def take(self, value: int) -> QueryBuilder:
        """Alias for limit() matching Laravel query conventions."""
        return self.limit(value)

    def skip(self, value: int) -> QueryBuilder:
        """Alias for offset() matching Laravel query conventions."""
        return self.offset(value)

    def group_by(self, *columns: str | Sequence[str]) -> QueryBuilder:
        """Add one or more column expressions to the GROUP BY clause."""
        if not columns:
            return self

        flat_cols: list[str] = []
        for item in columns:
            if isinstance(item, (list, tuple)):
                for sub in item:
                    if isinstance(sub, bool) or not isinstance(sub, str):
                        raise TypeError(
                            f"Group by column must be a str, got {type(sub).__name__}."
                        )
                    flat_cols.append(sub)
            elif isinstance(item, str):
                flat_cols.append(item)
            else:
                raise TypeError(
                    f"Group by column must be a str, got {type(item).__name__}."
                )

        for col in flat_cols:
            safe_col = quote_identifier(col, allow_dotted=True, allow_wildcard=False)
            if safe_col not in self._groups:
                self._groups.append(safe_col)

        return self

    def having(
        self,
        column: str,
        operator_or_value: Any,
        value: Any = _MISSING,
        boolean: str = "AND",
    ) -> QueryBuilder:
        """Add a condition to the HAVING clause filtering grouped aggregates."""
        if not isinstance(column, str) or not column.strip():
            raise TypeError("HAVING column or expression must be a non-empty string.")

        if value is _MISSING:
            operator = "="
            val = operator_or_value
        else:
            operator = " ".join(str(operator_or_value).upper().split())
            val = value

        allowed_operators = {
            "=",
            "!=",
            "<>",
            "<",
            ">",
            "<=",
            ">=",
            "LIKE",
            "ILIKE",
            "NOT LIKE",
            "NOT ILIKE",
            "IS",
            "IS NOT",
        }
        if operator not in allowed_operators:
            raise ValueError(f"Operator '{operator}' is not allowed in HAVING clause.")

        col = column.strip()
        if col not in self._select_aliases and _parse_having_aggregate(col) is None:
            validate_identifier(col, allow_dotted=True, allow_wildcard=False)

        bool_op = _validate_boolean(boolean, context="HAVING")

        self._havings.append((bool_op, col, operator, val))
        return self

    def or_having(
        self,
        column: str,
        operator_or_value: Any,
        value: Any = _MISSING,
    ) -> QueryBuilder:
        """Add an OR condition to the HAVING clause filtering grouped aggregates."""
        return self.having(column, operator_or_value, value, boolean="OR")

    def having_raw(
        self,
        sql: str,
        bindings: Sequence[Any] | None = None,
        boolean: str = "AND",
    ) -> QueryBuilder:
        """
        Add a raw SQL condition to the HAVING clause with parameter bindings.

        SECURITY CONTRACT:
        - The `sql` parameter is trusted, application-authored SQL. Never interpolate
          or concatenate untrusted user input directly into `sql`.
        - All dynamic, user-supplied values MUST be passed via `bindings` (using `?` in `sql`),
          which are safely converted into numbered PostgreSQL placeholders (`$1, $2, ...`).
        """
        if not isinstance(sql, str):
            raise TypeError("HAVING raw expression must be a string.")

        bindings_list = _normalize_raw_bindings(sql, bindings, method="having_raw")
        bool_op = _validate_boolean(boolean, context="HAVING")
        self._raw_havings.append((bool_op, Raw(sql, bindings_list)))
        return self

    def or_having_raw(
        self, sql: str, bindings: Sequence[Any] | None = None
    ) -> QueryBuilder:
        """Add an OR raw SQL condition to the HAVING clause with parameter bindings."""
        return self.having_raw(sql, bindings, boolean="OR")

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
                sql, raw_bindings, idx = _compile_raw_sql(column, idx)
                bindings.extend(raw_bindings)
                select_parts.append(sql)
                continue

            select_parts.append(quote_identifier(column))

        select_str = ", ".join(select_parts)
        if self._distinct:
            if self._distinct_columns:
                distinct_on = ", ".join(
                    quote_identifier(c) for c in self._distinct_columns
                )
                select_str = f"DISTINCT ON ({distinct_on}) {select_str}"
            else:
                select_str = f"DISTINCT {select_str}"

        return select_str, bindings, idx

    def _compile_joins(self) -> str:
        if not self._joins:
            return ""

        clauses = []
        for type_, table, first, operator, second in self._joins:
            clauses.append(f"{type_} JOIN {table} ON {first} {operator} {second}")
        return " " + " ".join(clauses)

    @staticmethod
    def _compile_simple_where(
        boolean: str,
        column: str,
        operator: str,
        value: Any,
        idx: int,
    ) -> tuple[tuple[str, str], list[Any], int]:
        col_quoted = quote_identifier(column)
        if isinstance(value, QueryBuilder):
            sub_sql, sub_bindings = value.to_sql(idx)
            return (
                (boolean, f"{col_quoted} {operator} ({sub_sql})"),
                sub_bindings,
                idx + len(sub_bindings),
            )

        if operator in {"IN", "NOT IN"} and isinstance(value, (list, tuple)):
            if not value:
                return (boolean, "FALSE" if operator == "IN" else "TRUE"), [], idx
            placeholders = [f"${idx + i}" for i in range(len(value))]
            return (
                (boolean, f"{col_quoted} {operator} ({', '.join(placeholders)})"),
                list(value),
                idx + len(value),
            )

        if value is None and operator in {"IS", "IS NOT"}:
            return (boolean, f"{col_quoted} {operator} NULL"), [], idx

        if operator in {"LIKE", "ILIKE"}:
            return (
                (boolean, f"{col_quoted} {operator} ${idx} ESCAPE '\\'"),
                [value],
                idx + 1,
            )

        return (boolean, f"{col_quoted} {operator} ${idx}"), [value], idx + 1

    @staticmethod
    def _compile_clauses(
        clause_list: Sequence[_WhereClauseType], start_idx: int
    ) -> tuple[list[tuple[str, str]], list[Any], int]:
        clauses: list[tuple[str, str]] = []
        bindings: list[Any] = []
        idx = start_idx

        for clause in clause_list:
            if isinstance(clause, _WhereBetweenClause):
                col_quoted = quote_identifier(clause.column)
                op_str = "NOT BETWEEN" if clause.negated else "BETWEEN"
                clauses.append(
                    (clause.boolean, f"{col_quoted} {op_str} ${idx} AND ${idx + 1}")
                )
                bindings.extend(clause.values)
                idx += 2
                continue

            if isinstance(clause, _WhereRawClause):
                sql, raw_bindings, idx = _compile_raw_sql(clause.raw, idx)
                bindings.extend(raw_bindings)
                clauses.append((clause.boolean, sql))
                continue

            if isinstance(clause, _WhereGroupClause):
                inner_clauses, inner_bindings, next_idx = QueryBuilder._compile_clauses(
                    clause.clauses, idx
                )
                if not inner_clauses:
                    continue
                inner_parts: list[str] = [inner_clauses[0][1]]
                for bool_op, clause_str in inner_clauses[1:]:
                    inner_parts.append(f"{bool_op} {clause_str}")
                inner_sql = f"({' '.join(inner_parts)})"
                clauses.append((clause.boolean, inner_sql))
                bindings.extend(inner_bindings)
                idx = next_idx
                continue

            if isinstance(clause, _WhereClause):
                compiled_clause, clause_bindings, idx = QueryBuilder._compile_simple_where(
                    clause.boolean, clause.column, clause.operator, clause.value, idx
                )
                clauses.append(compiled_clause)
                bindings.extend(clause_bindings)
                continue

        return clauses, bindings, idx

    def _compile_where(self, start_idx: int = 1) -> tuple[str, list[Any], int]:
        if not self._where_clauses and not self._wheres and not self._raw_wheres:
            return "", [], start_idx

        if self._where_clauses:
            clauses, bindings, idx = self._compile_clauses(self._where_clauses, start_idx)
        else:
            clauses = []
            bindings = []
            idx = start_idx
            for column, operator, value in self._wheres:
                compiled_clause, clause_bindings, idx = QueryBuilder._compile_simple_where(
                    "AND", column, operator, value, idx
                )
                clauses.append(compiled_clause)
                bindings.extend(clause_bindings)

            for raw in self._raw_wheres:
                sql, raw_bindings, idx = _compile_raw_sql(raw, idx)
                bindings.extend(raw_bindings)
                clauses.append(("AND", sql))

        if not clauses:
            return "", [], idx

        sql_parts: list[str] = [clauses[0][1]]
        for bool_op, clause_str in clauses[1:]:
            sql_parts.append(f"{bool_op} {clause_str}")

        return " WHERE " + " ".join(sql_parts), bindings, idx

    def _compile_groups(self) -> str:
        if not self._groups:
            return ""
        return " GROUP BY " + ", ".join(self._groups)

    def _compile_having(self, start_idx: int = 1) -> tuple[str, list[Any], int]:
        clauses: list[tuple[str, str]] = []
        bindings: list[Any] = []
        idx = start_idx

        for bool_op, column, operator, value in self._havings:
            if column in self._select_aliases:
                expr = self._select_aliases[column]
            elif (agg_expr := _parse_having_aggregate(column)) is not None:
                expr = agg_expr
            else:
                expr = quote_identifier(column, allow_dotted=True, allow_wildcard=False)

            if operator in {"IS", "IS NOT"}:
                if value is not None:
                    raise ValueError(
                        f"Operator '{operator}' in HAVING clause only supports None/NULL value."
                    )
                clauses.append((bool_op, f"{expr} {operator} NULL"))
                continue

            clauses.append((bool_op, f"{expr} {operator} ${idx}"))
            bindings.append(value)
            idx += 1

        for bool_op, raw in self._raw_havings:
            sql, raw_bindings, idx = _compile_raw_sql(raw, idx)
            bindings.extend(raw_bindings)
            clauses.append((bool_op, sql))

        if not clauses:
            return "", [], idx

        sql_parts: list[str] = [clauses[0][1]]
        for bool_op, clause_str in clauses[1:]:
            sql_parts.append(f"{bool_op} {clause_str}")

        return " HAVING " + " ".join(sql_parts), bindings, idx

    def to_sql(self, start_idx: int = 1) -> tuple[str, list[Any]]:
        """Compiles the entire query to raw SQL and bindings."""
        select_sql, select_bindings, idx = self._compile_select(start_idx)
        join_sql = self._compile_joins()
        where_sql, where_bindings, idx = self._compile_where(idx)
        group_sql = self._compile_groups()
        having_sql, having_bindings, idx = self._compile_having(idx)

        bindings = select_bindings + where_bindings + having_bindings
        sql = f"SELECT {select_sql} FROM {quote_identifier(self._table)}{join_sql}{where_sql}{group_sql}{having_sql}"

        if self._order_random:
            sql += " ORDER BY RANDOM()"
        elif self._order_column:
            sql += f" ORDER BY {self._order_column} {self._order_direction}"

        if self._limit_value is not None:
            sql += f" LIMIT ${idx}"
            bindings.append(self._limit_value)
            idx += 1

        if self._offset_value is not None:
            sql += f" OFFSET ${idx}"
            bindings.append(self._offset_value)
            idx += 1

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

    async def first_or_fail(self) -> dict[str, Any]:
        """Fetch the first record or raise RecordNotFoundError if no record matches."""
        row = await self.first()
        if row is None:
            raise RecordNotFoundError(f"No query results for table [{self._table}].")
        return row

    async def value(self, column: str) -> Any | None:
        """Fetch the value of a single column from the first matching row."""
        col = validate_identifier(column, allow_dotted=True, allow_wildcard=False)
        clone = self.clone().select(col).limit(1)
        row = await clone.first()
        if row is None:
            return None
        col_name = col.split(".")[-1]
        return row.get(col_name, row.get(col))

    @overload
    async def pluck(self, column: str, key: None = None) -> list[Any]: ...

    @overload
    async def pluck(self, column: str, key: str) -> dict[Any, Any]: ...

    async def pluck(
        self, column: str, key: str | None = None
    ) -> list[Any] | dict[Any, Any]:
        """
        Retrieve an array or dictionary of values for the given column.
        If key is specified, returns a dictionary mapping key column values to target column values.
        """
        col = validate_identifier(column, allow_dotted=True, allow_wildcard=False)
        col_name = col.split(".")[-1]
        if key is not None:
            k_col = validate_identifier(key, allow_dotted=True, allow_wildcard=False)
            k_name = k_col.split(".")[-1]
            clone = self.clone().select(col, k_col)
            rows = await clone.get()
            return {
                r.get(k_name, r.get(k_col)): r.get(col_name, r.get(col))
                for r in rows
            }
        clone = self.clone().select(col)
        rows = await clone.get()
        return [r.get(col_name, r.get(col)) for r in rows]

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
        cloned._limit_value = None
        cloned._offset_value = None
        rows = await cloned.get()
        return rows[0]["agg_result"] if rows else None

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

    async def paginate(
        self, per_page: int = 15, page: int = 1
    ) -> Paginator[dict[str, Any]]:
        """
        Execute the query with length-aware pagination, counting total matching records or groups.
        """
        if isinstance(page, bool) or not isinstance(page, int):
            raise TypeError("page must be an integer.")
        if page < 1:
            raise ValueError("page must be >= 1.")
        if isinstance(per_page, bool) or not isinstance(per_page, int):
            raise TypeError("per_page must be an integer.")
        if per_page < 1:
            raise ValueError("per_page must be >= 1.")

        offset = (page - 1) * per_page

        count_builder = self.clone()
        count_builder._limit_value = None
        count_builder._offset_value = None
        count_builder._order_column = None

        if (
            count_builder._groups
            or count_builder._havings
            or count_builder._raw_havings
        ):
            if count_builder._columns == ["*"] and count_builder._groups:
                count_builder._columns = list(count_builder._groups)

            inner_sql, inner_bindings = count_builder.to_sql(1)
            count_sql = f"SELECT COUNT(*) AS total FROM ({inner_sql}) AS grouped_results"
            conn = await count_builder._connection()
            try:
                val = await conn.fetchval(count_sql, *inner_bindings)
                total = int(val or 0)
            finally:
                await count_builder._release(conn)
        else:
            total = await count_builder.count()

        data_builder = self.clone()
        data_builder.limit(per_page).offset(offset)
        items = await data_builder.get()

        last_page = max(1, math.ceil(total / per_page))
        return Paginator(
            items=items,
            total=total,
            per_page=per_page,
            current_page=page,
            last_page=last_page,
        )

    async def simple_paginate(
        self, per_page: int = 15, page: int = 1
    ) -> SimplePaginator[dict[str, Any]]:
        """
        Execute the query with lightweight pagination without counting total records.
        """
        if isinstance(page, bool) or not isinstance(page, int):
            raise TypeError("page must be an integer.")
        if page < 1:
            raise ValueError("page must be >= 1.")
        if isinstance(per_page, bool) or not isinstance(per_page, int):
            raise TypeError("per_page must be an integer.")
        if per_page < 1:
            raise ValueError("per_page must be >= 1.")

        offset = (page - 1) * per_page

        data_builder = self.clone()
        data_builder.limit(per_page + 1).offset(offset)
        raw_items = await data_builder.get()

        has_more = len(raw_items) > per_page
        items = raw_items[:per_page] if has_more else raw_items

        return SimplePaginator(
            items=items,
            per_page=per_page,
            current_page=page,
            has_more=has_more,
        )

    async def insert_returning(
        self,
        data: dict[str, Any],
        *,
        returning: Sequence[str] | str | None = None,
        allow_default_values: bool = False,
    ) -> dict[str, Any]:
        """Insert a row and return the RETURNING row as a dictionary."""
        if not isinstance(data, dict) or (not data and not allow_default_values):
            raise QueryError("Cannot execute INSERT with empty data dictionary.")

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

        if not data:
            values: list[Any] = []
            sql = (
                f"INSERT INTO {quote_identifier(self._table)} "
                f"DEFAULT VALUES "
                f"RETURNING {returning_clause}"
            )
        else:
            columns = [
                quote_identifier(column, allow_dotted=False, allow_wildcard=False)
                for column in data
            ]
            values = list(data.values())
            placeholders = [f"${i}" for i in range(1, len(values) + 1)]
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

        if not self._where_clauses and not self._wheres and not self._raw_wheres and not self._allow_all:
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
        if not self._where_clauses and not self._wheres and not self._raw_wheres and not self._allow_all:
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

    async def increment(
        self,
        column: str,
        amount: float = 1,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Atomically increment a column's value by amount."""
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise TypeError(
                f"Amount must be a numeric int or float, got {type(amount).__name__}."
            )
        if math.isnan(amount) or math.isinf(amount):
            raise ValueError("Increment amount cannot be NaN or Infinity.")
        if amount < 0:
            raise ValueError("Increment amount must be non-negative. Use decrement() instead.")
        col = validate_identifier(column, allow_dotted=False, allow_wildcard=False)
        col_quoted = quote_identifier(col)

        if extra and column in extra:
            raise QueryError(
                f"Column '{column}' cannot be passed in extra while being incremented."
            )

        if not self._where_clauses and not self._wheres and not self._raw_wheres and not self._allow_all:
            raise QueryError(
                "Refusing to execute increment without a WHERE clause. Call .allow_all() to update all rows explicitly."
            )

        sets: list[str] = [f"{col_quoted} = {col_quoted} + $1"]
        bindings: list[Any] = [amount]
        idx = 2

        if extra:
            for extra_col, extra_val in extra.items():
                safe_col = quote_identifier(
                    validate_identifier(extra_col, allow_dotted=False, allow_wildcard=False)
                )
                sets.append(f"{safe_col} = ${idx}")
                bindings.append(extra_val)
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

    async def decrement(
        self,
        column: str,
        amount: float = 1,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Atomically decrement a column's value by amount."""
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise TypeError(
                f"Amount must be a numeric int or float, got {type(amount).__name__}."
            )
        if math.isnan(amount) or math.isinf(amount):
            raise ValueError("Decrement amount cannot be NaN or Infinity.")
        if amount < 0:
            raise ValueError("Decrement amount must be non-negative. Use increment() instead.")
        col = validate_identifier(column, allow_dotted=False, allow_wildcard=False)
        col_quoted = quote_identifier(col)

        if extra and column in extra:
            raise QueryError(
                f"Column '{column}' cannot be passed in extra while being decremented."
            )

        if not self._where_clauses and not self._wheres and not self._raw_wheres and not self._allow_all:
            raise QueryError(
                "Refusing to execute decrement without a WHERE clause. Call .allow_all() to update all rows explicitly."
            )

        sets: list[str] = [f"{col_quoted} = {col_quoted} - $1"]
        bindings: list[Any] = [amount]
        idx = 2

        if extra:
            for extra_col, extra_val in extra.items():
                safe_col = quote_identifier(
                    validate_identifier(extra_col, allow_dotted=False, allow_wildcard=False)
                )
                sets.append(f"{safe_col} = ${idx}")
                bindings.append(extra_val)
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

    async def chunk(
        self,
        count: int,
        callback: Callable[[list[dict[str, Any]]], Awaitable[bool | None] | bool | None],
    ) -> bool:
        """
        Process records in batches of size count via limit and offset.
        Returning False from callback terminates iteration early.
        """
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("Chunk count must be an integer >= 1.")

        base_query = self.clone()
        base_query._limit_value = None
        base_query._offset_value = None

        page = 1
        while True:
            results = await base_query.clone().offset((page - 1) * count).limit(count).get()
            if not results:
                break

            callback_res = callback(results)
            if inspect.isawaitable(callback_res):
                callback_res = await callback_res

            if callback_res is False:
                return False

            if len(results) < count:
                break
            page += 1

        return True

    async def chunk_by_id(
        self,
        count: int,
        callback: Callable[[list[dict[str, Any]]], Awaitable[bool | None] | bool | None],
        column: str = "id",
    ) -> bool:
        """
        Process records in batches ordered by ID, advancing past the last observed ID.
        Safe against record updates/deletions during iteration.
        """
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("Chunk count must be an integer >= 1.")

        col = validate_identifier(column, allow_dotted=False, allow_wildcard=False)
        last_id: Any = None
        base_query = self.clone()
        base_query._limit_value = None
        base_query._offset_value = None

        while True:
            query = base_query.clone().order_by(col, "ASC").limit(count)
            if last_id is not None:
                query = query.where(col, ">", last_id)

            results = await query.get()
            if not results:
                break

            last_record = results[-1]
            if col not in last_record:
                raise QueryError(
                    f"Column '{col}' not present in query projection for chunk_by_id cursor advancement."
                )
            last_id = last_record.get(col)

            callback_res = callback(results)
            if inspect.isawaitable(callback_res):
                callback_res = await callback_res

            if callback_res is False:
                return False

            if len(results) < count:
                break

        return True

    async def upsert(
        self,
        values: list[dict[str, Any]],
        unique_by: list[str] | str,
        update_columns: list[str] | None = None,
    ) -> int:
        """
        Perform a PostgreSQL INSERT ... ON CONFLICT (unique_by) DO UPDATE / DO NOTHING.
        Returns the number of affected rows.
        """
        if not values or not isinstance(values, list):
            raise QueryError("upsert() requires a non-empty list of dictionary rows.")

        first_keys = set(values[0].keys()) if isinstance(values[0], dict) else set()
        for i, row in enumerate(values):
            if not isinstance(row, dict) or not row:
                raise QueryError("Each row in values must be a non-empty dictionary.")
            if set(row.keys()) != first_keys:
                raise QueryError(
                    f"Row {i} in upsert batch has mismatched keys: expected {sorted(first_keys)}, "
                    f"got {sorted(row.keys())}."
                )

        unique_cols = [unique_by] if isinstance(unique_by, str) else list(unique_by)
        if not unique_cols:
            raise QueryError("unique_by columns must not be empty.")

        for col in unique_cols:
            if col not in first_keys:
                raise QueryError(f"Conflict key column '{col}' is not present in row data.")

        # Detect duplicate conflict keys in input batch upfront (prevents PostgreSQL cardinality_violation)
        seen_conflict_keys: set[tuple[Any, ...]] = set()
        for i, row in enumerate(values):
            conflict_key = tuple(row.get(col) for col in unique_cols)
            if conflict_key in seen_conflict_keys:
                raise QueryError(
                    f"Duplicate conflict key {conflict_key} detected at row {i} in upsert batch. "
                    "PostgreSQL raises cardinality_violation if multiple rows in the same batch target the same conflicting record."
                )
            seen_conflict_keys.add(conflict_key)

        safe_unique = [
            quote_identifier(validate_identifier(c, allow_dotted=False, allow_wildcard=False))
            for c in unique_cols
        ]

        columns = list(values[0].keys())
        safe_columns = [
            quote_identifier(validate_identifier(c, allow_dotted=False, allow_wildcard=False))
            for c in columns
        ]

        if update_columns is None:
            update_cols = [c for c in columns if c not in unique_cols]
        else:
            update_cols = update_columns

        safe_update_cols = [
            validate_identifier(c, allow_dotted=False, allow_wildcard=False)
            for c in update_cols
        ]

        if safe_update_cols:
            update_assignments = [
                f"{quote_identifier(c)} = EXCLUDED.{quote_identifier(c)}"
                for c in safe_update_cols
            ]
            conflict_clause = f"ON CONFLICT ({', '.join(safe_unique)}) DO UPDATE SET {', '.join(update_assignments)}"
        else:
            conflict_clause = f"ON CONFLICT ({', '.join(safe_unique)}) DO NOTHING"

        bindings: list[Any] = []
        rows_sql: list[str] = []
        idx = 1

        for row in values:
            placeholders: list[str] = []
            for col_name in columns:
                placeholders.append(f"${idx}")
                bindings.append(row.get(col_name))
                idx += 1
            rows_sql.append(f"({', '.join(placeholders)})")

        sql = (
            f"INSERT INTO {quote_identifier(self._table)} ({', '.join(safe_columns)}) "
            f"VALUES {', '.join(rows_sql)} {conflict_clause}"
        )

        conn = await self._connection()
        try:
            status = await conn.execute(sql, *bindings)
            parts = status.split()
            if len(parts) >= 3 and parts[-1].isdigit():
                return int(parts[-1])
            return len(values)
        finally:
            await self._release(conn)


class Query:
    @staticmethod
    def table(table_name: str) -> QueryBuilder:
        return QueryBuilder(table_name)
