# Validation DSL & Form Requests

SwiftPY provides an async-first validation engine for request payloads, form submissions, and arbitrary dictionaries. You can validate input automatically before a route handler runs using typed **`FormRequest`** classes, or validate data anywhere in your application using **`validate()`** and **`Validator`**.

Valid payloads are sanitized, type-coerced (for example, `"42"` -> `42` with `int` or normalized email domains), and restricted to declared fields to prevent mass assignment. Invalid payloads automatically produce a structured HTTP `422` JSON response (`{"errors": {...}}`).

Quick imports from `swiftpy.validation` (or top-level `swiftpy`):

```python
from swiftpy.validation import (
    FormRequest,
    ValidationException,
    ValidationResult,
    Validator,
    ValidatorRule,
    email,
    exists,
    int_,
    min_,
    nullable,
    register_rule,
    required,
    str_,
    unique,
    validate,
)
```

---

## Creating & Organizing `FormRequest` Classes

### Generating a Request with the CLI

Use `swiftpy make:request` to generate a new `FormRequest` subclass inside `app/http/requests/`:

```bash
swiftpy make:request CreateOrderRequest
```

Optional flags:

- `--path <dir>` — write to a custom directory instead of `app/http/requests/`.
- `--force` — overwrite an existing file at the target path.

A fresh project created with `swiftpy new` already includes `app/http/requests/create_user_request.py`:

```python
# app/http/requests/create_user_request.py
from __future__ import annotations

from swiftpy.validation.form_request import FormRequest


class CreateUserRequest(FormRequest):
    """
    FormRequest validating inbound user registration payloads.
    Encapsulates field rules, sanitization, and custom error messages.
    """

    def rules(self) -> dict[str, str]:
        return {
            "name": "required|string|min:2|max:100",
            "email": "required|email|max:255",
            "password": "required|string|min:8",
        }
```

### Static `schema` vs. Dynamic `rules()`

`FormRequest` supports two ways to declare rules:

1. **Class-level `schema` (cached once per rule registry):** Use `schema: ClassVar[dict[str, Any]]` when the rules do not depend on the incoming `Request`:

   ```python
   from __future__ import annotations

   from typing import Any, ClassVar

   from swiftpy.validation import FormRequest


   class LoginRequest(FormRequest):
       schema: ClassVar[dict[str, Any]] = {
           "email": "required|email:lowercase",
           "password": "required|str|min:8",
           "remember": "bool",
       }
   ```

2. **Dynamic `rules()` method (evaluated per request):** Override `def rules(self) -> dict[str, Any]:` whenever validation depends on `self.request` (such as `POST` vs. `PATCH`), route state, or injected services. SwiftPY evaluates overridden `rules()` per request rather than freezing rules on the class:

   ```python
   from __future__ import annotations

   from typing import Any

   from swiftpy.validation import FormRequest


   class ArticleRequest(FormRequest):
       def rules(self) -> dict[str, Any]:
           if self.request is not None and self.request.method == "PATCH":
               return {
                   "title": "str|min:3|max:150",
                   "body": "str|min:10",
               }
           return {
               "title": "required|str|min:3|max:150",
               "body": "required|str|min:10",
           }
   ```

> [!NOTE]
> `FormRequest` focuses strictly on payload validation, coercion, and custom error messages (it does **not** define an `authorize()` hook). Handle authentication and role checks in route or group middleware such as `JWTGuard`, `AuthMiddleware`, or `AdminMiddleware`.

---

## Using `FormRequest` in Routes

Declare any `FormRequest` subclass as a typed parameter on a route handler. You can combine it with path parameters, DI services, and an optional `request: Request` parameter:

```python
# routes/users.py
from __future__ import annotations

from swiftpy.http.response import Response, json_response
from swiftpy.http.router import Router

from app.http.requests.create_user_request import CreateUserRequest
from app.services.user_service import UserService

router = Router()


@router.post("/users")
async def create_user(
    form: CreateUserRequest,
    users: UserService,
) -> Response:
    clean = form.validated()
    user = await users.register(clean)
    return json_response({"id": user["id"], "email": form.email}, status_code=201)
```

Before your handler runs, SwiftPY inspects `Content-Type` and parses the request body automatically:

- **`application/json`** (or when no form media type is specified): requires a non-empty, valid JSON **object** (`dict`). Empty bodies, malformed JSON, or non-object JSON payloads (`[]`, `"text"`, `123`, `true`, `null`) fail immediately with HTTP `422` and `{"errors": {"body": [...]}}` — your handler is never called.
- **`application/x-www-form-urlencoded`**: parses URL-encoded fields via `await request.form()`. Single field values stay scalar `str`; repeated keys become `list[str]`. Empty bodies return `422`.
- **`multipart/form-data`**: parses both form fields and uploaded files (`UploadedFile` / `list[UploadedFile]`; form fields take precedence if a field and file share the same key). Completely empty multipart payloads return `422`.

### Mass-Assignment Protection & `ValidatedData` Accessors

If a client submits extra unvalidated keys such as `{"name": "Jane", "is_admin": true, "role": "superuser"}`, only fields declared in your rules appear in validated data:

- `form.validated()` — returns a `ValidatedData` (`dict[str, Any]` subclass) copy of **validated and coerced fields only** (never includes `"is_admin"` or `"role"` unless declared in your rules). Because `ValidatedData` subclasses `dict`, it works seamlessly with `**data`, `data["email"]`, `data.get(...)`, and nested dot access like `data.profile.handle` (keys that match `dict` method names such as `"items"` still resolve via bracket lookup `data["items"]`).
- `form.all()` — returns a `dict[str, Any]` copy of all raw parsed input fields.
- `form.get(key, default=None)` / `form[key]` / `key in form` — reads from validated fields.
- `form.<field>` (attribute access, e.g. `form.email`) — reads from validated fields and raises `AttributeError` for unvalidated attributes.
- `form.request` — the active `Request` instance (or `None` outside HTTP dispatch).
- `form.validation_result` — the underlying `ValidationResult` (or `None` if instantiated manually).
- `stop_on_first_error: ClassVar[bool] = True` — set on a `FormRequest` subclass to stop rule evaluation as soon as the first error occurs (also combine with `"bail"` for single-error payloads).

> [!TIP]
> Always pass `form.validated()` — not `form.all()` or `await request.json()` — into ORM `create()` / `update()` methods so untrusted clients cannot smuggle unexpected attributes. See [Application Schemas (`app/schemas`)](2026-11-19-application-schemas.md) for structuring request/response DTOs alongside `FormRequest`.

---

## Standalone Validation with `validate()` & `Validator`

Use `await validate(data_or_request, schema, ...)` (or `Validator(data, schema, ...).validate()`) in services, WebSocket handlers, background jobs, or routes. Passing a `Request` instance directly reads JSON, form, or query input automatically based on HTTP method and `Content-Type`:

```python
from __future__ import annotations

from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.validation import ValidationException, validate


async def register_account(request: Request) -> Response:
    data = (
        await validate(
            request,
            {
                "display_name": "required|str|min:2|max:80",
                "age": "nullable|int|between:13,120",
                "website": "nullable|url",
            },
            messages={
                "display_name.min": "Display name must be at least 2 characters.",
            },
            stop_on_first_error=False,
        )
    ).raise_if_failed().validated()

    # ValidatedData works both as a dict and with attribute access (data.display_name)
    return json_response({"display_name": data.display_name, "age": data.get("age")})
```

To return single-error JSON payloads (`{"message": "...", "field": "..."}`) on failure, catch `ValidationException as exc` and return/raise `exc.single()`, which exposes `.first_field()`, `.first_error(field_name=None)`, and `.first_errors()` without mutating the original exception.

### `ValidationResult` API

`validate()` returns a `ValidationResult` dataclass with these attributes and methods:

| Member | Type / Return | Description |
|---|---|---|
| `is_valid` | `bool` | `True` when `errors` is empty (`bool(result)` matches `is_valid`) |
| `passes()` / `fails()` | `bool` | Explicit validity helpers |
| `raise_if_failed()` | `Self` | Raises `ValidationException(self.errors)` if `fails()`, otherwise returns `self` for fluent chaining (`.raise_if_failed().validated()`) |
| `errors` | `dict[str, list[str]]` | Field-keyed lists of validation error messages |
| `validated_data` | `ValidatedData` | Dict subclass of validated and coerced values supporting dot and bracket access |
| `validated()` | `ValidatedData` | Returns a fresh `ValidatedData` copy of `validated_data` |
| `first_error(field_name=None)` | `str \| None` | First error for `field_name`, or first error across all fields |
| `first_errors()` | `dict[str, str]` | Mapping of each invalid field name to its first error message |
| `errors_for(field_name)` | `list[str]` | Copy of all error messages for `field_name` |
| `all_errors()` | `list[str]` | Flattened list of every error message across all fields |

