from __future__ import annotations

import pytest

from swiftpy.database.identifiers import (
    MAX_IDENTIFIER_LENGTH,
    quote_identifier,
    validate_column_identifier,
    validate_identifier,
)


def test_valid_identifiers() -> None:
    assert validate_identifier("users") == "users"
    assert validate_identifier("_users") == "_users"
    assert validate_identifier("user_123") == "user_123"
    assert validate_identifier("User") == "User"

    # Exactly 63 characters (PostgreSQL NAMEDATALEN - 1)
    max_id = "a" * MAX_IDENTIFIER_LENGTH
    assert validate_identifier(max_id) == max_id


def test_rejects_trailing_or_leading_whitespace_and_newlines() -> None:
    for invalid in ("users\n", "\nusers", "users\r\n", "users ", " users", "users\t"):
        with pytest.raises(ValueError, match="Invalid SQL identifier detected"):
            validate_identifier(invalid)


def test_rejects_overlong_identifiers() -> None:
    too_long = "a" * (MAX_IDENTIFIER_LENGTH + 1)
    with pytest.raises(ValueError, match="Invalid SQL identifier detected"):
        validate_identifier(too_long)


def test_rejects_unicode_characters() -> None:
    for unicode_id in ("usérs", "таблица", "üser", "user_😊"):
        with pytest.raises(ValueError, match="Invalid SQL identifier detected"):
            validate_identifier(unicode_id)


def test_rejects_empty_and_non_identifier_characters() -> None:
    for invalid in ("", "123user", "user-name", "user name", "user;drop table users;", "user--"):
        with pytest.raises(ValueError, match="Invalid SQL identifier detected"):
            validate_identifier(invalid)


def test_dotted_identifiers() -> None:
    # Dotted not allowed by default
    with pytest.raises(ValueError, match="Invalid SQL identifier detected"):
        validate_identifier("users.id", allow_dotted=False)

    # Allowed when allow_dotted=True
    assert validate_identifier("users.id", allow_dotted=True) == "users.id"
    assert validate_identifier("public.users.id", allow_dotted=True) == "public.users.id"

    # Reject empty segments or invalid segments in dotted notation
    for invalid in ("users..id", ".users", "users.", "users.id\n", "users.id;drop"):
        with pytest.raises(ValueError, match="Invalid SQL identifier detected"):
            validate_identifier(invalid, allow_dotted=True)

    # Reject segment longer than 63 characters
    long_segment = f"users.{'a' * (MAX_IDENTIFIER_LENGTH + 1)}"
    with pytest.raises(ValueError, match="Invalid SQL identifier detected"):
        validate_identifier(long_segment, allow_dotted=True)


def test_wildcard_handling() -> None:
    # Wildcard rejected by default
    with pytest.raises(ValueError, match="Invalid SQL identifier detected"):
        validate_identifier("*", allow_wildcard=False)

    with pytest.raises(ValueError, match="Invalid SQL identifier detected"):
        validate_identifier("users.*", allow_dotted=True, allow_wildcard=False)

    # Allowed when allow_wildcard=True
    assert validate_identifier("*", allow_wildcard=True) == "*"
    assert validate_identifier("users.*", allow_dotted=True, allow_wildcard=True) == "users.*"


def test_custom_param_name_error_message() -> None:
    with pytest.raises(ValueError, match="Invalid table identifier 'users\n'"):
        validate_identifier("users\n", param_name="table")

    with pytest.raises(ValueError, match="Invalid column identifier 'id\n'"):
        validate_identifier("id\n", param_name="column")


def test_validate_column_identifier() -> None:
    assert validate_column_identifier("*") == "*"
    assert validate_column_identifier("id") == "id"
    assert validate_column_identifier("users.id") == "users.id"
    assert validate_column_identifier("users.*") == "users.*"

    for invalid in ("id\n", "users.id\n", "users.* \n", "*; drop table users;"):
        with pytest.raises(ValueError, match="Invalid SQL column identifier detected"):
            validate_column_identifier(invalid)


def test_quote_identifier() -> None:
    assert quote_identifier("*") == "*"
    assert quote_identifier("users") == '"users"'
    assert quote_identifier("users.id") == '"users"."id"'
    assert quote_identifier("users.*") == '"users".*'

    with pytest.raises(ValueError, match="Invalid SQL column identifier detected"):
        quote_identifier("users\n.id")
