# Active Record ORM & Relationships

## Introduction

SwiftPY includes an async Active Record ORM built on top of `DatabasePool` and the `Query` builder. Each model class maps to a database table, validates and tracks field changes in memory, guards against mass-assignment and sensitive-field leaks by default, dispatches transaction-aware lifecycle events, and resolves relationships with built-in protection against silent N+1 queries.

Core ORM symbols are exported from `swiftpy.database.orm` (and lazily from top-level `swiftpy` for `Model`, `Field`, `Vector`, `HasOne`, `HasMany`, `BelongsTo`, `BelongsToMany`, `LazyLoadingDisabledError`, and `RelationNotLoadedError`):

```python
from swiftpy.database.orm import (
    BelongsTo,
    BelongsToMany,
    Field,
    HasMany,
    HasOne,
    LazyLoadingDisabledError,
    Model,
    RelationNotLoadedError,
    Vector,
)
from swiftpy.database.orm.model import MassAssignmentError, PrimaryKeyError
```

> **Note:** SwiftPY's ORM is **not** SQLAlchemy or Django ORM. Models require explicit mass-assignment opt-in (`__fillable__` or `__guarded__`), track dirty state via deep-copied field snapshots, and raise `RelationNotLoadedError` if you try to iterate or index an unloaded relationship synchronously.

---

## Creating & Organizing Files

By convention, models live under `app/models/` and are paired with migrations in `database/migrations/`.

### Generating a Model with the CLI

Use `swiftpy make:model` to scaffold a model file (and optionally a matching timestamped migration):

```bash
# Create app/models/post.py (maps to table "posts")
swiftpy make:model Post

# Create the model and a matching create_posts_table migration
swiftpy make:model Post -m

# Override the inferred table name
swiftpy make:model AuditEntry --table audit_entries --migration

# Organize models into nested subdirectories under app/models/
swiftpy make:model Billing/Invoice --migration

# Specify custom output directories or overwrite an existing file
swiftpy make:model Post --path app/models --migration-path database/migrations --force
```

Available flags on `swiftpy make:model`:

| Flag | Description |
| :--- | :--- |
| `-m`, `--migration` | Also generate a migration file for the model's table |
| `--table` | Explicit table name instead of auto-pluralizing the snake-case model name |
| `--path` | Output directory for the model (defaults to `app/models`) |
| `--migration-path` | Output directory for `-m` migrations (defaults to `database/migrations`) |
| `--force` | Overwrite the target file if it already exists |

### Table Name Inference & Primary Keys

When a `Model` subclass is defined, SwiftPY derives its table name and primary key configuration automatically:

- **Table name (`__table__`)**: If you omit `__table__` on the class itself, SwiftPY pluralizes the snake-case class name with `pluralize()` (for example, `BlogPost` -> `"blog_posts"`). Define `__table__ = "legacy_users"` when mapping to an existing custom table.
- **Primary key (`__primary_key__`)**: Defaults to `"id"`. Override it with `__primary_key__ = "uuid"` or by marking a typed attribute with `Field(primary_key=True)`. The primary key attribute is always included in `__fields__` (defaulting to `int` if not annotated). Composite primary keys (such as `"a, b"`) and invalid SQL identifiers raise `PrimaryKeyError`.

```python
from datetime import datetime

from swiftpy.database.orm import Model


class User(Model):
    __table__ = "users"
    __primary_key__ = "id"
    __hidden__ = ["password"]
    __fillable__ = ["name", "email", "password"]

    id: int | None = None
    name: str = ""
    email: str = ""
    password: str = ""
    is_admin: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
```

You do **not** need `typing.ClassVar` on model configuration attributes (`__table__`, `__primary_key__`, `__fillable__`, `__guarded__`, `__hidden__`, `strict_events`, `prevent_lazy_loading`) or relationship descriptors—the `Model` base class already supplies those metadata declarations for static type checkers, and new projects (`swiftpy new`) configure Ruff's `per-file-ignores` for `app/models/**/*.py` (`RUF012`) automatically. Existing models that still annotate with `ClassVar` continue to work identically.

