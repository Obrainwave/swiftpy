from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum, IntEnum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import pytest

from swiftpy.database.orm.fields import Field as OrmField
from swiftpy.database.orm.model import Model
from swiftpy.database.orm.relationships import HasMany
from swiftpy.http.response import json_response
from swiftpy.schemas import (
    BaseSchema,
    Field,
    Schema,
    SchemaField,
    field_validator,
    model_validator,
)
from swiftpy.validation.exceptions import ValidationException


class EggMovementType(str, Enum):
    COLLECTION = "COLLECTION"
    SPOILAGE = "SPOILAGE"


class PriorityLevel(IntEnum):
    LOW = 1
    HIGH = 2


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
    cracked_eggs: int = Field(0, ge=0)
    dirty_eggs: int = Field(0, ge=0)
    spoiled_eggs: int = Field(0, ge=0)
    reference: str | None = Field(None, max_length=100)
    notes: str | None = None


class _SampleLedgerModel(Model):
    __table__ = "sample_ledgers"
    __guarded__ = []

    id: UUID
    movement_date: date
    movement_type: str
    running_good_eggs_balance: int
    notes: str | None = None
    items = HasMany("MissingChild", foreign_key="ledger_id")


def test_schema_alias_and_requested_example_validation() -> None:
    assert Schema is BaseSchema
    assert SchemaField is Field

    flock = uuid4()
    req = EggSpoilageCreateRequest.validate(
        {
            "movement_date": "2026-11-25",
            "flock_id": str(flock),
            "spoiled_or_broken_eggs": "12",
            "reference": "REF-01",
        }
    )
    assert req.movement_date == date(2026, 11, 25)
    assert req.flock_id == flock
    assert req.spoiled_or_broken_eggs == 12
    assert req.reference == "REF-01"
    assert req.notes is None
    assert req.is_set("reference") is True
    assert req.is_set("notes") is False
    assert req.fields_set == frozenset(
        {"movement_date", "flock_id", "spoiled_or_broken_eggs", "reference"}
    )

    # Unset optional field omitted when exclude_unset=True
    assert req.to_dict(exclude_unset=True) == {
        "movement_date": date(2026, 11, 25),
        "flock_id": flock,
        "spoiled_or_broken_eggs": 12,
        "reference": "REF-01",
    }

    # Explicit None preserves field in fields_set
    req_cleared = EggSpoilageCreateRequest.validate(
        {
            "movement_date": date(2026, 11, 25),
            "spoiled_or_broken_eggs": 3,
            "notes": None,
        }
    )
    assert req_cleared.is_set("notes") is True
    assert "notes" in req_cleared.to_dict(exclude_unset=True)
    assert "notes" not in req_cleared.to_dict(exclude_none=True)


def test_schema_validation_errors_and_constraints() -> None:
    # Missing required fields + invalid gt=0 + max_length > 100
    with pytest.raises(ValidationException) as exc_info:
        EggSpoilageCreateRequest.validate(
            {
                "spoiled_or_broken_eggs": 0,
                "reference": "x" * 101,
            }
        )

    errors = exc_info.value.errors
    assert "movement_date" in errors
    assert "spoiled_or_broken_eggs" in errors
    assert "reference" in errors
    assert exc_info.value.status_code == 422

    # Bool must not coerce to int
    with pytest.raises(ValidationException) as bool_exc:
        EggCollectionCreateRequest.validate(
            {
                "movement_date": "2026-11-25",
                "good_eggs": True,
            }
        )
    assert "good_eggs" in bool_exc.value.errors

    # datetime must not silently pass for pure date
    with pytest.raises(ValidationException) as dt_exc:
        EggSpoilageCreateRequest.validate(
            {
                "movement_date": datetime(2026, 11, 25, 10, 0, tzinfo=UTC),
                "spoiled_or_broken_eggs": 1,
            }
        )
    assert "movement_date" in dt_exc.value.errors


