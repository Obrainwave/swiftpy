# Changelog

All notable changes to SwiftPY will be documented in this file.

## [Unreleased]

## [0.3.9] - 2026-11-27

### Changed
- **Scaffolding Static Analysis Hardening (`swiftpy/console/commands/new.py`, `pyproject.toml`):**
  - Replaced global `ignore_missing_imports = true` and `follow_imports = "silent"` under `[tool.mypy]` in `swiftpy new` (`_get_pyproject_toml`) with scoped `[[tool.mypy.overrides]]` for untyped third-party packages (`asyncpg`, `aiosmtplib`, `jinja2`, and `disallow_untyped_calls = false` for `redis.*`). Because Pyrefly imports `[tool.mypy]` when `pyrefly.toml` is absent, removing global `ignore_missing_imports = true` ensures Pyrefly immediately reports `[missing-import]` when a host IDE interpreter lacks `swiftpy-framework` instead of silently replacing `Model` with `Unknown` and triggering misleading `[no-any-return-implicit]` errors on repository methods.
  - Expanded `[tool.ruff.lint.per-file-ignores]` in `pyproject.toml` and `swiftpy new` to cover `"app/models/**/*.py" = ["RUF012"]`, `"app/**/models/**/*.py" = ["RUF012"]`, and `"tests/**/*.py" = ["RUF012"]`, allowing models in standard, feature-grouped, and test directories to define `__fillable__ = [...]`, `__guarded__ = [...]`, and `__hidden__ = [...]` without `typing.ClassVar`.
  - Bumped `swiftpy-framework` package version and `swiftpy new` dependency floor to `0.3.9`.

### Added
- **Static Typing & Wheel Packaging Regression Tests (`tests/typing/test_orm_typing.py`, `tests/console/test_new_command.py`):**
  - Added `typing.assert_type` assertions across direct `Model` query chains, repository methods, and relationship chains (`HasMany`, `BelongsTo`) verified under both `mypy --strict` and `pyrefly check --error=no-any-return-implicit`.
  - Added `test_py_typed_marker_present_in_package_and_wheel_config` verifying `swiftpy/py.typed` exists on disk, in Hatchling wheel config, and inside built wheel artifacts (`dist/swiftpy_framework-*.whl`).

### Documentation
- Updated [ORM Models & Relationships](documentations/2026-11-15-orm-models-and-relationships.md), [Installation & Quick Start](documentations/2026-11-14-installation-quick-start.md), [CLI & Testing](documentations/2026-11-19-cli-and-testing.md), and [README.md](README.md) with guidance on `ClassVar`-free model metadata (`__fillable__`, `__guarded__`, `__hidden__`), host IDE `.venv` setup for Dockerized projects, and avoiding global `ignore_missing_imports = true` with Pyrefly.

## [0.3.8] - 2026-11-25

### Added
- **Source-Level ORM & Relationship Static Typing (`Model`, `ModelQueryBuilder[T]`, `RelationshipQueryBuilder[T]`, `SingleRelationshipQueryBuilder[T]`):** Propagated concrete `Model` types through `Relation._related_cls`, `_get_target_model_typed()`, `Relation.build_lazy_query() -> ModelQueryBuilder[T]`, `_BaseRelationshipQueryBuilder[T]` (explicit typed forwarding for all `ModelQueryBuilder[T]` fluent and terminal methods), `RelationshipQueryBuilder[T].__await__() -> Generator[Any, None, list[T]]`, and `SingleRelationshipQueryBuilder[T].__await__() -> Generator[Any, None, T | None]`. Application repositories (`model = YourModel`), direct model query chains (`DocVersion.with_(...).order_by(...).get()`), and relationship chains infer concrete return types in Pyrefly (including `no-any-return-implicit`), Mypy (`--strict`), and Pyright without requiring user-side `Generic`, `TypeVar`, `Self`, or `model: type[...]` boilerplate.
- **Covariant Inputs & Schema Parity Across Query Builders and Relations:** Widened `QueryBuilder` / `ModelQueryBuilder` / `Model` filter and write APIs (`where_in`, `or_where_in`, `where_not_in`, `or_where_not_in`, `insert`, `insert_returning`, `update`, `upsert`, `increment`, `decrement`, `first_or_create`, `update_or_create`, `select_raw`, `having_raw`, `or_having_raw`) to accept covariant `Mapping[str, Any]` / non-string `Sequence[Any]` and `ModelQueryBuilder[Any]` subqueries while continuing to reject `str`/`bytes`/`bytearray`/`memoryview` in `IN`/`NOT IN`. Added `BaseSchema` support to `ModelQueryBuilder.update()`, `Model.first_or_create()`, `Model.update_or_create()`, `RelationshipQueryBuilder.create()`, and `RelationshipQueryBuilder.update()`.

### Fixed
- **Pyrefly ORM Compliance (`swiftpy/database/orm/model.py`):** Annotated empty vector return in `_coerce_db_value()` and guarded `isinstance(other, Model)` in `Model.__eq__()` so `swiftpy/database/` passes Pyrefly cleanly.

### Changed
- **Packaging & Scaffolding:** Bumped `swiftpy-framework` to `0.3.8` (`pyproject.toml`, `uv.lock`), bumped the `swiftpy new` dependency floor to `"swiftpy-framework>=0.3.8"`, and simplified `repository.stub` to zero-boilerplate `model = YourModel` repository guidance.

### Documentation
- Updated [ORM Models & Relationships](documentations/2026-11-15-orm-models-and-relationships.md), [Application Schemas](documentations/2026-11-19-application-schemas.md), [Database Pool, Query Builder & Transactions](documentations/2026-11-15-database-query-builder-and-migrations.md), [CLI & Testing](documentations/2026-11-19-cli-and-testing.md), [Application Architecture & Project Structure](documentations/2026-11-21-application-architecture-and-project-structure.md), [Installation & Quick Start](documentations/2026-11-14-installation-quick-start.md), and [README.md](README.md) for zero-boilerplate repository typing, typed relationship chains, `BaseSchema` query-builder/relation interop, covariant `Mapping`/`Sequence` inputs, Pyrefly/Mypy commands, and `>=0.3.8`.

## [0.3.7] - 2026-11-25

### Added
- **PEP 561 Static Typing Support (`swiftpy/py.typed`):** Added the PEP 561 `py.typed` marker packaged into distributions, and updated `Model` / `ModelQueryBuilder[T]` fluent query annotations so repository chains (`await self.model.with_(...).where(...).first()` and `.get()`) preserve concrete model return types in Pyrefly, Mypy, and Pyright.
- **Declarative Application Schemas (`swiftpy.schemas`):** Added zero-dependency declarative schema module exporting `BaseSchema`, `Schema` (alias of `BaseSchema`), `Field`, `SchemaField`, `SchemaFieldInfo`, `@field_validator`, and `@model_validator` with `@dataclass_transform(kw_only_default=True, field_specifiers=(Field, SchemaField, SchemaFieldInfo))`.
  - Supports automatic type coercion (`str`, `int`, `float`, `bool`, `Decimal`, `UUID`, `date`, `datetime`, `time`, `Enum`, `IntEnum`, `Literal`, `Annotated`, unions, nested schemas, and container generics) and `Field(...)` constraints (`gt`, `ge`, `lt`, `le`, `multiple_of`, `min_length`, `max_length`, `pattern`, `strip_whitespace`, `to_lower`, `to_upper`, `alias`, `exclude`, `default_factory`), raising HTTP 422 `ValidationException` on failure.
  - Added `.validate()`, `.from_dict()`, `.model_validate()`, `await .from_request()`, `.from_model()`, `.to_dict(exclude_none=..., exclude_unset=..., by_alias=...)`, `.model_dump()`, `.fields_set`, and `.is_set()`.
- **HTTP Schema Auto-Injection & Serialization (`Router`, `Container`, `Application`, `Response`):** Route functions and controller handlers declaring a `BaseSchema` / `Schema` parameter automatically parse JSON, `application/x-www-form-urlencoded`, or `multipart/form-data` payloads into the validated schema before execution; unbound `BaseSchema` subclasses are excluded from DI auto-wiring; handlers returning `BaseSchema` (or `list[BaseSchema]`) auto-serialize to JSON (including `set` / `frozenset` -> JSON arrays via `_json_default`).
- **ORM & Auth Interoperability:** `Model.create()`, `Model.force_create()`, `model.update()`, and `model.force_update()` accept `BaseSchema` instances directly (`to_dict()` on create with keyword overrides, `to_dict(exclude_unset=True)` on update to preserve omitted fields); `Auth.attempt()`, `Auth.attempt_when()`, and `Auth.once()` (along with `AuthManager` / `AuthProxy`) accept `BaseSchema` payloads; and `isset()` resolves canonical schema fields, aliases, nested schemas, and `__extra_fields__`.
- **Schema Hardening & Performance:** `BaseSchema` tracks post-validation field mutations in `__fields_set__`, enforces `Annotated[T, Field(...)]` constraints inside generic collections, normalizes mixed `Decimal`/float bounds, rejects `_`-prefixed keys and prevents method shadowing under `extra = "allow"`, supports single-mapping and positional runtime constructors, skips `copy.deepcopy()` on immutable defaults, and avoids duplicate `request.input(_preload=True)` body parsing when handlers take both `Request` and `BaseSchema`/`FormRequest`.

### Changed
- **CLI Scaffolding (`make:schema`, `make:repository`, `swiftpy new`):** Updated `schema.stub` and starter `app/schemas/user_schema.py` to generate declarative `BaseSchema` DTOs (`UserCreateSchema` / `UserResponseSchema`), updated `repository.stub` with typed `model: type[YourModel]` guidance, and bumped the `swiftpy-framework` dependency floor to `>=0.3.7` (package version `0.3.7`).

### Documentation
- Updated Application Schemas, Validation & Form Requests, HTTP Routing, ORM Models, CLI & Testing, Application Architecture, and Installation Quick Start guides for `swiftpy.schemas`, typed repositories, and `0.3.7`.

## [0.3.6] - 2026-11-24

### Added
- **Query Builder Predicates:** Added `where_column()`, correlated `where_exists()` / `where_not_exists()` (including OR variants), grouped `where_any()` / `where_all()` / `where_none()`, and `where_past()` / `where_future()` with ORM parity.
- **JSON Query Support:** Added strict `column->path` expressions for `where()`, `where_in()` and query-builder `update()` keys, plus JSON containment, key-existence, and array-length helpers with ORM parity. JSON paths are bound as PostgreSQL `text[]`; JSON path updates do not create missing intermediate parents.
- **Query Builder Unions & Row Locks:** Added `union()` / `union_all()` to `QueryBuilder`, plus `shared_lock()` / `lock_for_update()` on query builders and model queries. Row locks require an active transaction; unsupported lock combinations and union terminals raise `QueryError`.
- **Query Debugging:** Added `to_raw_sql()`, `dump()`, `dump_raw_sql()`, `dd()`, and `dd_raw_sql()` to `QueryBuilder` and `ModelQueryBuilder`, plus the exported `QueryDumpError`. Rendered SQL and logged bindings are for development/debugging only and must not be executed or used to build queries.

### Changed
- **Packaging & Scaffolding:** Bumped `swiftpy-framework` and the `swiftpy new` dependency floor to `0.3.6`.

## [0.3.5] - 2026-11-23

### Added
- **Proxy-Aware Client IP Detection (`swiftpy.http.client_ip`, `swiftpy.http.request`, `swiftpy.websockets.connection`, `swiftpy.auth.auth_manager`):**
  - Added pure stateless resolver module `swiftpy.http.client_ip` (`parse_trusted_proxies`, `extract_trusted_proxies_from_config`, `resolve_client_ip`) defaulting `http.trusted_proxies` fail-safely to `[]` via [`config/http.py`](config/http.py) (`TRUSTED_PROXIES`, `HTTP_TRUSTED_PROXIES`).
  - Added dual instance/class descriptor `request.ip()` and `Request.ip()` on `Request`, `WebSocketConnection.ip()`, WebSocket handshake `Request` context binding in `Application`, and unified `Auth` / `LoginThrottle` IP extraction via `req.ip() or "unknown"`.
  - Enforces strict CIDR validation (`strict=True`), IPv4-mapped IPv6 unwrapping (`::ffff:172.30.0.5` -> `172.30.0.5`), right-to-left `X-Forwarded-For` traversal with header/hop bounds (`4096` bytes / `32` hops) and port/zone-ID/hostname rejection, and conservative resolution to direct peer (without consulting `X-Real-IP`) whenever `X-Forwarded-For` is malformed, empty, or all-trusted.