Your application template ships a starter `User` model in `app/models/user.py` that already sets `__primary_key__ = "id"`, `__fillable__ = ["name", "email", "password", "is_admin"]`, and `__hidden__ = ["password"]`.

---

## Step-by-Step Usage Guides & Practical Examples

### 1. Typed Fields, `Field()`, Mutable Defaults & `Vector`

SwiftPY inspects type annotations on your model class (supporting forward references registered in the model registry, while skipping dunder configuration attributes and `ClassVar` annotations if present). Subclasses inherit `__fields__`, defaults, factories, vector definitions, and relationship descriptors from parent models.

Use `Field()` when you need a dynamic per-instance default factory or explicit primary-key metadata:

```python
import uuid
from datetime import UTC, datetime
from typing import Any

from swiftpy.database.orm import Field, Model


class Document(Model):
    __fillable__ = ["title", "tags", "metadata"]

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    title: str = ""
    tags: list[str] = []
    metadata: dict[str, Any] = {}
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = None
```

Key field behaviors:

- **Per-instance defaults (`default_factory`)**: A `default_factory` callable (or callable `default`) runs fresh on every new instance instead of sharing a single timestamp or UUID across rows.
- **Safe mutable defaults**: Even if you declare a bare mutable default like `tags: list[str] = []` or `metadata: dict[str, Any] = {}`, SwiftPY `deepcopy`s it per instance during initialization and database hydration so mutating `doc_a.tags` never leaks into `doc_b.tags`.
- **`Field(nullable=...)` is metadata only**: Passing `primary_key=True` sets `__primary_key__`, and `Field(...)` records metadata in `FieldInfo`. Always define actual PostgreSQL column types, nullability, and constraints in your `Blueprint` migrations.

For `pgvector` embedding columns, declare fields with `Vector(dimensions, metric=...)`:

```python
from swiftpy.database.orm import Model, Vector


class KnowledgeChunk(Model):
    __table__ = "knowledge_chunks"
    __fillable__ = ["content", "embedding"]

    id: int | None = None
    content: str = ""
    embedding: list[float] | None = Vector(1536, metric="cosine")
```

- `dimensions` must be a positive integer (and not a `bool`).
- `metric` must be one of `"cosine"` (default), `"l2"`, or `"inner_product"`.
- Assigning or filling an embedding checks length (`len(value) == dimensions`), rejects `bool` or non-numeric entries, and rejects `NaN` or `inf`.
- SwiftPY automatically serializes Python float lists to PostgreSQL `pgvector` strings (`"[0.1,0.2,...]"`) when writing and coerces returned string vectors back to `list[float]` when hydrating models. Similarity search queries use `PgVectorAdapter` or structured SQL expressions.

### 2. Mass Assignment Protection (`__fillable__` & `__guarded__`)

By default, **both `__fillable__` and `__guarded__` are `None`**, which puts every model in **deny-all** mode for mass assignment (`Model(...)`, `.fill(...)`, and `Model.create(...)` raise `MassAssignmentError`). This prevents request payloads from modifying unintended columns such as `is_admin` or `balance`.

Choose one strategy per model:

```python
from swiftpy.database.orm import Model


# Strategy A: Allowlist (recommended for user-facing payloads)
class ProfileUpdateModel(Model):
    __table__ = "users"
    __fillable__ = ["name", "email"]

    id: int | None = None
    name: str = ""
    email: str = ""
    is_admin: bool = False


# Strategy B: Blocklist (explicitly forbid privileged columns; [] allows all declared fields)
class Article(Model):
    __guarded__ = ["published_by_admin"]

    id: int | None = None
    title: str = ""
    body: str = ""
    published_by_admin: bool = False
```

Important differences between the two modes:

