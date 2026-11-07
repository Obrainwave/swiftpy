# Changelog

All notable changes to SwiftPY will be documented in this file.

## [Unreleased]

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