- **Native Task Scheduler (`swiftpy.scheduling`):**
  - Added zero-dependency strict 5-field Vixie cron + IANA `ZoneInfo` engine (`cron.py`: macros, ranges, steps, lists, named month/weekday aliases, DOM/DOW OR rule, impossible calendar date rejection with `InvalidCronError`, DST spring-forward/fall-back handling with `fold=0` single firing, `UTC` default).
  - Separated persistent `ScheduleRecord` definitions from `ScheduleOccurrence` execution lifecycles (`pending` -> `claimed` -> `dispatch_pending` -> `dispatched` -> `running` -> `completed` | `failed` | `skipped` | `cancelled`).
  - Added fluent `Schedule` facade, `Scheduler` singleton, `scheduler()` helper, and builders (`cron`, `every_minute`, `every(N).seconds()/minutes()/hours()/days()`, `hourly`, `hourly_at`, `daily`, `daily_at`, `weekly`, `weekly_at`, `monthly`, `monthly_at`, `schedule(at=...)`, `delay(seconds=...)`, `@scheduler.task(...)`, `upsert()`, `update()`, `enable()`, `disable()`, `cancel()`, `get()`, `list_schedules()`, `get_occurrence()`).
  - Added durable `RedisScheduleStore` on `REDIS_QUEUE_DB` (`swiftpy_schedule:`) using atomic Lua scripts (`SAVE_SCHEDULE_LUA`, `CLAIM_DUE_LUA`, `RECLAIM_PENDING_LUA`, `CONFIRM_DISPATCHED_LUA`, compare-and-del `RELEASE_OVERLAP_LOCK_LUA`, monotonic state guards, bounded occurrence history + retention TTLs), plus non-durable process-local `MemoryScheduleStore` (`is_durable = False`, production `ConfigurationError` guard unless opted in) and typed `scheduler.*` lifecycle events (`ScheduleRegistered`, `ScheduledTaskDue`, `ScheduledTaskDispatched`, `ScheduledTaskSkipped`, `ScheduledTaskCompleted`, `ScheduledTaskFailed`).
  - Integrated `QueueWorker` (`running` / `completed` / `failed` hooks, retry overlap lock retention, DLQ lock release, standalone worker Redis prefix resolution), `FrameworkServiceProvider` (`schedule.auto_start = False` default, `SCHEDULE_AUTO_START=true`, clean loop cancellation on shutdown), and CLI commands `schedule:list [--app] [--json] [--prefix]`, `schedule:run [--app] [--limit] [--prefix]`, and `schedule:work [--app] [--sleep] [--max-ticks] [--prefix]`.

### Changed
- **Packaging, Configuration & Scaffolding:** Bumped `swiftpy-framework` and `swiftpy new` dependency floor to `0.3.5`, added [`config/http.py`](config/http.py) and [`config/schedule.py`](config/schedule.py), added `SCHEDULE_*` keys to [`.env.example`](.env.example), and updated `swiftpy new` templates accordingly.

### Fixed
- **`Application(trusted_proxies="a,b")`:** a comma-separated string passed validation but was stored in config as a single invalid entry, making every `request.ip()` (and therefore `Auth.attempt`) raise `ConfigurationError`. The normalized network list is now stored.
- **Scheduler wake-up cost:** the scheduler loop no longer lists every schedule per iteration; it reads the earliest due time through the new `ScheduleStore.next_due_at()` (`ZRANGE ... 0 0` on Redis) and backs off instead of spinning when overdue work cannot be claimed.
- **Overlap lock lifetime:** `without_overlapping()` locks are re-armed to their full `lock_ttl` when the worker starts the job, kept alive by a worker heartbeat while the job runs, and extended across retry backoff delays, so queue wait time or long runs no longer allow overlapping executions. Added `ScheduleStore.extend_overlap_lock()` (token-verified compare-and-pexpire).
- **Duplicate delivery:** `QueueWorker` acknowledges and skips a scheduled job whose occurrence has already completed. Delivery stays at-least-once; use `job.data["_swiftpy_schedule_occurrence"]` as an idempotency key for non-idempotent work.
- **Request/Scheduler internals:** `request.ip()` is memoized per request, `_instantiate_job_class` no longer relies on `assert`, and the production `memory` schedule-driver guard is shared through `ensure_memory_driver_allowed()`.

### Security
- **Login throttle (`LoginThrottle`):** IPv6 clients are bucketed by `/64` (IPv4-mapped IPv6 collapses to IPv4) so rotating addresses inside one prefix cannot bypass lockouts; the per-key generation counter now expires (`2 * (lockout_seconds + decay_seconds)`) instead of living forever; added optional identifier-wide cap across all IPs via `auth.throttle.identifier_max_attempts` / `identifier_window_seconds` (`AUTH_THROTTLE_IDENTIFIER_MAX_ATTEMPTS`, `AUTH_THROTTLE_IDENTIFIER_WINDOW_SECONDS`; disabled by default because it lets an attacker lock a known account for `lockout_seconds`).
- **Signed schedules:** schedule records now carry an HMAC-SHA256 `signature` (APP_KEY, previous keys accepted) over `task_kind`, `task_name`, `args`, `kwargs`, and `queue`. The signature is copied onto each occurrence and verified before a job is built; tampered or unsigned records (for example rewritten directly in Redis) are marked `failed` with `ScheduleSignatureError` and never pushed. Schedules registered before an `APP_KEY` was configured must be re-registered.

### Documentation
- Added dedicated Task Scheduler user guide [`documentations/2026-11-22-task-scheduling.md`](documentations/2026-11-22-task-scheduling.md) and updated Requests & Responses, WebSockets, Authentication & Security, Cache/Events/Queues, CLI & Testing, Installation Quick Start, [`docs/2026-11-14_documentation_plan.md`](docs/2026-11-14_documentation_plan.md), and [`README.md`](README.md) for `v0.3.5`.

## [0.3.1] - 2026-11-22

### Added
- **Credential Authentication Facade (`swiftpy.auth.auth_manager.Auth` & `AuthProxy`):**
  - Added `Auth.attempt(credentials, remember=False)` verifying non-password column conditions against the resolved `User` model (`auth.model` / `app.models.user.User`) with non-blocking `Hash.check_async()` (and constant-time dummy bcrypt execution when no user matches), automatically coercing `1`/`0` integers to Python `bool` for PostgreSQL `BOOLEAN` fields (e.g., `{"email": email, "password": password, "active": 1}`), issuing JWT pairs when configured, and binding task-scoped `Context` / `CurrentUser`.
  - Added `Callback` wrapper supporting closure conditions inside credentials dictionaries (`Callback(lambda q: q.has("activeSubscription"))` as key or callable value) and post-lookup inspection via `Auth.attempt_when(credentials, callback, remember=False)` / `attemptWhen(...)` for sync or async predicates.
  - Added persistent "Remember Me" cookie sessions (`remember=True` on `attempt`, `attempt_when`, `login`, `login_using_id`): `Blueprint.remember_token()` (`VARCHAR(100) NULL`), `User.remember_token: str | None = None` (`__hidden__` & `SENSITIVE_USER_KEYS`), HMAC-SHA256 signed `HttpOnly` `swiftpy_remember` cookies queued onto HTTP responses via `Application`, `require_jwt()` / passive cookie authentication, `Auth.via_remember()` / `viaRemember()`, and DB/cookie invalidation in `AuthProxy.logout()` / `logout_all()`.
  - Added `Auth.login(user, remember=False)`, `Auth.login_using_id(user_id, remember=False)` (plus `loginUsingId` and `login_sing_id` aliases), `Auth.once(credentials)`, and `Auth.once_using_id(user_id)` / `onceUsingId` (authenticating current task `Context` only with no cookies, no `remember_token` mutation, and no `Login` event).
  - Added `Request.cookies` and `Request.cookie(name, default=None)` in `swiftpy.http.request`.
- **Auth Lifecycle Events (`swiftpy.auth.events`):** Added `Attempting`, `Authenticated`, `Login`, `Failed`, `Logout`, and `Lockout` dataclasses dispatched as both typed event classes and `"auth.<name>"` dotted aliases (with sensitive credential fields stripped from event payloads).
- **Default Login Throttling (`swiftpy.auth.throttle.LoginThrottle`, `TooManyRequestsError`):** Added HTTP `429 TooManyRequestsError` (with `Retry-After` header) in `swiftpy.http.exceptions` / `swiftpy.core.exceptions` / `swiftpy.auth.exceptions` and enabled cache-backed `LoginThrottle` (`max_attempts=5`, `decay_seconds=60`, `lockout_seconds=60`) by default in `Auth.attempt()` and `Auth.attempt_when()`, with `Auth.too_many_attempts()`, `Auth.available_in()`, `Auth.clear_throttle()`, `config/auth.py` (`remember`, `throttle`), `.env.example`, and `swiftpy new` scaffolding updates.
- **ORM Relationship Existence Queries (`swiftpy.database.orm.model`):** Added `has()`, `or_has()`, `doesnt_have()`, `or_doesnt_have()`, `where_has()`, `or_where_has()`, `where_doesnt_have()`, and `or_where_doesnt_have()` on `ModelQueryBuilder` and `Model` supporting `HasOne`, `HasMany`, `BelongsTo`, `BelongsToMany`, `camelCase`/`snake_case` relation lookup, dot-nested relation chains, count operators, and NULL-safe PostgreSQL `NOT IN` compilation.

### Changed
- **Packaging & Scaffolding:** Bumped `swiftpy-framework` to `0.3.1`, refreshed `uv.lock`, updated `create_users_table` migration and `app/models/user.py`, and updated `swiftpy new` templates to emit `"swiftpy-framework>=0.3.1"`, `table.remember_token()`, `User.remember_token`, and default `remember` / `throttle` config sections.

### Fixed
- **`JWTIssuer` Class-Level Facade Calls (`swiftpy.auth.jwt_issuer.JWTIssuer`):** Fixed a `v0.3.0` regression where calling `await JWTIssuer.refresh_token_pair(...)` (or `issue_token`, `issue_access_token`, `issue_refresh_token`, `issue_token_pair`, `decode_token`) directly on the `JWTIssuer` class raised `TypeError: missing 1 required positional argument: 'self'`. Direct class calls now resolve the active container's `JWTIssuer` singleton (or construct one from `Config` without global state) while preserving bound instance calls (`issuer.refresh_token_pair(...)`).

### Documentation
- Updated Authentication & Security, Active Record ORM & Relationships, Database Schema & `Blueprint`, Installation Quick Start, and `README.md` for `v0.3.1`.

## [0.3.0] - 2026-11-22

### Added
- **Routing & Controller DI Ergonomics (`swiftpy.http.router`, `swiftpy.core.application`):** Added dual decorator + direct handler binding across `Router`, `RouteGroup`, and `Application` (`router.get("/posts", PostController.index)`, `@router.get("/posts")`, `app.get("/posts", PostController.index)`), plus automatic per-request container resolution for unbound controller methods (`controller_cls` resolved once cleanly at compile/dispatch and instantiated isolated per request). Rejects passing raw controller classes directly with `RouteRegistrationError`.
- **Prompt Versioning & `messages` Templates (`swiftpy.ai.prompts`, `swiftpy.ai.client`):** Added `Prompt` facade (`Prompt.load("summarise.v2", ...)`, `await Prompt.aload(...)`, `Prompt.load_messages(...)`, `Prompt.aload_messages(...)`), module-level `messages` list support (`.py` sequences or zero-arg callables of role/content dicts or `Message` objects), `ai.prompts.path` config resolution, multi-message appending in `AIClient.prompt()` / `AIClient.aprompt()`, compiled cache round-tripping for `messages` lists and dotted versioned names, and exports via `swiftpy.ai.prompts`, `swiftpy.ai`, and top-level `swiftpy`.
- **CLI AI Generators (`make:agent`, `make:tool`, `make:prompt`):** Added `agent.stub`, `tool.stub`, `prompt.stub` and `swiftpy make:agent`, `swiftpy make:tool`, and `swiftpy make:prompt` (supporting nested folders and versioned `.v2` filenames such as `summarise.v2` with strict path-traversal and extension guards).
- **Starter Scaffolding Alignment (`swiftpy new`):** Scaffolded `app/ai/{agents,tools,pipelines,providers,vectorstores,observers}/` (`AssistantAgent` and `SystemStatusTool`), `prompts/{summarise.py,classify.py}`, `UserController` + `UserResource` + `UserResponseSchema` -> `routes/api.py` (`router.get("/users", UserController.index)`), and `config/ai.py` (`prompts.path`, `prompts.cache`, `http`).

### Changed
- **Stubs & Resource Invariance (`swiftpy/console/stubs/`):** Refined `controller.stub`, `service.stub`, `repository.stub`, `schema.stub`, and `resource.stub` (typing `collection(cls, items: Iterable[Any])` for `mypy --strict` variance compatibility and removing unused imports).
- **Packaging:** Bumped `swiftpy-framework` package version and `swiftpy new` dependency floor to `0.3.0`.

### Removed
- **Admin Dashboard (`swiftpy.admin`, `views/admin/`):** Completely removed the built-in HTML Admin Dashboard package (`swiftpy/admin/`), admin HTML templates (`views/admin/`), `AdminSite`/`ModelAdmin`/`AdminSessionStore` exports and scaffolding references, while preserving `app/http/middleware/admin_middleware.py` (`is_admin` API guard) and `views/mail/template.html`.

### Documentation
- Added [`documentations/2026-11-21-application-architecture-and-project-structure.md`](documentations/2026-11-21-application-architecture-and-project-structure.md) covering the full layered `app/` architecture, `Request` vs. `Schema` vs. `Resource` vs. `Model`, Patterns A–D + feature grouping, `app/ai/` and `prompts/`, decision table, Golden Rule, and verified CRUD walkthrough.
- Updated Installation Quick Start, HTTP Routing, Application Schemas, AI Subsystem, CLI & Testing, Authentication, Filesystem/Mail, CORS, WebSockets, `README.md`, and `docs/2026-11-14_documentation_plan.md` for `v0.3.0`.

## [0.2.2] - 2026-11-21