- **Allowlist (`__fillable__`)**: Permitted keys (plus the primary key) are assigned; **unlisted declared keys are silently ignored** (useful when passing validated dicts containing extra fields).
- **Blocklist (`__guarded__`)**: Passing any guarded key raises `MassAssignmentError` immediately.
- **Undeclared and private keys**: Keys not declared in `__fields__` or starting with `_` **always** raise `MassAssignmentError`—even in `.force_fill()` and `Model.force_create()`.
- **Trusted internal writes**: In seeders, background jobs, or internal services where you explicitly need to populate guarded fields, use `await User.force_create(...)` or `user.force_fill(...)`:

```python
admin = await User.force_create(
    name="System Admin",
    email="admin@example.com",
    is_admin=True,
)
```

### 3. Serialization, `__hidden__` & Safe `__repr__`

Declare sensitive columns in `__hidden__` so they are omitted whenever a model is serialized:

```python
user = await User.find_or_fail(1)

# Excludes __hidden__ fields (e.g. "password") recursively across loaded relations
public_payload = user.to_dict()

# Include hidden fields explicitly for internal exports or audits
full_payload = user.to_dict(include_hidden=True)

# Override visibility for a single instance without mutating the class
user.make_visible("password").to_dict()
user.make_hidden("email").to_dict()
```

Additional safety built into every `Model`:

- `json_response(model)` uses `to_dict()`, keeping `__hidden__` attributes out of HTTP responses automatically.
- `repr(model)` renders only `<ClassName pk_name=pk_val>` (for example `<User id=1>`), preventing passwords, tokens, or PII from appearing in logs or exception tracebacks.
- Equality (`==`) requires the exact same model class (`type(self) is type(other)`) and compares primary keys (falling back to object identity `is` when the primary key is `None`). Unsaved models without a primary key raise `TypeError` on `hash(model)`.

### 4. CRUD Operations, Dirty Tracking & Querying

#### Creating, Updating, Refreshing & Deleting

```python
# Create and insert immediately
user = await User.create(name="Ada", email="ada@example.com", password=hashed_pw)

# Or build, mutate, and save
draft = Post(title="Intro to SwiftPY", user_id=user.id)
draft.body = "Full article body..."
await draft.save()

# Dirty tracking detects both attribute reassignment and in-place dict/list mutations
draft.title = "Async ORM in SwiftPY"
if draft.is_dirty("title"):
    dirty_fields = draft.get_dirty()
    await draft.save()

# Reload latest database state
await draft.refresh()

# Atomic counter updates (updates DB atomically and refreshes the instance)
await draft.increment("views", amount=1)
await draft.decrement("views", amount=1, extra={"featured": False})

# Delete the row (raises RuntimeError on unsaved instances without a PK)
await draft.delete()
```

Dirty-tracking safeguards:

- If an existing persisted model has **no dirty non-PK attributes**, calling `await model.save()` performs **0 SQL queries** and emits no `updated` event.

- **Direct instance updates (`update()` & `force_update()`):** On a persisted model instance, call `await model.update({"name": "New Name"})`, `await model.update(name="New Name")`, or pass a `BaseSchema` DTO (`await model.update(payload)`) to apply attributes via `.fill()`, respect `__fillable__` / `__guarded__` rules, update `updated_at`, run dirty checks (0 SQL queries if nothing changed), emit `updating` / `updated` events, and persist via `.save()` (returning `self`). When given a `BaseSchema`, `update()` extracts `payload.to_dict(exclude_unset=True)` so omitted optional fields in partial-update payloads do not overwrite existing columns, while `Model.create(payload)` extracts `payload.to_dict()` (merging any extra keyword overrides such as `organization_id=org_id`). For trusted internal writes bypassing mass-assignment rules, use `await Model.force_create(...)` or `await model.force_update(...)`. Calling `update()` or `force_update()` on an unsaved model raises `PrimaryKeyError`.
- If the model defines `updated_at`, saving a dirty existing record refreshes `updated_at` to `datetime.now(UTC)` automatically.
- If you load a partial subset of columns with `.select("id", "title")`, SwiftPY records only the selected columns in its original snapshot. Calling `await post.save()` after modifying `post.title` updates only `title` and never clobbers unselected database columns.

