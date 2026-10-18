from __future__ import annotations

import re
from typing import Any, Self

_UNSET = object()
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def validate_identifier(name: str) -> str:
    """Validate that an SQL identifier contains only alphanumeric characters and underscores."""
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL identifier detected: {name!r}")
    return name


class ColumnDefinition:
    """Represents a fluent column definition inside a table blueprint."""

    def __init__(self, name: str, type_name: str) -> None:
        self.name: str = validate_identifier(name)
        self.type_name: str = type_name
        self.length: int | None = None
        self.precision: int | None = None
        self.scale: int | None = None
        self.is_nullable: bool = False
        self.is_unique: bool = False
        self.is_primary: bool = False
        self.default_val: Any = _UNSET
        self.is_index: bool = False
        self.index_name: str | None = None
        self.comment_text: str | None = None
        self.is_unsigned: bool = False

    def nullable(self, value: bool = True) -> Self:
        """Specify whether the column may accept NULL values."""
        self.is_nullable = value
        return self

    def default(self, value: Any) -> Self:
        """Specify the default value for the column."""
        self.default_val = value
        return self

    def unique(self, name: str | None = None) -> Self:
        """Add a unique constraint or index for this column."""
        self.is_unique = True
        if name:
            self.index_name = validate_identifier(name)
        return self

    def index(self, name: str | None = None) -> Self:
        """Add an index for this column."""
        self.is_index = True
        if name:
            self.index_name = validate_identifier(name)
        return self

    def primary(self) -> Self:
        """Mark this column as the primary key."""
        self.is_primary = True
        return self

    def comment(self, text: str) -> Self:
        """Set a comment on this column."""
        self.comment_text = text
        return self

    def unsigned(self) -> Self:
        """Mark an integer column as unsigned."""
        self.is_unsigned = True
        return self


class ForeignKeyDefinition:
    """Represents a fluent foreign key constraint definition."""

    def __init__(self, column: str, name: str | None = None) -> None:
        self.column: str = validate_identifier(column)
        self.name: str | None = validate_identifier(name) if name else None
        self.ref_column: str = "id"
        self.ref_table: str | None = None
        self.on_delete_action: str = "NO ACTION"
        self.on_update_action: str = "NO ACTION"

    def references(self, column: str) -> Self:
        """Specify the referenced column on the foreign table."""
        self.ref_column = validate_identifier(column)
        return self

    def on(self, table: str) -> Self:
        """Specify the referenced foreign table."""
        self.ref_table = validate_identifier(table)
        return self

    def on_delete(self, action: str) -> Self:
        """Set ON DELETE referential action."""
        action_clean = action.strip().upper()
        allowed = {"CASCADE", "SET NULL", "SET DEFAULT", "RESTRICT", "NO ACTION"}
        if action_clean not in allowed:
            raise ValueError(f"Invalid ON DELETE action: {action!r}")
        self.on_delete_action = action_clean
        return self

    def on_update(self, action: str) -> Self:
        """Set ON UPDATE referential action."""
        action_clean = action.strip().upper()
        allowed = {"CASCADE", "SET NULL", "SET DEFAULT", "RESTRICT", "NO ACTION"}
        if action_clean not in allowed:
            raise ValueError(f"Invalid ON UPDATE action: {action!r}")
        self.on_update_action = action_clean
        return self

    def cascade_on_delete(self) -> Self:
        """Convenience method for ON DELETE CASCADE."""
        return self.on_delete("CASCADE")

    def cascade_on_update(self) -> Self:
        """Convenience method for ON UPDATE CASCADE."""
        return self.on_update("CASCADE")

    def null_on_delete(self) -> Self:
        """Convenience method for ON DELETE SET NULL."""
        return self.on_delete("SET NULL")


class IndexDefinition:
    """Represents a table-level index or composite unique constraint."""

    def __init__(
        self,
        columns: list[str],
        name: str | None = None,
        *,
        is_unique: bool = False,
    ) -> None:
        self.columns: list[str] = [validate_identifier(c) for c in columns]
        self.name: str | None = validate_identifier(name) if name else None
        self.is_unique: bool = is_unique