### Added
- **ORM (`Model.update` & `Model.force_update`):** Added async `update()` and `force_update()` methods on `Model` instances accepting either a mapping (`await user.update({"name": "Alice"})`) or keyword attributes (`await user.update(name="Alice")`). Respects `__fillable__` / `__guarded__`, updates `updated_at`, short-circuits cleanly on unchanged attributes, fires `updating` / `updated` events, and raises `PrimaryKeyError` on unsaved instances. Existing query-builder `Model.where(...).update(...)` remains unchanged.
- **Helpers (`isset()`):** Added `isset(data, *keys)` in `swiftpy.helpers` (and re-exported from top-level `swiftpy`) to test non-`None` key, index, dot-path, and attribute existence across mappings, sequences, sets, `Request`, `FormData`, `FormRequest` (inspecting only actual form input), `ValidatedData`, `Config`, loaded `Model` fields/relations (without triggering lazy loads on unloaded relations or exposing `_`-prefixed private attributes), and generic Python objects.
- **HTTP (`Request.has_file`):** Added `request.has_file(name)` returning a dual sync/awaitable result (`if request.has_file("image"):` and `if await request.has_file("image"):`, plus direct boolean serialization in `encode_json` / `json_response`) with targeted multipart pre-loading when `Application` detects `has_file` usage on a route, while keeping non-`has_file` multipart routes lazy (`multipart_body_calls == 0`).

### Changed
- **Auth (`JWTIssuer` / Config / Scaffolding):** Increased default `JWT_ACCESS_TTL` / `access_token_ttl` from `900` (15 minutes) to `3600` seconds (60 minutes) across `JWTIssuer`, `config/auth.py`, `.env.example`, and `swiftpy new` templates while retaining `JWT_REFRESH_TTL=604800` (7 days), and added strict positive-integer validation for `access_token_ttl` and `refresh_token_ttl`.
- **Routing (`MethodNotAllowedError`):** Updated 405 error detail wording in `Router` from `"Method '...' not allowed for path '...'"` to `"Method '...' not allowed for route '...'"` to match framework routing terminology.
- **Packaging & Scaffolding:** Bumped `swiftpy-framework` package version and `swiftpy new` dependency floor to `0.2.2`.

### Documentation
- Added dedicated CORS guide [`documentations/2026-11-21-cors.md`](documentations/2026-11-21-cors.md) covering `CORSMiddleware` / `CORS`, `config/cors.py` and `.env` / `CORS__*` configuration, global vs. `Router`/`RouteGroup` attachment (including ordering ahead of `JWTGuard`), preflight `OPTIONS` handling, `Vary: Origin`, exact origin matching, wildcard + credentials (`G-3`) enforcement, and `TestClient` verification; cross-linked from [the HTTP routing guide](documentations/2026-11-15-http-routing-middleware-and-exceptions.md) and [README.md](README.md).
- Updated ORM, Auth, Core Helpers, HTTP Routing, Request/Response, Filesystem Upload, and Installation guides for `v0.2.2`.

## [0.2.1] - 2026-11-20

### Fixed
- **`CurrentUser` and `auth()` after an access token is issued (`swiftpy.auth`):** `JWTIssuer.issue_token(token_type="access")`, `issue_access_token()`, `issue_token_pair()` and `refresh_token_pair()` now bind the user into the request `Context` and `request.state`, so an already-injected `CurrentUser` and `auth().check()` / `id()` / `user()` work immediately (for example inside `/login` or `/refresh`). Refresh tokens never bind a user, and issuing a token for a different subject never replaces an already-authenticated user.
- `FrameworkServiceProvider` now registers `CurrentUser` as a scoped binding, so injecting `CurrentUser` works with `auto_discover_providers=False`.

### Added
- `JWTIssuer` token methods accept a saved `Model`, a mapping with `id`/`sub`, or a plain id as `subject`, plus an optional `user=` keyword (`Model` or mapping). A `user=` that does not match the subject, or a model without a primary key, raises `ValueError`. Sensitive keys and `__hidden__` fields are never bound.
- Opt-in `auth.hydrate_user` (`AUTH_HYDRATE_USER`, default `false`): `require_jwt()` / `JWTGuard` load the User model by `sub` (one query per request, degrading to token claims with a warning on database errors) so sub-only tokens expose every non-hidden field.
- Opt-in `auth.passive_resolution` (`AUTH_PASSIVE_RESOLUTION`, default `false`): routes without a guard resolve a valid Bearer access token into `CurrentUser` / `auth()` when the default guard is `jwt`. Revoked, expired or invalid tokens stay guests.
- New internal module `swiftpy.auth.user_context` shared by `JWTIssuer`, `require_jwt()` and `AuthManager`; `resolve_user_model_class` is still exported from `swiftpy.auth`, `swiftpy.auth.auth_manager` and `swiftpy.auth_manager`.

### Changed
- `require_jwt()` now returns the bound user dictionary (token claims plus model fields when hydrated) instead of the raw claims dictionary.
- Generated projects (`swiftpy new`) include `hydrate_user` / `passive_resolution` in `config/auth.py`, matching `.env` keys, and depend on `swiftpy-framework>=0.2.1`.

### Documentation
- Updated [`documentations/2026-11-15-authentication-and-security.md`](documentations/2026-11-15-authentication-and-security.md) with issuing-time context binding, `hydrate_user`, `passive_resolution`, and the `CurrentUser.id` vs `auth().id()` typing note.

## [0.2.0] - 2026-11-19

### Added
- **Structured Asynchronous Logging (`swiftpy.logging` / `Log`):**
  - Added the `swiftpy.logging` package (`Log`, `LogManager`, `ChannelLogger`, `DEFAULT_LOG_FORMAT`, `parse_log_level`, `resolve_log_path`) and exported `Log` from top-level `swiftpy`.
  - Wrapped concrete handlers (`single`, `daily` via `TimedRotatingFileHandler`, `stderr` supporting `"stderr"`/`"stdout"`/writable streams, and recursive cycle-checked `stack`) behind bounded `queue.Queue` (`queue_size=10_000` default) + `QueueHandler` + `QueueListener(..., respect_handler_level=True)` so request coroutines never block on file I/O.
  - Added automatic recursive redaction of sensitive credential keys (`password`, `secret`, `token`, `api_key`, `authorization`, `pepper`, etc.) in structured `Log` keyword context.
  - Wired `LogManager` startup first (with failure rollback) and shutdown flush/stop last in `FrameworkServiceProvider`, with per-`Application` isolation for internal channel loggers and framework `swiftpy.*` records.
- **Validation Ergonomics (`swiftpy.validation`):**
  - Added `ValidatedData` (`dict[str, Any]` subclass with recursive dot attribute access and dict compatibility, keeping bracket access safe for keys matching `dict` method names).
  - Updated `validate(data_or_request, schema, ...)` to extract JSON, form, or query input automatically when given a `Request`, added `ValidationResult.raise_if_failed()`, `ValidationResult.first_errors()`, and `stop_on_first_error` support on both `validate()` and `FormRequest`.
  - Added `ValidationException.first_field()`, `first_error()`, `first_errors()`, `is_single`, and `single()` returning single-error `{"message": ..., "field": ...}` responses cleanly.
- **HTTP Request, Router & Exception Ergonomics (`swiftpy.http` / `swiftpy.core`):**
  - Added `Request.scheme`, `Request.input()`, `Request.all()`, `Request.get()`, `Request.has()`, and dynamic `request.<field>` attribute lookup over pre-loaded non-multipart input (`Application(..., auto_load_request_input=True)` with opt-out for raw proxy routes, safe pre-load fallback on non-JSON raw webhook bodies, and reserved-attribute protection preventing client input from spoofing `user`, `auth`, `session`, `is_admin`, etc. via `getattr`/`hasattr`).
  - Added `Router(prefix=..., middleware=...)` constructor configuration (validating dynamic `{...}` segments eagerly and composing router middleware cleanly across `include_router`).
  - Added declarative `HttpException` class defaults (`status_code`, `message`) and the `abort(status_code, message=None, *, headers=None)` helper exported from `swiftpy`, `swiftpy.http`, `swiftpy.http.exceptions`, and `swiftpy.core.exceptions`.
- **Database Migrations (`Blueprint`):**
  - Added `Blueprint.enum(name, values)` compiling PostgreSQL `VARCHAR` columns with named `CHECK` constraints, SQL literal escaping, and enum default validation, plus `Blueprint.medium_text(name)` and `Blueprint.long_text(name)` aliases mapped to PostgreSQL `TEXT`.
  - **PostgreSQL design note:** Column positioning (`after()`) is intentionally not offered on `ColumnDefinition` because PostgreSQL `ALTER TABLE ... ADD COLUMN` appends new columns and does not support positional insertion; `Schema.rename` is similarly omitted from `Schema`.
- **Authentication Token Refresh & Session Revocation (`swiftpy.auth` / `swiftpy.admin`):**
  - Added `JWTIssuer.refresh_token_pair()` with automatic refresh token verification, `TokenBlacklist` checks, atomic Redis `SET ... NX` rotation (`TokenBlacklist.revoke_if_not_revoked()`, `rotate=True` by default), and `TokenRevokedError` (inheriting from both `AuthenticationError` and `JWTError`, HTTP 401) on reuse or revocation.
  - Added `AuthProxy.logout()` and `AuthProxy.logout_all()` on `auth()`, plus `AdminSessionStore.flush()` epoch-based in-memory admin session invalidation.

### Changed
- **Models & Ruff Defaults:** Removed explicit `ClassVar` boilerplate from `app/models/user.py`, `swiftpy/console/stubs/model.stub`, and `swiftpy new` model templates; added per-file `"app/models/**/*.py" = ["RUF012"]` ignore in `pyproject.toml` and scaffolded project templates.
- **Scaffolding (`swiftpy new`) & Config:**
  - Shortened generated `app/http/handler/exceptions.py` to demonstrate declarative `HttpException` subclasses without boilerplate handler registration.
  - Added the `"stderr"` channel to `config/logging.py` and `swiftpy new` logging config templates.
  - Bumped generated `pyproject.toml` dependency floor to `swiftpy-framework>=0.2.0` (`tests/console/test_phase9_cli_remediation.py` updated accordingly).

### Verified
- Added PostgreSQL integration tests in `tests/database/test_sequence_gaps.py` verifying non-transactional `BIGSERIAL` / `GENERATED BY DEFAULT AS IDENTITY` sequence gap behavior after failed unique inserts and rolled-back transactions without data loss.

### Documentation
- Added six dedicated user guides in `documentations/`: `2026-11-20-request-and-responses.md`, `2026-11-20-error-handling.md`, `2026-11-19-application-schemas.md` (`app/schemas/`), `2026-11-19-database-schema-and-blueprint.md`, `2026-11-20-logging.md`, and `2026-11-19-cli-and-testing.md`.
- Updated existing guides (`2026-11-14-installation-quick-start.md`, `2026-11-15-core-application-di-and-config.md`, `2026-11-15-http-routing-middleware-and-exceptions.md`, `2026-11-15-validation-and-form-requests.md`, `2026-11-15-database-query-builder-and-migrations.md`, `2026-11-15-orm-models-and-relationships.md`, `2026-11-15-authentication-and-security.md`, `2026-11-15-ai-subsystem.md`), `README.md`, and `docs/2026-11-14_documentation_plan.md` (linking all 17 sequential guides and replacing nonexistent `StorageFake` references with `VectorStoreFake`).

## [0.1.6] - 2026-11-17

### Added
- **Authentication Proxy (`swiftpy.auth_manager` / `auth()`):**
  - Added Laravel-style `auth()` / `auth(guard)` request-scoped proxy importable via `from swiftpy.auth_manager import auth` in any file and re-exported from `swiftpy.auth`.
  - Added `auth().check() -> bool` returning whether the current request task has an authenticated user.
  - Added `auth().guest() -> bool` returning whether the current request task is unauthenticated.
  - Added `auth().id() -> Any | None` returning the primary key identifier of the authenticated user.
  - Added `auth().user() -> AuthUser | None` returning an entity with all declared `User` model fields present and all `__hidden__` fields strictly excluded. Supports both attribute access (`user.id`, `user.name`, `user.email`, `user.is_admin`) and dict access (`user["id"]`), `dict(user)`, `json.dumps(user)`, and `user.to_dict()`.
  - Added guard-type selection via `auth("jwt")`, `auth("api_key")`, etc., enforcing guard isolation when a specific guard is requested.
  - Added `set_user_model(model, guard=None)` and `current_user_model()` helpers to bind authenticated ORM Model instances into task-scoped Context.
  - Enhanced `CurrentUser` DI container binding with `__getattr__` attribute access support across declared user attributes.
  - Exported `AuthProxy`, `AuthUser`, `auth`, `set_user_model`, and `current_user_model` across `swiftpy.auth_manager`, `swiftpy.auth`, and top-level `swiftpy`.


## [0.1.5] - 2026-11-16

### Fixed
- **CLI (`swiftpy storage:link`):** Fixed `StorageCommand.handle_link` (`swiftpy/console/commands/storage.py`) to compute a relative symlink target (`../storage/app/public` with absolute fallback across drives) and resolve `link_path` without dereferencing an existing `public/storage` symlink (preventing `--force` from deleting target files in `storage/app/public`).

### Changed
- **Scaffolding (`swiftpy new`):**
  - Added `public/storage` to generated `.dockerignore` and `.gitignore` templates (and repository root `.dockerignore`/`.gitignore`) to prevent Windows Docker Desktop BuildKit context failures (`invalid file request public/storage`) when bind-mounting `.:/app`.
  - Updated generated `pyproject.toml` dependency floor to `swiftpy-framework>=0.1.5`.