#### Repositories & Static Type Inference

SwiftPY's `Model`, `ModelQueryBuilder`, and relationship builders preserve concrete model types end-to-end across chains in Pyrefly, Mypy (`--strict`), and Pyright—without requiring generic repository base classes, `TypeVar`, `Self`, or explicit `model: type[...]` annotations in application code:

```python
from swiftpy.database.orm import BelongsTo, HasMany, Model


class DocVersion(Model):
    __table__ = "doc_versions"
    id: int
    version: str
    slug: str
    sort_order: int = 0
    groups = HasMany(DocGroup, foreign_key="version_id")
    author = BelongsTo(User, foreign_key="author_id")


class DocVersionRepository:
    model = DocVersion

    async def get_doc_version(self, version_id: int) -> DocVersion:
        version = await self.model.with_("groups").where("id", version_id).first()
        if not version:
            raise NotFoundError("Version not found")
        return version

    async def get_doc_versions(self) -> list[DocVersion]:
        return await self.model.with_("groups", "author").order_by("sort_order").get()

    async def create_doc_version(self, payload: DocVersionCreateSchema) -> DocVersion:
        return await self.model.create(payload)
```

Direct `Model` calls and class-based relationships (`HasMany(DocGroup)`, `BelongsTo(User)`, `HasOne(Profile)`, `BelongsToMany(Role)`) preserve concrete return types across `await` and chained builders:

```python
versions: list[DocVersion] = await DocVersion.with_("groups", "author").order_by("sort_order").get()
first: DocVersion | None = await DocVersion.where("id", 1).first()
groups: list[DocGroup] = await versions[0].groups.where_in("id", [1, 2]).latest("id").get()
author: User | None = await versions[0].author
```

#### Query Builder Methods on `Model`

Every `Model` exposes classmethods that return a `ModelQueryBuilder` bound to that model. You can chain filters and either call a terminal method or `await` the query directly:

```python
# Direct lookups
user = await User.find(1)
user = await User.find_or_fail(1)        # Raises ModelNotFoundError (HTTP 404)
latest_user = await User.latest().first()
required_user = await User.where("email", "ada@example.com").first_or_fail()
all_users = await User.all()

# Composing filters (awaiting a ModelQueryBuilder calls .get())
active_authors = await (
    User.where("is_active", True)
    .where_not_null("verified_at")
    .where_group(lambda q: q.where("role", "editor").or_where("role", "admin"))
    .latest()
    .limit(25)
)

# Find-or-create / update-or-create / bulk update / upsert (accepts Mapping or BaseSchema)
tag = await Tag.first_or_create(
    {"slug": "async"},
    values={"name": "Async Python"},
)
profile = await Profile.update_or_create(
    {"user_id": user.id},
    values={"bio": "Building with SwiftPY"},
)
await User.where("id", user.id).update(patch_schema)
await Tag.upsert(
    [{"slug": "async", "name": "AsyncIO"}, {"slug": "typing", "name": "Strict Typing"}],
    unique_by=["slug"],
    update_columns=["name"],
)

# Aggregates, pagination, and callback-based batch processing
total = await User.where("is_active", True).count()
page = await Post.where("published", True).latest().paginate(page=1, per_page=20)
simple_page = await Post.where("published", True).simple_paginate(page=1, per_page=20)

async def send_digest_batch(users: list[User]) -> None:
    for recipient in users:
        await queue_digest(recipient.id)

completed = await User.where("needs_digest", True).chunk_by_id(200, send_digest_batch)
```

