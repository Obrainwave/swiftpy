# Application Schemas & Declarative DTOs (`swiftpy.schemas` & `app/schemas/`)

## Introduction

In SwiftPY, **`swiftpy.schemas`** provides native, zero-dependency declarative data schemas (**`BaseSchema`** / **`Schema`**) with automatic runtime type coercion, **`Field(...)`** constraint validation, custom **`@field_validator`** / **`@model_validator`** hooks, HTTP route/controller auto-injection, response JSON serialization, and ORM `Model` interoperability.

With **`@dataclass_transform`**, both **Pyrefly**, **Mypy (`--strict`)**, and **Pyright** understand every schema field and attribute statically—so controllers, services, and repositories get full autocomplete and type checking without third-party dependencies.

| Concern | Recommended Tool | Purpose |
|---|---|---|
| **Typed API Request/Response DTOs** | `app/schemas/` (`BaseSchema` / `Schema`) | Strongly typed input coercion & constraint validation, HTTP handler auto-injection, `from_model()`, `to_dict()`, and direct `Model.create()` / `model.update()` |
| **Rule-String / DB Validation** | `app/http/requests/` (`FormRequest`) or `validate()` | String/rule-object DSL including async `"unique:table,col"` and `"exists:table,col"` database checks |
| **HTTP Response Transformers** | `app/http/resources/` (`*Resource`) | Tailored HTTP JSON formatting adapters wrapping ORM models or schemas |

> **Naming Note:** `swiftpy.schemas.Schema` (alias of `BaseSchema`) and `swiftpy.schemas.Field` are for application DTOs, whereas `swiftpy.database.Schema` (migration DDL builder) and `swiftpy.database.orm.Field` (ORM column descriptors) belong to the database layer. Always import `Schema` / `BaseSchema` / `Field` from `swiftpy.schemas` inside `app/schemas/`.

---

## Generating & Inspecting Files (`swiftpy make:schema`)

When you scaffold a new project (`swiftpy new my_api`), SwiftPY creates [user_schema.py](../app/schemas/user_schema.py):

```python
# app/schemas/user_schema.py
from __future__ import annotations

from swiftpy.schemas import BaseSchema, Field


class UserCreateSchema(BaseSchema):
    """Input validation schema for user creation requests."""

    name: str = Field(..., min_length=2, max_length=100)
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=8)


class UserResponseSchema(BaseSchema):
    """Output serialization schema for user responses."""

    id: int
    name: str
    email: str


UserSchema = UserResponseSchema
```

Create additional schema files in `app/schemas/` with the CLI:

```bash
uv run swiftpy make:schema OrderSchema
# Creates app/schemas/order_schema.py

# Or organize in subdirectories under app/schemas/
uv run swiftpy make:schema billing/InvoiceSchema
# Creates app/schemas/billing/invoice_schema.py
```

The generated file subclasses `BaseSchema` cleanly:

```python
# app/schemas/order_schema.py
from __future__ import annotations

from swiftpy.schemas import BaseSchema


class OrderSchema(BaseSchema):
    """OrderSchema structured data schema."""

    # Example with constraints (`from swiftpy.schemas import BaseSchema, Field`):
    # id: int
    # name: str = Field(..., min_length=1, max_length=100)
```

---

## Defining Declarative Schemas (`BaseSchema` / `Schema` & `Field`)

Import `BaseSchema` (or its alias `Schema`) and `Field` (or alias `SchemaField`) from `swiftpy.schemas`:

```python
from datetime import date
from decimal import Decimal
from uuid import UUID

from swiftpy.schemas import BaseSchema, Field, Schema


class EggSpoilageCreateRequest(BaseSchema):
    movement_date: date
    flock_id: UUID | None = None
    spoiled_or_broken_eggs: int = Field(..., gt=0)
    reference: str | None = Field(default=None, max_length=100)
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
```

### Supported Types & Coercion

`BaseSchema` automatically coerces inbound JSON, URL-encoded form, and multipart values to your declared type annotations:

- **Scalars:** `str`, `int` (rejecting `bool`), `float` (rejecting `bool`, `NaN`, and `Infinity`), `bool` (`"true"`, `"1"`, `"yes"`, `"on"` / `"false"`, `"0"`, `"no"`, `"off"`), `Decimal` (rejecting `bool`, `NaN`, and `Infinity`), `bytes`
- **Dates & Identifiers:** `UUID`, ISO-8601 `date`, `datetime` (normalizing trailing `"Z"`), and `time`
- **Enums & Literals:** `Enum`, `IntEnum`, and `Literal[...]`
- **Containers & Nested Schemas:** `list[T]`, `Sequence[T]`, `set[T]`, `tuple[...]`, `dict[K, V]`, `Mapping[K, V]`, `T | None`, `Union[...]`, `Annotated[T, Field(...)]`, and nested `BaseSchema` subclasses (including dotted error keys such as `items.0.qty`)

