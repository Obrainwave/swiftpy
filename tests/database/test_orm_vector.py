from __future__ import annotations

from typing import ClassVar

import pytest

from swiftpy.database.migrations.blueprint import Blueprint
from swiftpy.database.migrations.schema import SchemaGrammar
from swiftpy.database.orm import Field, Model, Vector
from swiftpy.database.orm.fields import VectorFieldInfo


class DocumentModel(Model):
    __table__ = "documents"
    __guarded__: ClassVar[list[str]] = []
    id: int = Field(primary_key=True)
    title: str
    embedding: list[float] = Vector(4, metric="cosine")


def test_model_vector_metadata() -> None:
    """Verify that Vector field registers VectorFieldInfo and __vector_fields__."""
    assert "embedding" in DocumentModel.__vector_fields__
    assert DocumentModel.__vector_fields__["embedding"].dimensions == 4
    assert DocumentModel.__vector_fields__["embedding"].metric == "cosine"
    assert isinstance(DocumentModel.__dict__["embedding"], VectorFieldInfo)


def test_migration_blueprint_vector_compilation() -> None:
    """Verify that Blueprint.vector() compiles into PostgreSQL pgvector DDL."""
    bp = Blueprint("documents")
    bp.id()
    bp.string("title")
    bp.vector("embedding", 1536)

    sql_statements = SchemaGrammar.compile_create(bp)
    create_sql = sql_statements[0]

    assert 'CREATE TABLE "documents"' in create_sql
    assert '"embedding" vector(1536) NOT NULL' in create_sql


def test_model_vector_validation_on_instantiation_and_fill() -> None:
    """Verify dimensional validation and NaN rejection on Model.__init__ and fill."""
    # Valid instantiation
    doc = DocumentModel(id=1, title="Test", embedding=[0.1, 0.2, 0.3, 0.4])
    assert doc.embedding == [0.1, 0.2, 0.3, 0.4]

    # Dimension mismatch
    with pytest.raises(ValueError, match="vector dimension 2 does not match expected 4"):
        DocumentModel(id=2, title="Bad", embedding=[0.1, 0.2])

    # NaN rejection
    with pytest.raises(ValueError, match="NaN or Infinite"):
        DocumentModel(id=3, title="Bad", embedding=[0.1, float("nan"), 0.3, 0.4])


def test_model_vector_hydration_coercion() -> None:
    """Verify that _coerce_db_value coerces pgvector string output to typed float list."""
    # String from database: "[0.1, 0.2, 0.3, 0.4]"
    coerced = DocumentModel._coerce_db_value("embedding", "[0.1, 0.2, 0.3, 0.4]")
    assert coerced == [0.1, 0.2, 0.3, 0.4]
    assert all(isinstance(x, float) for x in coerced)

    # Already a list
    coerced_list = DocumentModel._coerce_db_value("embedding", [0.1, 0.2, 0.3, 0.4])
    assert coerced_list == [0.1, 0.2, 0.3, 0.4]

    # Serialization for SQL query binding
    persisted = DocumentModel._persistable([0.1, 0.2, 0.3, 0.4])
    assert persisted == "[0.1,0.2,0.3,0.4]"


def test_model_vector_dirty_tracking() -> None:
    """Verify that modifying a vector field marks the model as dirty."""
    doc = DocumentModel(id=1, title="Test", embedding=[0.1, 0.2, 0.3, 0.4])
    doc._mark_persisted(["id", "title", "embedding"])
    assert not doc.is_dirty()

    doc.embedding = [0.5, 0.6, 0.7, 0.8]
    assert doc.is_dirty()
    assert doc.is_dirty("embedding")
    assert doc.get_dirty()["embedding"] == [0.5, 0.6, 0.7, 0.8]