def test_schema_rich_types_nested_schemas_and_validators() -> None:
    class AddressSchema(BaseSchema):
        city: str = Field(..., strip_whitespace=True, min_length=2)
        country: Literal["NG", "US", "GB"] = "NG"

    class LineItemSchema(BaseSchema):
        sku: str = Field(..., to_upper=True, pattern=r"^[A-Z0-9-]+$")
        qty: int = Field(..., ge=1, le=100)
        unit_price: Decimal = Field(..., gt=Decimal(0))

    class OrderPayloadSchema(BaseSchema):
        order_id: UUID = Field(default_factory=uuid4)
        created_at: datetime
        delivery_time: time | None = None
        movement_type: EggMovementType
        priority: PriorityLevel = PriorityLevel.LOW
        address: AddressSchema
        items: list[LineItemSchema] = Field(..., min_length=1)
        tags: set[str] = Field(default_factory=set)
        metadata: dict[str, int] = Field(default_factory=dict)
        internal_secret: str | None = Field(None, exclude=True)
        discount_code: Annotated[str | None, Field(None, strip_whitespace=True)] = None

        @field_validator("tags")
        @classmethod
        def normalize_tags(cls, value: set[str]) -> set[str]:
            return {t.strip().lower() for t in value if t.strip()}

        @model_validator
        def check_total(self) -> None:
            if self.priority == PriorityLevel.HIGH and len(self.items) < 1:
                raise ValueError("High priority requires items.")

    uid = uuid4()
    order = OrderPayloadSchema.validate(
        {
            "order_id": str(uid),
            "created_at": "2026-11-25T16:30:00Z",
            "delivery_time": "14:00:00",
            "movement_type": "COLLECTION",
            "priority": "2",
            "address": {"city": "  Lagos  ", "country": "NG"},
            "items": [{"sku": "egg-01", "qty": "2", "unit_price": "12.50"}],
            "tags": ["  Fresh ", "ORGANIC"],
            "metadata": {"crate_count": "4"},
            "internal_secret": "hidden-token",
            "discount_code": "  SAVE10  ",
        }
    )
    assert order.order_id == uid
    assert order.created_at.year == 2026
    assert order.delivery_time == time(14, 0, 0)
    assert order.movement_type is EggMovementType.COLLECTION
    assert order.priority is PriorityLevel.HIGH
    assert order.address.city == "Lagos"
    assert order.items[0].sku == "EGG-01"
    assert order.items[0].unit_price == Decimal("12.50")
    assert order.tags == {"fresh", "organic"}
    assert order.metadata == {"crate_count": 4}
    assert order.discount_code == "SAVE10"

    dumped = order.to_dict()
    assert "internal_secret" not in dumped
    assert dumped["address"] == {"city": "Lagos", "country": "NG"}
    assert dumped["items"] == [
        {"sku": "EGG-01", "qty": 2, "unit_price": Decimal("12.50")}
    ]

    # JSON response serialization uses to_dict() and encodes UUID/datetime/Decimal/Enum
    resp = json_response(order)
    assert b"hidden-token" not in resp.content
    assert b"EGG-01" in resp.content

    # Nested item error reporting includes dotted indices
    with pytest.raises(ValidationException) as nested_exc:
        OrderPayloadSchema.validate(
            {
                "created_at": "2026-11-25T16:30:00Z",
                "movement_type": "COLLECTION",
                "address": {"city": "A"},
                "items": [{"sku": "bad sku!", "qty": 0, "unit_price": -1}],
            }
        )
    assert "address.city" in nested_exc.value.errors
    assert "items.0.sku" in nested_exc.value.errors
    assert "items.0.qty" in nested_exc.value.errors
    assert "items.0.unit_price" in nested_exc.value.errors


