# Application Schemas & DTOs (`app/schemas/`)

## Introduction

In a SwiftPY project, `app/schemas/` holds your **application-layer Data Transfer Objects (DTOs) and API serialization contracts**—classes that define what shape goes *into* your service layer and what shape comes *out* in JSON responses.

Because SwiftPY also has **Database `Schema`/`Blueprint`** for PostgreSQL tables and **validation rule dicts** for request validation, keeping the three roles clear prevents common confusion:

| Layer | Location / Symbol | Responsibility |
|---|---|---|
| **Application Schemas / DTOs** | `app/schemas/` (`*Schema`) | Typed dataclasses & output serializers mapping validated input -> domain services -> JSON responses |
| **Validation Rules & `FormRequest`** | `app/http/requests/` or inline `validate(request, rules)` | Parses, coerces, and validates untrusted JSON/form/query payloads (returns `422` on failure) |
| **Database Schema** | `swiftpy.database.migrations.Schema` & `Blueprint` in `database/migrations/` | Creates and alters PostgreSQL tables, columns, indexes, and foreign keys |

---

## Generating & Inspecting Files (`swiftpy make:schema`)

When you scaffold a new project (`swiftpy new my_api`), SwiftPY creates [user_schema.py](../app/schemas/user_schema.py):

```python
# app/schemas/user_schema.py
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class UserCreateSchema:
    """Input validation DTO for user creation requests."""

    name: str
    email: str
    password: str


@dataclass(slots=True)
class UserResponseSchema:
    """Output serialization schema for user responses."""

    id: int
    name: str
    email: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

Create additional schema files in `app/schemas/` with the CLI:

```bash
swiftpy make:schema OrderSchema
# Creates app/schemas/order_schema.py

# Or organize in subdirectories under app/schemas/
swiftpy make:schema billing/InvoiceSchema
# Creates app/schemas/billing/invoice_schema.py
```

```python
# app/schemas/order_schema.py
from __future__ import annotations

from typing import Any


class OrderSchema:
    """OrderSchema data schema."""

    schema: dict[str, Any] = {
        # "id": int,
        # "name": str,
    }
```

From that starting point, you can either keep reusable validation rule dictionaries (`schema` / `RULES`) alongside your schema class or define `@dataclass(slots=True)` DTOs following `app/schemas/user_schema.py`.

---

## Step-by-Step Usage Patterns

### 1. Output Serialization Schemas (`to_dict()` & `json_response()`)

SwiftPY's JSON encoder (`encode_json()` inside `json_response()`, `created()`, and `dict`/`list` auto-JSON) automatically checks `hasattr(obj, "to_dict") and callable(obj.to_dict)`—and automatically formats nested `datetime`, `UUID`, `Decimal`, and `Enum` values as ISO strings / primitives while omitting any `__hidden__` fields.

Add a `@classmethod def from_model(cls, user: User)` factory plus `to_dict()` on your response schema so routes and services never expose internal ORM columns (`password`, internal flags) or repeat mapping code:

```python
# app/schemas/user_schema.py
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, ClassVar

from app.models.user import User


@dataclass(slots=True)
class UserCreateSchema:
    """Typed input DTO passed into services."""

    RULES: ClassVar[dict[str, str]] = {
        "name": "required|str|min:2|max:100",
        "email": "required|email:lowercase|max:255",
        "password": "required|str|min:8",
    }

    name: str
    email: str
    password: str