- **Documentation (`documentations/2026-11-14-installation-quick-start.md`):** Added project upgrade instructions for Dockerized and local virtual environments, including Windows Docker Desktop `public/storage` bind-mount troubleshooting.

## [0.1.4] - 2026-11-16

### Added
- **Password Hashing (`swiftpy.support.hashing` / `Hash`):**
  - Added the Laravel-style `Hash` support class with sync-first class methods (`Hash.make()`, `Hash.check()`, `Hash.needs_rehash()`) and non-blocking async methods (`Hash.make_async()`, `Hash.check_async()` via `asyncio.to_thread`).
  - Added convenience helpers `hash_password()`, `verify_password()`, `hash_password_async()`, and `verify_password_async()` exported from `swiftpy`, `swiftpy.auth`, `swiftpy.support`, and `swiftpy.helpers`.
  - Standardized bcrypt secret preparation in `prepare_bcrypt_secret()` (shared with `ApiKeyGuard`) to preserve raw UTF-8 `$2b$` compatibility up to 72 bytes and SHA-256 pre-hash secrets exceeding 72 bytes.
  - Added configurable `auth.hash.rounds` (`BCRYPT_ROUNDS`, default `12`) in `config/auth.py` and `swiftpy new` config templates, and registered `Hash` as a singleton in `AuthServiceProvider`.

### Changed
- **Scaffolding (`swiftpy new`):** Updated generated `pyproject.toml` dependency floor to `swiftpy-framework>=0.1.4`, updated generated `config/auth.py` and `app/providers/auth_service_provider.py` with bcrypt rounds config and `Hash` singleton binding, and updated `documentations/2026-11-14-installation-quick-start.md`.
- **Documentation (`documentations/2026-11-14-installation-quick-start.md`):** Updated Track 2 scaffolding instructions to use `uvx` or an isolated virtual environment (with `python -m swiftpy.console.cli` fallback) so global/user site-packages are not polluted and Windows `Scripts` `PATH` errors are avoided.

## [0.1.3] - 2026-11-16

### Changed
- **Scaffolding (`swiftpy new`):**
  - Updated generated `main.py` with a root `GET /` handler returning JSON status alongside `register_exception_handlers(app)` and `/api` routing.
  - Added `DB_HOST: postgres` and `REDIS_HOST: redis` under the `app` service in generated `docker-compose.yml` so containerized runs connect to Compose services automatically without modifying `.env`.
  - Updated generated `pyproject.toml` dependency floor to `swiftpy-framework>=0.1.3`.
- **Documentation (`documentations/2026-11-14-installation-quick-start.md`):** Reorganized quick start into clear tracks: minimal app without scaffolding first, followed by scaffolded setup with separate fully Dockerized and Docker-free instructions.

## [0.1.2] - 2026-11-16

### Added
- **CLI (`swiftpy -v` / `swiftpy --version`):** Added `-v` and `--version` flags to `CLI` reporting the installed `swiftpy-framework` version (with `pyproject.toml` fallback).

### Changed
- **Scaffolding (`swiftpy new`):**
  - Removed hardcoded fallback database names, users, and passwords from generated `docker-compose.yml` so `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `pg_isready` `healthcheck` directly reference `${DB_DATABASE}`, `${DB_USERNAME}`, and `${DB_PASSWORD}` from `.env`.
  - Added top-level `name: <project_name>` to generated `docker-compose.yml` and initialized `DB_DATABASE=<project_name>` in generated `.env`, `.env.example`, and `config/database.py`.
  - Updated generated `pyproject.toml` dependency floor to `swiftpy-framework>=0.1.2`.

## [0.1.1] - 2026-11-15

### Fixed
- **CLI (`swiftpy start`):** Added `ensure_project_in_sys_path()` in `swiftpy.console.loader` and invoked it in `StartCommand.handle()` so `Path.cwd()` is placed at `sys.path[0]` before launching Uvicorn. Fixes `Error loading ASGI app. Could not import module "main".` when running `swiftpy start`, `swiftpy start --reload`, or `swiftpy start --workers <N>` from a wheel-installed `swiftpy` console entrypoint.
- **Scaffolding (`swiftpy new`):** Updated generated `pyproject.toml` dependency floor to `swiftpy-framework>=0.1.1`.

### Audit Remediation & Approved Breaking Changes (`G-1`–`G-16`)
- **Packaging, Core, & HTTP (`G-1`, `G-2`, `G-16`):**
  - **Policy Update (`G-1`):** `swiftpy/ai` (LLM clients, embeddings, vector stores, RAG pipelines, agents, tools, prompts, and observability) is fully implemented and included in the main `swiftpy-framework` v0.1.0 wheel (superseding the earlier `P10-L7` wheel exclusion); `swiftpy/__init__.py` uses PEP 562 lazy exports so lightweight imports do not pull heavy optional submodules eagerly, and `swiftpy.testing` remains documented as a dev-only test utility.
  - **Breaking (`G-2`):** `config/app.py` defaults `app.debug` (`APP_DEBUG`) to `False`, and explicit `Application(**kwargs)` overrides always take precedence over environment/cache defaults.
  - **Breaking (`G-16`):** Removed dead unbootstrapped `RouteCache` (`swiftpy cache:build` precompiles `config.json` and `prompts.json`), enabled automatic JSON serialization when route handlers return `dict` or `list` values, and preserved `.gitignore` exclusion of `/docs/` and `AGENTS.md`.
- **Security, Starter App, Admin, & WebSockets (`G-3`, `G-4`, `P1-S1`–`P1-S5`, `P2-*`, `P3-A1`):**
  - Removed unauthenticated `/benchmarks/token`, gated benchmark routes behind `APP_ENABLE_BENCHMARKS` (`app/http/benchmarks.py` relocated to `tests/benchmarks.py`), and hardened starter app auth/token handling.
  - **Breaking (`G-3`):** `CORSMiddleware` fails closed when combining wildcard `"*"` with `allow_credentials=True` (`ConfigurationError`), `CORS_ALLOW_CREDENTIALS` uses strict boolean parsing, and Redis DB defaults align with `.env.example`.
  - **Breaking (`G-4`):** Production `AdminAuthenticator` rejects default `"admin123"` credentials and missing/short secrets, enforces `is_admin` and non-empty `session_id`, requires `POST` + CSRF validation on logout, bounds revoked sessions, and escapes `OR` search filters.
  - Isolated nested `Container.scope()` and `CurrentUser`, ensured `StreamingResponse` closes inside request context and stops on `HEAD`, prevented `OPTIONS` 500 on braced paths, and hardened WebSocket `urlsplit` origin parsing and distributed broadcast error handling.
- **Database, ORM, & Migrations (`G-6`, `P4-*`):**
  - Hardened `having()`, `where_null()`, and `select_raw()` against invalid/injected SQL identifiers, fixed `on_commit` ordering after transaction close, fixed ORM `create()` defaults, JSON list persistence, custom `__primary_key__`, dirty-hook updates, and `BelongsToMany` transaction propagation.
- **Queue, Cache, & Events (`G-5`, `P5-*`):**
  - **Breaking (`G-5`):** Queue and `RedisEventBus` HMAC signing require explicit `>= 32`-byte `APP_KEY` and reject `"tests"` / `test*` job namespaces by default (use explicit `Job.allow_job_class(...)` or configured trusted namespaces).
  - Fixed `_REAP_LEASES_LUA` signature & empty `args=[]` preservation, DLQ list capping (`failed_max_size`), worker concurrency/cancellation/shutdown cleanup, `MemoryDriver` bounded LRU sweeping, single-flight `RedisDriver.remember`, and `EventDispatcher` `**kwargs` + `forget()`.
- **Filesystem, Mail, & Validation (`G-7`, `G-8`, `G-9`, `P6-*`):**
  - **Breaking (`G-7`):** Enforced upload `Sequence` & magic-byte validation, raised on S3 listing HTTP failures, required signing keys for local temporary URLs, and enforced symlink containment.
  - **Breaking (`G-8`):** Propagated `SendQueuedMailableJob` exceptions to worker DLQ handling, enforced mailable allowlists, and standardized sandboxed Jinja2 mail templates on the `views/` root (`MailTemplateRenderer(template_dir="views")`).
  - **Breaking (`G-9`):** Hardened `FormRequest` dynamic `rules()` / `messages()` / multipart / `SAVEPOINT` / `regex:` handling with strict schema failure reporting and canonical coercion ordering.
- **AI, Vector Stores, RAG, Agents, Tools, & Prompts (`G-10`–`G-15`, `P7-*`, `P8-*`):**
  - **Breaking (`G-10`–`G-15`):** Enforced fail-closed missing `OPENAI_API_KEY` in `EmbeddingsClient`, longest-prefix model pricing, split UTF-8 SSE decoding, LanceDB merge/append upsert and safe delete filters, Pinecone SSRF validation, `AI.vectorstore("pgvector")`, `<= 63`-byte PostgreSQL HNSW index names, per-item RAG ingest validation + reranker ordering, strict `BaseAgent` tool/budget enforcement, session locks, strict `BaseTool` nested schema / `WebSearchTool` checks, and `base_dir`-scoped `PromptLoader` compiled caches.
- **Testing, CLI, CI/CD, & Hygiene (`P9-*`, `P10-*`):**
  - Aligned `TestClient` query/non-ASCII/`Set-Cookie`/lifespan parity; updated `swiftpy new` scaffolding (`Dockerfile`, `storage:link`, `WelcomeEmail`) and `swiftpy queue:failed`/`retry`/`flush` (`--app`, resource cleanup, bounded limits); added `--timeout-method=thread` reliability, SHA-pinned CI/publish GitHub Actions, isolated clean-venv wheel smoke gate, `${VAR:?}` Docker Compose secrets, `pip-audit`, zero `# type: ignore` comments & zero inline imports across `swiftpy/` and `tests/`, and updated `README.md` + `tests/docs/test_readme_examples.py`.

### Phase 1 – Shared Foundations
- **P1-F1 (Security / Database):** Consolidated SQL identifier validation into `swiftpy.database.identifiers` (`validate_identifier`, `validate_column_identifier`, `quote_identifier`) using `re.fullmatch` and enforcing PostgreSQL's 63-character limit (`MAX_IDENTIFIER_LENGTH = 63`). Rejected trailing newlines (`"users\n"`), whitespace, and wildcards (`"*"`) in `WHERE`, `JOIN`, `ORDER BY`, `INSERT`, and `UPDATE` column identifiers across `QueryBuilder`, migrations (`Blueprint`, `Schema`, `Migrator`), validation rules (`UniqueRule`, `ExistsRule`), and `ApiKeyGuard`.
- **P1-F2 (Support / ORM):** Extracted `snake_case` and `pluralize` into `swiftpy.support.strings` (re-exported from `swiftpy.helpers` and `swiftpy.helpers.helper` for backward compatibility) to eliminate circular import coupling with the ORM.
  - **Breaking:** Fixed acronym handling in `snake_case` so consecutive uppercase prefixes stay grouped (`snake_case("APIKey") == "api_key"`, `snake_case("HTTPServer") == "http_server"`). Models with acronym class names that do not declare an explicit `__table__` now infer pluralized snake_case table names accordingly (e.g. `APIKey` infers `api_keys` instead of `a_p_i_keys`). Set `__table__` explicitly on models that need the legacy table name.
- **P1-F3 (Support / Redis Config):** Added `swiftpy.support.redis_config` (`build_redis_url`, `resolve_redis_params`) to resolve Redis connection settings exclusively from `Config` with RFC-compliant URL percent-encoding (`urllib.parse.quote`) for passwords and usernames, while preserving subsystem-specific database overrides (`REDIS_QUEUE_DB`, `REDIS_CACHE_DB`, `REDIS_EVENTS_DB`).

### Phase 2 – Security Blockers
- **P2-A1 (Security / Auth – JWT Secret Enforcement):**
  - **Breaking:** `JWTIssuer` now requires an explicit `secret` of at least 32 bytes for symmetric algorithms (`HS256`) or a valid key pair for `RS256`, raising `ConfigurationError` otherwise. Removed the hardcoded fallback secret from `config/auth.py`. In non-debug mode (`APP_DEBUG=false`), `app.boot()` (when the default guard is `jwt`) and `JWTIssuer.from_config()` fail fast with `ConfigurationError` when `JWT_SECRET` is unset; in debug mode, an ephemeral per-call random secret is generated (cached per application via the container singleton) with a security warning.
  - Wired `issuer`, `audience`, and `leeway` into `config/auth.py` and `JWTIssuer.from_config()`.
- **P2-A2 (Security / Auth – API Key Guard Redesign):**
  - **Breaking:** Redesigned API keys around a `<prefix>.<secret>` format (`generate_api_key`, `hash_api_key`, `verify_api_key_hash`) backed by a single indexed `WHERE "<prefix_column>" = $1 LIMIT 1` lookup and constant-time HMAC-SHA256 verification with `AUTH_KEY_PEPPER` (requiring at least 32 bytes; in non-debug mode, `app.boot()` and `get_server_pepper()` fail fast with `ConfigurationError` when `AUTH_KEY_PEPPER` is unset or shorter than 32 bytes). Legacy unsegmented API keys without a `<prefix>.<secret>` separator are rejected upfront with `InvalidApiKeyError` (HTTP 401), and the full-table scan fallback has been removed. Existing unsegmented keys must be re-issued using `swiftpy make:api-key`.
  - Legacy `$2...` bcrypt hashes on prefix-matched rows are verified inside `loop.run_in_executor` so the asyncio event loop is never blocked.
  - Added `ApiKeyGuard.from_config()`, active transaction connection reuse via `get_active_connection()`, stripping of `key_hash`/`secret`/`password` fields from `request.state["user"]` and `Context`, `swiftpy make:api-key` CLI command, and `migration.api_keys.stub`.
