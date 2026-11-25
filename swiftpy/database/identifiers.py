from __future__ import annotations

import re

__all__ = [
    "MAX_IDENTIFIER_LENGTH",
    "quote_identifier",
    "split_json_path",
    "validate_column_identifier",
    "validate_identifier",
]

MAX_IDENTIFIER_LENGTH: int = 63

_IDENTIFIER_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_COLUMN_RE: re.Pattern[str] = re.compile(
    r"^(?:"
    r"\*"
    r"|"
    r"[a-zA-Z_][a-zA-Z0-9_]*"
    r"(?:\.(?:[a-zA-Z_][a-zA-Z0-9_]*|\*))?"
    r")$"
)
_JSON_PATH_SEGMENT_RE: re.Pattern[str] = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*|0|[1-9][0-9]*)$"
)


def split_json_path(
    expr: str, *, require_path: bool = True
) -> tuple[str, tuple[str, ...]]:
    """Validate a column or ``column->path`` expression and return its components."""
    if not isinstance(expr, str):
        raise TypeError("JSON path expression must be a string.")

    def invalid() -> ValueError:
        return ValueError(f"Invalid JSON path expression {expr!r}")

    column, separator, path = expr.partition("->")
    try:
        safe_column = validate_identifier(
            column, allow_dotted=True, allow_wildcard=False
        )
    except (TypeError, ValueError):
        raise invalid() from None

    if not separator:
        if require_path:
            raise invalid()
        return safe_column, ()

    segments = tuple(path.split("->"))
    if (
        not path
        or len(segments) > 16
        or any(
            len(segment) > MAX_IDENTIFIER_LENGTH
            or not _JSON_PATH_SEGMENT_RE.fullmatch(segment)
            for segment in segments
        )
    ):
        raise invalid()
    return safe_column, segments


def validate_identifier(
    identifier: str,
    *,
    allow_dotted: bool = False,
    allow_wildcard: bool = False,
    param_name: str | None = None,
) -> str:
    """
    Validate that an SQL identifier is secure, strictly alphanumeric/underscores,
    within the 63-character PostgreSQL identifier limit, and contains no newlines or SQL injection.
    """
    if not isinstance(identifier, str):
        msg = (
            f"Invalid {param_name} identifier '{identifier}'"
            if param_name
            else f"Invalid SQL identifier detected: {identifier}"
        )
        raise TypeError(msg)

    if allow_wildcard and identifier == "*":
        return identifier

    if not identifier:
        msg = (
            f"Invalid {param_name} identifier '{identifier}'"
            if param_name
            else f"Invalid SQL identifier detected: {identifier}"
        )
        raise ValueError(msg)

    segments = identifier.split(".") if allow_dotted else [identifier]
    for seg in segments:
        if allow_wildcard and seg == "*":
            continue
        if len(seg) > MAX_IDENTIFIER_LENGTH or not _IDENTIFIER_RE.fullmatch(seg):
            msg = (
                f"Invalid {param_name} identifier '{identifier}'"
                if param_name
                else f"Invalid SQL identifier detected: {identifier}"
            )
            raise ValueError(msg)

    return identifier


def validate_column_identifier(column: str) -> str:
    """
    Validate a column identifier used in projections, supporting '*' or 'table.*' or 'table.column'.
    """
    if not isinstance(column, str):
        raise TypeError(
            f"Invalid SQL column identifier type: expected str, got {type(column).__name__}"
        )
    if not _COLUMN_RE.fullmatch(column):
        raise ValueError(f"Invalid SQL column identifier detected: {column}")

    segments = column.split(".")
    for seg in segments:
        if seg != "*" and len(seg) > MAX_IDENTIFIER_LENGTH:
            raise ValueError(f"Invalid SQL column identifier detected: {column}")

    return column


def quote_identifier(
    identifier: str,
    *,
    allow_dotted: bool = True,
    allow_wildcard: bool = True,
) -> str:
    """Validate, then quote each dotted segment: t.col becomes "t"."col"."""
    validate_identifier(
        identifier,
        allow_dotted=allow_dotted,
        allow_wildcard=allow_wildcard,
    )
    if identifier == "*":
        return "*"
    return ".".join(
        f'"{part}"' if part != "*" else "*" for part in identifier.split(".")
    )