@dataclass(slots=True)
class UserResponseSchema:
    """Typed public response schema with automatic JSON serialization."""

    id: int
    name: str
    email: str
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, user: User) -> UserResponseSchema:
        return cls(
            id=int(user.id),
            name=str(user.name),
            email=str(user.email),
            created_at=getattr(user, "created_at", None),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

Returning instances directly in `routes/api.py`:

```python
# routes/api.py
from swiftpy.http.response import Response, created, json_response
from swiftpy.http.router import Router

from app.models.user import User
from app.schemas.user_schema import UserResponseSchema

router = Router(prefix="/api")


@router.get("/users")
async def list_users() -> Response:
    users = await User.all()
    payload = [UserResponseSchema.from_model(u) for u in users]
    return json_response(payload)  # Calls .to_dict() and encodes datetimes automatically
```

### 2. Pairing Input DTOs with `FormRequest` or `validate()`

SwiftPY keeps HTTP input validation explicit in **`FormRequest`** or **`await validate(request, ...)`**, then lets you build clean typed DTOs from the validated output:

#### With `validate(request, Schema.RULES)`

```python
from swiftpy import Request, Response, validate
from swiftpy.http.response import created
from swiftpy.support.hashing import Hash
from swiftpy.validation import ValidationException

from app.schemas.user_schema import UserCreateSchema, UserResponseSchema
from app.services.user_service import UserService


@router.post("/users")
async def create_user(request: Request, users: UserService) -> Response:
    try:
        validated = (
            await validate(request, UserCreateSchema.RULES)
        ).raise_if_failed().validated()
    except ValidationException as exc:
        raise exc.single()  # Or omit try/except to return all field errors

    dto = UserCreateSchema(
        name=validated.name,
        email=validated.email,
        password=await Hash.make_async(validated.password),
    )
    user = await users.create_user(
        name=dto.name,
        email=dto.email,
        password_hash=dto.password,
    )
    return created(UserResponseSchema.from_model(user))
```

#### With `FormRequest` (`CreateUserRequest`)

You can also share a schema dictionary or hydrate a DTO directly from `FormRequest.validated()`:

```python
# app/http/requests/create_user_request.py
from swiftpy.validation import FormRequest

from app.schemas.user_schema import UserCreateSchema


class CreateUserRequest(FormRequest):
    schema = UserCreateSchema.RULES

    def to_dto(self) -> UserCreateSchema:
        data = self.validated()
        return UserCreateSchema(
            name=data.name,
            email=data.email,
            password=data.password,
        )
```

Then route handlers stay tiny and strongly typed:

```python
@router.post("/users")
async def store_user(form: CreateUserRequest, users: UserService) -> Response:
    dto = form.to_dto()
    password_hash = await Hash.make_async(dto.password)
    user = await users.create_user(dto.name, dto.email, password_hash)
    return created(UserResponseSchema.from_model(user))
```

### 3. Using `make:schema`'s `.schema` Dict with `Validator`

If you use the default `OrderSchema.schema` dictionary scaffolded by `swiftpy make:schema OrderSchema`, populate it with SwiftPY validation rule strings and pass it straight to `validate()` or `FormRequest.schema`:

```python
# app/schemas/order_schema.py
from __future__ import annotations


class OrderSchema:
    schema = {
        "customer_email": "required|email:lowercase",
        "items": "required",
        "items.*.sku": "required|str|min:3",
        "items.*.qty": "required|int|min:1",
    }
```

```python
data = (await validate(request, OrderSchema.schema)).raise_if_failed().validated()
```

---

## `app/schemas/` vs. `app/http/resources/` (`make:resource`)

Both `make:schema` and `make:resource` help structure API responses:

- **`app/schemas/` (`UserResponseSchema`, `OrderCreateSchema`):** Framework-agnostic typed `@dataclass(slots=True)` DTOs ideal when you want strict `mypy` field checking, shared input/output contracts between routes and `app/services/`, and reusable `RULES` dicts.
- **`app/http/resources/` (`UserResource`):** HTTP-layer transformer wrappers (`UserResource(model).to_dict()`, `UserResource.collection(models)`) geared toward transforming ORM models directly for HTTP responses.

Pick one convention per module or combine input DTOs in `app/schemas/` with resources in `app/http/resources/` for complex response shaping.

---

## Common Pitfalls

> [!IMPORTANT]
> **1. Do not annotate route handler parameters with dataclass schemas directly:** SwiftPY's `Router` only injects `Request`, `FormRequest` subclasses, `UploadedFile`, URL path parameters, and DI container services. Writing `async def store(body: UserCreateSchema)` causes the DI container to try constructing `UserCreateSchema()` with missing constructor arguments (`RouteRegistrationError` / `ResolutionError`). Always extract/validate via `FormRequest` or `await validate(request, ...)` first.

> [!IMPORTANT]
> **2. Always define `to_dict()` on `@dataclass` response schemas:** SwiftPY's `encode_json()` converts objects via `.to_dict()`, not `dataclasses.is_dataclass()`. Passing a raw `@dataclass` instance without a `to_dict()` method to `json_response()` or `created()` raises `TypeError: Object of type ... is not JSON serializable`.

---

## Next Step

Continue to [Database Pool, Query Builder & Transactions](2026-11-15-database-query-builder-and-migrations.md) (or jump straight to [Database Schema, Blueprint & Migrations](2026-11-19-database-schema-and-blueprint.md)).