- **P2-A3 (Security / Auth – Token Revocation Hardening):**
  - **Breaking:** Removed `JWTIssuer.decode_unverified()`. `TokenBlacklist.revoke_token()` now requires signature verification (accepting an explicit `issuer`, a constructor-injected `JWTIssuer`, or resolving `JWTIssuer` from the container) and rejects unverified or tampered tokens before writing to Redis.
  - Fixed `issue_token_pair()` so custom access-token `claims` are no longer copied onto the refresh token and header `typ` matches the token type (`"access"` / `"refresh"`).
  - Updated `TokenBlacklist` to resolve Redis settings via `swiftpy.support.redis_config.resolve_redis_params` and validate finite numeric `exp` claims.
- **P2-A4 (Security / Auth – Generic Auth Error Messages):**
  - **Breaking:** Client-facing `JWTGuard`, `ApiKeyGuard`, and `TokenRevokedError` HTTP 401 responses now return generic messages (`"Invalid or expired token"` / `"Invalid API key"`) without leaking expected `iss`/`aud` values, revocation timestamps, or raw error internals. Backend Redis/DB outages raise `AuthenticationServiceUnavailableError("Authentication service unavailable")` (HTTP 503) while logging details server-side via `logger.warning` / `logger.exception` without raw tokens.
- **P2-A5 (Security / Auth – JWT Hardening & Tidy-Up):**
  - **Breaking:** `JWTIssuer.issue_token()` now raises `ValueError` if `claims` attempts to override any reserved claim (`sub`, `exp`, `iat`, `jti`, `type`, `typ`, `nbf`, `iss`, `aud`). Algorithm validation enforces exact case matching (`"hs256"` is rejected while all case variants of `"none"` remain forbidden).
  - Enforced future `iat` and `nbf` validation with configurable `leeway` (seconds), rejected boolean (`True`/`False`) and non-finite (`NaN`, `Infinity`) timestamps, added `aud` list support, treated `expires_in=0` explicitly, validated `expires_at` column types in `ApiKeyGuard`, and removed inline imports across `swiftpy/auth/`.
- **P2-CLI1 (Security / CLI – `make:*` Path Containment & Identifier Validation):**
  - Hardened `swiftpy make:*` commands (`make:model`, `make:migration`, `make:service`, `make:request`, `make:resource`, `make:repository`, `make:schema`, `make:api-key`) against path traversal, absolute paths, symlink escapes (`Path.resolve().is_relative_to(...)`), invalid Python identifier segments, and invalid SQL table identifiers (`validate_identifier`).

### Phase 3 – Database Correctness and Safety
- **P3-D1 (Database / ORM – Non-Mutating Terminal Query Methods):**
  - Added `QueryBuilder.clone()` and `ModelQueryBuilder.clone()`.
  - `count()`, `sum()`, `avg()`, `min()`, `max()`, `exists()`, and `first()` now execute on a cloned builder so calling terminal methods never mutates `_columns`, `_limit`, or `_order_column` on a shared `QueryBuilder` or `ModelQueryBuilder` instance.
  - Cleared `ORDER BY` on aggregate and `EXISTS` subqueries so PostgreSQL never raises `GroupingError` when `.order_by()` was chained prior to `.count()`, `.exists()`, or aggregate helpers.
  - Added `.exists()` on `QueryBuilder`, `ModelQueryBuilder`, and `RelationshipQueryBuilder` (emitting `SELECT EXISTS(SELECT 1 FROM ... LIMIT 1)`), and added `.sum()`, `.avg()`, `.min()`, and `.max()` delegating methods on `ModelQueryBuilder`.
- **P3-D2 (Database / ORM – Arbitrary Primary Keys & Single-Query `RETURNING` Hydration):**
  - Added `QueryBuilder.insert_returning(data, *, returning=None) -> dict[str, Any]` and updated `QueryBuilder.insert(data, *, returning=None)` to support custom primary key columns while preserving `int` return values for default `id` callers.
  - `Model.create()` and `Model.save()` (on insert) now hydrate directly from `INSERT ... RETURNING *` in **1 query** instead of issuing a second `SELECT` via `cls.find()`, supporting `int`, `UUID`, and custom string primary keys (`__primary_key__ = "code"`), as well as caller-supplied primary keys on new instances.
  - Added `PrimaryKeyError`: `Model.save()` on an existing model with `pk is None` raises `PrimaryKeyError`, and declaring a composite or non-string `__primary_key__` on a `Model` subclass raises `PrimaryKeyError` at class definition time.
