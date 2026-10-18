from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

import asyncpg

from swiftpy.core.bootstrap import get_container
from swiftpy.database.migrations.blueprint import (
    _UNSET,
    Blueprint,
    ColumnDefinition,
    validate_identifier,
)
from swiftpy.database.pool import DatabasePool
from swiftpy.database.transaction import get_active_connection


class SchemaGrammar:
    """Translates a Blueprint into PostgreSQL Data Definition Language (DDL) statements."""

    @classmethod
    def compile_create(cls, blueprint: Blueprint) -> list[str]:
        table = blueprint.table
        columns_sql: list[str] = []
        constraints_sql: list[str] = []
        post_statements: list[str] = []

        # 1. Compile columns
        for col in blueprint.columns:
            col_sql = cls._compile_column(col)
            columns_sql.append(col_sql)

            # Standalone index on column
            if col.is_index:
                idx_name = col.index_name or f"idx_{table}_{col.name}"
                post_statements.append(
                    f'CREATE INDEX "{idx_name}" ON "{table}" ("{col.name}");'
                )

            # Standalone named unique constraint on column
            if col.is_unique and col.index_name:
                constraints_sql.append(
                    f'CONSTRAINT "{col.index_name}" UNIQUE ("{col.name}")'
                )

            # Column comments
            post_statements.extend(cls._compile_comment(table, col))

        # 2. Compile table-level primary key (e.g. composite primary key)
        if blueprint.primary_columns:
            pk_name = blueprint.primary_key_name or f"pk_{table}"
            cols_joined = ", ".join(f'"{c}"' for c in blueprint.primary_columns)
            constraints_sql.append(
                f'CONSTRAINT "{pk_name}" PRIMARY KEY ({cols_joined})'
            )

        # 3. Compile table-level unique constraints and indexes
        for idx in blueprint.indexes:
            cols_joined = ", ".join(f'"{c}"' for c in idx.columns)
            name_suffix = "_".join(idx.columns)

            if idx.is_unique:
                uq_name = idx.name or f"uq_{table}_{name_suffix}"
                constraints_sql.append(f'CONSTRAINT "{uq_name}" UNIQUE ({cols_joined})')
            else:
                idx_name = idx.name or f"idx_{table}_{name_suffix}"
                post_statements.append(
                    f'CREATE INDEX "{idx_name}" ON "{table}" ({cols_joined});'
                )

        # 4. Compile foreign key constraints
        for fk in blueprint.foreign_keys:
            if not fk.ref_table:
                raise ValueError(
                    f"Foreign key on {table}.{fk.column} must specify a referenced table via .on('table_name')"
                )
            fk_name = fk.name or f"fk_{table}_{fk.column}_{fk.ref_table}"
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
        table = blueprint.table
        statements: list[str] = []

        # 1. Drops first, so one blueprint can replace what it removes
        for name in blueprint.drop_foreign_list:
            statements.append(
                f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{name}";'
            )
        for name in blueprint.drop_unique_list:
            statements.append(
                f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{name}";'
            )
        for name in blueprint.drop_indexes_list:
            statements.append(f'DROP INDEX IF EXISTS "{name}";')
        for name in blueprint.drop_columns_list:
            statements.append(f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{name}";')

        # 2. Renames
        for old_name, new_name in blueprint.rename_columns_map.items():
            statements.append(
                f'ALTER TABLE "{table}" RENAME COLUMN "{old_name}" TO "{new_name}";'
            )

        # 3. Adds
        for col in blueprint.columns:
            statements.append(
                f'ALTER TABLE "{table}" ADD COLUMN {cls._compile_column(col)};'
            )
            if col.is_index:
                idx_name = col.index_name or f"idx_{table}_{col.name}"
                statements.append(
                    f'CREATE INDEX "{idx_name}" ON "{table}" ("{col.name}");'
                )
            statements.extend(cls._compile_comment(table, col))

        for idx in blueprint.indexes:
            cols_joined = ", ".join(f'"{c}"' for c in idx.columns)
            suffix = "_".join(idx.columns)
            if idx.is_unique:
                name = idx.name or f"uq_{table}_{suffix}"
                statements.append(
                    f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" UNIQUE ({cols_joined});'
                )
            else:
                name = idx.name or f"idx_{table}_{suffix}"
                statements.append(
                    f'CREATE INDEX "{name}" ON "{table}" ({cols_joined});'
                )

        for fk in blueprint.foreign_keys:
            if not fk.ref_table:
                raise ValueError(
                    f"Foreign key on {table}.{fk.column} must specify a referenced table via .on('table_name')"
                )
            fk_name = fk.name or f"fk_{table}_{fk.column}_{fk.ref_table}"
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

    @classmethod
    def _compile_column(cls, col: ColumnDefinition) -> str:
        parts: list[str] = [f'"{col.name}"']
        t = col.type_name.lower()

        # Type mapping
        if t == "id" or t == "big_increments":
            parts.append("BIGSERIAL PRIMARY KEY")
            return " ".join(parts)
        elif t == "increments":
            parts.append("SERIAL PRIMARY KEY")
            return " ".join(parts)
        elif t == "uuid":
            parts.append("UUID")
        elif t == "string":
            parts.append(f"VARCHAR({col.length})" if col.length else "VARCHAR")
        elif t == "text":
            parts.append("TEXT")
        elif t == "integer":
            parts.append("INTEGER")
        elif t == "big_integer":
            parts.append("BIGINT")
        elif t == "small_integer":
            parts.append("SMALLINT")
        elif t == "boolean":
            parts.append("BOOLEAN")
        elif t == "decimal":
            precision = col.precision or 8
            scale = col.scale if col.scale is not None else 2
            parts.append(f"NUMERIC({precision}, {scale})")
        elif t == "float":
            parts.append("DOUBLE PRECISION")
        elif t == "json":
            parts.append("JSON")
        elif t == "jsonb":
            parts.append("JSONB")
        elif t == "date":
            parts.append("DATE")
        elif t == "time":
            parts.append("TIME")
        elif t == "timestamp":
            parts.append("TIMESTAMP")
        elif t == "timestamptz":
            parts.append("TIMESTAMPTZ")
        else:
            parts.append(col.type_name.upper())

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
            default_expr = cls._format_default_value(col.default_val)
            parts.append(f"DEFAULT {default_expr}")

        # Unique (if not named; named unique is emitted in constraints_sql)
        if col.is_unique and not col.index_name:
            parts.append("UNIQUE")

        # Unsigned check
        if col.is_unsigned:
            parts.append(f'CHECK ("{col.name}" >= 0)')

        return " ".join(parts)

    @staticmethod
    def _compile_comment(table: str, col: ColumnDefinition) -> list[str]:
        if col.comment_text is None:
            return []
        escaped = col.comment_text.replace("'", "''")
        return [f'COMMENT ON COLUMN "{table}"."{col.name}" IS \'{escaped}\';']

    @classmethod
    def _format_default_value(cls, val: Any) -> str:
        if val is None:
            return "NULL"
        if isinstance(val, bool):
            return "TRUE" if val else "FALSE"
        if isinstance(val, (int, float)):
            return str(val)
        if isinstance(val, (dict, list)):
            json_escaped = json.dumps(val).replace("'", "''")
            return f"'{json_escaped}'"
        if isinstance(val, datetime):
            iso_escaped = val.isoformat().replace("'", "''")
            return f"'{iso_escaped}'"
        if isinstance(val, str):
            # Check for PostgreSQL functions/keywords
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
                return val.strip()
            # String literal: escape single quotes
            escaped = val.replace("'", "''")
            return f"'{escaped}'"
        return f"'{val}'"


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
    async def _execute_statements(cls, statements: list[str]) -> None:
        conn, should_release = await cls._get_connection()
        try:
            async with conn.transaction():
                for sql in statements:
                    if sql.strip():
                        await conn.execute(sql)
        finally:
            if should_release:
                container = get_container()
                await container.resolve(DatabasePool).get_pool().release(conn)

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

    @classmethod
    async def has_table(cls, table: str) -> bool:
        """Determine whether the given table exists in the database."""
        safe_table = validate_identifier(table)
        conn, should_release = await cls._get_connection()
        try:
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
            if should_release:
                container = get_container()
                await container.resolve(DatabasePool).get_pool().release(conn)

    @classmethod
    async def has_column(cls, table: str, column: str) -> bool:
        """Determine whether the given column exists on the specified table."""
        safe_table = validate_identifier(table)
        safe_column = validate_identifier(column)
        conn, should_release = await cls._get_connection()
        try:
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
            if should_release:
                container = get_container()
                await container.resolve(DatabasePool).get_pool().release(conn)