def test_schema_aliases_extra_policies_and_validators() -> None:
    class AliasStrictSchema(BaseSchema):
        extra = "forbid"
        user_name: str = Field(..., alias="userName", min_length=2)
        even_step: float = Field(2.0, multiple_of=0.5)

        @classmethod
        @field_validator("user_name")
        def reject_reserved(cls, value: str) -> str:
            if value.lower() == "admin":
                raise ValueError("Reserved username.")
            return value

        @model_validator()
        def check_combo(self) -> None:
            if self.user_name == "forbidden_combo" and self.even_step > 1:
                raise ValueError("Invalid combination.")

    s = AliasStrictSchema.validate({"userName": "Alice", "even_step": "3.5"})
    assert s.user_name == "Alice"
    assert s.even_step == 3.5
    assert s.to_dict(by_alias=True) == {"userName": "Alice", "even_step": 3.5}

    with pytest.raises(ValidationException) as extra_err:
        AliasStrictSchema.validate({"userName": "Alice", "unknownField": 123})
    assert "unknownField" in extra_err.value.errors

    with pytest.raises(ValidationException) as mult_err:
        AliasStrictSchema.validate({"userName": "Alice", "even_step": 0.3})
    assert "even_step" in mult_err.value.errors

    with pytest.raises(ValidationException) as fv_err:
        AliasStrictSchema.validate({"userName": "admin"})
    assert fv_err.value.errors == {"user_name": ["Reserved username."]}

    with pytest.raises(ValidationException) as mv_err:
        AliasStrictSchema.validate({"userName": "forbidden_combo", "even_step": 2.0})
    assert mv_err.value.errors == {"__root__": ["Invalid combination."]}


def test_schema_from_model_and_inheritance_and_orm_field_compat() -> None:
    class BaseAuditSchema(BaseSchema):
        notes: str | None = None

    class LedgerResponseSchema(BaseAuditSchema):
        id: UUID
        movement_date: date
        movement_type: str
        running_good_eggs_balance: int
        extra_default: list[str] = OrmField(default_factory=list)

    uid = uuid4()
    entry = _SampleLedgerModel(
        id=uid,
        movement_date=date(2026, 11, 25),
        movement_type="COLLECTION",
        running_good_eggs_balance=420,
        notes="Morning batch",
    )

    dto = LedgerResponseSchema.from_model(entry)
    assert dto.id == uid
    assert dto.movement_date == date(2026, 11, 25)
    assert dto.movement_type == "COLLECTION"
    assert dto.running_good_eggs_balance == 420
    assert dto.notes == "Morning batch"
    assert dto.extra_default == []

    # Mutable default_factory creates independent lists
    dto2 = LedgerResponseSchema.from_model(entry)
    dto.extra_default.append("tag")
    assert dto2.extra_default == []


def test_schema_field_info_guards_and_stop_on_first_error() -> None:
    with pytest.raises(ValueError, match="Cannot specify both 'default' and 'default_factory'"):
        Field(1, default_factory=lambda: 2)
    with pytest.raises(ValueError, match="Cannot specify both 'gt' and 'ge'"):
        Field(..., gt=1, ge=1)
    with pytest.raises(ValueError, match="cannot exceed"):
        Field(..., min_length=5, max_length=2)
    with pytest.raises(ValueError, match="multiple_of"):
        Field(..., multiple_of=0)
    with pytest.raises(TypeError, match="must be int, float, or Decimal"):
        Field(..., gt=True)

    class FailFastSchema(BaseSchema):
        stop_on_first_error = True
        first_field: int
        second_field: int

    with pytest.raises(ValidationException) as fast_exc:
        FailFastSchema.validate({})
    assert fast_exc.value.is_single is True
    assert list(fast_exc.value.errors.keys()) == ["first_field"]


def test_decimal_float_bounds_and_non_numeric_constraint_errors() -> None:
    class PricingSchema(BaseSchema):
        decimal_rate: Decimal = Field(..., ge=0.05, le=1.50)
        float_limit: float = Field(..., ge=Decimal("0.05"), le=Decimal("1.50"))
        bad_date_bound: date | None = Field(default=None, gt=0)

    ok = PricingSchema.validate({"decimal_rate": "0.05", "float_limit": "1.50"})
    assert ok.decimal_rate == Decimal("0.05")
    assert ok.float_limit == 1.50

    with pytest.raises(ValidationException) as low_exc:
        PricingSchema.validate({"decimal_rate": "0.049", "float_limit": "0.049"})
    assert "decimal_rate" in low_exc.value.errors
    assert "float_limit" in low_exc.value.errors

    with pytest.raises(ValidationException) as date_exc:
        PricingSchema.validate(
            {
                "decimal_rate": "0.50",
                "float_limit": "0.50",
                "bad_date_bound": "2026-11-25",
            }
        )
    assert date_exc.value.errors["bad_date_bound"] == [
        "The bad_date_bound field must be numeric."
    ]


