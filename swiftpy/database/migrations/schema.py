from __future__ import annotations

import json
import math
import warnings
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

import asyncpg

from swiftpy.core.container import get_container
from swiftpy.database.identifiers import MAX_IDENTIFIER_LENGTH, validate_identifier
from swiftpy.database.migrations.blueprint import (
    _UNSET,
    ALLOWED_COLUMN_TYPES,
    Blueprint,
    ColumnDefinition,
    DefaultRaw,
    _validate_column_length,
    _validate_decimal_params,
    _validate_enum_values,
    raw,
)
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import get_active_connection


class SchemaGrammar:
    """Translates a Blueprint into PostgreSQL Data Definition Language (DDL) statements."""

    @classmethod
    def compile_create(cls, blueprint: Blueprint) -> list[str]:
        table = validate_identifier(blueprint.table)
        columns_sql: list[str] = []
        constraints_sql: list[str] = []
        post_statements: list[str] = []

        # 1. Compile columns
        for col in blueprint.columns:
            if col.is_change:
                raise ValueError(
                    f"Column '{col.name}' cannot use .change() inside Schema.create(); "
                    "use Schema.table() to modify existing columns."
                )
            if col.using_expression is not None:
                raise ValueError(
                    f"Column '{col.name}' cannot use .using() inside Schema.create(); "
                    "use .using() with .change() inside Schema.table()."
                )
            col_sql = cls._compile_column(col, table=table)
            columns_sql.append(col_sql)

            # Standalone index on column
            if col.is_index:
                idx_name = validate_identifier(
                    col.index_name or f"idx_{table}_{col.name}"
                )
                post_statements.append(
                    f'CREATE INDEX "{idx_name}" ON "{table}" ("{col.name}");'
                )

            # Standalone named unique constraint on column
            uq_explicit = col.unique_name or (
                col.index_name if not col.is_index else None
            )
            if col.is_unique and uq_explicit:
                uq_col_name = validate_identifier(uq_explicit)
                constraints_sql.append(
                    f'CONSTRAINT "{uq_col_name}" UNIQUE ("{col.name}")'
                )

            # Column comments
            post_statements.extend(cls._compile_comment(table, col))

        # 2. Compile table-level primary key (e.g. composite primary key)
        if blueprint.primary_columns:
            pk_name = validate_identifier(blueprint.primary_key_name or f"pk_{table}")
            cols_joined = ", ".join(f'"{c}"' for c in blueprint.primary_columns)
            constraints_sql.append(
                f'CONSTRAINT "{pk_name}" PRIMARY KEY ({cols_joined})'
            )

        # 3. Compile table-level unique constraints and indexes
        for idx in blueprint.indexes:
            cols_joined = ", ".join(f'"{c}"' for c in idx.columns)
            name_suffix = "_".join(idx.columns)

            if idx.is_unique:
                uq_name = validate_identifier(idx.name or f"uq_{table}_{name_suffix}")
                constraints_sql.append(f'CONSTRAINT "{uq_name}" UNIQUE ({cols_joined})')
            else:
                idx_name = validate_identifier(idx.name or f"idx_{table}_{name_suffix}")
                post_statements.append(
                    f'CREATE INDEX "{idx_name}" ON "{table}" ({cols_joined});'
                )

        # 4. Compile foreign key constraints
        for fk in blueprint.foreign_keys:
            if not fk.ref_table:
                raise ValueError(
                    f"Foreign key on {table}.{fk.column} must specify a referenced table via .on('table_name')"
                )
            fk_name = validate_identifier(
                fk.name or f"fk_{table}_{fk.column}_{fk.ref_table}"
            )
            constraints_sql.append(
                f'CONSTRAINT "{fk_name}" FOREIGN KEY ("{fk.column}") '
                f'REFERENCES "{fk.ref_table}" ("{fk.ref_column}") '
                f"ON DELETE {fk.on_delete_action} ON UPDATE {fk.on_update_action}"
            )

        definitions = columns_sql + constraints_sql
        create_sql = f'CREATE TABLE "{table}" (\n    '
        create_sql += ",\n    ".join(definitions)
        create_sql += "\n);"

        return [create_sql, *post_statements]

    @classmethod
    def compile_table(cls, blueprint: Blueprint) -> list[str]:
        table = validate_identifier(blueprint.table)
        statements: list[str] = []

        # 1. Drops first, so one blueprint can replace what it removes
        for name in blueprint.drop_foreign_list:
            safe_name = validate_identifier(name)
            statements.append(
                f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{safe_name}";'
            )
        for name in blueprint.drop_unique_list:
            safe_name = validate_identifier(name)
            statements.append(
                f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{safe_name}";'
            )
        for name in blueprint.drop_indexes_list:
            safe_name = validate_identifier(name)
            statements.append(f'DROP INDEX IF EXISTS "{safe_name}";')
        for name in blueprint.drop_columns_list:
            safe_name = validate_identifier(name)
            statements.append(
                f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{safe_name}";'
            )

        # 2. Renames
        for old_name, new_name in blueprint.rename_columns_map.items():
            safe_old = validate_identifier(old_name)
            safe_new = validate_identifier(new_name)
            statements.append(
                f'ALTER TABLE "{table}" RENAME COLUMN "{safe_old}" TO "{safe_new}";'
            )

        # 3. Adds and column modifications
        for col in blueprint.columns:
            if col.is_change:
                statements.extend(cls._compile_modify_column(table, col))
                continue
            if col.using_expression is not None:
                raise ValueError(
                    f"Column '{col.name}' .using() can only be used with .change()."
                )
            statements.append(
                f'ALTER TABLE "{table}" ADD COLUMN {cls._compile_column(col, table=table)};'
            )
            if col.is_index:
                idx_name = validate_identifier(
                    col.index_name or f"idx_{table}_{col.name}"
                )
                statements.append(
                    f'CREATE INDEX "{idx_name}" ON "{table}" ("{col.name}");'
                )
            statements.extend(cls._compile_comment(table, col))

        for idx in blueprint.indexes:
            cols_joined = ", ".join(f'"{c}"' for c in idx.columns)
            suffix = "_".join(idx.columns)
            if idx.is_unique:
                name = validate_identifier(idx.name or f"uq_{table}_{suffix}")
                statements.append(
                    f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" UNIQUE ({cols_joined});'
                )
            else:
                name = validate_identifier(idx.name or f"idx_{table}_{suffix}")
                statements.append(
                    f'CREATE INDEX "{name}" ON "{table}" ({cols_joined});'
                )

        for fk in blueprint.foreign_keys:
            if not fk.ref_table:
                raise ValueError(
                    f"Foreign key on {table}.{fk.column} must specify a referenced table via .on('table_name')"
                )
            fk_name = validate_identifier(
                fk.name or f"fk_{table}_{fk.column}_{fk.ref_table}"
            )
            statements.append(
                f'ALTER TABLE "{table}" ADD CONSTRAINT "{fk_name}" '
                f'FOREIGN KEY ("{fk.column}") REFERENCES "{fk.ref_table}" ("{fk.ref_column}") '
                f"ON DELETE {fk.on_delete_action} ON UPDATE {fk.on_update_action};"
            )

        return statements

    @classmethod
    def compile_drop(cls, table: str) -> str:
        return f'DROP TABLE "{validate_identifier(table)}";'

    @classmethod
    def compile_drop_if_exists(cls, table: str) -> str:
        return f'DROP TABLE IF EXISTS "{validate_identifier(table)}";'

    @staticmethod
    def _quote_sql_literal(value: str) -> str:
        """Quote and escape a single-quoted PostgreSQL string literal."""
        return "'" + value.replace("'", "''") + "'"

    @classmethod
    def _compile_column_sql_type(
        cls, col: ColumnDefinition, col_name: str
    ) -> tuple[str, tuple[str, ...] | None]:
        """Resolve the PostgreSQL column type SQL fragment and optional enum values."""
        t = col.type_name.strip().lower() if isinstance(col.type_name, str) else ""
        if t not in ALLOWED_COLUMN_TYPES:
            raise ValueError(f"Unsupported column type: {col.type_name!r}")

        enum_values: tuple[str, ...] | None = None

        if t == "uuid":
            return "UUID", None
        if t == "string":
            validated_length = _validate_column_length(col.length)
            return (
                f"VARCHAR({validated_length})"
                if validated_length is not None
                else "VARCHAR"
            ), None
        if t in {"text", "medium_text", "long_text"}:
            return "TEXT", None
        if t == "binary":
            return "BYTEA", None
        if t == "enum":
            enum_values = _validate_enum_values(col.enum_values or ())
            if col.default_val is not _UNSET:
                if col.default_val is None:
                    if not col.is_nullable:
                        raise ValueError(
                            f"Non-nullable enum column '{col_name}' cannot default to None."
                        )
                elif not isinstance(col.default_val, DefaultRaw) and (
                    not isinstance(col.default_val, str)
                    or col.default_val not in enum_values
                ):
                    raise ValueError(
                        f"Default value {col.default_val!r} for enum column '{col_name}' "
                        f"must be one of the enum values {list(enum_values)!r}."
                    )
            varchar_len = max(255, max((len(v) for v in enum_values), default=255))
            return f"VARCHAR({varchar_len})", enum_values
        if t == "integer":
            return "INTEGER", None
        if t == "big_integer":
            return "BIGINT", None
        if t == "small_integer":
            return "SMALLINT", None
        if t == "boolean":
            return "BOOLEAN", None
        if t == "decimal":
            raw_precision = 8 if col.precision is None else col.precision
            raw_scale = 2 if col.scale is None else col.scale
            precision, scale = _validate_decimal_params(raw_precision, raw_scale)
            return f"NUMERIC({precision}, {scale})", None
        if t == "float":
            return "DOUBLE PRECISION", None
        if t == "json":
            return "JSON", None
        if t == "jsonb":
            return "JSONB", None
        if t == "date":
            return "DATE", None
        if t == "time":
            return "TIME", None
        if t == "timestamp":
            return "TIMESTAMP", None
        if t == "timestamptz":
            return "TIMESTAMPTZ", None
        if t == "vector":
            raw_dim = getattr(col, "dimensions", None)
            dim = 1536 if raw_dim is None else raw_dim
            if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
                raise ValueError(
                    f"Vector dimensions must be a positive integer, got {raw_dim!r}"
                )
            return f"vector({dim})", None
        raise ValueError(
            f"Column type '{col.type_name}' cannot be used in this DDL context."
        )

    @classmethod
    def _format_column_default_expr(
        cls, col: ColumnDefinition, type_name: str
    ) -> str:
        """Format the SQL default expression for a column definition."""
        if type_name == "enum" and isinstance(col.default_val, str):
            return cls._quote_sql_literal(col.default_val)
        return cls._format_default_value(col.default_val)

    @classmethod
    def _compile_column(cls, col: ColumnDefinition, table: str | None = None) -> str:
        col_name = validate_identifier(col.name)
        parts: list[str] = [f'"{col_name}"']
        t = col.type_name.strip().lower() if isinstance(col.type_name, str) else ""
        if t not in ALLOWED_COLUMN_TYPES:
            raise ValueError(f"Unsupported column type: {col.type_name!r}")

        if t in {"id", "big_increments"}:
            parts.append("BIGSERIAL PRIMARY KEY")
            return " ".join(parts)
        if t == "increments":
            parts.append("SERIAL PRIMARY KEY")
            return " ".join(parts)

        sql_type, enum_values = cls._compile_column_sql_type(col, col_name)
        parts.append(sql_type)

        # Primary key
        if col.is_primary:
            parts.append("PRIMARY KEY")

        # Nullability
        if col.is_nullable:
            parts.append("NULL")
        elif not col.is_primary:
            parts.append("NOT NULL")

        # Default value
        if col.default_val is not _UNSET:
            default_expr = cls._format_column_default_expr(col, t)
            parts.append(f"DEFAULT {default_expr}")

        # Unique (if not named; named unique is emitted in constraints_sql)
        uq_explicit = col.unique_name or (col.index_name if not col.is_index else None)
        if col.is_unique and not uq_explicit:
            parts.append("UNIQUE")

        # Unsigned check
        if col.is_unsigned:
            parts.append(f'CHECK ("{col_name}" >= 0)')

        # Enum CHECK constraint
        if enum_values is not None:
            literals = ", ".join(cls._quote_sql_literal(v) for v in enum_values)
            if table is not None:
                chk_name = validate_identifier(f"{table}_{col_name}_check")
                parts.append(
                    f'CONSTRAINT "{chk_name}" CHECK ("{col_name}" IN ({literals}))'
                )
            else:
                parts.append(f'CHECK ("{col_name}" IN ({literals}))')

        return " ".join(parts)

    @classmethod
    def _compile_modify_column(cls, table: str, col: ColumnDefinition) -> list[str]:
        """Compile PostgreSQL ALTER COLUMN statements for a `.change()` column definition."""
        col_name = validate_identifier(col.name)
        t = col.type_name.strip().lower() if isinstance(col.type_name, str) else ""
        if t in {"id", "increments", "big_increments"}:
            raise ValueError(
                f"Auto-incrementing identity column '{col_name}' ({col.type_name}) cannot be used with .change()."
            )

        sql_type, enum_values = cls._compile_column_sql_type(col, col_name)
        statements: list[str] = []

        # Drop any existing column check constraint so unsigned/enum checks are replaced cleanly
        raw_chk_name = f"{table}_{col_name}_check"
        chk_name: str | None = None
        if len(raw_chk_name) <= MAX_IDENTIFIER_LENGTH:
            chk_name = validate_identifier(raw_chk_name)
            statements.append(
                f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{chk_name}";'
            )
        elif col.is_unsigned or enum_values is not None:
            validate_identifier(raw_chk_name)

        # Always drop existing default prior to TYPE alteration so incompatible defaults do not fail casts
        statements.append(
            f'ALTER TABLE "{table}" ALTER COLUMN "{col_name}" DROP DEFAULT;'
        )

        using_sql = f" USING {col.using_expression}" if col.using_expression else ""
        statements.append(
            f'ALTER TABLE "{table}" ALTER COLUMN "{col_name}" TYPE {sql_type}{using_sql};'
        )

        # Replace nullability explicitly
        if col.is_nullable:
            statements.append(
                f'ALTER TABLE "{table}" ALTER COLUMN "{col_name}" DROP NOT NULL;'
            )
        elif not col.is_primary:
            statements.append(
                f'ALTER TABLE "{table}" ALTER COLUMN "{col_name}" SET NOT NULL;'
            )

        # Re-apply default when explicitly specified
        if col.default_val is not _UNSET:
            default_expr = cls._format_column_default_expr(col, t)
            statements.append(
                f'ALTER TABLE "{table}" ALTER COLUMN "{col_name}" SET DEFAULT {default_expr};'
            )

        # Re-apply unsigned check constraint when specified
        if col.is_unsigned and enum_values is not None:
            raise ValueError(
                f"Enum column '{col_name}' cannot also be marked .unsigned()."
            )
        if col.is_unsigned and chk_name is not None:
            statements.append(
                f'ALTER TABLE "{table}" ADD CONSTRAINT "{chk_name}" CHECK ("{col_name}" >= 0);'
            )

        # Re-apply enum check constraint when changing to/updating an enum
        if enum_values is not None and chk_name is not None:
            literals = ", ".join(cls._quote_sql_literal(v) for v in enum_values)
            statements.append(
                f'ALTER TABLE "{table}" ADD CONSTRAINT "{chk_name}" CHECK ("{col_name}" IN ({literals}));'
            )

        if col.is_unique:
            uq_explicit = col.unique_name or (
                col.index_name if not col.is_index else None
            )
            uq_name = validate_identifier(uq_explicit or f"uq_{table}_{col_name}")
            default_pg_uq = f"{table}_{col_name}_key"
            if (
                not uq_explicit
                and len(default_pg_uq) <= MAX_IDENTIFIER_LENGTH
                and default_pg_uq != uq_name
            ):
                validate_identifier(default_pg_uq)
                statements.append(
                    f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{default_pg_uq}";'
                )
            statements.append(
                f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{uq_name}";'
            )
            statements.append(
                f'ALTER TABLE "{table}" ADD CONSTRAINT "{uq_name}" UNIQUE ("{col_name}");'
            )

        if col.is_index:
            idx_name = validate_identifier(col.index_name or f"idx_{table}_{col_name}")
            statements.append(
                f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{table}" ("{col_name}");'
            )

        # Replace or drop column comment explicitly
        if col.comment_text is not None:
            literal = cls._quote_sql_literal(col.comment_text)
            statements.append(
                f'COMMENT ON COLUMN "{table}"."{col_name}" IS {literal};'
            )
        else:
            statements.append(f'COMMENT ON COLUMN "{table}"."{col_name}" IS NULL;')

        return statements

    @staticmethod
    def _compile_comment(table: str, col: ColumnDefinition) -> list[str]:
        if col.comment_text is None:
            return []
        literal = SchemaGrammar._quote_sql_literal(col.comment_text)
        return [f'COMMENT ON COLUMN "{table}"."{col.name}" IS {literal};']

    @classmethod
    def _format_default_value(cls, val: Any) -> str:
        if val is None:
            return "NULL"
        if isinstance(val, bool):
            return "TRUE" if val else "FALSE"
        if isinstance(val, int):
            return str(val)
        if isinstance(val, float):
            if not math.isfinite(val):
                raise ValueError(f"Default float value must be finite, got {val!r}")
            return str(val)
        if isinstance(val, DefaultRaw):
            return val.sql
        if hasattr(val, "sql") and isinstance(val.sql, str):
            return val.sql.strip()
        if isinstance(val, (bytes, bytearray, memoryview)):
            hex_bytes = bytes(val).hex()
            return f"'\\x{hex_bytes}'::bytea"
        if isinstance(val, (dict, list)):
            json_escaped = json.dumps(val).replace("'", "''")
            return f"'{json_escaped}'"
        if isinstance(val, datetime):
            iso_escaped = val.isoformat().replace("'", "''")
            return f"'{iso_escaped}'"
        if isinstance(val, str):
            # Legacy magic PostgreSQL functions/keywords (deprecated in favor of raw(...))
            upper = val.strip().upper()
            if upper in {
                "CURRENT_TIMESTAMP",
                "CURRENT_DATE",
                "CURRENT_TIME",
                "NOW()",
                "GEN_RANDOM_UUID()",
                "TRUE",
                "FALSE",
                "NULL",
            }:
                warnings.warn(
                    f"Passing SQL expression {val!r} as a string to .default() is deprecated; "
                    f"use raw({val!r}) instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                return val.strip()
            # String literal: escape single quotes
            escaped = val.replace("'", "''")
            return f"'{escaped}'"
        escaped = str(val).replace("'", "''")
        return f"'{escaped}'"


class Schema:
    """Schema builder facade for defining and altering database tables."""

    @classmethod
    async def _get_connection(cls) -> tuple[asyncpg.Connection, bool]:
        """Returns the active transaction connection or acquires a new one from the pool."""
        tx_conn = get_active_connection()
        if tx_conn is not None:
            return tx_conn, False

        container = get_container()
        pool = container.resolve(DatabasePool).get_pool()
        conn = await pool.acquire()
        return conn, True

    @classmethod
    async def _release_connection(
        cls, conn: asyncpg.Connection, should_release: bool
    ) -> None:
        if should_release:
            container = get_container()
            await container.resolve(DatabasePool).get_pool().release(conn)

    @classmethod
    async def _execute_statements(cls, statements: list[str]) -> None:
        conn, should_release = await cls._get_connection()
        try:
            async with conn.transaction():
                for sql in statements:
                    if sql.strip():
                        await conn.execute(sql)
        finally:
            await cls._release_connection(conn, should_release)

    @classmethod
    async def create(cls, table: str, callback: Callable[[Blueprint], Any]) -> None:
        """Create a new database table using a fluent Blueprint."""
        blueprint = Blueprint(table, is_create=True)
        callback(blueprint)
        statements = SchemaGrammar.compile_create(blueprint)
        await cls._execute_statements(statements)

    @classmethod
    async def table(cls, table: str, callback: Callable[[Blueprint], Any]) -> None:
        """Modify an existing database table using a fluent Blueprint."""
        blueprint = Blueprint(table, is_create=False)
        callback(blueprint)
        statements = SchemaGrammar.compile_table(blueprint)
        await cls._execute_statements(statements)

    @classmethod
    async def drop(cls, table: str) -> None:
        """Drop a database table."""
        sql = SchemaGrammar.compile_drop(table)
        await cls._execute_statements([sql])

    @classmethod
    async def drop_if_exists(cls, table: str) -> None:
        """Drop a database table if it exists."""
        sql = SchemaGrammar.compile_drop_if_exists(table)
        await cls._execute_statements([sql])

    @staticmethod
    def _split_schema_and_table(table: str) -> tuple[str | None, str]:
        safe = validate_identifier(table, allow_dotted=True, allow_wildcard=False)
        parts = safe.split(".")
        if len(parts) > 2:
            raise ValueError(
                f"Invalid table identifier '{table}': expected 'table' or 'schema.table'."
            )
        if len(parts) == 2:
            return parts[0], parts[1]
        return None, parts[0]

    @classmethod
    async def has_table(cls, table: str) -> bool:
        """Determine whether the given table exists in the database."""
        schema_name, safe_table = cls._split_schema_and_table(table)
        conn, should_release = await cls._get_connection()
        try:
            if schema_name is not None:
                query = """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_name = $1
                        AND table_schema = $2
                    );
                """
                result = await conn.fetchval(query, safe_table, schema_name)
            else:
                query = """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_name = $1
                        AND table_schema = current_schema()
                    );
                """
                result = await conn.fetchval(query, safe_table)
            return bool(result)
        finally:
            await cls._release_connection(conn, should_release)

    @classmethod
    async def has_column(cls, table: str, column: str) -> bool:
        """Determine whether the given column exists on the specified table."""
        schema_name, safe_table = cls._split_schema_and_table(table)
        safe_column = validate_identifier(column)
        conn, should_release = await cls._get_connection()
        try:
            if schema_name is not None:
                query = """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_name = $1
                        AND column_name = $2
                        AND table_schema = $3
                    );
                """
                result = await conn.fetchval(query, safe_table, safe_column, schema_name)
            else:
                query = """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_name = $1
                        AND column_name = $2
                        AND table_schema = current_schema()
                    );
                """
                result = await conn.fetchval(query, safe_table, safe_column)
            return bool(result)
        finally:
            await cls._release_connection(conn, should_release)

    @classmethod
    async def has_columns(cls, table: str, columns: Sequence[str]) -> bool:
        """Determine whether all specified columns exist on the given table."""
        if isinstance(columns, (str, bytes, bytearray, memoryview)) or not isinstance(
            columns, Sequence
        ):
            raise TypeError(
                "columns must be a non-empty sequence of column name strings."
            )
        if len(columns) == 0:
            raise ValueError(
                "columns must be a non-empty sequence of column name strings."
            )

        schema_name, safe_table = cls._split_schema_and_table(table)
        safe_cols = list(dict.fromkeys(validate_identifier(c) for c in columns))
        conn, should_release = await cls._get_connection()
        try:
            if schema_name is not None:
                query = """
                    SELECT COUNT(DISTINCT column_name)
                    FROM information_schema.columns
                    WHERE table_name = $1
                    AND table_schema = $3
                    AND column_name = ANY($2::text[]);
                """
                count = await conn.fetchval(query, safe_table, safe_cols, schema_name)
            else:
                query = """
                    SELECT COUNT(DISTINCT column_name)
                    FROM information_schema.columns
                    WHERE table_name = $1
                    AND table_schema = current_schema()
                    AND column_name = ANY($2::text[]);
                """
                count = await conn.fetchval(query, safe_table, safe_cols)
            return int(count or 0) == len(safe_cols)
        finally:
            await cls._release_connection(conn, should_release)

    @classmethod
    async def has_index(
        cls,
        table: str,
        index: str | Sequence[str],
        type_: str | None = None,
        *,
        type: str | None = None,
    ) -> bool:
        """
        Determine whether a table has an index matching a name or exact ordered column sequence.

        Notes:
            - Constraint filters ``"primary"`` and ``"unique"`` match PostgreSQL primary/unique
              index flags (note: PostgreSQL primary keys are also unique indexes).
            - Access-method filters (e.g. ``"btree"``, ``"gin"``, ``"gist"``, ``"hash"``) match
              ``pg_am.amname``.
            - Partial indexes (with a ``WHERE`` predicate) are included; expression indexes match
              by index name (not plain column lists).
        """
        if type_ is not None and type is not None:
            raise TypeError("Specify at most one of 'type_' or 'type'.")

        effective_type = type if type is not None else type_
        normalized_type: str | None = None
        if effective_type is not None:
            if not isinstance(effective_type, str):
                raise TypeError("Index type filter must be a string.")
            normalized_type = effective_type.strip().lower()
            if not normalized_type:
                raise ValueError("Index type filter must not be empty.")

        schema_name, safe_table = cls._split_schema_and_table(table)
        target_name: str | None = None
        target_columns: list[str] | None = None

        if isinstance(index, str):
            target_name = validate_identifier(index, param_name="index").lower()
        elif isinstance(index, Sequence) and not isinstance(
            index, (bytes, bytearray, memoryview)
        ):
            if len(index) == 0:
                raise ValueError("Index column list must not be empty.")
            target_columns = [
                validate_identifier(c, param_name="index column").lower() for c in index
            ]
        else:
            raise TypeError(
                "index must be an index name string or a non-empty sequence of column names."
            )

        conn, should_release = await cls._get_connection()
        try:
            schema_predicate = (
                "tn.nspname = $2" if schema_name is not None else "tn.nspname = current_schema()"
            )
            query = f"""
                SELECT
                    ic.relname AS name,
                    am.amname AS type,
                    i.indisunique AS is_unique,
                    i.indisprimary AS is_primary,
                    array_agg(a.attname::text ORDER BY indseq.ord) AS columns
                FROM pg_index i
                JOIN pg_class tc ON tc.oid = i.indrelid
                JOIN pg_namespace tn ON tn.oid = tc.relnamespace
                JOIN pg_class ic ON ic.oid = i.indexrelid
                JOIN pg_am am ON am.oid = ic.relam
                CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS indseq(attnum, ord)
                LEFT JOIN pg_attribute a
                    ON a.attrelid = tc.oid
                    AND a.attnum = indseq.attnum
                    AND indseq.attnum > 0
                WHERE tc.relname = $1
                  AND {schema_predicate}
                  AND i.indisvalid
                  AND indseq.ord <= i.indnkeyatts
                GROUP BY ic.relname, am.amname, i.indisunique, i.indisprimary;
            """
            rows = (
                await conn.fetch(query, safe_table, schema_name)
                if schema_name is not None
                else await conn.fetch(query, safe_table)
            )
        finally:
            await cls._release_connection(conn, should_release)

        for row in rows:
            if normalized_type is not None:
                if normalized_type == "primary" and not bool(row["is_primary"]):
                    continue
                if normalized_type == "unique" and not bool(row["is_unique"]):
                    continue
                if normalized_type not in {"primary", "unique"} and (
                    str(row["type"]).lower() != normalized_type
                ):
                    continue

            if target_name is not None and str(row["name"]).lower() == target_name:
                return True

            if target_columns is not None:
                raw_cols = row["columns"] or []
                row_cols = [
                    c.lower() if isinstance(c, str) else None for c in raw_cols
                ]
                if row_cols == target_columns:
                    return True

        return False


__all__ = ["Schema", "SchemaGrammar", "raw"]
