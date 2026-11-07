from __future__ import annotations

from typing import ClassVar

from swiftpy.database.orm.model import Model
from swiftpy.database.orm.relationships import HasOne
from swiftpy.http.response import json_response


class SecretProfile(Model):
    __table__ = "secret_profiles"
    id: int
    user_id: int
    ssn: str
    bio: str
    __hidden__: ClassVar[tuple[str, ...]] = ("ssn",)
    __guarded__: ClassVar[list[str]] = []


class SecretUser(Model):
    __table__ = "secret_users"
    id: int
    name: str
    email: str
    password_hash: str
    api_token: str
    __hidden__: ClassVar[tuple[str, ...]] = ("password_hash", "api_token")
    __guarded__: ClassVar[list[str]] = []

    profile = HasOne("SecretProfile", foreign_key="user_id")


def test_hidden_fields_omitted_by_default() -> None:
    user = SecretUser(
        id=1,
        name="Alice",
        email="alice@example.com",
        password_hash="argon2$xyz",
        api_token="tok_12345",
    )
    data = user.to_dict()

    assert "id" in data
    assert "name" in data
    assert "email" in data
    assert "password_hash" not in data
    assert "api_token" not in data


def test_hidden_fields_included_with_flag() -> None:
    user = SecretUser(
        id=1,
        name="Alice",
        email="alice@example.com",
        password_hash="argon2$xyz",
        api_token="tok_12345",
    )
    data = user.to_dict(include_hidden=True)

    assert data["password_hash"] == "argon2$xyz"
    assert data["api_token"] == "tok_12345"


def test_make_visible_and_make_hidden_helpers() -> None:
    user = SecretUser(
        id=1,
        name="Alice",
        email="alice@example.com",
        password_hash="argon2$xyz",
        api_token="tok_12345",
    )

    # make_visible reveals password_hash
    user.make_visible("password_hash")
    data1 = user.to_dict()
    assert "password_hash" in data1
    assert "api_token" not in data1

    # make_hidden hides email
    user.make_hidden("email")
    data2 = user.to_dict()
    assert "email" not in data2
    assert "password_hash" in data2


def test_repr_shows_only_class_and_primary_key() -> None:
    user = SecretUser(
        id=42,
        name="Bob",
        email="bob@example.com",
        password_hash="secret_hash",
        api_token="secret_token",
    )
    rep = repr(user)

    assert rep == "<SecretUser id=42>"
    assert "secret_hash" not in rep
    assert "secret_token" not in rep
    assert "bob@example.com" not in rep


def test_relations_respect_hidden_fields() -> None:
    user = SecretUser(
        id=1,
        name="Alice",
        email="alice@example.com",
        password_hash="hash",
        api_token="token",
    )
    profile = SecretProfile(id=10, user_id=1, ssn="999-99-9999", bio="Developer")
    user._loaded_relations["profile"] = profile

    data = user.to_dict()

    assert "profile" in data
    assert data["profile"]["bio"] == "Developer"
    assert "ssn" not in data["profile"]

    # When including hidden, relation also includes hidden
    full_data = user.to_dict(include_hidden=True)
    assert full_data["profile"]["ssn"] == "999-99-9999"


def test_json_response_omits_hidden_fields() -> None:
    user = SecretUser(
        id=1,
        name="Alice",
        email="alice@example.com",
        password_hash="hash",
        api_token="token",
    )
    resp = json_response(user)
    body = resp.content.decode("utf-8")

    assert "Alice" in body
    assert "password_hash" not in body
    assert "api_token" not in body
