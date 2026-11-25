from __future__ import annotations

import inspect
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, NoReturn, cast, overload
from uuid import UUID

import asyncpg

from swiftpy.core.container import get_container
from swiftpy.database.exceptions import QueryDumpError, RecordNotFoundError
from swiftpy.database.identifiers import (
    quote_identifier,
    split_json_path,
    validate_column_identifier,
    validate_identifier,
)
from swiftpy.database.pagination.paginator import Paginator
from swiftpy.database.pagination.simple_paginator import SimplePaginator
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import get_active_connection

_MISSING = object()
_WHERE_OPERATORS = frozenset(
    {"=", "!=", "<>", "<", ">", "<=", ">=", "LIKE", "ILIKE", "IN", "NOT IN"}
)
_COMPARISON_OPERATORS = frozenset({"=", "!=", "<>", "<", ">", "<=", ">="})
logger = logging.getLogger(__name__)

_DOLLAR_TAG_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_HAVING_AGGREGATE_RE = re.compile(
    r"^(COUNT|SUM|AVG|MIN|MAX)\(\s*(DISTINCT\s+)?(\*|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*\)$",
    re.IGNORECASE,
)


def _is_non_string_sequence(value: Any) -> bool:
    """Return True if value is a Sequence other than str, bytes, bytearray, or memoryview."""
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
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


def _encode_json_value(value: Any) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError) as exc:
        raise QueryError(f"Value is not JSON serializable: {exc}") from exc


def _quote_sql_string(value: str) -> str:
    return "'" + value.replace("\x00", "\\x00").replace("'", "''") + "'"


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "'NaN'::float8"
        if math.isinf(value):
            return "'Infinity'::float8" if value > 0 else "'-Infinity'::float8"
        return repr(value)
    if isinstance(value, Decimal):
        if value.is_nan():
            return "'NaN'::numeric"
        if value.is_infinite():
            return "'Infinity'::numeric" if value > 0 else "'-Infinity'::numeric"
        return str(value)
    if isinstance(value, str):
        return _quote_sql_string(value)
    if isinstance(value, bytes):
        return "'\\x" + value.hex() + "'::bytea"
    if isinstance(value, datetime | date | time):
        return _quote_sql_string(value.isoformat())
    if isinstance(value, UUID):
        return _quote_sql_string(str(value))
    if isinstance(value, (list, tuple)):
        if not value:
            return "'{}'"
        return f"ARRAY[{', '.join(_sql_literal(item) for item in value)}]"
    if isinstance(value, dict):
        return f"{_quote_sql_string(_encode_json_value(value))}::jsonb"
    return _quote_sql_string(str(value))


def _scan_quoted_sql_region(
    sql: str,
    start: int,
    quote_char: str,
    *,
    escape_backslash: bool = False,
) -> int:
    """Return the index immediately after a single- or double-quoted SQL region."""
    index = start
    length = len(sql)
    while index < length:
        char = sql[index]
        if escape_backslash and char == "\\" and index + 1 < length:
            index += 2
            continue
        if char == quote_char:
            if index + 1 < length and sql[index + 1] == quote_char:
                index += 2
                continue
            return index + 1
        index += 1
    return length


def _scan_block_comment(sql: str, start: int) -> int:
    """Return the index immediately after a possibly nested ``/* ... */`` block comment."""
    index = start + 2
    length = len(sql)
    depth = 1
    while index < length and depth > 0:
        if sql.startswith("/*", index):
            depth += 1
            index += 2
        elif sql.startswith("*/", index):
            depth -= 1
            index += 2
        else:
            index += 1
    return index


def _substitute_bindings(sql: str, bindings: Sequence[Any]) -> str:
    output: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if (
            char in {"e", "E"}
            and index + 1 < length
            and sql[index + 1] == "'"
            and (index == 0 or not (sql[index - 1].isalnum() or sql[index - 1] in {"_", "$"}))
        ):
            end = _scan_quoted_sql_region(sql, index + 2, "'", escape_backslash=True)
            output.append(sql[index:end])
            index = end
            continue
        if char == "'":
            end = _scan_quoted_sql_region(sql, index + 1, "'")
            output.append(sql[index:end])
            index = end
            continue
        if char == '"':
            end = _scan_quoted_sql_region(sql, index + 1, '"')
            output.append(sql[index:end])
            index = end
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            end = length if newline == -1 else newline + 1
            output.append(sql[index:end])
            index = end
            continue
        if sql.startswith("/*", index):
            end = _scan_block_comment(sql, index)
            output.append(sql[index:end])
            index = end
            continue
        if char == "$":
            is_standalone = index == 0 or not (
                sql[index - 1].isalnum() or sql[index - 1] == "_"
            )
            if is_standalone:
                dollar_match = _DOLLAR_TAG_RE.match(sql, index)
                if dollar_match is not None:
                    tag = dollar_match.group(0)
                    close = sql.find(tag, dollar_match.end())
                    end = length if close == -1 else close + len(tag)
                    output.append(sql[index:end])
                    index = end
                    continue
            if index + 1 < length and sql[index + 1].isdigit():
                end = index + 1
                while end < length and sql[end].isdigit():
                    end += 1
                binding_index = int(sql[index + 1 : end])
                if binding_index < 1 or binding_index > len(bindings):
                    raise QueryError(
                        f"SQL placeholder ${binding_index} has no corresponding binding."
                    )
                output.append(_sql_literal(bindings[binding_index - 1]))
                index = end
                continue
        output.append(char)
        index += 1
    return "".join(output)


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
class _WhereColumnClause:
    boolean: str
    first: str
    operator: str
    second: str