> [!NOTE]
> If `data` passed to `validate()` is not a `dict` or `Request` (for instance `[]` or a string), `validate()` returns `ValidationResult(is_valid=False, errors={"body": ["The request body must be a JSON object."]})`.

---

## Rule Syntax & Built-In Rules

You can define each field's rules in any of these ordered formats:

1. **Pipe-delimited string:** `"required|str|min:3|max:100"`
2. **List or tuple of rule strings, helper functions, rule instances, rule classes, or Python built-in types:**
   - `[required(), str_(), min_(3), max_(100)]`
   - `[required, int, min_(1)]` (built-in Python types `str`, `int`, `float`, `bool` are mapped automatically)
   - `("required", "email:lowercase")`

> [!WARNING]
> Do **not** pass an unordered `set` or `frozenset` as a field's rule collection (`{"required", "int"}`); SwiftPY raises `TypeError` because rule lists must preserve deterministic ordering.

### Presence & Control-Flow Rules

| String DSL | Helper / Class | Behavior |
|---|---|---|
| `required` | `required()` / `RequiredRule` | Field key must be present, not `None`, not whitespace-only `""`/`"   "`, and not an empty `list`/`dict`/`set`/`tuple`. `0` and `False` are valid. Stops evaluating further rules on that field if missing/empty. |
| `nullable` | `nullable()` / `NullableRule` | When a key is present with `None` or `""`, keeps that value in `validated_data` and skips remaining rules for that field. Missing optional keys pass without being added to `validated_data`. |
| `bail` | `bail()` / `BailRule` | Stops running remaining rules on that field after the first rule failure. |

### Type & Coercion Rules

SwiftPY sorts each field's rules so type and coercion rules (`str`, `email`, `int`, `float`, `bool`) run deterministically before constraint rules (`min`, `max`, `between`, `in`, etc.). That means `"min:5|required|int"` and `"required|int|min:5"` behave identically when given `"10"` (coerced to `10` before `min:5` checks `10 >= 5`).

| String DSL | Helper / Python Type | Accepted Input & Output Coercion |
|---|---|---|
| `str` / `string` | `str_()` / `string()` / `str` | Any `str` (rejects `bool`, numbers, lists). |
| `int` / `integer` | `int_()` / `integer()` / `int` | `int` (rejects `bool`, floats like `3.0`, and Unicode superscripts like `"²"`) or ASCII integer string (`^[+-]?[0-9]+$`). Trims and coerces strings (`" 42 "` -> `42`). |
| `float` | `float_()` / `float` | Finite `int`/`float` or finite float string (`"3.14"`, `".5"`, `"+1e-3"`). Rejects `bool`, `NaN`, `inf`, and `-inf`. Coerces `int` and numeric `str` to `float` (`3` -> `3.0`). |
| `bool` / `boolean` | `bool_()` / `boolean()` / `bool` | `bool`, `0`, `1`, or stripped case-insensitive strings `"true"`, `"false"`, `"1"`, `"0"`. Coerces to `bool`. |

### Format, Range & Membership Rules

| String DSL | Helper / Class | Behavior |
|---|---|---|
| `email` / `email:lowercase` | `email(lowercase=False)` / `EmailRule` | Strict RFC-style email validation (local part `<= 64` chars, domain `<= 253` chars with at least two labels, total `<= 254` chars, no `..`). Trims whitespace and lowercases the domain (`"  Alice@EXAMPLE.COM "` -> `"Alice@example.com"`); pass `email:lowercase` (or `email:ci` / `email:lc` / `email(lowercase=True)`) to lowercase the entire address. |
| `url` | `url()` / `UrlRule` | Stripped `str` up to `2048` characters with scheme `http` or `https` and non-empty host (`netloc`). |
| `min:<n>` | `min_(n)` / `MinRule` | Numeric `>= n` for `int`/`float` (`bool` rejected); `len >= n` for `str`, `list`, `dict`, `set`, or `tuple`. |
| `max:<n>` | `max_(n)` / `MaxRule` | Numeric `<= n` for `int`/`float` (`bool` rejected); `len <= n` for `str`, `list`, `dict`, `set`, or `tuple`. |
| `between:<min>,<max>` | `between(min_val, max_val)` / `BetweenRule` | Inclusive range check on numeric value or collection/string length. Raises `ConfigurationError` (string DSL) or `ValueError` (direct class) if `min > max`. |
| `in:<a,b,...>` (or `in_:...`) | `in_(values)` / `InRule` | Matches against allowed values with strict boolean vs. integer separation (`True` never matches `1`, and `False` never matches `0`). |
| `not_in:<a,b,...>` | `not_in(values)` / `NotInRule` | Rejects values inside a forbidden sequence with strict boolean vs. integer separation. |
| `regex:<pattern>` | `regex(pattern)` / `RegexRule` | Runs `re.search` on `str(value)` for `str`, `int`, or `float` (rejects `bool`). See escaping note below. Invalid patterns raise `ConfigurationError` eagerly when parsed. |