`ModelQueryBuilder` supports `.where()`, `.or_where()`, `.where_group()`, `.or_where_group()`, `.where_in()`, `.where_like()`, `.where_ilike()`, `.where_between()`, `.where_null()`, `.where_not_null()`, `.where_raw()`, `.join()`, `.left_join()`, `.right_join()`, `.select()`, `.select_raw()`, `.distinct()`, `.group_by()`, `.having()`, `.latest()`, `.oldest()`, `.in_random_order()`, `.when()`, `.unless()`, `.limit()`/`.take()`, `.offset()`/`.skip()`, `.clone()`, `.count()`, `.exists()`, `.sum()`, `.avg()`, `.min()`, `.max()`, `.value()`, `.pluck()`, `.upsert()`, `.chunk()`, `.chunk_by_id()`, `.paginate()`, `.simple_paginate()`, and `.allow_all()`. It also forwards `.where_column()`, `.where_exists()`/`.where_not_exists()`, `.where_any()`/`.where_all()`/`.where_none()`, `.where_past()`/`.where_future()`, the `.where_json_*()` helpers, `.shared_lock()`/`.lock_for_update()`, `.to_sql()`, `.to_raw_sql()`, `.dump()`, `.dump_raw_sql()`, `.dd()`, and `.dd_raw_sql()`. Both `.latest()` and `.oldest()` default to sorting by `"created_at"` (pass a column name such as `.latest("id")` if your table uses another column), and `.chunk(count, callback)` / `.chunk_by_id(count, callback)` return `False` early if `callback` returns `False`.

#### Column, JSON, Existence & Time Filters

The ORM forwards these filters through `ModelQueryBuilder` and model classmethods. `where_exists()` and `where_in()` / `where_not_in()` (plus `or_` variants and `where(col, "IN", ...)`) accept either another `ModelQueryBuilder` subquery, a core `QueryBuilder` subquery, or any non-string `Sequence` (`list`, `tuple`, custom `Sequence`; `str`, `bytes`, `bytearray`, and `memoryview` raise `QueryError`). Write operations (`insert`, `insert_returning`, `update`, `upsert`, `increment`, `decrement`, `first_or_create`, `update_or_create`) accept covariant `Mapping[str, Any]` inputs cleanly without type casts. Use the real outer table name in a correlated `where_column()` condition:

```python
users_with_posts = User.where_exists(
    Post.query().where_column("posts.user_id", "=", "users.id")
)
authors = User.where_in("id", Post.select("user_id").where("published", True))
active_users = User.where_any(["email", "name"], "ada").where_future("created_at")
```

JSON `->` paths are supported in `Model.where()` / `where_in()` and in the `where_json_*()` methods. For example, `User.where_json_contains("preferences->roles", "editor")` or `User.where_json_length("preferences->languages", ">=", 2)`. Query-builder `.update({...})` also accepts JSON path keys; instance `await user.update({...})` intentionally does not.

`where_past()` and `where_future()` compare with PostgreSQL `CURRENT_TIMESTAMP`, which is fixed at the start of the active transaction. Row locks must be read inside `db.transaction()`:

```python
from swiftpy.database import db

async with db.transaction():
    user = await User.where("id", user_id).lock_for_update().first()
```

Unions are intentionally limited to the lower-level `QueryBuilder`; `ModelQueryBuilder` and `Model` do not expose `union()`. Table aliases are not supported for correlated existence queries. See the [query-builder guide](./2026-11-15-database-query-builder-and-migrations.md) for JSON comparison rules, update limitations, lock compatibility, union terminal restrictions, and debugging safety.

### 5. Lifecycle Events & Transactions

Models dispatch events through `EventDispatcher.get_instance()` using `<snake_case_model>.<action>` names:

- Before SQL runs: `creating`, `updating`, `deleting`
- After SQL succeeds (and after `db.transaction()` commits): `created`, `updated`, `deleted`