class Blueprint:
    """Table definition Domain-Specific Language (DSL) for schema creation and modification."""

    def __init__(self, table: str, *, is_create: bool = True) -> None:
        self.table: str = validate_identifier(table)
        self.is_create: bool = is_create
        self.columns: list[ColumnDefinition] = []
        self.foreign_keys: list[ForeignKeyDefinition] = []
        self.indexes: list[IndexDefinition] = []
        self.primary_columns: list[str] = []
        self.primary_key_name: str | None = None
        self.drop_columns_list: list[str] = []
        self.rename_columns_map: dict[str, str] = {}
        self.drop_indexes_list: list[str] = []
        self.drop_unique_list: list[str] = []
        self.drop_foreign_list: list[str] = []

    def _add_column(self, name: str, type_name: str) -> ColumnDefinition:
        col = ColumnDefinition(name, type_name)
        self.columns.append(col)
        return col

    # --- Identity & Primary Keys ---

    def id(self, name: str = "id") -> ColumnDefinition:
        """Create a Bigint auto-incrementing identity primary key."""
        col = self._add_column(name, "id")
        col.is_primary = True
        return col

    def increments(self, name: str = "id") -> ColumnDefinition:
        """Create a 32-bit serial auto-incrementing primary key."""
        col = self._add_column(name, "increments")
        col.is_primary = True
        return col

    def big_increments(self, name: str = "id") -> ColumnDefinition:
        """Create a 64-bit bigserial auto-incrementing primary key."""
        col = self._add_column(name, "big_increments")
        col.is_primary = True
        return col

    # --- Data Types ---

    def uuid(self, name: str) -> ColumnDefinition:
        """Create a UUID column."""
        return self._add_column(name, "uuid")

    def string(self, name: str, length: int | None = 255) -> ColumnDefinition:
        """Create a VARCHAR string column with optional max length."""
        col = self._add_column(name, "string")
        col.length = length
        return col

    def text(self, name: str) -> ColumnDefinition:
        """Create a TEXT column."""
        return self._add_column(name, "text")

    def integer(self, name: str) -> ColumnDefinition:
        """Create a standard 32-bit INTEGER column."""
        return self._add_column(name, "integer")

    def big_integer(self, name: str) -> ColumnDefinition:
        """Create a 64-bit BIGINT column."""
        return self._add_column(name, "big_integer")

    def small_integer(self, name: str) -> ColumnDefinition:
        """Create a 16-bit SMALLINT column."""
        return self._add_column(name, "small_integer")

    def boolean(self, name: str) -> ColumnDefinition:
        """Create a BOOLEAN column."""
        return self._add_column(name, "boolean")

    def decimal(
        self, name: str, precision: int = 8, scale: int = 2
    ) -> ColumnDefinition:
        """Create a fixed-point NUMERIC/DECIMAL column."""
        col = self._add_column(name, "decimal")
        col.precision = precision
        col.scale = scale
        return col

    def float(self, name: str) -> ColumnDefinition:
        """Create a floating-point DOUBLE PRECISION column."""
        return self._add_column(name, "float")

    def json(self, name: str) -> ColumnDefinition:
        """Create a JSON column."""
        return self._add_column(name, "json")

    def jsonb(self, name: str) -> ColumnDefinition:
        """Create a binary JSONB column."""
        return self._add_column(name, "jsonb")

    def date(self, name: str) -> ColumnDefinition:
        """Create a DATE column."""
        return self._add_column(name, "date")

    def time(self, name: str) -> ColumnDefinition:
        """Create a TIME column."""
        return self._add_column(name, "time")

    def timestamp(self, name: str, tz: bool = True) -> ColumnDefinition:
        """Create a TIMESTAMP column (with or without time zone)."""
        type_name = "timestamptz" if tz else "timestamp"
        return self._add_column(name, type_name)

    def timestamps(self, tz: bool = True) -> tuple[ColumnDefinition, ColumnDefinition]:
        """Create created_at and updated_at timestamp columns with default CURRENT_TIMESTAMP."""
        created = self.timestamp("created_at", tz=tz).default("CURRENT_TIMESTAMP")
        updated = self.timestamp("updated_at", tz=tz).default("CURRENT_TIMESTAMP")
        return created, updated

    def soft_deletes(
        self, name: str = "deleted_at", tz: bool = True
    ) -> ColumnDefinition:
        """Create a nullable deleted_at timestamp column for soft deletion."""
        return self.timestamp(name, tz=tz).nullable()

    # --- Constraints & Indexes ---

    def unique(self, columns: str | list[str], name: str | None = None) -> Self:
        """Add a table-level or composite UNIQUE constraint."""
        cols = [columns] if isinstance(columns, str) else columns
        self.indexes.append(IndexDefinition(cols, name, is_unique=True))
        return self

    def index(self, columns: str | list[str], name: str | None = None) -> Self:
        """Add a table-level or composite index."""
        cols = [columns] if isinstance(columns, str) else columns
        self.indexes.append(IndexDefinition(cols, name, is_unique=False))
        return self

    def primary(self, columns: str | list[str], name: str | None = None) -> Self:
        """Define a table-level or composite primary key constraint."""
        cols = [columns] if isinstance(columns, str) else columns
        self.primary_columns = [validate_identifier(c) for c in cols]
        if name:
            self.primary_key_name = validate_identifier(name)
        return self

    def foreign(self, column: str, name: str | None = None) -> ForeignKeyDefinition:
        """Define a fluent foreign key constraint on the given column."""
        fk = ForeignKeyDefinition(column, name)
        self.foreign_keys.append(fk)
        return fk

    def foreign_key(
        self,
        column: str,
        references: str,
        on: str,
        on_delete: str = "NO ACTION",
        on_update: str = "NO ACTION",
        name: str | None = None,
    ) -> Self:
        """Direct helper to define a complete foreign key constraint."""
        fk = self.foreign(column, name)
        fk.references(references).on(on).on_delete(on_delete).on_update(on_update)
        return self

    # --- Alter Operations ---

    def drop_column(self, *names: str) -> Self:
        """Drop one or more columns from an existing table."""
        for n in names:
            self.drop_columns_list.append(validate_identifier(n))
        return self

    def rename_column(self, from_name: str, to_name: str) -> Self:
        """Rename an existing column."""
        self.rename_columns_map[validate_identifier(from_name)] = validate_identifier(
            to_name
        )
        return self

    def drop_index(self, name: str) -> Self:
        """Drop an index by name."""
        self.drop_indexes_list.append(validate_identifier(name))
        return self

    def drop_unique(self, name: str) -> Self:
        """Drop a unique constraint by name."""
        self.drop_unique_list.append(validate_identifier(name))
        return self

    def drop_foreign(self, name: str) -> Self:
        """Drop a foreign key constraint by name."""
        self.drop_foreign_list.append(validate_identifier(name))
        return self