**Escaping `|` and `:` inside string `regex` rules:**

Inside pipe-delimited rule strings, escape literal `|` as `\|` and literal `:` as `\:`, or pass rules as a `list` so no string escaping is needed:

```python
schema = {
    # Escaped pipe inside a rule string:
    "tier": r"required|regex:^(free\|pro)$",
    # Or use a list without escaping pipe/colon characters:
    "sku": ["required", regex(r"^(SKU|ITEM):[0-9]{4}$")],
}
```

---

## Wildcards & Nested Array Validation

Use `*` segments to validate every element of a list or every object inside a list of dictionaries:

```python
from __future__ import annotations

from swiftpy.validation import validate


async def validate_batch(payload: dict[str, object]) -> dict[str, object]:
    result = await validate(
        payload,
        {
            "tags": "required",
            "tags.*": "required|str|min:2|max:30",
            "users.*.email": "required|email:lowercase",
            "users.*.age": "required|int|between:18,120",
        },
    )
    return result.validated() if result.passes() else result.errors
```

- Coerced/normalized nested elements are written back into `result.validated_data` (for example `users` becomes `[{"email": "alice@example.com", "age": 25}]`) without mutating your input `payload`.
- Errors use concrete indexed keys such as `"tags.1"` or `"users.0.email"`.
- If a container segment fails structure checks (for example passing `"tags": "not-a-list"` or `"users": ["scalar"]`), SwiftPY records `"The tags field must be a list."` or `"The users.0 field must be an object."`.

---

## Database Rules (`unique` & `exists`)

SwiftPY includes non-blocking `asyncpg` validation rules for uniqueness and foreign-key existence checks:

```python
from __future__ import annotations

from typing import Any

from swiftpy.validation import FormRequest, exists, unique


class StorePostRequest(FormRequest):
    def rules(self) -> dict[str, Any]:
        return {
            "author_id": "required|int|exists:users,id",
            "slug": "required|str|min:3|unique:posts,slug,ci",
        }


def build_update_user_rules(user_id: int) -> dict[str, Any]:
    return {
        "email": [
            "required",
            "email:lowercase",
            unique(
                table="users",
                column="email",
                ignore_id=user_id,
                id_column="id",
                case_insensitive=True,
            ),
        ],
        "role_id": ["required", "int", exists("roles", "id")],
    }
```

### Rule Arguments in String Syntax

- **`exists:<table>,<column>`**: Passes if at least one matching row exists.
- **`unique:<table>,<column>[,<ignore_id>][,<id_column>][,ci]`**: Passes if no matching row exists:
  - `unique:users,email` — exact match against `users.email`.
  - `unique:users,email,42` — ignore the row where `id = 42`.
  - `unique:users,email,42,user_uuid` — ignore the row where `user_uuid = 42`.
  - `unique:users,email,ci` or `unique:users,email,42,ci` — case-insensitive match via `LOWER("email") = LOWER($1)`.

### Connection Resolution, Transactions & Safety