```python
from swiftpy.events import EventDispatcher


dispatcher = EventDispatcher.get_instance()


def ensure_slug(post: Post) -> None:
    if not post.slug:
        post.slug = post.title.lower().replace(" ", "-")


def notify_search_index(post: Post) -> None:
    # Only runs after the surrounding db.transaction() commits (never on rollback)
    enqueue_index_update(post.id)


dispatcher.listen("post.creating", ensure_slug)
dispatcher.listen("post.created", notify_search_index)
```

Lifecycle event behavior:

- **Before-events (`creating`, `updating`, `deleting`)** execute inline before the SQL query runs; raising an exception cancels the database write.
- **After-events (`created`, `updated`, `deleted`)** automatically defer via `on_commit` inside `db.transaction()`. If the transaction rolls back, the listeners never fire.
- **`strict_events`**: By default (`strict_events = False`), exceptions inside after-event listeners are logged without failing an already-committed database write. Set `strict_events = True` on a model class if after-event failures must propagate.

### 6. Relationships, Eager Loading & N+1 Protection

Declare relationships directly on the model class using `HasOne`, `HasMany`, `BelongsTo`, and `BelongsToMany`. Use string model names (such as `"Post"` or `"Tag"`) to prevent circular imports between modules:

```python
from swiftpy.database.orm import BelongsTo, BelongsToMany, HasMany, HasOne, Model


class User(Model):
    __fillable__ = ["name", "email"]

    id: int | None = None
    name: str = ""
    email: str = ""

    profile = HasOne("Profile")
    posts = HasMany("Post")


class Profile(Model):
    __fillable__ = ["user_id", "bio"]

    id: int | None = None
    user_id: int | None = None
    bio: str = ""

    user = BelongsTo("User")


class Post(Model):
    __fillable__ = ["user_id", "title", "body"]

    id: int | None = None
    user_id: int | None = None
    title: str = ""
    body: str = ""

    author = BelongsTo("User", foreign_key="user_id")
    tags = BelongsToMany("Tag")


class Tag(Model):
    __fillable__ = ["name"]

    id: int | None = None
    name: str = ""

    posts = BelongsToMany("Post")
```

> **Important — Import Related Models First (`RuntimeError` on Unregistered Targets):**
> String relationship names (such as `HasMany("Post")` or `.with_("posts.tags")`) are resolved lazily through SwiftPY's global model registry when a relationship query or eager load runs. Defining a relationship with a string does **not** import the target module for you:
>
> - If the related model class has **not** been imported before `user.posts`, `User.with_("posts")`, or `.attach()` runs, SwiftPY raises:
>   `RuntimeError: Model 'Post' has not been registered.`
> - **Recommended pattern:** Export and import all your domain models together in `app/models/__init__.py` (or import the related model modules at the top of `main.py` / your service module before executing queries).
> - If two different modules define models with the **same class name** (for example `billing.models.Item` and `inventory.models.Item`), string lookup raises:
>   `RuntimeError: Model name 'Item' is ambiguous: ... Pass the class itself instead of a string.`
>   In that case (or whenever imports are not circular), import and pass the target model class directly (`items = HasMany(BillingItem)`), which also gives static type checkers exact target-model types (`await order.items -> list[BillingItem]`).

> **Note on relationship keys & pivot tables:** `HasOne`, `HasMany`, and `BelongsTo` all accept `(related_model, foreign_key=None, local_key=None)`. When `foreign_key` is omitted on `BelongsTo`, SwiftPY infers `<relation_attribute_name>_id` (so `user = BelongsTo("User")` uses `"user_id"`, whereas `author = BelongsTo("User", foreign_key="user_id")` explicitly maps `author` to `user_id`). `BelongsToMany` accepts `(related_model, pivot_table=None, foreign_pivot_key=None, related_pivot_key=None, parent_key=None, related_key=None)`; when `pivot_table` is omitted, SwiftPY sorts the two snake-case model names alphabetically and joins them with `_` (e.g. `"post_tag"`).