@dataclass(slots=True, frozen=True)
class _WhereExistsClause:
    boolean: str
    query: QueryBuilder
    negated: bool


@dataclass(slots=True, frozen=True)
class _WhereJsonClause:
    boolean: str
    column: str
    path: tuple[str, ...]
    kind: str
    operator: str
    value: Any


@dataclass(slots=True, frozen=True)
class _WhereGroupClause:
    boolean: str
    clauses: tuple[_WhereClauseType, ...]
    negated: bool = False


_WhereClauseType = (
    _WhereClause
    | _WhereBetweenClause
    | _WhereRawClause
    | _WhereColumnClause
    | _WhereExistsClause
    | _WhereJsonClause
    | _WhereGroupClause
)


def _clone_where_clause(wc: _WhereClauseType) -> _WhereClauseType:
    if isinstance(wc, _WhereRawClause):
        return _WhereRawClause(wc.boolean, Raw(wc.raw.sql, list(wc.raw.bindings)))
    if isinstance(wc, _WhereGroupClause):
        return _WhereGroupClause(
            wc.boolean,
            tuple(_clone_where_clause(child) for child in wc.clauses),
            wc.negated,
        )
    if isinstance(wc, _WhereExistsClause):
        return _WhereExistsClause(wc.boolean, wc.query.clone(), wc.negated)
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
        self._lock: str | None = None
        self._unions: list[tuple[bool, QueryBuilder]] = []
        self._order_name: str | None = None

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
        cloned._lock = self._lock
        cloned._unions = [(all_flag, query.clone()) for all_flag, query in self._unions]
        cloned._order_name = self._order_name
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

    @staticmethod
    def _make_where_clause(
        column: str,
        operator_or_value: Any,
        value: Any,
        boolean: str,
    ) -> _WhereClause:
        column = validate_identifier(column, allow_dotted=True, allow_wildcard=False)
        bool_op = _validate_boolean(boolean, context="WHERE")

        if value is _MISSING:
            operator = "="
            value = operator_or_value

        else:
            operator = " ".join(str(operator_or_value).upper().split())

        if operator not in _WHERE_OPERATORS:
            raise QueryError(f"Unsupported operator: {operator}")

        if value is None:
            if operator == "=":
                return _WhereClause(bool_op, column, "IS", None)

            if operator in {"!=", "<>"}:
                return _WhereClause(bool_op, column, "IS NOT", None)

            raise QueryError(
                f"Cannot compare NULL using operator '{operator}'. Use '=' / '!=' or where_null() / where_not_null()."
            )

        if operator in {"IN", "NOT IN"} and not (
            _is_non_string_sequence(value) or isinstance(value, QueryBuilder)
        ):
            raise QueryError(
                f"Operator '{operator}' requires a list, tuple, or QueryBuilder subquery, got {type(value).__name__}."
            )

        return _WhereClause(bool_op, column, operator, value)

    def where(
        self,
        column: str,
        operator_or_value: Any,
        value: Any = _MISSING,
        *,
        boolean: str = "AND",
    ) -> QueryBuilder:
        if isinstance(column, str) and "->" in column:
            json_clause = self._make_json_where_clause(
                column, operator_or_value, value, boolean
            )
            self._where_clauses.append(json_clause)
            return self

        plain_clause = self._make_where_clause(
            column, operator_or_value, value, boolean
        )
        self._wheres.append(
            (plain_clause.column, plain_clause.operator, plain_clause.value)
        )
        self._where_clauses.append(plain_clause)

        return self

    def _make_json_where_clause(
        self, column: str, operator_or_value: Any, value: Any, boolean: str
    ) -> _WhereJsonClause:
        safe_column, path = split_json_path(column)
        bool_op = _validate_boolean(boolean, context="WHERE")
        if value is _MISSING:
            operator = "="
            value = operator_or_value
        else:
            operator = " ".join(str(operator_or_value).upper().split())
        if operator not in _WHERE_OPERATORS:
            raise QueryError(f"Unsupported operator: {operator}")

        encoded: str | tuple[str, ...] | int | float | None
        if operator in {"=", "!=", "<>"}:
            encoded = None if value is None else _encode_json_value(value)
        elif operator in {"<", ">", "<=", ">="}:
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise QueryError(
                    f"JSON operator '{operator}' requires a string or number value."
                )
            if isinstance(value, float) and not math.isfinite(value):
                raise QueryError(
                    "Value is not JSON serializable: Out of range float values are not JSON compliant"
                )
            encoded = value
        elif operator in {"LIKE", "ILIKE"}:
            if not isinstance(value, str):
                raise QueryError(
                    f"JSON operator '{operator}' requires a string value."
                )
            encoded = value
        else:
            if not _is_non_string_sequence(value):
                raise QueryError(
                    f"Operator '{operator}' requires a list or tuple of JSON scalars."
                )
            encoded_values: list[str] = []
            for item in value:
                if item is not None and not isinstance(item, (str, int, float, bool)):
                    raise QueryError(
                        f"Operator '{operator}' requires a list or tuple of JSON scalars."
                    )
                encoded_values.append(_encode_json_value(item))
            encoded = tuple(encoded_values)
        return _WhereJsonClause(
            bool_op, safe_column, path, "where", operator, encoded
        )

    def where_json_contains(
        self, column: str, value: Any, *, boolean: str = "AND"
    ) -> QueryBuilder:
        return self._where_json_contains(column, value, boolean=boolean, negated=False)

    def where_json_doesnt_contain(
        self, column: str, value: Any, *, boolean: str = "AND"
    ) -> QueryBuilder:
        return self._where_json_contains(column, value, boolean=boolean, negated=True)

    def or_where_json_contains(self, column: str, value: Any) -> QueryBuilder:
        return self.where_json_contains(column, value, boolean="OR")

    def or_where_json_doesnt_contain(self, column: str, value: Any) -> QueryBuilder:
        return self.where_json_doesnt_contain(column, value, boolean="OR")

    def _where_json_contains(
        self, column: str, value: Any, *, boolean: str, negated: bool
    ) -> QueryBuilder:
        safe_column, path = split_json_path(column, require_path=False)
        bool_op = _validate_boolean(boolean, context="WHERE")
        self._where_clauses.append(
            _WhereJsonClause(
                bool_op,
                safe_column,
                path,
                "doesnt_contain" if negated else "contains",
                "@>",
                _encode_json_value(value),
            )
        )
        return self

    def where_json_contains_key(
        self, column: str, *, boolean: str = "AND"
    ) -> QueryBuilder:
        return self._where_json_key(column, boolean=boolean, negated=False)

    def where_json_doesnt_contain_key(
        self, column: str, *, boolean: str = "AND"
    ) -> QueryBuilder:
        return self._where_json_key(column, boolean=boolean, negated=True)

    def or_where_json_contains_key(self, column: str) -> QueryBuilder:
        return self.where_json_contains_key(column, boolean="OR")

    def or_where_json_doesnt_contain_key(self, column: str) -> QueryBuilder:
        return self.where_json_doesnt_contain_key(column, boolean="OR")

    def _where_json_key(
        self, column: str, *, boolean: str, negated: bool
    ) -> QueryBuilder:
        safe_column, path = split_json_path(column)
        bool_op = _validate_boolean(boolean, context="WHERE")
        self._where_clauses.append(
            _WhereJsonClause(
                bool_op,
                safe_column,
                path,
                "doesnt_contain_key" if negated else "contains_key",
                "IS",
                None,
            )
        )
        return self

    def where_json_length(
        self,
        column: str,
        operator_or_value: Any,
        value: Any = _MISSING,
        *,
        boolean: str = "AND",
    ) -> QueryBuilder:
        safe_column, path = split_json_path(column, require_path=False)
        bool_op = _validate_boolean(boolean, context="WHERE")
        if value is _MISSING:
            operator = "="
            value = operator_or_value
        else:
            operator = " ".join(str(operator_or_value).upper().split())
        if operator not in _COMPARISON_OPERATORS:
            raise QueryError(f"Unsupported operator: {operator}")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise QueryError("JSON length must be a non-negative integer.")
        self._where_clauses.append(
            _WhereJsonClause(
                bool_op, safe_column, path, "length", operator, value
            )
        )
        return self

    def or_where_json_length(
        self, column: str, operator_or_value: Any, value: Any = _MISSING
    ) -> QueryBuilder:
        return self.where_json_length(
            column, operator_or_value, value, boolean="OR"
        )

    def where_column(
        self, first: str, operator: str, second: str, *, boolean: str = "AND"
    ) -> QueryBuilder:
        first = validate_identifier(first, allow_dotted=True, allow_wildcard=False)
        second = validate_identifier(second, allow_dotted=True, allow_wildcard=False)
        if not isinstance(operator, str):
            raise TypeError("Column comparison operator must be a string.")
        operator = " ".join(operator.upper().split())
        if operator not in _COMPARISON_OPERATORS:
            raise QueryError(f"Unsupported operator: {operator}")
        bool_op = _validate_boolean(boolean, context="WHERE")
        self._where_clauses.append(
            _WhereColumnClause(bool_op, first, operator, second)
        )
        return self

    def or_where_column(self, first: str, operator: str, second: str) -> QueryBuilder:
        return self.where_column(first, operator, second, boolean="OR")

    def where_exists(
        self, query: QueryBuilder, *, boolean: str = "AND"
    ) -> QueryBuilder:
        return self._where_exists(query, boolean=boolean, negated=False)

    def where_not_exists(
        self, query: QueryBuilder, *, boolean: str = "AND"
    ) -> QueryBuilder:
        return self._where_exists(query, boolean=boolean, negated=True)

    def or_where_exists(self, query: QueryBuilder) -> QueryBuilder:
        return self.where_exists(query, boolean="OR")

    def or_where_not_exists(self, query: QueryBuilder) -> QueryBuilder:
        return self.where_not_exists(query, boolean="OR")

    def _where_exists(
        self, query: QueryBuilder, *, boolean: str, negated: bool
    ) -> QueryBuilder:
        if not isinstance(query, QueryBuilder):
            raise TypeError("where_exists expects a QueryBuilder.")
        if query._lock is not None:
            raise QueryError("A locked query cannot be used as an EXISTS subquery.")
        if query._table == self._table:
            raise QueryError(
                "EXISTS subqueries must use a different table; aliases are not supported."
            )
        bool_op = _validate_boolean(boolean, context="WHERE")
        self._where_clauses.append(
            _WhereExistsClause(bool_op, query.clone(), negated)
        )
        return self

    def where_any(
        self,
        columns: Sequence[str],
        operator_or_value: Any,
        value: Any = _MISSING,
        *,
        boolean: str = "AND",
    ) -> QueryBuilder:
        return self._where_columns_group(
            columns, operator_or_value, value, boolean=boolean, mode="any"
        )

    def where_all(
        self,
        columns: Sequence[str],
        operator_or_value: Any,
        value: Any = _MISSING,
        *,
        boolean: str = "AND",
    ) -> QueryBuilder:
        return self._where_columns_group(
            columns, operator_or_value, value, boolean=boolean, mode="all"
        )

    def where_none(
        self,
        columns: Sequence[str],
        operator_or_value: Any,
        value: Any = _MISSING,
        *,
        boolean: str = "AND",
    ) -> QueryBuilder:
        return self._where_columns_group(
            columns, operator_or_value, value, boolean=boolean, mode="none"
        )

    def or_where_any(
        self,
        columns: Sequence[str],
        operator_or_value: Any,
        value: Any = _MISSING,
    ) -> QueryBuilder:
        return self.where_any(columns, operator_or_value, value, boolean="OR")

    def or_where_all(
        self,
        columns: Sequence[str],
        operator_or_value: Any,
        value: Any = _MISSING,
    ) -> QueryBuilder:
        return self.where_all(columns, operator_or_value, value, boolean="OR")

    def or_where_none(
        self,
        columns: Sequence[str],
        operator_or_value: Any,
        value: Any = _MISSING,
    ) -> QueryBuilder:
        return self.where_none(columns, operator_or_value, value, boolean="OR")

    def _where_columns_group(
        self,
        columns: Sequence[str],
        operator_or_value: Any,
        value: Any,
        *,
        boolean: str,
        mode: str,
    ) -> QueryBuilder:
        if isinstance(columns, str) or not isinstance(columns, (list, tuple)):
            raise TypeError("columns must be a non-empty list or tuple of strings.")
        if not columns:
            raise QueryError("columns must not be empty.")
        if any(not isinstance(column, str) for column in columns):
            raise TypeError("Every column must be a string.")

        clauses = tuple(
            self._make_where_clause(column, operator_or_value, value, "AND")
            for column in columns
        )
        bool_op = _validate_boolean(boolean, context="WHERE")
        if mode == "all":
            clauses = tuple(
                _WhereClause(
                    "AND", clause.column, clause.operator, clause.value
                )
                for clause in clauses
            )
            group_negated = False
        else:
            clauses = tuple(
                _WhereClause("OR", clause.column, clause.operator, clause.value)
                for clause in clauses
            )
            group_negated = mode == "none"
        self._where_clauses.append(
            _WhereGroupClause(bool_op, clauses, negated=group_negated)
        )
        return self

    def where_past(self, column: str, *, boolean: str = "AND") -> QueryBuilder:
        return self._where_time_comparison(column, "<", boolean=boolean)

    def where_future(self, column: str, *, boolean: str = "AND") -> QueryBuilder:
        return self._where_time_comparison(column, ">", boolean=boolean)

    def or_where_past(self, column: str) -> QueryBuilder:
        return self.where_past(column, boolean="OR")

    def or_where_future(self, column: str) -> QueryBuilder:
        return self.where_future(column, boolean="OR")

    def _where_time_comparison(
        self, column: str, operator: str, *, boolean: str
    ) -> QueryBuilder:
        safe_column = validate_identifier(
            column, allow_dotted=True, allow_wildcard=False
        )
        bool_op = _validate_boolean(boolean, context="WHERE")
        raw = Raw(
            f"{quote_identifier(safe_column, allow_wildcard=False)} "
            f"{operator} CURRENT_TIMESTAMP"
        )
        self._where_clauses.append(_WhereRawClause(bool_op, raw))
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
        values: Sequence[Any] | QueryBuilder,
        *,
        boolean: str = "AND",
    ) -> QueryBuilder:
        """Add a WHERE column IN (...) clause."""
        return self.where(column, "IN", values, boolean=boolean)

    def or_where_in(
        self,
        column: str,
        values: Sequence[Any] | QueryBuilder,
    ) -> QueryBuilder:
        """Add an OR WHERE column IN (...) clause."""
        return self.where_in(column, values, boolean="OR")

    def where_not_in(
        self,
        column: str,
        values: Sequence[Any] | QueryBuilder,
        *,
        boolean: str = "AND",
    ) -> QueryBuilder:
        """Add a WHERE column NOT IN (...) clause."""
        return self.where(column, "NOT IN", values, boolean=boolean)

    def or_where_not_in(
        self,
        column: str,
        values: Sequence[Any] | QueryBuilder,
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
        self._order_name = column
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
        self._order_name = None
        return self

    def union(self, query: QueryBuilder) -> QueryBuilder:
        return self._add_union(query, all_rows=False)

    def union_all(self, query: QueryBuilder) -> QueryBuilder:
        return self._add_union(query, all_rows=True)

    def _add_union(self, query: QueryBuilder, *, all_rows: bool) -> QueryBuilder:
        if not isinstance(query, QueryBuilder):
            raise TypeError("union expects a QueryBuilder.")
        if query._unions:
            raise QueryError("Nested union queries are not supported.")
        if query._lock is not None:
            raise QueryError("A locked query cannot be used as a UNION branch.")
        if self._lock is not None:
            raise QueryError("A locked query cannot be combined with a UNION.")
        self._unions.append((all_rows, query.clone()))
        return self

    def _ensure_not_union(self, method: str) -> None:
        if self._unions:
            raise QueryError(
                f"{method}() is not supported on a union query."
            )

    def shared_lock(self) -> QueryBuilder:
        """Lock selected rows for sharing; execution requires an active transaction."""
        self._lock = "SHARE"
        return self

    def lock_for_update(self) -> QueryBuilder:
        """Lock selected rows for update; execution requires an active transaction."""
        self._lock = "UPDATE"
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

        if operator in {"IN", "NOT IN"} and _is_non_string_sequence(value):
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
    def _compile_json_clause(
        clause: _WhereJsonClause, start_idx: int
    ) -> tuple[tuple[str, str], list[Any], int]:
        column = quote_identifier(
            clause.column, allow_dotted=True, allow_wildcard=False
        )
        root = f"{column}::jsonb"
        bindings: list[Any] = []
        idx = start_idx
        path_placeholder: str | None = None
        if clause.path:
            path_placeholder = f"${idx}::text[]"
            bindings.append(list(clause.path))
            idx += 1
        expression = (
            f"({root} #> {path_placeholder})"
            if path_placeholder is not None
            else root
        )
        kind = clause.kind

        if kind == "where":
            operator = clause.operator
            if operator in {"=", "!=", "<>"} and clause.value is None:
                comparison = "=" if operator == "=" else "<>"
                sql = (
                    "COALESCE(jsonb_typeof("
                    f"{expression}), 'null') {comparison} 'null'"
                )
                return (clause.boolean, sql), bindings, idx
            if operator in {"=", "!=", "<>"}:
                sql = f"{expression} {operator} ${idx}::jsonb"
                bindings.append(clause.value)
                return (clause.boolean, sql), bindings, idx + 1
            if operator in {"IN", "NOT IN"}:
                values = cast(tuple[str, ...], clause.value)
                if not values:
                    return (
                        clause.boolean,
                        "FALSE" if operator == "IN" else "TRUE",
                    ), [], start_idx
                placeholders = [f"${idx + offset}::jsonb" for offset in range(len(values))]
                bindings.extend(values)
                return (
                    clause.boolean,
                    f"{expression} {operator} ({', '.join(placeholders)})",
                ), bindings, idx + len(values)
            if operator in {"<", ">", "<=", ">="}:
                json_type = "number" if isinstance(clause.value, (int, float)) else "string"
                scalar = (
                    f"({root} #>> {path_placeholder})"
                    if path_placeholder is not None
                    else "NULL"
                )
                scalar = f"({scalar})::numeric" if json_type == "number" else scalar
                sql = (
                    f"CASE WHEN jsonb_typeof({expression}) = '{json_type}' "
                    f"THEN {scalar} END {operator} ${idx}"
                )
                bindings.append(clause.value)
                return (clause.boolean, sql), bindings, idx + 1
            sql = (
                f"CASE WHEN jsonb_typeof({expression}) = 'string' "
                f"THEN {root} #>> {path_placeholder} END "
                f"{operator} ${idx} ESCAPE '\\'"
            )
            bindings.append(clause.value)
            return (clause.boolean, sql), bindings, idx + 1

        if kind in {"contains", "doesnt_contain"}:
            predicate = f"{expression} @> ${idx}::jsonb"
            if kind == "doesnt_contain":
                predicate = f"NOT COALESCE({predicate}, FALSE)"
            bindings.append(clause.value)
            return (clause.boolean, predicate), bindings, idx + 1
        if kind == "contains_key":
            return (clause.boolean, f"{expression} IS NOT NULL"), bindings, idx
        if kind == "doesnt_contain_key":
            return (clause.boolean, f"{expression} IS NULL"), bindings, idx
        if kind == "length":
            sql = (
                f"CASE WHEN jsonb_typeof({expression}) = 'array' "
                f"THEN jsonb_array_length({expression}) END "
                f"{clause.operator} ${idx}"
            )
            bindings.append(clause.value)
            return (clause.boolean, sql), bindings, idx + 1
        raise QueryError(f"Unknown JSON where clause kind: {kind}")

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

            if isinstance(clause, _WhereColumnClause):
                first = quote_identifier(
                    clause.first, allow_dotted=True, allow_wildcard=False
                )
                second = quote_identifier(
                    clause.second, allow_dotted=True, allow_wildcard=False
                )
                clauses.append(
                    (clause.boolean, f"{first} {clause.operator} {second}")
                )
                continue

            if isinstance(clause, _WhereJsonClause):
                compiled_clause, clause_bindings, idx = (
                    QueryBuilder._compile_json_clause(clause, idx)
                )
                clauses.append(compiled_clause)
                bindings.extend(clause_bindings)
                continue

            if isinstance(clause, _WhereExistsClause):
                subquery = clause.query.clone()
                subquery._columns = [Raw("1")]
                subquery._distinct = False
                subquery._distinct_columns = []
                subquery._order_column = None
                subquery._order_name = None
                subquery._order_random = False
                for _, branch in subquery._unions:
                    branch._columns = [Raw("1")]
                    branch._distinct = False
                    branch._distinct_columns = []
                    branch._order_column = None
                    branch._order_name = None
                    branch._order_random = False
                sub_sql, sub_bindings = subquery.to_sql(idx)
                operator = "NOT EXISTS" if clause.negated else "EXISTS"
                clauses.append(
                    (clause.boolean, f"{operator} ({sub_sql})")
                )
                bindings.extend(sub_bindings)
                idx += len(sub_bindings)
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
                if clause.negated:
                    inner_sql = f"NOT {inner_sql}"
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

    def _compile_core(self, start_idx: int = 1) -> tuple[str, list[Any], int]:
        select_sql, select_bindings, idx = self._compile_select(start_idx)
        join_sql = self._compile_joins()
        where_sql, where_bindings, idx = self._compile_where(idx)
        group_sql = self._compile_groups()
        having_sql, having_bindings, idx = self._compile_having(idx)

        bindings = select_bindings + where_bindings + having_bindings
        sql = f"SELECT {select_sql} FROM {quote_identifier(self._table)}{join_sql}{where_sql}{group_sql}{having_sql}"
        return sql, bindings, idx

    def _validate_lock_compatibility(self) -> None:
        if self._lock is None:
            return
        if self._distinct:
            raise QueryError("Row locks cannot be combined with DISTINCT.")
        if self._groups or self._havings or self._raw_havings:
            raise QueryError("Row locks cannot be combined with GROUP BY or HAVING.")
        if self._unions:
            raise QueryError("Row locks cannot be combined with UNION.")

    @staticmethod
    def _append_order_and_pagination(
        sql: str,
        bindings: list[Any],
        idx: int,
        *,
        order_random: bool,
        order_column: str | None,
        order_direction: str,
        limit_value: int | None,
        offset_value: int | None,
    ) -> tuple[str, list[Any], int]:
        if order_random:
            sql += " ORDER BY RANDOM()"
        elif order_column:
            sql += f" ORDER BY {order_column} {order_direction}"

        if limit_value is not None:
            sql += f" LIMIT ${idx}"
            bindings.append(limit_value)
            idx += 1

        if offset_value is not None:
            sql += f" OFFSET ${idx}"
            bindings.append(offset_value)
            idx += 1
        return sql, bindings, idx

    def to_sql(self, start_idx: int = 1) -> tuple[str, list[Any]]:
        """Compile the entire query to parameterized SQL and bindings."""
        self._validate_lock_compatibility()

        core_sql, bindings, idx = self._compile_core(start_idx)
        if self._unions:
            parts = [f"({core_sql})"]
            for all_rows, branch in self._unions:
                branch_sql, branch_bindings = branch.to_sql(idx)
                parts.append(
                    f"{'UNION ALL' if all_rows else 'UNION'} ({branch_sql})"
                )
                bindings.extend(branch_bindings)
                idx += len(branch_bindings)
            combined = " ".join(parts)
            has_outer_clauses = (
                self._order_random
                or self._order_name is not None
                or self._limit_value is not None
                or self._offset_value is not None
            )
            if has_outer_clauses:
                if self._order_random:
                    outer_order = "RANDOM()"
                elif self._order_name is not None:
                    outer_column = self._order_name.split(".")[-1]
                    outer_order = (
                        f"{quote_identifier(outer_column, allow_dotted=False, allow_wildcard=False)} "
                        f"{self._order_direction}"
                    )
                else:
                    outer_order = None
                sql = f'SELECT * FROM ({combined}) AS "swiftpy_union"'
                if outer_order is not None:
                    sql += f" ORDER BY {outer_order}"
                sql, bindings, idx = self._append_order_and_pagination(
                    sql,
                    bindings,
                    idx,
                    order_random=False,
                    order_column=None,
                    order_direction=self._order_direction,
                    limit_value=self._limit_value,
                    offset_value=self._offset_value,
                )
            else:
                sql = combined
        else:
            sql = core_sql
            sql, bindings, idx = self._append_order_and_pagination(
                sql,
                bindings,
                idx,
                order_random=self._order_random,
                order_column=self._order_column,
                order_direction=self._order_direction,
                limit_value=self._limit_value,
                offset_value=self._offset_value,
            )

        if self._lock == "SHARE":
            sql += " FOR SHARE"
        elif self._lock == "UPDATE":
            sql += " FOR UPDATE"
        return sql, bindings

    def to_raw_sql(self) -> str:
        """
        Return a debug-only SQL rendering. Never execute this output or build queries from it.

        String escaping assumes PostgreSQL's default ``standard_conforming_strings=on``.
        Values that cannot be represented as ordinary PostgreSQL text literals (such as
        strings containing NUL bytes or unsupported custom Python objects) are rendered
        diagnostically for human inspection only.
        """
        sql, bindings = self.to_sql()
        return _substitute_bindings(sql, bindings)

    def dump(self) -> QueryBuilder:
        """Log parameterized SQL and bindings at INFO and return this builder."""
        sql, bindings = self.to_sql()
        logger.info("Query SQL: %s | bindings=%r", sql, bindings)
        return self

    def dump_raw_sql(self) -> QueryBuilder:
        """Log the human-readable debug SQL at INFO and return this builder."""
        logger.info("Query SQL (debug rendering): %s", self.to_raw_sql())
        return self

    def dd(self) -> NoReturn:
        """Log parameterized SQL and stop execution with QueryDumpError."""
        self.dump()
        raise QueryDumpError("Query execution stopped by dd().")

    def dd_raw_sql(self) -> NoReturn:
        """Log debug SQL and stop execution with QueryDumpError."""
        self.dump_raw_sql()
        raise QueryDumpError("Query execution stopped by dd_raw_sql().")

    def _ensure_lock_has_transaction(self) -> None:
        if self._lock is not None and get_active_connection() is None:
            raise QueryError(
                "Row locks require an active transaction. "
                "Wrap the query in `async with db.transaction():`."
            )

    async def get(self) -> list[dict[str, Any]]:
        self._ensure_lock_has_transaction()
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
        self._ensure_not_union("value")
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
        self._ensure_not_union("pluck")
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
        self._ensure_not_union("exists")
        cloned = self.clone()
        cloned._columns = [Raw("1")]
        cloned._order_column = None
        cloned._order_name = None
        cloned._order_random = False
        cloned._lock = None
        row = await cloned.first()
        return row is not None

    async def _aggregate(self, function: str, column: str) -> Any:
        self._ensure_not_union(function.lower())
        col_safe = (
            quote_identifier(column, allow_wildcard=False) if column != "*" else "*"
        )
        cloned = self.clone()
        cloned._columns = [Raw(f"{function}({col_safe}) AS agg_result")]
        cloned._order_column = None
        cloned._order_name = None
        cloned._order_random = False
        cloned._lock = None
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
        self._ensure_not_union("paginate")
        self._ensure_lock_has_transaction()
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
        count_builder._order_name = None
        count_builder._order_random = False
        count_builder._lock = None

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
        data: Mapping[str, Any],
        *,
        returning: Sequence[str] | str | None = None,
        allow_default_values: bool = False,
    ) -> dict[str, Any]:
        """Insert a row and return the RETURNING row as a dictionary."""
        if not isinstance(data, Mapping) or (not data and not allow_default_values):
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
    async def insert(self, data: Mapping[str, Any], *, returning: None = None) -> int: ...

    @overload
    async def insert(
        self, data: Mapping[str, Any], *, returning: Sequence[str] | str
    ) -> Any: ...

    async def insert(
        self,
        data: Mapping[str, Any],
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

    def _compile_update_sets(
        self, data: Mapping[str, Any]
    ) -> tuple[list[str], list[Any]]:
        bindings: list[Any] = []
        sets: list[str] = []
        idx = 1
        ordered_keys: list[tuple[str, str]] = []
        plain_columns: set[str] = set()
        json_paths: dict[str, list[tuple[tuple[str, ...], str]]] = {}

        for column, value in data.items():
            if "->" not in column:
                safe_name = validate_identifier(
                    column, allow_dotted=False, allow_wildcard=False
                )
                if safe_name in json_paths:
                    raise QueryError(
                        f"Cannot update both JSON paths and the whole column '{safe_name}'."
                    )
                plain_columns.add(safe_name)
                ordered_keys.append(("plain", safe_name))
                continue

            safe_column, path = split_json_path(column)
            safe_column = validate_identifier(
                safe_column, allow_dotted=False, allow_wildcard=False
            )
            if safe_column in plain_columns:
                raise QueryError(
                    f"Cannot update both JSON paths and the whole column '{safe_column}'."
                )
            if safe_column not in json_paths:
                json_paths[safe_column] = []
                ordered_keys.append(("json", safe_column))
            json_paths[safe_column].append((path, _encode_json_value(value)))

        for kind, column in ordered_keys:
            quoted_column = quote_identifier(
                column, allow_dotted=False, allow_wildcard=False
            )
            if kind == "plain":
                value = data[column]
                sets.append(f"{quoted_column} = ${idx}")
                bindings.append(value)
                idx += 1
                continue

            expression = f"COALESCE({quoted_column}::jsonb, '{{}}'::jsonb)"
            for path, encoded_value in json_paths[column]:
                expression = (
                    f"jsonb_set({expression}, ${idx}::text[], "
                    f"${idx + 1}::jsonb, true)"
                )
                bindings.extend((list(path), encoded_value))
                idx += 2
            sets.append(f"{quoted_column} = {expression}")
        return sets, bindings

    async def update(self, data: Mapping[str, Any]) -> str:
        self._ensure_not_union("update")
        if self._lock is not None:
            raise QueryError("A row lock cannot be used with UPDATE.")
        if not isinstance(data, Mapping) or not data:
            raise QueryError("Cannot execute UPDATE with empty data dictionary.")

        if not self._where_clauses and not self._wheres and not self._raw_wheres and not self._allow_all:
            raise QueryError(
                "Refusing to execute UPDATE without a WHERE clause. Call .allow_all() to update all rows explicitly."
            )

        sets, bindings = self._compile_update_sets(data)
        idx = len(bindings) + 1

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
        self._ensure_not_union("delete")
        if self._lock is not None:
            raise QueryError("A row lock cannot be used with DELETE.")
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
        extra: Mapping[str, Any] | None = None,
    ) -> str:
        """Atomically increment a column's value by amount."""
        self._ensure_not_union("increment")
        if self._lock is not None:
            raise QueryError("A row lock cannot be used with increment().")
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
        extra: Mapping[str, Any] | None = None,
    ) -> str:
        """Atomically decrement a column's value by amount."""
        self._ensure_not_union("decrement")
        if self._lock is not None:
            raise QueryError("A row lock cannot be used with decrement().")
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
        self._ensure_not_union("chunk")
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
        self._ensure_not_union("chunk_by_id")
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
        values: Sequence[Mapping[str, Any]],
        unique_by: Sequence[str] | str,
        update_columns: Sequence[str] | None = None,
    ) -> int:
        """
        Perform a PostgreSQL INSERT ... ON CONFLICT (unique_by) DO UPDATE / DO NOTHING.
        Returns the number of affected rows.
        """
        self._ensure_not_union("upsert")
        if self._lock is not None:
            raise QueryError("A row lock cannot be used with UPSERT.")
        if not values or not _is_non_string_sequence(values):
            raise QueryError("upsert() requires a non-empty list of dictionary rows.")

        first_keys = set(values[0].keys()) if isinstance(values[0], Mapping) else set()
        for i, row in enumerate(values):
            if not isinstance(row, Mapping) or not row:
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
            update_cols = list(update_columns)

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