- **Connection pool resolution:** Both `UniqueRule` and `ExistsRule` resolve `DatabasePool` automatically from the application `Container` (or accept an explicit `pool=` argument).
- **Transaction & SAVEPOINT safety:** When executed inside `async with db.transaction():`, both rules reuse the active transaction connection (seeing uncommitted writes in the same transaction) and wrap each check in a sub-transaction (`SAVEPOINT`) so a database type mismatch (such as passing `"abc"` for an integer column) returns a clean `422` validation failure without poisoning the outer transaction.
- **SQL injection defense:** Table and column identifiers are checked by `validate_identifier()` at rule construction time, rejecting unsafe identifiers upfront with `ValueError`, and field values are always bound as `$1` / `$2` parameters.
- **Empty values:** When a value is `None` or `""`, `ExistsRule` and `UniqueRule` return `True` — always pair them with `required` when the field cannot be omitted or blank.

---

## Custom Error Messages

### In a `FormRequest`

Override `messages(self) -> dict[str, str]`:

```python
from __future__ import annotations

from typing import Any

from swiftpy.validation import FormRequest


class SignupRequest(FormRequest):
    def rules(self) -> dict[str, Any]:
        return {
            "email": "required|email|unique:users,email,ci",
            "password": "required|str|min:8",
        }

    def messages(self) -> dict[str, str]:
        return {
            "email.required": "Please enter your email address.",
            "email.unique": "An account with this email already exists.",
            "password.min": "Password must be at least 8 characters long.",
        }
```

### In Standalone `validate()`

Pass `messages={...}` directly:

```python
result = await validate(
    payload,
    {"items.*": "required|int|min:1"},
    messages={"items.*.min": "Each item quantity must be at least 1."},
)
```

### Message Lookup Precedence

When a rule fails on a field, SwiftPY checks overrides in order from most specific to least specific:

1. Concrete field + rule alias (e.g. `"items.0.min"` or `"email.email"`).
2. Wildcard pattern + rule alias (e.g. `"items.*.min"`).
3. Concrete field name or wildcard pattern (e.g. `"email"` or `"items.*"`).
4. Rule alias globally (e.g. `"required"` or `"min"`).
5. The rule's own `.message(field)` (or string `.message`), falling back to `f"The {field} field is invalid."`.

---

## Custom Rules (`ValidatorRule`) & Dependency Injection

### Sync or Async Custom Rule Classes

Implement the `ValidatorRule` protocol (`validate(field, value, data)` + `message(field)`). `validate()` can be a normal `def` or an `async def`:

```python
from __future__ import annotations

from typing import Any

from swiftpy.validation import RuleRegistry, register_rule, validate


class AllowedDomainRule:
    rule_name = "allowed_domain"

    def __init__(self, allowed_domains: tuple[str, ...]) -> None:
        self.allowed_domains = allowed_domains

    def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        if not isinstance(value, str) or "@" not in value:
            return False
        _, domain = value.rsplit("@", 1)
        return domain.lower() in self.allowed_domains

    def message(self, field: str) -> str:
        allowed = ", ".join(self.allowed_domains)
        return f"The {field} field must belong to one of: {allowed}."
```

You can pass rule instances inline in a schema (`{"email": ["required", "email", AllowedDomainRule(("acme.com",))]}`), register a string rule globally with `register_rule(name, factory)`, or isolate rules in a dedicated `RuleRegistry`:

```python
# Global registration (usable in strings like "required|starts_with:INV-")
class StartsWithRule:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        return isinstance(value, str) and value.startswith(self.prefix)

    def message(self, field: str) -> str:
        return f"The {field} field must start with '{self.prefix}'."


register_rule("starts_with", StartsWithRule)

# Or use an isolated registry for scoped/test rule sets:
custom_registry = RuleRegistry()
custom_registry.register("starts_with", StartsWithRule)
```

> [!IMPORTANT]
> Never perform blocking synchronous network or database calls inside a custom `def validate(...)` method. Use `async def validate(...)` with async clients or `DatabasePool`. Note that SwiftPY automatically awaits fields concurrently via `asyncio.gather()` outside DB transactions, and serializes DB-backed rules cleanly when a transaction is active.

### Constructor Dependency Injection in `FormRequest`

If your `FormRequest.__init__` declares typed dependencies registered in the DI container, SwiftPY resolves them automatically before calling `rules()`:

```python
from __future__ import annotations

from typing import Any

from swiftpy.validation import FormRequest

from app.services.tenant_policy_service import TenantPolicyService


class InviteUserRequest(FormRequest):
    def __init__(self, policies: TenantPolicyService) -> None:
        super().__init__()
        self.policies = policies

    def rules(self) -> dict[str, Any]:
        min_pw = self.policies.minimum_password_length()
        return {
            "email": "required|email:lowercase",
            "password": f"required|str|min:{min_pw}",
        }
```