#### Eager Loading with `.with_()`

Always eager-load relationships with `.with_()` when rendering lists or serializing trees to avoid N+1 queries. SwiftPY batches parent keys into `WHERE foreign_key IN (...)` queries (chunked in groups of 1,000) and supports nested dot notation:

```python
# Executes batched queries instead of 1 + N queries
users = await User.with_("profile", "posts.tags").where("is_active", True).get()

for user in users:
    # Already loaded: synchronous iteration, indexing, and to_dict() work cleanly
    titles = [post.title for post in user.posts]
    payload = user.to_dict()
```

Eager-loaded to-many relationships return a `LoadedList` (a `list` subclass that also works if awaited with `await user.posts`), while eager-loaded to-one relationships resolve to `Model | None`.

#### Filtering by Relationship Existence (`has`, `doesnt_have`, `where_has`)

Query parent models based on whether related records exist or match additional constraints—across `HasOne`, `HasMany`, `BelongsTo`, `BelongsToMany`, and nested dot-paths:

```python
# Users with at least one subscription / post (supports camelCase or snake_case relation names)
subscribed = await User.has("activeSubscription").get()
active_authors = await User.has("posts", ">=", 2).get()

# Users without any posts (NULL-safe NOT IN)
no_posts = await User.doesnt_have("posts").get()

# Constrain the related query inside the subquery
published_authors = await User.where_has(
    "posts", lambda q: q.where("published", True)
).get()

# Nested relationships + OR combinators
approved_commenters = await User.where_has(
    "posts.comments", lambda q: q.where("approved", True)
).get()
mixed = await User.where("is_admin", True).or_where_doesnt_have("posts").get()
```

#### Async Lazy Queries vs `RelationNotLoadedError`

By default (`prevent_lazy_loading = False`), accessing an **unloaded** relationship attribute returns a `RelationshipQueryBuilder[T]` (for `HasMany`/`BelongsToMany`) or `SingleRelationshipQueryBuilder[T]` (for `HasOne`/`BelongsTo`), forwarding the full set of `ModelQueryBuilder` fluent and terminal methods while preserving concrete `Model` types:

```python
user = await User.find_or_fail(1)

# Awaiting directly returns list[Post] (or Profile | None for HasOne/BelongsTo)
posts = await user.posts
profile = await user.profile

# Refine relationship queries with full ModelQueryBuilder method parity
published_posts = await user.posts.where("published", True).latest("id").get()
filtered_posts = await user.posts.where_in("id", [1, 2]).paginate(per_page=10, page=1)
required_profile = await user.profile.where_not_null("bio").first_or_fail()

# Creating or updating child records through a relation accepts kwargs, mappings, or BaseSchema
new_post = await user.posts.create(title="First Post", body="Hello!")
new_from_schema = await user.posts.create(create_post_schema)
await user.posts.where("id", new_post.id).update(patch_post_schema)
```

However, trying to use an unloaded relationship **synchronously**—such as `for p in user.posts`, `len(user.posts)`, `bool(user.posts)`, `user.posts[0]`, or `user.profile.bio`—raises **`RelationNotLoadedError`** rather than silently executing blocking I/O inside an async event loop.

#### Enforcing Strict Eager Loading (`prevent_lazy_loading`)

In endpoints or tests where any accidental lazy query must fail fast, set `prevent_lazy_loading = True` on a model class or a specific instance:

```python
class StrictUser(Model):
    __table__ = "users"
    prevent_lazy_loading = True

    id: int | None = None
    posts = HasMany("Post", foreign_key="user_id")


user = await StrictUser.first_or_fail()
user.posts  # Raises LazyLoadingDisabledError immediately when not loaded via .with_("posts")
```

#### Many-to-Many Pivot Operations (`BelongsToMany`)

Use `.attach()`, `.detach()`, and `.sync()` on a `BelongsToMany` relationship to manage pivot table rows:

```python
post = await Post.find_or_fail(1)

# Attach tag IDs or Tag model instances (with optional extra pivot columns)
await post.tags.attach(tag_id, assigned_by="editor")

# Detach a specific tag, or detach all tags when called with no argument
removed_count = await post.tags.detach(tag_id)
removed_all = await post.tags.detach()

# Replace all pivot associations atomically inside db.transaction()
await post.tags.sync([tag_a.id, tag_b])
```

`BelongsToMany` relationships do not implement `.create()`; create the related `Tag` record first and link it with `.attach()` or `.sync()`.

---

## Important Defaults & Production Safeguards

- **Deny-all mass assignment by default**: Leaving both `__fillable__` and `__guarded__` unset (`None`) blocks all mass assignment via `Model(...)`, `.fill(...)`, and `.create(...)`. Always declare an explicit `__fillable__` allowlist on models populated from request input, and reserve `.force_fill()` / `.force_create()` for trusted internal code.
- **Hide credentials and tokens with `__hidden__`**: Declare password hashes, API secrets, and internal tokens in `__hidden__` so `.to_dict()` and `json_response()` omit them by default.
- **Never share models with unloaded relations into sync templates/serializers**: Either eager-load with `.with_()` upfront or catch missing eager loads early in development/CI with `prevent_lazy_loading = True` so `LazyLoadingDisabledError` surfaces N+1 paths before production.
- **Bulk deletes/updates vs single-record saves**: `model.save()` and `model.delete()` execute targeted single-row operations wrapped in dirty checks and lifecycle hooks. Never loop `await row.save()` inside a handler when a single batched query or `Model.upsert()` / `Query` update is appropriate, and remember `Query` bulk updates/deletes require `.allow_all()` if called intentionally without a `WHERE` clause.
- **Database migrations remain the source of truth**: `Field(...)`, `Vector(...)`, and relationship descriptors describe application-level behavior; always create corresponding tables, foreign keys, indexes, and `vector(...)` columns in `database/migrations/` using `Schema` and `Blueprint` (see [Database Schema, `Blueprint` & Migrations](2026-11-19-database-schema-and-blueprint.md)).
- **Combine models cleanly with schemas**: Return domain `Model` records from repositories and transform or validate payloads with `BaseSchema.from_model()`, `Model.create(schema)`, `model.update(schema)`, `.to_dict()`, or `Resource` classes in [Application Schemas (`app/schemas`)](2026-11-19-application-schemas.md).

---

## Understanding PostgreSQL Primary Key Sequence Gaps

SwiftPY's `table.id()` creates PostgreSQL `BIGSERIAL` (or `GENERATED ... AS IDENTITY`) primary keys backed by PostgreSQL `SEQUENCE` objects. Because PostgreSQL sequences advance outside transaction rollback (`nextval()` is non-transactional for concurrency performance):

- **Failed inserts leave harmless ID gaps:** If `await User.create(...)` fails with `asyncpg.UniqueViolationError` or exits early from a rolled-back `async with db.transaction():` block, the allocated primary key (`2`, `3`, ...) is consumed and never reused. The next successful insert receives the next sequence value (`4`).
- **No data loss occurs:** All committed rows remain intact; gaps are normal PostgreSQL behavior, not missing records.
- **Need gapless business numbers (e.g., invoice numbers)?** Keep surrogate primary keys (`id`) on `BIGSERIAL`, and generate sequential legal/accounting numbers separately using an explicit locked sequence table row (`SELECT ... FOR UPDATE`) or transactional numbering strategy only where gap-free numbering is legally required.

---

## Next Step

Continue to [Authentication, JWT, Token Blacklist & API Keys](2026-11-15-authentication-and-security.md) to configure `AuthManager`, `JWTIssuer`, `TokenBlacklist`, `ApiKeyGuard`, and `CurrentUser` dependencies on top of your models.
