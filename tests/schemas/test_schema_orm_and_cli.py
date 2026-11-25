from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, ClassVar

import pytest

from app.schemas.user_schema import (
    UserCreateSchema,
    UserResponseSchema,
    UserSchema,
)
from swiftpy.console.cli import CLI
from swiftpy.database.orm import Field as OrmField
from swiftpy.database.orm import Model
from swiftpy.schemas import BaseSchema, Field
from swiftpy.validation.exceptions import ValidationException


class _DocVersionItem(Model):
    __table__ = "doc_version_items"
    __fillable__: ClassVar[list[str]] = [
        "version",
        "slug",
        "description",
        "sort_order",
        "is_active",
    ]

    id: int | None = OrmField(default=None, primary_key=True)
    version: str = ""
    slug: str = ""
    description: str | None = None
    sort_order: int = 0
    is_active: bool = True
    internal_flag: str = "default-flag"


class _DocVersionCreateSchema(BaseSchema):
    version: str = Field(..., min_length=1, max_length=32)
    slug: str = Field(..., min_length=1, max_length=64)
    description: str | None = None
    sort_order: int = 0
    is_active: bool = True


class _DocVersionUpdateSchema(BaseSchema):
    version: str | None = None
    slug: str | None = None
    description: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class _DocVersionForceSchema(BaseSchema):
    internal_flag: str


class _DocVersionOutSchema(BaseSchema):
    id: int
    version: str
    slug: str
    description: str | None = None
    sort_order: int
    is_active: bool


class _RecordingQuery:
    def __init__(self) -> None:
        self.inserted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.where_clauses: list[tuple[str, Any]] = []

    def where(self, column: str, value: Any) -> _RecordingQuery:
        self.where_clauses.append((column, value))
        return self

    async def insert_returning(
        self, data: dict[str, Any], *, allow_default_values: bool = False
    ) -> dict[str, Any]:
        _ = allow_default_values
        self.inserted.append(dict(data))
        return {"id": 101, **data}

    async def update(self, data: dict[str, Any]) -> int:
        self.updated.append(dict(data))
        return 1


async def test_model_create_and_update_accept_base_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _RecordingQuery()
    monkeypatch.setattr(
        _DocVersionItem,
        "query",
        classmethod(lambda cls: recorder),
    )

    create_payload = _DocVersionCreateSchema.validate(
        {
            "version": "v0.3.7",
            "slug": "v0-3-7",
            "description": "Release 0.3.7",
            "sort_order": "3",
        }
    )
    created = await _DocVersionItem.create(create_payload)
    assert created.id == 101
    assert created.version == "v0.3.7"
    assert created.slug == "v0-3-7"
    assert created.sort_order == 3
    assert recorder.inserted == [
        {
            "version": "v0.3.7",
            "slug": "v0-3-7",
            "description": "Release 0.3.7",
            "sort_order": 3,
            "is_active": True,
            "internal_flag": "default-flag",
        }
    ]

    # Partial update using exclude_unset=True preserves omitted fields
    partial_update = _DocVersionUpdateSchema.validate({"slug": "v0-3-7-final"})
    await created.update(partial_update)
    assert created.slug == "v0-3-7-final"
    assert created.version == "v0.3.7"
    assert created.description == "Release 0.3.7"
    assert recorder.updated == [{"slug": "v0-3-7-final"}]

    # Guarded field on non-force update is ignored by __fillable__
    await created.update(_DocVersionForceSchema(internal_flag="ignored-by-fillable"))
    assert created.internal_flag == "default-flag"

    # force_create and force_update bypass __fillable__
    forced = await _DocVersionItem.force_create(
        create_payload, internal_flag="admin-created"
    )
    assert forced.internal_flag == "admin-created"

    await forced.force_update(_DocVersionForceSchema(internal_flag="admin-updated"))
    assert forced.internal_flag == "admin-updated"
    assert recorder.updated[-1] == {"internal_flag": "admin-updated"}

    # from_model builds a typed response schema from the ORM instance
    out = _DocVersionOutSchema.from_model(created)
    assert out.id == 101
    assert out.slug == "v0-3-7-final"


def test_make_schema_and_new_project_scaffold_use_base_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cli = CLI()
    cli.run(["make:schema", "EggCollectionSchema"])

    schema_file = tmp_path / "app" / "schemas" / "egg_collection_schema.py"
    assert schema_file.is_file()
    content = schema_file.read_text(encoding="utf-8")
    assert "from swiftpy.schemas import BaseSchema" in content
    assert "class EggCollectionSchema(BaseSchema):" in content

    spec = importlib.util.spec_from_file_location("generated_egg_schema", schema_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    generated_cls = mod.EggCollectionSchema
    assert issubclass(generated_cls, BaseSchema)

    # Verify starter UserCreateSchema / UserResponseSchema contract
    assert UserSchema is UserResponseSchema
    valid = UserCreateSchema.validate(
        {
            "name": "Alice Doe",
            "email": "alice@example.com",
            "password": "secret-password",
        }
    )
    assert valid.name == "Alice Doe"

    with pytest.raises(ValidationException) as exc:
        UserCreateSchema.validate(
            {
                "name": "A",
                "email": "alice@example.com",
                "password": "short",
            }
        )
    assert "name" in exc.value.errors
    assert "password" in exc.value.errors