If a required dependency cannot be resolved from the container, SwiftPY raises `ConfigurationError` and fails closed before the handler executes.

---

## Validation Exceptions & Customizing `422` Responses

### Raising `ValidationException` Manually

You can raise `ValidationException` anywhere in a controller, route handler, or service — SwiftPY converts it to an HTTP `422` JSON response automatically:

```python
from swiftpy.validation import ValidationException

if not user_can_register:
    raise ValidationException({"email": ["Registration is closed for this domain."]})
```

Default HTTP response returned by SwiftPY for `ValidationException` (as well as `ValidationError` and `FileValidationError`):

```json
{
  "errors": {
    "email": ["Registration is closed for this domain."]
  }
}
```

### Customizing the `422` Payload Shape

To wrap validation errors in a custom API envelope, register a handler in `app/http/handler/exceptions.py`:

```python
from __future__ import annotations

from swiftpy.core.application import Application
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.validation import ValidationException


def register_validation_renderer(app: Application) -> None:
    @app.exception_handler(ValidationException)
    async def handle_validation_error(
        request: Request, exc: ValidationException
    ) -> Response:
        return json_response(
            {
                "status": "validation_failed",
                "message": exc.message,
                "errors": exc.errors,
            },
            status_code=422,
        )
```

---

## Important Defaults & Production Safeguards

- **Fail-closed `FormRequest` metadata:** If `FormRequest.rules()` or `messages()` raises an error or returns a non-`dict`, or if constructor DI fails, SwiftPY raises `ConfigurationError` (`500`) and guarantees the route handler **never executes**.
- **Eager rule validation (`ConfigurationError`):** Malformed rule strings (`min:abc`, `min:nan`, `between:10,2`, `exists:users` missing a column, invalid `regex` patterns) fail immediately when rules are parsed rather than silently accepting bad input. Unknown rule names raise `ValueError`.
- **No accidental rule exception swallowing:** Programming bugs (`TypeError`, `AttributeError`, missing DB tables in `unique`/`exists`) propagate out of validation rather than silently evaluating as valid or masking root causes.
- **Strict boolean vs. integer separation:** `True` and `False` are never treated as `1` or `0` by `int`, `float`, `min`, `max`, `between`, `in`, or `not_in`.
- **Canonical coercion ordering:** Type rules always execute before constraint rules on each field so string inputs (such as form fields `"25"`) are coerced before range comparisons run.
- **Immutable caller input:** `Validator` never mutates the caller's input dictionary in place; coerced scalars and nested wildcard structures live strictly in `result.validated_data`.

---

## Choosing Between `BaseSchema` / `Schema` and `FormRequest`

Both systems return HTTP `422` (`ValidationException`) automatically when annotated on route or controller handlers. Choose based on what each endpoint needs:

| Need | Use `BaseSchema` / `Schema` (`swiftpy.schemas`) | Use `FormRequest` (`swiftpy.validation`) |
|---|---|---|
| **Static typing in Pyrefly / Mypy** | Full typed attribute access (`payload.movement_date`, `payload.good_eggs`) | Dictionary-like `form.validated()` (`ValidatedData`) |
| **Schema definition style** | Type annotations + `Field(..., gt=0, max_length=100)` + `@field_validator` / `@model_validator` | String or rule-object DSL (`"required|email|unique:users,email"`) |
| **Database checks (`unique` / `exists`)** | Validate in service/repository or pair with explicit checks | Built-in async `"unique:table,col"` and `"exists:table,col"` rules |
| **Service & ORM DTO reuse** | Pass directly to services/repositories and `Model.create(payload)` / `model.update(payload)` | Primarily an HTTP request boundary wrapper |
| **Nested & union schemas** | Native nested `BaseSchema`, `list[ItemSchema]`, `UUID`, `Decimal`, `date`, `Enum`, `Literal` | Wildcard keys (`"items.*.sku"`) |

See [Application Schemas (`app/schemas`)](2026-11-19-application-schemas.md) for the complete `BaseSchema` / `Schema` guide.

---

## Next Step

Continue to [Application Schemas (`app/schemas`)](2026-11-19-application-schemas.md) to organize strongly typed `BaseSchema` / `Schema` DTOs and `make:schema` contracts across routes, services, repositories, and ORM models.