- **P3-D3 (Database / ORM – Unguarded Mass-Write Protection & Valid SQL Compilation):**
  - **Breaking:** `QueryBuilder.update()`, `QueryBuilder.delete()`, `ModelQueryBuilder.update()`, and `ModelQueryBuilder.delete()` now raise `QueryError` when invoked without any `WHERE` clause unless `.allow_all()` is explicitly chained first.
  - `insert({})` and `update({})` now raise `QueryError` instead of emitting invalid empty SQL.
  - `WHERE col IN []` now compiles to `FALSE` and `WHERE col NOT IN []` compiles to `TRUE` instead of invalid `IN ()`; passing a non-list/non-`QueryBuilder` to `IN`/`NOT IN` raises `QueryError`.
  - Passing `None` with comparison/pattern/set operators (`<`, `>`, `<=`, `>=`, `LIKE`, `ILIKE`, `IN`, `NOT IN`) raises `QueryError`.
  - Added `escape_like()`, `where_in()`, `where_not_in()`, `where_like()`, and `where_ilike()` (emitting `ESCAPE '\'` and escaping `%`, `_`, and `\` in literal patterns).
- **P3-D4 (Database / Validation – Transaction Ownership, Serialization & Connection Reuse):**
  - Wrapped active transaction connections in `TransactionConnection` with a `closed` state flag (`TransactionClosedError` raised if a child task attempts to use a transaction connection after the `transaction()` block exits) and a per-transaction `asyncio.Lock` so concurrent queries inside a single transaction serialize safely instead of raising `asyncpg.InterfaceError`.
  - Reset `_active_connection` via `ContextVar.reset(token)` on transaction exit.
  - Wired `UniqueRule` and `ExistsRule` (`requires_db = True`) to check `get_active_connection()` before acquiring from `DatabasePool`, preventing pool exhaustion (`max_size=1` deadlock) and allowing validation rules to see uncommitted rows inside an active transaction.
  - Updated `Validator.validate()` to run field/rule validation sequentially when a transaction is active in the task context.
- **P3-D5 (Database – Connection Pool Configuration, SSL Modes & Safe Shutdown):**
  - **Breaking (Defaults):** Lowered default `database.pool.max_size` from `50` to `20` (matching `.env.example`), and enforced non-empty database passwords in production/non-debug mode (`ConfigurationError` raised when `DB_PASSWORD` is empty outside debug mode; warning logged in debug mode).
  - Guarded `DatabasePool.init()` with an `asyncio.Lock` so concurrent initialization calls are idempotent and create only one underlying `asyncpg` pool.
  - Added `_build_ssl_context()` supporting `ssl` modes `disable`, `require`, `verify-ca`, and `verify-full` plus `ssl_ca_file` (`DB_SSL_CA_FILE`), mapping boolean `True` / `"true"` to `require` with a warning for backward compatibility.
  - Wired `database.timeouts.statement` (`server_settings={"statement_timeout": ...}`) and `database.pool.command_timeout` into `asyncpg.create_pool()`, and updated `DatabasePool.close(timeout=5.0)` to fall back to `pool.terminate()` on timeout so shutdown never hangs on in-flight connections.
- **P3-D6 (Database / Migrations / CLI – Schema Validation, `raw()` Defaults, Checksum Drift & Production Guard):**
  - **Breaking:** Whitelisted allowed `Blueprint` / `ColumnDefinition` column types (`ALLOWED_COLUMN_TYPES`), rejecting unknown types and out-of-range `length` (`1..10_485_760`), `precision` (`1..1000`), and `scale` (`0..precision`) with `ValueError`. Enforced the 63-character identifier limit across all generated constraint and index names (`idx_`, `uq_`, `pk_`, `fk_`).
  - Added explicit `raw(sql)` (`DefaultRaw`) helper for SQL default expressions (`table.timestamp("created_at").default(raw("now()"))`); legacy magic SQL strings in `.default("NOW()")` continue to work with a `DeprecationWarning`.
  - `Migrator` now stores a SHA-256 `checksum` per applied migration, raises `RuntimeError` on checksum drift or out-of-order unapplied migrations, and validates `step >= 1` / `steps >= 1`.
  - **Breaking (CLI):** `swiftpy migrate`, `swiftpy migrate:rollback`, and the new `swiftpy migrate:reset` command now refuse to run when `APP_ENV=production` unless `--force` is passed, and exit with code `1` when `--step < 1`.

### Phase 4 – HTTP Correctness
- **P4-H1 (HTTP – `Response` Body Encoding, Multi-Header `Set-Cookie` & CRLF Injection Prevention):**
  - Added `ResponseHeaders(MutableMapping[str, str])` preserving multi-value headers (`Set-Cookie`) via `.add()`, `.getlist()`, and `.raw_items()` alongside `Response.set_cookie()`.
  - Rejected `\r`, `\n`, and `\x00` in header names and values with `ValueError`.
  - Encoded `str` bodies as UTF-8, defaulted `Content-Type: text/plain; charset=utf-8` for `str` bodies when unset, and set `Content-Length` automatically for non-streaming responses (except `1xx`, `204`, `304`).
- **P4-H2 (HTTP / ORM – JSON Encoding & `Model.__hidden__`):**
  - Added `encode_json()` (`allow_nan=False`) supporting `datetime`/`date`/`time` (ISO-8601), `UUID` (`str`), `Decimal` (`str`), `Enum` (`.value`), and objects with `.to_dict()`, while rejecting `bytes` and `NaN`/`Infinity`.
  - Added `Model.__hidden__: ClassVar[set[str] | list[str] | tuple[str, ...]] = ()` and updated `Model.to_dict()` to omit `__hidden__` fields and relations while preserving raw Python types on `.to_dict()`.
- **P4-H3 (HTTP – Router Precedence, Regex Escaping, Strict Path Coercion, WebSocket Isolation & Auto `HEAD`/`OPTIONS`):**
  - **Breaking:** Static route segments now take precedence over dynamic `{param}` segments regardless of registration order (`(num_dynamic, segment_kinds, -static_chars, order)`), and registering a duplicate `(method, normalized_path)` raises `RouteRegistrationError(ConfigurationError, TypeError)`.
  - **Breaking:** Path parameter coercion failures now raise `PathParamCoercionError(ValidationError)` (HTTP 422 instead of 400). Boolean path parameters strictly accept `true/false/1/0/yes/no` (case-insensitive) and float path parameters reject `NaN`/`Infinity`.
  - Escaped literal path segments with `re.escape` so `/v1.0/users` does not match `/v1X0/users`, indexed routes by HTTP method, isolated `WEBSOCKET` routes to `match_websocket()`, and added automatic `HEAD` fallback to `GET` (preserving `Content-Length` with an empty body) and automatic `OPTIONS` (`204 No Content` with `Allow` header).
- **P4-H4 (HTTP / DI – Handler Parameter Injection & Registration-Time Signature Caching):**
  - **Breaking:** Removed duck-typing on `.schema`/`.rules` and removed the fallback that injected `Request` into an untyped first parameter; only `issubclass(ann, FormRequest)` is treated as a body validator and only `issubclass(ann, Request)` injects `Request`. Untyped or unsatisfiable parameters without defaults now raise `RouteRegistrationError` at route registration time naming both the handler and the parameter.
  - Added container DI resolution (`Container.can_resolve` + `app.container.resolve`) for handler parameters (including `CurrentUser` and registered services) and cached `inspect.signature` and `get_type_hints` once at registration time (zero `inspect.signature` calls per request).
- **P4-H5 (HTTP – Request Body Size Limits, Disconnect Handling, Header `getlist` & Cached Query Params):**
  - Enforced `max_body_size` (default `2 MiB`, configurable on `Application` and per-route) both via `Content-Length` pre-check and during chunk streaming, raising `PayloadTooLargeError` (HTTP 413).
  - Handled `{"type": "http.disconnect"}` mid-stream by raising `ClientDisconnect` and aborting cleanly without running the handler or logging a 500 error.
  - Added `RequestHeaders` (decoded as `latin-1`, cached once, supporting `.getlist(name)` and last-wins dict access), cached `query_params` and `query_param_list` (UTF-8 with replacement), moved `parse_qs` import to module top, and mapped direct `await request.json()` syntax/encoding/recursion errors to `HttpException(status_code=400)`.
- **P4-H6 (HTTP / Core – Exception Handler Hardening, Production Debug Guard & Built-In Security Middleware):**
  - **Breaking:** `Application(debug=True)` now raises `ConfigurationError` when `APP_ENV=production`.
  - Removed duck-typing on `.errors` in `ExceptionHandler` (only `ValidationError` and `ValidationException` produce 422), selected custom exception handlers by MRO specificity, logged 4xx `HttpException` at `INFO` and 5xx at `ERROR` without request/response bodies, and ran global middleware around `_dispatch_http` so global middleware post-processes 200, 404, 405, 422, and 500 responses.
  - Added `SecurityHeadersMiddleware` (`SecurityHeaders`), `RequestIdMiddleware` (`RequestId`, validating `[A-Za-z0-9._-]{1,128}` and binding `request_id` into `Context`), and `CORSMiddleware` (`CORS`, backed by `config/cors.py` with deny-all defaults and rejecting `allow_origins=["*"]` with `allow_credentials=True`).

### Phase 5 – Lifecycle, Container, and Config
- **P5-C1 (Core – Async Startup/Shutdown Lifecycle & Lifespan Integration):**
  - Added optional `async def startup(self) -> None` and `async def shutdown(self) -> None` hooks to `ServiceProvider` (default no-ops).
  - Added `FrameworkServiceProvider` registered first on `Application`: initializes `DatabasePool` on `startup()` when database config is present, and on `shutdown()` closes active WebSocket connections with code `1001` (`Going Away`), stops and closes `RedisEventBus`, closes Redis clients (`Cache`, `QueueDispatcher`, `TokenBlacklist`), and closes `DatabasePool`.
  - Updated ASGI `lifespan` handling in `Application` (`startup()` runs providers in registration order and rolls back already-started providers in reverse order on failure before sending `lifespan.startup.failed`; `shutdown()` runs providers in reverse order, catching/logging individual errors, before sending `lifespan.shutdown.complete`).
  - Added `startup()`, `shutdown()`, and `async with TestClient(app)` lifespan context manager support to `TestClient`.
- **P5-C2 (Core – Container Rebind Invalidation, Scoped Disposal & Child-Task Isolation):**
  - **Breaking:** Calling `Container.bind()`, `Container.singleton()`, or `Container.scoped()` now invalidates any cached singleton or current-task scoped instance for that interface (Decision `D-7`).
  - **Breaking:** `Scope.SCOPED` instances cached in `Context` are now keyed to the current `asyncio.Task` and are no longer inherited by child tasks spawned via `asyncio.create_task` or `asyncio.gather` (each child task resolves its own scoped instances).
  - Cached `None` singleton results via a sentinel so `None`-returning singleton factories execute only once.
  - Wrapped `NameError`/`TypeError`/`ValueError` from `get_type_hints()` and `inspect.signature()` in `ResolutionError` naming the target class/callable.
  - Added `await container.aclose_scoped()` and `async with container.scope():` disposing scoped instances implementing `aclose()`, `__aexit__()`, `close()`, or `__exit__()` in reverse creation order at the end of every HTTP/WebSocket request scope.
- **P5-C3 (Auth – Guard Dispatch by Registered Name & Lazy `CurrentUser` Resolution):**
  - `AuthManager.authenticate()` now dispatches to the guard instance registered for the configured name (`g.authenticate(request)`) instead of bypassing custom guards via hardcoded `require_jwt` / `require_api_key` calls; `"jwt"` and `"api_key"` are registered as default guard factories and `AuthManager.extend(name, creator)` invalidates any cached guard instance for `name`.
  - `CurrentUser` resolved from the container before authentication runs is no longer cached as an unauthenticated instance in scope and syncs lazily from `Context` on attribute access.
  - Enforced canonical base64url verification in `_base64url_decode` (`swiftpy.auth.jwt_issuer`) to reject non-zero padding bits in tampered JWT signatures.
- **P5-C4 (Core – Instance-Scoped Singletons & Container-Resolved Helpers):**
  - **Breaking:** `Config`, `WebSocketManager`, `EventDispatcher`, `QueueDispatcher`, and `RedisEventBus` are now created and owned per `Application` / `Container` instance rather than shared via process-global class attributes. `get_instance()` resolves from the active `Container` when bound. `Config.__init__` no longer overwrites `Config._instance`.
  - **Breaking:** `helpers.dispatcher()`, `helpers.queue()`, `helpers.cache()`, and `helpers.websocket_manager()` now raise `BindingNotFoundError` (`ResolutionError`) when the corresponding service is not registered in the active container instead of auto-wiring a new leaked instance per call.
  - Added `snapshot_model_registry()` and `restore_model_registry()` in `swiftpy.database.orm.registry` to isolate process-global ORM class metadata across tests.
- **P5-C5 (Core – Config Typed Coercion, Strict File Loading & Deep-Copy Accessors):**
  - **Breaking:** `Config` environment variable merging now coerces `bool`, `int`, and `float` values only when the target key has an existing typed default (or is a known boolean flag like `app.debug`), preserving string values such as `APP__SECRET="0012345678"` without stripping leading zeros (Decision `D-8`).
  - **Breaking:** `Config.load()` now defaults to `strict=True` and raises `ConfigurationError` when a configuration file in `config_dir` fails to load or defines a non-dict `CONFIG` (pass `strict=False` for lenient CLI tooling).
  - Ignored malformed environment variable keys with empty path segments (`__WEIRD__KEY`, `APP____EMPTY`, `TRAILING__`), updated `Config.get()` to distinguish missing keys from explicit `None` values via a sentinel, updated `Config.all()` and `Config.__init__()` to deep-copy dictionaries, and replaced direct `os.getenv` reads in `RedisEventBus` and `QueueDispatcher` with `Config` / `resolve_redis_params`.
- **P5-C6 (Core – Safe Package-Based Provider Discovery):**
  - **Breaking:** `discover_providers()` no longer inserts `app/providers` into `sys.path` (preventing provider files named `queue.py` or `logging.py` from shadowing Python standard library modules) and raises `ConfigurationError` on broken provider modules by default (`strict=True`; pass `strict=False` or `Application(strict_providers=False)` to skip broken providers). Added `project_root` support to `discover_providers()` and `Application`.

### Phase 6 – Queue, Events, and Cache
- **P6-Q1 (Queue – Reliable Queue Consumption):**
  - **Breaking:** Replaced fire-and-forget queue popping with reliable delivery based on list-plus-lease architecture (Decision `D-3`).
  - Active jobs are moved from `queue:<name>` to per-worker processing lists `queue:<name>:processing:<worker_id>` via `LMOVE`/`BLMOVE`, with lease deadlines tracked in sorted set `queue:<name>:leases`.
  - Atomic Lua scripts handle job popping and lease creation (`pop_and_lease`), job acknowledgment (`ack`), and delayed backoff retries (`release`).
  - Added background lease reaper (`reap_expired_leases`) that re-queues expired jobs at the head of the queue preserving attempt counts, and moves jobs exceeding maximum `tries` to the Dead Letter Queue.
  - Jobs cancelled mid-flight re-queue at the head with attempt counts intact.
  - Documented at-least-once queue delivery semantics and idempotency requirements.
- **P6-Q2 (Queue – Poison-Message Handling):**
  - Hardened `Job.deserialize()` to strictly validate envelope field types (`args` as list, `kwargs` as dict, `tries` as int >= 1, `attempts` as int >= 0, `data` as dict) and wrap deserialization/instantiation failures in `JobSerializationError`.
  - Workers catch `JobSerializationError` and malformed payloads, record them in the failed DLQ with failure diagnostics, ack them from processing queues, and continue processing without loop termination.
  - Configured Redis connection readers with `encoding_errors="replace"` and hardened Lua acknowledgment scripts to safely handle non-UTF-8 binary payloads.
- **P6-Q3 (Queue – Trusted Payloads & HMAC Envelope Signing):**
  - **Breaking:** Queue envelopes are signed with HMAC-SHA256 using `app.key` (`APP_KEY`). Verification is performed using `hmac.compare_digest` **before** any class or module is imported via `importlib.import_module()`.
  - Added `set_app_key()`, `get_app_key()`, and key rotation support via `set_previous_keys()` / `get_previous_keys()`.
  - Enforced `_trusted_namespaces` (defaulting to application packages and `swiftpy`) to reject arbitrary module imports even when signing is unconfigured.
- **P6-Q4 (Queue / CLI – Worker Loop Efficiency & CLI Commands):**
  - Converted worker loop to block via Redis `BLMOVE` (honoring `--sleep`), promoted delayed jobs using cached Lua scripts, and restored original signal handlers on worker exit.
  - Added `--concurrency N` support to `QueueWorker` and `swiftpy queue:work` using bounded semaphores and task tracking with done-callbacks per the Task Rule.
  - Enforced queue name validation (`^[A-Za-z0-9_.-]{1,64}$`), added DLQ capping (`failed_max_size`, default 1000), and added CLI commands `swiftpy queue:failed`, `swiftpy queue:retry <id|all>`, and `swiftpy queue:flush`.
- **P6-Q5 (Queue / Database – After-Commit Dispatch):**
  - Added transaction commit hook mechanism: `on_commit(callback)` in `swiftpy.database.transaction` (supported on `TransactionConnection`, `Database`, and module-level).
  - Outermost successful commit triggers callbacks in registration order, inner savepoint commits promote callbacks to parent level, and rollbacks cleanly discard registered callbacks.
  - Added `Job.dispatch(after_commit=True)`: defers pushing the job until the active database transaction successfully commits; drops the job on rollback; falls back to immediate dispatch when no transaction is active.
- **P6-Q6 (Events – Redis Ownership, Event Bus Hardening & Dispatcher Optimization):**
  - Added `owns_pool` flag to `QueueDispatcher`, `RedisDriver`, and `RedisEventBus` so external shared connection pools are never disconnected by consumers that did not create them. Guarded lazy `get_client()` with `asyncio.Lock` across all drivers.
  - Hardened `RedisEventBus`: added done-callback on `_listener_task`, re-raised `CancelledError`, implemented exponential backoff with jitter and rate-limited logging during Redis reconnects, and dispatched incoming messages in bounded tracked background tasks (`max_concurrency`) so slow listeners never stall consumption.
  - Added HMAC-SHA256 message signing to `RedisEventBus` with key rotation support; dropped unsigned or tampered messages.
  - Documented at-most-once pub/sub delivery semantics and local vs. remote payload contracts.
  - Optimized `EventDispatcher`: precompiled invocation strategies at listener registration (eliminating repeated `inspect.signature` during dispatch), indexed exact registrations in O(1) dictionary and wildcards separately, and added `remove_listener` / `forget`.
- **P6-K1 (Cache – Driver Correctness, Atomicity & Memory LRU Eviction):**
  - Hardened `remember()`: on lock timeout, executes the callback directly without caching and logs a warning (never returns `None` silently). Reference-counted `_remember_locks` dictionary so locks are cleaned up immediately when unused.
  - Implemented atomic `increment(key, value=1, ttl=None)` with Lua script setting TTL on initial creation and never extending it on subsequent increments. Implemented atomic `add(key, value, ttl=None)` using `SET ... NX`.
  - Scoped `flush()` using `SCAN` with store prefix and batched `UNLINK` in chunks of 500 keys, scoping default prefixes with `app.name`.
  - Added pluggable `CacheSerializer` interface with default `JsonCacheSerializer` supporting `datetime`, `date`, `time`, `UUID`, `Decimal`, and models with `to_dict()` and `__hidden__`.
  - Added `max_items` LRU eviction and lazy expiry sweeping to `MemoryDriver`, performing `copy.deepcopy` outside the lock to minimize lock contention.
  - Updated `Cache._resolve_driver` to honor `redis_url`, `max_connections`, and `timeout`, sharing connection pools across stores with identical connection configurations.

### Phase 7 – ORM Safety and Typing
- **P7-M1 (ORM – Hidden Attributes & Safe Serialization):**
  - **Breaking:** `Model.__repr__` now outputs `<ClassName pk_name=pk_val!r>` only, preventing accidental credential/token leakage in logs, interactive shells, and exception traces (Decision `D-5`).
  - Added `__hidden__: ClassVar[tuple[str, ...]] = ()` support on `Model`. Fields declared in `__hidden__` are omitted from `to_dict()`, `encode_json()`, and event payloads published on the bus, while remaining accessible as Python attributes on the instance.
  - Added `model.make_visible(*fields)` and `model.make_hidden(*fields)` instance methods to temporarily expose or hide fields in `to_dict()` outputs.
  - Updated `to_dict(include_hidden=False)` to recursively respect `__hidden__` declarations across loaded relationships.
- **P7-M2 (ORM – Mass Assignment Protection):**
  - **Breaking:** Mass assignment via `Model(...)`, `Model.create(...)`, `Model.fill(...)`, and `ModelQueryBuilder.create(...)` now denies all fields by default unless declared in `__fillable__` or allowed via `__guarded__` (Decision `D-4`). To opt out of mass assignment protection, models must explicitly declare `__guarded__: ClassVar[list[str]] = []`.
  - Added `MassAssignmentError(ValueError)`. Attributes starting with `_` (e.g. `_exists`, `_original`) and undeclared attribute keys passed to `__init__`, `create()`, or `fill()` raise `MassAssignmentError`.
  - Added `Model.force_fill(attributes)` and `Model.force_create(**attributes)` to explicitly bypass mass assignment checks for trusted internal code.
  - Added `Model._hydrate(row)` to instantiate models directly from trusted database rows without mass-assignment validation overhead.
- **P7-M3 (ORM – Defaults and Dirty Tracking):**
  - Implemented `Field(default=..., default_factory=..., primary_key=..., nullable=...)` returning `FieldInfo` metadata.
  - Added per-instance deep-copying (`copy.deepcopy`) of mutable class defaults, callables, and `default_factory` callbacks evaluated at instance creation time.
  - Implemented deep-copy snapshotting of `_original` so in-place mutations of mutable types (`dict`, `list`, JSON/JSONB) are detected as dirty.
  - Added `model.is_dirty(*fields)` and `model.get_dirty()`. Partial select models track explicit modifications via `_modified_keys` to ensure unselected columns are never clobbered on update.
  - `model.save()` skips database `UPDATE` and suppresses `.updated` lifecycle events when dirty tracking detects no changes (no-op save).
  - Added `_coerce_db_value` to automatically deserialize PostgreSQL JSON/JSONB string columns into typed Python dictionaries and lists.
- **P7-M4 (ORM – Events and Transaction Ordering):**
  - Refactored model lifecycle dispatch (`_dispatch_lifecycle_event`): `*ing` (before) events (`creating`, `updating`, `deleting`) execute inline and abort database operations if an exception is raised or a listener returns `False`.
  - Deferred `*ed` (after) events (`created`, `updated`, `deleted`) to execute post-commit via `on_commit` when inside an active database transaction.
  - Added `Model.strict_events: ClassVar[bool] = False`. When `strict_events` is False (default), listener exceptions on `*ed` events are logged without turning committed database writes into apparent failures; when True, listener exceptions propagate to the caller.
- **P7-M5 (ORM – Relationships Hardening):**
  - Chunked `IN` queries during eager loading in batches of 1,000 keys (`_chunk_list`) across `HasMany`, `HasOne`, `BelongsTo`, and `BelongsToMany` to prevent hitting database parameter limits.
  - Implemented multi-level nested eager loading (e.g. `with_("books.reviews")`, `with_("author.books.tags")`) issuing exactly 1 query per relation level via recursive relation dependency grouping.
  - Guarded `RelationshipQueryBuilder.__getattr__` against private/dunder attribute lookups to prevent recursion during `copy.copy` or inspection.
  - Added pivot manipulation methods on `BelongsToMany` (`attach(model_or_id, **pivot_extra)`, `detach(model_or_id=None)`, `sync(ids_or_models)`).
  - Added `Model.prevent_lazy_loading: ClassVar[bool] = False`. When enabled, accessing un-eager-loaded relationships raises `LazyLoadingDisabledError`.
  - Enforced symmetric `__eq__` and `__hash__` semantics (`type(self) is type(other)` and identical primary key).
- **P7-M6 (ORM – Public Typing for Models & Relations):**
  - Added descriptor protocol (`@overload def __get__(...)`) to `Relation` and `RelationDescriptor` in `swiftpy/database/orm/relationships.py`, allowing Mypy to recognize relationship properties on model classes.
  - Added `tests/typing/test_orm_typing.py` validating static and runtime contracts for attributes, relationships, dirty tracking, hidden fields, and BelongsToMany operations.
  - Completely resolved the 25 legacy Mypy errors in `tests/orm/test_eager_loading.py`. Mypy now passes with **0 errors across 208 source files** in strict mode.

### Phase 8 – Validation Hardening
- **P8-V1 (Validation – Rule Correctness & Coercion):**
  - Hardened `IntRule` with ASCII-only regex `^[+-]?[0-9]+$` rejecting unicode superscript digits (e.g. `"²"`, `"³"`) and booleans, coercing valid numeric strings into integers.
  - Hardened `FloatRule` to reject `NaN`, `Infinity`, and `-Infinity`, coercing finite numeric strings into floats.
  - Hardened `EmailRule` with tighter RFC-compliant checks (local <= 64, total <= 254, rejecting consecutive dots `..` and leading/trailing dots in local part). Normalizes domain to lowercase and strips whitespace on validation and storage, preserving local part case (or full lowercase when `email:lowercase` is configured).
  - Hardened `UrlRule` enforcing a 2048-character maximum length limit and valid HTTP/HTTPS schemes.
  - Added support for escaping `\|` and `\:` in rule strings (e.g. `regex:^(a\|b)+$`) and list-based rule declarations `["required", "regex:^(a|b)+$"]`. Caught `re.error` so invalid regex patterns return validation errors rather than 500 errors.
  - `BetweenRule` parsing (`between:1`) now raises `ConfigurationError` at parse time when missing the required range boundaries.
  - Hardened `InRule` and `NotInRule` with strict type matching, ensuring boolean values (`True`/`False`) do not match integer `1`/`0` or string `"True"`.
  - Added case-insensitive support for database rules (`unique:table,column,ci` and `UniqueRule(..., case_insensitive=True)`) using `LOWER("{column}") = LOWER($1)`.
  - Database data errors (`asyncpg.DataError`) during `unique` or `exists` evaluation now fail validation gracefully with field errors rather than crashing the request with HTTP 500.
- **P8-V2 (Validation – Engine Optimization & Order Independence):**
  - Precomputed and cached rule invocation metadata and signatures (`(param_count, is_async)`), eliminating per-field `inspect.signature` overhead on every request.
  - Removed `except TypeError` retry fallback in `_run_rule`, ensuring rule implementation bugs propagate cleanly as framework errors instead of being re-executed.
  - Enforced structured 422 `Unprocessable Entity` response with `{"body": ["The request body must be a JSON object."]}` when non-dict payloads (`[]`, `"x"`, `null`, `123`) are sent.
  - Achieved rule order independence for coercion: sorted rule execution so type coercion rules (`IntRule`, `FloatRule`, `BoolRule`, `EmailRule`) run before magnitude and length constraint rules (`MinRule`, `MaxRule`, `BetweenRule`).
- **P8-V3 (Validation – Fail-Closed Validation & DI Resolution):**
  - **Breaking (Security):** `FormRequest.get_rules()` now fails closed with `ConfigurationError` (HTTP 500) if rule instantiation or definition raises `TypeError` or any exception, removing the unsafe fallback that previously returned `{}` and allowed unvalidated requests to execute route handlers (Decision `D-9`).
  - Added parse-once caching (`_cached_parsed_rules`) per `FormRequest` class so static rule definitions are parsed once and reused across requests.
  - Integrated container dependency injection for `FormRequest`: constructors requiring registered services or dependencies are automatically resolved via `container.resolve(ann)`.

### Phase 9 – WebSockets
- **P9-W1 (WebSockets – Handshake Middleware, Origin Checks & Auth):**
  - **Breaking (Security):** Implemented Origin header checks per Decision `D-14`: default is same-origin only (matching the request `Host` header or server host). Configurable via `Application(allowed_origins=[...])` or `websockets.allowed_origins` config; wildcard `*` must be explicit. Disallowed origins are rejected before accept with close code 1008 (`Policy Violation`). Missing Origin headers (CLI / mobile / non-browser clients) remain permitted.
  - Implemented full handshake middleware pipeline running global middleware and route group middleware on a `Request(scope, receive)` **before** `ws.accept()`. If any middleware returns a response with status >= 400 (e.g. `unauthorized(401)`, `forbidden(403)`) or raises `HttpException` / `AuthenticationError`, the connection is closed with code 1008 before route handlers execute.
  - State set on `Request.state` during handshake middleware is automatically transferred to `WebSocketConnection.state`.
  - Added WebSocket handshake support to `JWTGuard` (`require_jwt`) and `ApiKeyGuard` (`require_api_key`), extracting credentials from `Authorization` header, `Sec-WebSocket-Protocol` subprotocols (`["bearer", token]`, `["access_token", token]`, or `["apikey", key]`), or URL query parameters (`?token=...`, `?access_token=...`, `?api_key=...`, `?key=...`) with documented log-leak warnings for query strings.
- **P9-W2 (WebSockets – Connection Lifecycle & Safety Limits):**
  - Added `try/finally` cleanup in `Application.__call__` ensuring `await self.websocket_manager.disconnect(connection)` is always executed on connection completion or termination, and closing active connections with code 1000.
  - If a route handler raises an unhandled exception before `ws.accept()`, the connection is rejected with code 1008 (`Policy Violation`); unhandled exceptions after accept close with code 1011 (`Internal Server Error`).
  - `WebSocketDisconnect` is treated as a standard exit and logged at `DEBUG` level rather than polluting application logs with errors.
  - Added configurable `max_message_size` (default 1 MiB / 1,048,576 bytes) and `idle_timeout` to `WebSocketConnection`. Messages exceeding size limits close with code 1009 (`Message Too Large`); idle timeouts close with code 1000 (`Idle timeout`).
- **P9-W3 (WebSockets – Broadcast Optimization & Resilience):**
  - Optimized `WebSocketManager.broadcast`: JSON dictionary and list payloads are serialized with `json.dumps()` exactly once across all recipients ($O(1)$ serialization overhead instead of $O(N)$).
  - Added `timeout` (default 5.0s) per connection send using `asyncio.wait_for(...)`. A stalled or hung client is cut off after the timeout window and evicted from the manager without delaying broadcast delivery to remaining clients.
  - Implemented bounded concurrency using `asyncio.Semaphore` with configurable `max_concurrency` (default 100).
  - Failed or disconnected clients during broadcast are automatically pruned from active connections and rooms.
  - Added distributed cross-worker broadcast support (`distribute=True`): publishes broadcast payloads via `RedisEventBus` (`"websocket.broadcast"`) for cross-process fanout.

### Phase 10 – Infra, CI, Repo Hygiene, and Documentation
- **P10-I1 (Infra – Multi-Stage Dockerfile & Hardened Docker Compose):**
  - Created `.dockerignore` excluding `.git`, `.venv`, caches, `__pycache__`, `*.pyc`, `.coverage`, `docs/`, and tests from production images.
  - Upgraded `Dockerfile` to a multi-stage build creating a non-root `swiftpy:swiftpy` user (UID/GID 1000) and running `CMD ["swiftpy", "start", "--host", "0.0.0.0", "--port", "8000"]`.
  - Hardened `docker-compose.yml`: bound PostgreSQL and Redis to `127.0.0.1` only, configured `--requirepass` for Redis, added healthchecks, and configured configurable host UID/GID.
- **P10-C1 (CLI – `swiftpy start` Configuration & Options):**
  - **Breaking:** `--reload` now defaults to `False` for production safety (P10-C1). To enable hot-reload during local development, pass `swiftpy start --reload`.
  - Added CLI options: `--host`, `--port`, `--workers`, `--reload`, and `--env-file`.
  - Refuses `--reload` combined with `--workers > 1` with an explanatory error.
  - Added CLI unit tests in `tests/console/test_start.py` verifying argument parsing, config defaults, and uvicorn kwargs.
- **P10-I2 (CI – Pinned GitHub Actions Workflow):**
  - Updated `.github/workflows/ci.yml` with actions pinned by full commit SHA, PostgreSQL 16 and Redis 7 service containers, frozen dependency installation via `uv sync --frozen`, ruff linting, mypy strict type checking, pytest with coverage, and security audit steps (`bandit`, `pip-audit`).
- **P10-R1 (Repo Hygiene – Git Untracking & `.gitignore` Hardening):**
  - Untracked previously committed `.coverage` and `*.pyc` files from the git index (`git rm --cached`).
  - Updated `.gitignore` to explicitly ignore `*.pyc`, `*.pyo`, `*.pyd`, `.coverage`, `.coverage.*`, and `htmlcov/`.
  - Confirmed `docs/`, `AGENTS.md`, and `DEVLOG.md` untracked status in git per Decision `D-12`.
- **P10-L7 (Repo Hygiene – Placeholder API Deprecation & Wheel Exclusion, Superseded by `G-1`):**
  - Earlier placeholder annotations and `[tool.hatch.build.targets.wheel]` `swiftpy/ai` exclusion were subsequently superseded by full `swiftpy/ai` implementation (Phases 11–13) and Decision `G-1` (shipping `swiftpy/ai` inside the main v0.1.0 `swiftpy-framework` wheel).
- **P10-D1 (Documentation – Readme Examples Test Suite & Migration Guide):**
  - Overhauled `README.md` to reflect completion of all 10 architectural phases and 600+ passing tests.
  - Aligned all code examples with actual SwiftPY APIs (`app.middleware`, `app.group`, `JWTGuard`, `CurrentUser`, `Blueprint`, `Job`).
  - Added comprehensive Configuration Reference table and Breaking Changes & Migration Guide documenting Decisions `D-1` through `D-14`.
  - Created `tests/docs/test_readme_examples.py` executing all README code snippets under automated pytest coverage to prevent future documentation drift.

### Phase 11 – AI Native Module (Week 22)
- **P11-T1 (AI / Tools – Tool Calling Abstraction & First-Party Tools):**
  - Added `BaseTool(ABC)`, `FunctionTool` with signature/type-hint introspection, and `@tool` decorator in `swiftpy/ai/tools/base_tool.py`.
  - Implemented `CalculatorTool` with AST-safe expression evaluation and numerical limits, `DateTimeTool` with timezone awareness, and `WebSearchTool` in `swiftpy/ai/tools/first_party.py`.
- **P11-T2 (AI / Tool Loop – Autonomous Multi-Turn Tool Execution):**
  - Added `ToolLoop` in `swiftpy/ai/tools/tool_loop.py` managing multi-turn conversation loops with dual guard limits (`max_iterations`, `max_tool_calls`), parallel tool execution via `asyncio.gather()`, and safe tool exception isolation.
  - Integrated fluent `.tools()` and `.auto_execute_tools()` on `AIClient`.
  - Added OpenAI and Anthropic tool calling wire protocols in `swiftpy/ai/providers/`.

### Phase 12 – Agent System and Admin Panel (Week 23)
- **P12-A1 (AI / Agents – Multi-Turn BaseAgent & Iteration Limits):**
  - Implemented `BaseAgent` (`Agent`) in `swiftpy/ai/agents/base_agent.py` wrapping the autonomous `ToolLoop`, managing memory sessions, enforcing system prompts on every turn, and enforcing iteration limits (`max_iterations`).
  - Implemented `AgentResponse` encapsulating generated content, executed tool calls, tool results, iterations, usage metrics, and model identifier.
  - Added strict boundary enforcement asserting exact iteration count, non-issuance of $(N+1)$th model requests, and memory preservation.
- **P12-A2 (AI / Agents – Memory Persistence Backends & Concurrency Safety):**
  - Implemented `BaseMemoryBackend` and `AgentMemory` in `swiftpy/ai/agents/memory.py` supporting interchangeable persistence backends:
    - `InMemoryMemoryBackend` for tests and local sessions.
    - `RedisMemoryBackend` using atomic `RPUSH`/`LRANGE` list operations and pipeline execution with TTL support, eliminating read-modify-write race conditions.
    - `DatabaseMemoryBackend` using dedicated relational tables (`agent_memories_messages`, `agent_memories_state`) with atomic SQL `INSERT`s and JSON upserts on PostgreSQL.
- **P12-A3 (AI / Agents – Non-Blocking Background Concurrency & Lifecycle Management):**
  - Added `AgentTask` running agents as background `asyncio.Task` instances that never block the server's event loop.
  - Adhered strictly to the Concurrency Task Rule by attaching `_on_done` callbacks that catch and log background task exceptions.
  - Added `AgentTracker` coordinating active background agent tasks, supporting concurrent monitoring and `cancel_all()` for graceful application shutdown.
- **P12-AD1 (Admin – Model Registration & Introspection):**
  - Implemented `AdminSite` and `ModelAdmin` in `swiftpy/admin/registry.py` with `@admin.register(Model)` decorator and automatic model introspection (`FieldMetadata`, `RelationshipMetadata`).
  - Added validation rejecting duplicate model registrations (`ValueError`) and non-Model subclasses (`TypeError`).
  - Introspected all 4 relationship types: `BelongsTo`, `HasMany`, `HasOne`, and `BelongsToMany` (with pivot table reflection).
  - Added automatic eager loading of `BelongsTo` relationships to prevent N+1 queries during list rendering.
  - Added primary key coercion helper `_coerce_pk` mapping URL parameters to declared integer/scalar column types for PostgreSQL compatibility.
- **P12-AD2 (Admin – CRUD Screens & Relationship Dropdowns):**
  - Implemented responsive HTML CRUD views and JSON API endpoints in `swiftpy/admin/views.py`: dashboard, list view with relationship badges, create and edit views with `<select>` foreign key dropdowns for `BelongsTo` relationships, and delete handler.
  - Protected fields filtering: automatically ignores submitted `id`, `created_at`, and `updated_at` fields on create/edit forms.
  - Comprehensive XSS prevention: all dynamic model fields, option values, headers, and representation strings are escaped via `html.escape(..., quote=True)`.
- **P12-AD3 (Admin – Session-Based Authentication & Security Hardening):**
  - Implemented session-based cookie authentication in `swiftpy/admin/auth.py` (`ADMIN_COOKIE_NAME`, `AdminSessionStore`, `AdminAuthenticator`, `AdminUser`, `require_admin`).
  - Strictly isolated admin authentication from the API's JWT and API key guards so admin cookies cannot access API routes and API tokens/keys cannot access admin views.
  - Implemented HMAC-SHA256 signed CSRF token generation and validation bound to admin `session_id`, rejecting mutating POST requests without valid tokens (HTTP 403).
  - Enforced session token verification, signature tampering rejection, payload validation, and expiration enforcement.
  - Disallowed default credentials (`admin`/`admin123`) in production mode (`APP_ENV=production`).

### Phase 13 – Prompt Manager, AI Middleware, Observability, and AI Mocking (Week 24)
- **P13-P1 (AI / Prompts – Prompt Template Loader & Production Caching):**
  - Implemented `PromptLoader` and `load_prompt()` in `swiftpy/ai/prompts/loader.py` loading `prompts/{name}.md` and `prompts/{name}.py` files with positional-only naming safety.
  - Supported `{{ variable }}` interpolation with whitespace tolerance and `KeyError` enforcement on missing placeholders.
  - Added production in-memory caching active when `APP_ENV=production` or `ai.prompts.cache=true`, with zero-delay live reload during development.
  - Integrated fluent `.prompt(name, **variables)` on `AIClient` and facade `AI.prompt()`.
- **P13-M1 (AI / Middleware – Rate Limiting, Token Budgets & Content Moderation):**
  - Implemented `AIMiddlewarePipeline` in `swiftpy/ai/middleware.py` executing pre/post processing layers around LLM providers.
  - `AIRateLimitMiddleware`: per-user sliding window rate limiting extracting identity from `request.extra` or `Context` (`auth.user`), raising `AIRateLimitError` with `retry_after`.
  - `TokenBudgetMiddleware`: daily token consumption and estimated dollar spend enforcement per user or globally, blocking requests with `AIBudgetExceededError`.
  - `ContentModerationMiddleware`: pre-LLM message evaluation against keyword blacklists, regex patterns, or custom async moderation API checks, raising `AIContentModerationError`.
  - Added `.use()` / `.middleware()` on `AIClient` and global `AI.add_middleware()`.
- **P13-O1 (AI / Observability – Telemetry Observer & Multi-Driver Persistence):**
  - Implemented `AIObservabilityManager` in `swiftpy/ai/observability/observer.py` hooking into all LLM `send()` and `stream()` invocations, recording model, provider, tokens, latency, estimated cost, and prompt SHA-256 hash.
  - Implemented `DatabaseObservabilityDriver` in `swiftpy/ai/observability/drivers.py` writing structured rows to the PostgreSQL `ai_observations` table with aggregate SQL analytics.
  - Implemented non-blocking `FileObservabilityDriver` writing JSON lines to `storage/logs/ai.log` via background thread offloading (`asyncio.to_thread`).
  - Implemented `InMemoryObservabilityDriver` for thread-safe ephemeral testing.
  - Extracted centralized token pricing logic into `swiftpy/ai/pricing.py` to prevent circular dependencies.
- **P13-AD1 (Admin – Observability Dashboard & Live Usage Metrics):**
  - Wired live AI observability directly into the admin console with dedicated view `GET {clean_prefix}/ai-usage` and JSON API endpoint.
  - Displayed metric cards (total calls, success/failure breakdown, token volumes, estimated spend, average latency), provider breakdown, model breakdown, and recent 50 observations.
  - Embedded AI Observability quick-status card in the main admin dashboard and added navigation shortcut in the sidebar.
- **P13-F1 (AI / Testing – AIFake Double & Telemetry Assertion Helpers):**
  - Implemented `AIFake` in `swiftpy/testing/fakes/ai_fake.py` intercepting all LLM calls across the application with zero real external API calls made and zero credentials required.
  - Supported string, `LLMResponse`, sequential queue, dictionary/model mapping, and custom callable fixtures, alongside realistic chunk streaming.
  - Exposed assertion helpers: `assert_called()`, `assert_called_once()`, `assert_not_called()`, and detailed mismatch diagnostic `assert_called_with()`.
  - Integrated `AI.fake()` and `AI.restore()` on the `AI` facade, ensuring all faked calls are recorded by the observability system.
- **P13-R1 (AI / Middleware – Distributed Concurrency & Preflight Budget Hardening):**
  - Added atomic Redis Lua sliding-window rate limiting script (`_LUA_SLIDING_WINDOW_RATE_LIMIT`) supporting multi-worker application deployments and preventing burst over-allocation.
  - Added configurable `fail_closed` policy for Redis backend rate limiting and token budgets.
  - Hardened `TokenBudgetMiddleware` with atomic preflight reservation (`mode="hard"`) and automatic reservation rollback on downstream provider failure, eliminating concurrent overspending races.
  - Added simultaneous per-user and global daily spend enforcement.
- **P13-R2 (AI / Testing – Task-Isolated Fake Double & Complete Entry Point Interception):**
  - Refactored `AIFake` to use task-local `ContextVar` stack storage, guaranteeing complete task isolation across concurrent `asyncio.gather()` workers and nested context managers.
  - Intercepted embedding calls across `AI.embed()`, `AI.embed_batch()`, and `EmbeddingsClient.embed_batch()`, recording `FakeEmbeddingCall` telemetry and providing `assert_embedding_called()`.
  - Verified end-to-end interception across all AI entry points: `AIClient.send()`, `AIClient.stream()`, `ToolLoop`, `BaseAgent`, and `RAGPipelineBuilder`.
  - Added network guard verification proving 0 real outbound HTTP calls are made during active fake sessions.
- **P13-R3 (AI / Observability – Resilience, Streaming TTFT, Cancellation & Metadata Redaction):**
  - Added `fail_closed: bool = False` isolation to `AIObservabilityManager` so telemetry driver failures never disrupt user LLM calls.
  - Tracked streaming TTFT (`ttft_ms`), total duration (`duration_ms`), and recorded `status="cancelled"` on stream consumer disconnection.
  - Added automatic redaction of sensitive credentials (`api_key`, `secret`, `password`, `token`, `authorization`) from observation metadata (`sanitize_metadata`).
  - Validated SQL table identifiers in `DatabaseObservabilityDriver` via `validate_identifier`.
  - Added date range (`start_date`, `end_date`), `provider`, `model`, and `status` query filtering and pagination (`page`, `per_page`) across all drivers and `/admin/ai-usage`.
- **P13-R4 (AI / Prompts – Path Traversal Guard & Precedence Hardening):**
  - Added path traversal prevention in `PromptLoader.get_template()` verifying resolved path containment (`is_relative_to`), rejecting directory traversal attempts with `ValueError`.
  - Enforced `.md` template precedence over `.py` files.
  - Supported escaped literal `\\{{ ... \\}}` placeholders and positional-only `(name, /)` signature preventing placeholder parameter collisions.

### Phase 5 – Polish, Harden, and Launch
- **P14-P1 (Performance / Benchmarks – Production Load Optimization & Targets):**
  - Ran full `wrk` benchmark suite against containerized production runtime in Docker with active PostgreSQL and Redis backends.
  - Achieved **6,507.08 req/s** on Simple JSON (`GET /benchmarks/json`), exceeding the 3,000 req/s target by +116.9%.
  - Achieved **1,064.82 req/s** on Authenticated DB Endpoint (`GET /benchmarks/auth-db`), fulfilling the $\ge$ 1,000 req/s requirement.
  - Achieved **~170ms P95 latency** on RAG Pipeline (`GET /benchmarks/rag`), beating the 900ms P95 latency ceiling by 5.3x.
  - Achieved **4,113.83 req/s** on Chunked SSE Streaming Endpoint (`GET /benchmarks/stream`) and **2,938.80 req/s** on Authenticated JWT Endpoint (`GET /benchmarks/auth`).
- **P14-P2 (Core / HTTP – $O(1)$ Static Routing Table):**
  - Implemented $O(1)$ static route hash table lookup (`_static_routes` and `_static_websocket_routes`) in `swiftpy/http/router.py`, indexing parameter-free routes by `(method, normalized_path)`.
  - Completely eliminated linear regular expression compilation and matching on static request paths, slashing per-request dispatch latency to sub-microsecond levels.
  - Added `Router.add_compiled_route(route)` for dynamically compiled routes.
- **P14-P3 (Core / DI – Reflection Caching on Hot Paths):**
  - Added `_inspect_callable_specs(fn)` and `_callable_reflection_cache` in `swiftpy/core/container.py`, caching callable parameter reflection and type hints indefinitely.
  - Eliminated repetitive runtime reflection overhead on controller actions, middleware adapters, and service autowiring under high concurrency.
- **P14-P4 (Auth / Database – Redis Pipelining & In-Memory Token Caching):**
  - Batched Redis token blacklist checks into atomic Redis pipelines (`pipe.exists(...).get(...)`) in `swiftpy/auth/token_blacklist.py`.
  - Added a thread-safe 2-second in-memory TTL cache (`_local_cache`) for non-revoked JWT tokens, immediately purged when tokens are explicitly revoked.
  - Optimized `QueryBuilder` connection pool acquisition with singleton caching in `_connection()` and `_release()`.
- **P14-P5 (CLI / Cache – Precompiled Production Cache Serialization):**
  - Implemented `swiftpy cache:build` and `swiftpy cache:clear` CLI commands.
  - Precompiled routes to `bootstrap/cache/routes.json` via `RouteCache.build(app)`.
  - Precompiled configuration to `bootstrap/cache/config.json` via `Config.dump_cache()`.
  - Precompiled AI prompt templates to `bootstrap/cache/prompts.json` via `PromptLoader.dump_cache()`.
  - Added automatic hydration in production environments (`APP_ENV=production`) completely eliminating cold-start parsing and disk exploration.