### `Field(...)` Constraints & Options

| Argument | Description |
|---|---|
| `default` / `...` | Required when `...` (default); pass a default value (prefer `default=...` keyword when calling the constructor directly under `mypy --strict`) |
| `default_factory` | Zero-arg callable invoked when the field is omitted (for example, `default_factory=uuid4` or `default_factory=list`) |
| `gt`, `ge`, `lt`, `le` | Numeric comparisons (`int`, `float`, `Decimal`) |
| `multiple_of` | Enforces `value % multiple_of == 0` |
| `min_length`, `max_length` | Length checks for `str`, `bytes`, `list`, `tuple`, `set`, `dict` |
| `pattern` | Regex `str` or compiled `re.Pattern[str]` matched with `search()` |
| `strip_whitespace`, `to_lower`, `to_upper` | String normalization applied before length, regex, and custom validators |
| `alias` | Alternate inbound key name accepted during validation and emitted by `to_dict(by_alias=True)` |
| `exclude` | Omit field from `to_dict()` and JSON responses when `True` |

### Class Options & Custom Validators

```python
from swiftpy.schemas import BaseSchema, Field, field_validator, model_validator


class OrderCreateSchema(BaseSchema):
    extra = "forbid"  # "ignore" (default), "forbid", or "allow"
    stop_on_first_error = False  # True emits {"message": ..., "field": ...} on 422

    sku: str = Field(..., to_upper=True, min_length=3)
    quantity: int = Field(..., gt=0, le=100)
    discount_code: str | None = Field(default=None, strip_whitespace=True)

    @field_validator("discount_code")
    @classmethod
    def normalize_code(cls, value: str | None) -> str | None:
        if value == "EXPIRED":
            raise ValueError("Discount code has expired.")
        return value

    @model_validator
    def check_bulk_order(self) -> None:
        if self.quantity < 10 and self.discount_code == "BULK":
            raise ValueError("BULK discount requires at least 10 items.")
```

---

## Automatic Route & Controller Injection

Annotate a `BaseSchema` / `Schema` parameter on any route function or controller method. SwiftPY automatically parses the JSON, `application/x-www-form-urlencoded`, or `multipart/form-data` request body, coerces and validates it, and raises `ValidationException` (HTTP `422`) before your handler runs:

```python
# app/http/controllers/egg_collection_controller.py
from uuid import UUID

from swiftpy.http.response import Response, created
from app.schemas.egg_schema import EggCollectionCreateRequest, EggCollectionResponse
from app.services.egg_ledger_service import EggLedgerService


class EggCollectionController:
    def __init__(self, service: EggLedgerService) -> None:
        self.service = service

    async def store(
        self,
        organization_id: UUID,
        payload: EggCollectionCreateRequest,
    ) -> Response:
        entry = await self.service.record_collection(organization_id, payload)
        return created(EggCollectionResponse.from_model(entry))
```

Route handlers may also return a `BaseSchema` instance or `list[BaseSchema]` directly—SwiftPY's ASGI pipeline automatically wraps and encodes it into an `application/json` `Response` via `to_dict()`.

---

## Services, Repositories & ORM `Model` Interoperability

Because `BaseSchema` instances carry statically typed attributes (`payload.movement_date`, `payload.good_eggs`, `payload.flock_id`), you can pass the validated schema directly from controllers into services and repositories:

```python
import uuid
from app.models.egg_stock_ledger import EggStockLedger
from app.schemas.egg_schema import EggCollectionCreateRequest


class EggLedgerRepository:
    model = EggStockLedger

    async def record_collection(
        self,
        organization_id: uuid.UUID,
        payload: EggCollectionCreateRequest,
    ) -> EggStockLedger:
        ref = payload.reference or f"EGG-COL-{payload.movement_date.strftime('%Y%m%d')}"
        return await self.model.create(
            payload,
            id=uuid.uuid4(),
            organization_id=organization_id,
            movement_type="COLLECTION",
            reference=ref,
        )

    async def update_notes(
        self, entry_id: uuid.UUID, patch: EggCollectionPatchRequest
    ) -> EggStockLedger | None:
        entry = await self.model.find(entry_id)
        if entry is None:
            return None
        return await entry.update(patch)
```

### Key ORM & Serialization Helpers