def test_collection_annotated_constraints_init_modes_and_setattr() -> None:
    class BatchSchema(BaseSchema):
        scores: list[Annotated[int, Field(gt=0)]]
        labels: list[Annotated[str, Field(strip_whitespace=True, min_length=2)]]
        codes: set[Annotated[str, Field(to_upper=True)]] = Field(default_factory=set)
        counts: dict[str, Annotated[int, Field(ge=1)]] = Field(default_factory=dict)

    valid = BatchSchema.validate(
        {
            "scores": ["1", 5],
            "labels": ["  ok  ", "valid"],
            "codes": ["abc", "def"],
            "counts": {"box": "2"},
        }
    )
    assert valid.scores == [1, 5]
    assert valid.labels == ["ok", "valid"]
    assert valid.codes == {"ABC", "DEF"}
    assert valid.counts == {"box": 2}

    with pytest.raises(ValidationException) as item_exc:
        BatchSchema.validate(
            {
                "scores": [1, 0],
                "labels": [" a "],
                "counts": {"bad": 0},
            }
        )
    assert "scores.1" in item_exc.value.errors
    assert "labels.0" in item_exc.value.errors
    assert "counts.bad" in item_exc.value.errors

    # Single-mapping and positional constructor compatibility
    class PointSchema(BaseSchema):
        x: int
        label: str = "origin"

    build_point: Callable[..., PointSchema] = PointSchema
    from_map = build_point({"x": "7", "label": "p1"})
    assert from_map.x == 7
    assert from_map.label == "p1"
    from_pos = build_point(3, "p2")
    assert from_pos.x == 3
    assert from_pos.label == "p2"

    with pytest.raises(TypeError, match="positional"):
        build_point(1, "p2", "extra")
    with pytest.raises(TypeError, match="multiple values"):
        build_point(1, x=2)
    with pytest.raises(TypeError, match="mapping keys must be str"):
        build_point({1: "bad"})

    # Post-validation mutation tracks __fields_set__ cleanly
    class PatchSchema(BaseSchema):
        slug: str | None = None
        description: str | None = None

    patch = PatchSchema.validate({"slug": "v0-3-7"})
    assert patch.to_dict(exclude_unset=True) == {"slug": "v0-3-7"}
    patch.description = "Updated release notes"
    assert patch.is_set("description") is True
    assert patch.to_dict(exclude_unset=True) == {
        "slug": "v0-3-7",
        "description": "Updated release notes",
    }


def test_extra_allow_blocks_private_keys_and_method_shadowing() -> None:
    class FlexibleSchema(BaseSchema):
        extra = "allow"
        name: str

    s = FlexibleSchema.validate(
        {"name": "Ada", "to_dict": "shadow-attempt", "validate": False, "tag": "vip"}
    )
    assert callable(s.to_dict)
    assert callable(s.validate)
    assert s.__dict__["tag"] == "vip"
    assert "to_dict" not in s.__dict__
    assert "validate" not in s.__dict__
    dumped: dict[str, Any] = s.to_dict()
    assert dumped == {
        "name": "Ada",
        "to_dict": "shadow-attempt",
        "validate": False,
        "tag": "vip",
    }

    with pytest.raises(ValidationException) as priv_exc:
        FlexibleSchema.validate({"name": "Ada", "__fields_set__": ["hijack"]})
    assert "__fields_set__" in priv_exc.value.errors

    with pytest.raises(ValidationException) as under_exc:
        FlexibleSchema.validate({"name": "Ada", "_secret": "123"})
    assert "_secret" in under_exc.value.errors
