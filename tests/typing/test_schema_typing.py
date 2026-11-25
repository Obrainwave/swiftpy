from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from swiftpy.schemas import BaseSchema, Field, Schema


class EggSpoilageCreateRequest(BaseSchema):
    movement_date: date
    flock_id: UUID | None = None
    spoiled_or_broken_eggs: int = Field(..., gt=0)
    reference: str | None = Field(None, max_length=100)
    notes: str | None = None


class EggCollectionCreateRequest(Schema):
    movement_date: date
    flock_id: UUID | None = None
    good_eggs: int = Field(..., ge=0)
    cracked_eggs: int = Field(default=0, ge=0)
    dirty_eggs: int = Field(default=0, ge=0)
    spoiled_eggs: int = Field(default=0, ge=0)
    reference: str | None = Field(default=None, max_length=100)
    notes: str | None = None
    unit_weight: Decimal | None = None


def test_schema_dataclass_transform_typing() -> None:
    """Verify strict mypy resolves Field(...) and typed attributes via @dataclass_transform."""
    flock = uuid4()
    spoilage = EggSpoilageCreateRequest(
        movement_date=date(2026, 11, 25),
        flock_id=flock,
        spoiled_or_broken_eggs=5,
        reference="SPOIL-1",
    )
    dt: date = spoilage.movement_date
    count: int = spoilage.spoiled_or_broken_eggs
    ref: str | None = spoilage.reference
    fid: UUID | None = spoilage.flock_id
    notes: str | None = spoilage.notes
    assert dt == date(2026, 11, 25)
    assert count == 5
    assert ref == "SPOIL-1"
    assert fid == flock
    assert notes is None

    validated_spoilage = EggSpoilageCreateRequest.validate(
        {
            "movement_date": "2026-11-25",
            "spoiled_or_broken_eggs": 4,
        }
    )
    assert validated_spoilage.reference is None

    collection = EggCollectionCreateRequest(
        movement_date=date(2026, 11, 25),
        good_eggs=120,
    )
    good: int = collection.good_eggs
    cracked: int = collection.cracked_eggs
    assert good == 120
    assert cracked == 0