- **`Schema.from_model(model)`:** Hydrates a response schema from an ORM `Model` instance, dict, or plain object (respecting loaded relations without triggering lazy loads on unloaded relationships).
- **`FormRequest[YourSchema]` & `request.validate(YourSchema)`:** Parameterize `class CreateItemRequest(FormRequest[YourSchema]):` (or pass `schema_cls=YourSchema` to `request.validate(rules, schema_cls=YourSchema)`, or call `request.validated(YourSchema)` / `request.to_schema(YourSchema)`) when combining `FormRequest` database rules (`unique:...`, `exists:...`) with typed `BaseSchema` service DTOs.
- **`payload.to_dict(exclude_none=False, exclude_unset=False, by_alias=False)`:** Recursively converts schemas into dictionaries (also supports `payload["field"]`, `payload.get("field")`, `"field" in payload`, and `payload.validated()` for `ValidatedData` interoperability).
- **`payload.is_set("field_name")` / `payload.fields_set`:** Inspects which fields were explicitly provided in the input payload (versus defaulted).
- **`await Model.create(payload, ...)` / `await Model.force_create(payload, ...)` / `await parent.children.create(payload, ...)`:** Extracts `payload.to_dict()` and merges any extra keyword overrides while enforcing `__fillable__` / `__guarded__` on `create()`.
- **`await model.update(payload)` / `await model.force_update(payload)` / `await Model.where(...).update(payload)` / `await parent.children.update(payload)`:** Extracts `payload.to_dict(exclude_unset=True)` so partial `PATCH` schemas never overwrite omitted columns with defaults (post-validation field assignments also update `fields_set`).
- **`await Model.first_or_create(attrs, payload)` / `await Model.update_or_create(attrs, payload)`:** Accepts `BaseSchema` payloads alongside covariant `Mapping[str, Any]` dictionaries (`to_dict()` when creating, `to_dict(exclude_unset=True)` when updating an existing row).
- **`await Auth.attempt(payload)` / `Auth.attempt_when(payload, fn)` / `Auth.once(payload)`:** Accepts validated `BaseSchema` login DTOs directly (`to_dict(exclude_unset=True)`).
- **`isset(payload, "field_or_alias")`:** Resolves canonical fields, `Field(alias=...)` names, nested schemas (`"meta.region"`), and `extra = "allow"` attributes safely.

---

## `app/schemas/` vs. `app/http/resources/` (`make:resource`)

Both `make:schema` and `make:resource` help structure API payloads:

- **`app/schemas/` (`BaseSchema` / `Schema`):** Declarative inbound validation and outbound response DTOs shared cleanly across HTTP handlers, services, repositories, and ORM models with full Pyrefly/Mypy typing.
- **`app/http/resources/` (`UserResource`):** HTTP-layer presentation transformers (`UserResource(model_or_schema).to_dict()`, `UserResource.collection(items)`) for custom envelope formatting or computed API fields.

SwiftPY's starter application wires these layers cleanly via **`EggController` / `UserController -> UserService -> UserRepository -> UserResponseSchema`**. See [Application Architecture & Project Structure](2026-11-21-application-architecture-and-project-structure.md) for architectural boundaries across layers.

---

## Important Notes & Best Practices

> **1. Plain `@dataclass` types vs. `BaseSchema` on routes:** Route parameter auto-validation and JSON body parsing apply only to `BaseSchema` / `Schema` subclasses and `FormRequest` subclasses. A plain Python `@dataclass` without a container binding will not parse request bodies and will fail route registration (`RouteRegistrationError`) if its constructor takes required arguments.

> **2. Keyword `default=` when instantiating schemas directly under `mypy --strict`:** In Python's `mypy --strict` plugin for `@dataclass_transform`, passing an optional default via keyword (`Field(default=None, max_length=100)`) allows mypy to recognize that field as optional when constructing `MySchema(...)` directly in Python code, whereas positional `Field(None, max_length=100)` works at runtime, via `MySchema.validate({...})`, via HTTP route injection, and in PEP 681 checkers such as Pyrefly and Pyright.

> **3. Keep ORM imports vs. schema imports distinct:** Import `BaseSchema`, `Schema`, `Field`, `field_validator`, and `model_validator` from `swiftpy.schemas`; import ORM `Model` and column `Field` from `swiftpy.database.orm`.

---

## Next Step

Continue to [Database Pool, Query Builder & Transactions](2026-11-15-database-query-builder-and-migrations.md) (or jump straight to [Database Schema, Blueprint & Migrations](2026-11-19-database-schema-and-blueprint.md)).
