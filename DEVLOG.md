# [Phase 01] - The Verified Core

---

## Week 1 - 2 - Raw ASGI Foundation & Concurrency Analysis

### Concurrency Verification

* **Command:** `curl http://localhost:8000/slow & curl http://localhost:8000/ping`

* **Observed Result:**
- `/ping` returned immediately.
- `/slow` completed approximately 1 second later.

* **Conclusion:**
await asyncio.sleep(1) yielded execution back to the event loop.
Concurrent requests continued to be processed while /slow was suspended.
No event-loop blocking was observed.

### 2. Micro-Benchmark Baseline (`wrk`)
* **Command:** `wrk -t4 -c200 -d10s http://localhost:8000/ping`
* **Server Setup:** Uvicorn 1.0 (Single Process Worker, Loop: `uvloop`/`asyncio`)
* **Environment:** Python 3.12, Windows WSL2 environment

```text
Running 10s test @ http://localhost:8000/ping
  4 threads and 200 connections
  Thread Stats   Avg      Stdev     Max   +/- Stdev
    Latency    74.17ms  123.77ms   1.99s    97.93%
    Req/Sec   792.26    261.16     1.41k    61.75%
  31574 requests in 10.04s, 4.73MB read
  Socket errors: connect 0, read 0, write 0, timeout 36
Requests/sec:   3144.16
Transfer/sec:    482.06KB
```

---

## Week 3 - Task Context, the Most Critical Week

### Status: PASSED
- **Test Suite:** `tests/stress/test_context_isolation.py`
- **Results:** 2/2 tests passed in 0.05s (WSL2 / Python 3.12.14 / pytest-asyncio).
- **Verified Guarantees:**
  1. No cross-task leakage across concurrent `asyncio.gather` workers.
  2. Immutability of parent context when child tasks call `Context.set()`.
  3. Clean fallback behavior when retrieving unset context keys (`default=None`).

---

## Week 4 - The DI Container

### Status: PASSED
* **Test Suite:** `tests/concurrency/test_resolution2.py`
* **Results:** 3/3 tests passed in 0.04s (WSL2 / Python 3.12.14 / pytest-asyncio).
* **Verified Guarantees:**
1. No cross-task leakage across 200 concurrent `asyncio.gather` worker tasks resolving `Scope.SCOPED` bindings simultaneously.
2. Complete task-scoped instance identity consistency within the same execution context (`inst_a is inst_b`).
3. Dynamic provider registration and idempotent deferred booting in `Application` without locking out late-registered service providers.

---

## Week 5 - Router, Middleware, Request and Response

### Status: PASSED
* **Test Suite:** `tests/http/test_http_pipeline.py`
* **Results:** 4/4 tests passed in 0.06s (WSL2 / Python 3.12.14 / pytest-asyncio).
* **Verified Guarantees:**
1. Accurate route parameter pattern matching and annotation-driven type coercion `(user_id: int)`.
2. Isolated middleware pipeline execution with in-flight request mutation and JIT container resolution.
3. Seamless route parameter tunneling `(*args, **kwargs)` through middleware adapter closures without arity mismatches.

---

## Week 6 - Connection Pool and Query Builder Foundation

### Status: PASSED

* **Test Suite:** `tests/db/load_test_database.py`
* **Results:** 2,000 total asynchronous database tasks (1,000 reads, 1,000 writes) completed successfully with zero connection leaks (WSL2 / Python 3.12.14 / pytest-asyncio).
* **Verified Guarantees:**
1. **Nested Transaction Integrity:** Task-scoped context variables successfully map to native PostgreSQL savepoints. Inner transactions gracefully roll back on exceptions without aborting the parent transaction block.
2. **Pool Saturation & Queue Management:** The asyncpg pool correctly bottlenecks at the configured saturation point (`max_size=20`) without deadlocking. Remaining tasks queue seamlessly in the connection wait-line across 1,000 concurrent requests.
3. **Leak Prevention:** Context manager teardown guarantees strict connection release. Post-load testing confirms active pool size matches idle size (20/20) with no orphaned connections.
4. **Throughput Performance:** System maintained stable execution under massive synthetic load, achieving ~657.25 Queries Per Second (QPS) for reads and ~833.07 writes per second.

---

## Week 7 - Query Builder Completion and Transactions

### Status: PASSED
* **Test Suites:** `tests/db/test_sql_injections.py` & `tests/db/load_test_database2.py`  
* **Results:** All 13 injection/feature tests passed; load test handled 1000 concurrent reads (1517 QPS) and 1000 concurrent writes (1826 writes/sec) with zero connection leaks (Environment: WSL2 / Python 3.12.14 / asyncpg).
* **Verified Guarantees:**
1. **SQL Injection Prevention** – All identifier inputs (table, column, order‑by, join, update keys, where columns, subqueries) are strictly validated via regex, blocking malicious payloads like `'; DROP TABLE` or `id; DROP`. Raw `where_raw()` binds values safely using parameterised queries, preventing injection even with malicious strings.
2. **Query Builder Correctness** – Aggregates (`count()`), `JOIN` with qualified columns, and `IN` subqueries return accurate results; chained methods (`select`, `where`, `limit`, `first`, `get`) compose properly.
3. **Transaction Semantics** – Nested transactions are implemented via savepoints; a rollback of the inner transaction does not affect the outer transaction, and an outer rollback cancels all nested work. Timeouts (e.g., `pg_sleep` inside a transaction) cancel and rollback the transaction correctly, leaving no orphaned data.
4. **Concurrent Load & Pool Health** – 1000 concurrent read workers and 1000 concurrent write workers completed without errors. The connection pool (size 50) returned to full idle capacity after all tasks, confirming no connection leaks. Performance: read QPS ~1517, write throughput ~1826 writes/sec.
5. **Data Consistency** – After all operations (including rollbacks and concurrent writes), the final row count (1101) matches expectations, and the nested transaction test correctly preserved the parent row while discarding the child row.

---

## Week 8 - The Active Record ORM

### Status: PASSED
* **Test Suite:** `tests/orm/test_eager_loading.py`
* **Results:** All 14 ORM test suites passed (100% test pass rate across 39 total tests in repository; strict mypy typing verified across 37 source files; ruff compliant).
* **Verified Guarantees:**
1. **Typed Model Base Class & Predictable Table Naming:** `Model` uses `typing.get_type_hints` at class definition time to dynamically inspect typed class attributes, supporting modern Python 3.12 syntax and field defaults. Automatic table naming uses a comprehensive zero-dependency inflection engine (`pluralize(snake_case(cls.__name__))`) supporting uncountables (`sheep`), irregulars (`person` $\to$ `people`, `mouse` $\to$ `mice`), suffix rules (`analysis` $\to$ `analyses`, `axis` $\to$ `axes`, `quiz` $\to$ `quizzes`, `bus` $\to$ `buses`), and case preservation, with explicit `__table__` overrides given first-class priority.
2. **Dirty-Tracking `save()` & Partial Select Protection:** `_mark_persisted()` tracks loaded column values. Updates only emit SQL for changed fields, preventing partial selects (`select("id", "name")`) from clobbering unselected columns, and inserts preserve database defaults (e.g., `NOW()`).
3. **Multi-Candidate Model Registry:** `_MODEL_REGISTRY` tracks models by qualified names and disambiguates candidates across modules, allowing test and application models to coexist without collision.
4. **LoadedList Dual-State Awaitability:** Eager-loaded collections use `LoadedList`, allowing `await user.posts` to resolve cleanly without `TypeError` in both eager and lazy states while functioning as standard lists.
5. **Relationships with Scoped Query Builders:** Implemented `HasMany`, `BelongsTo`, `HasOne`, and `BelongsToMany`. Each returns a lazily awaitable query builder scoped to the relationship (e.g., `await user.posts` or `await user.posts.where("published", True).get()`). Subclasses cleanly inherit relationships from ancestors.
6. **Explicit Eager Loading via `.with_()`:** Resolves related models across arbitrary collection sizes in batched `WHERE IN (...)` queries, loading dual relationship types (`HasMany` + `HasOne`) on 10 users in exactly 3 database queries instead of 21.
7. **BelongsToMany Integrity & Attach Enforcement:** `build_lazy_query` explicitly scopes columns to `target.*` to avoid pivot ID collisions, and `create()` on a `BelongsToMany` raises `NotImplementedError` to enforce explicit linking via `attach()`.
8. **Null Checking (`where_null` & `where_not_null`):** Raw `IS NULL` and `IS NOT NULL` expressions properly validated and compiled without operator mismatches.
9. **Mass Assignment Protection (`__fillable__`):** `Model.create(...)` enforces `__fillable__` whitelisting when declared, safely discarding unauthorized fields like `is_admin`.
10. **Strict N+1 Query Antipattern Prevention:** Synchronous access on un-eager-loaded relationships (`iter`, `len`, `bool`, subscripting, and direct model attribute access) strictly raises `RelationNotLoadedError` with actionable error messages, eliminating silent N+1 queries.

---

## Week 9 - Migration System, Schema Builder, and CLI Foundation

### Status: PASSED
* **Test Suite:** `tests/migrations/test_migrations.py`
* **Results:** All 14 migration, schema builder, concurrency lock, and CLI test suites passed (100% test pass rate across 53 total tests in repository; strict mypy typing verified across 41 source files; ruff compliant).
* **Verified Guarantees:**
1. **Framework-Application Decoupling:** CLI loader (`swiftpy/console/loader.py`) dynamically imports the project's application target (`load_application("main:app")`), ensuring `swiftpy` commands do not hardcode or leak imports from the consumer application.
2. **Deterministic Schema DDL Ordering:** `SchemaGrammar.compile_table` enforces a safe ordering: drops first (foreign keys, unique constraints, indexes, columns), renames second, and additions third. This guarantees that one blueprint can drop and replace an index, constraint, or column without collisions.
3. **Dedicated `drop_unique` Constraint Handling:** Dropping unique constraints routes to `ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{name}";` instead of dropping indexes, respecting PostgreSQL constraint-backed index semantics.
4. **Identifier Quoting & Reserved Words Support:** Comprehensive `quote_identifier` helper validates and quotes all table and column names across DDL and Query builder (select, where, join, order by, insert, update), allowing reserved words (`from`, `to`, `order`, `group`) and mixed-case identifiers.
5. **Enforced Column Comments and Unsigned Checks:** Column `.unsigned()` generates `CHECK ("{col}" >= 0)` constraints in PostgreSQL, and `.comment(...)` emits `COMMENT ON COLUMN "{table}"."{col}" IS '...'`.
6. **Schema DDL Transaction Atomicity:** `Schema._execute_statements` wraps all generated DDL statements inside an atomic `async with conn.transaction():` savepoint/transaction block, guaranteeing that partial failures in direct schema executions do not leave half-built tables.
7. **Clean Non-Destructive Defaults:** Removed dangerous `IF NOT EXISTS` from `CREATE TABLE` and `CREATE INDEX` (relying on the migration tracking table for idempotency), removed silent `CASCADE` drops, aligned `foreign_key` default to `NO ACTION`, and properly escaped structured JSON/list/datetime defaults.
8. **Automated `updated_at` Maintenance:** `Model.save()` automatically updates `updated_at = datetime.now(UTC)` when existing models are modified.
9. **Composite Primary Keys:** `Blueprint.primary(["post_id", "tag_id"], name=...)` generates table-level composite primary keys for pivot tables, and `Query.insert()` uses `RETURNING *` to support tables without standalone `id` columns.
10. **PostgreSQL Advisory Lock Concurrency & Timeouts:** `MigrationLock` prevents concurrent runner execution with configurable `lock_timeout` (default 300.0s). Concurrent migrations run through `asyncio.gather` verify that exactly one runner executes pending migrations (`[0, 0, 1]`).
11. **Strict Up-Front Missing File Validation:** `rollback()` and `reset()` validate all recorded migration files exist on disk before executing rollback transactions.
12. **Per-Migration Performance Timing & Scaffolding:** `Migrator.run()` and `rollback()` provide timing callbacks (`on_applied`, `on_rolled_back`) with terminal color formatting (`_paint()` respecting `sys.stdout.isatty()` and `NO_COLOR`). `make:migration` uses regex table guessing and generates typed `def define(table: Blueprint) -> None:` stubs.

---

## Week 10 - Authentication, JWT, and API Keys

### Status: PASSED
* **Test Suite:** `tests/auth/test_auth_security.py`
* **Results:** All 32 targeted authentication and security tests passed (100% test pass rate across 88 total tests in repository; strict mypy typing verified across 50 source files; ruff compliant).
* **Verified Guarantees & Tested Attack Paths:**
1. **Dual-Algorithm JWT Engine (`JWTIssuer`):** Implemented symmetric `HS256` (HMAC-SHA256 with constant-time `hmac.compare_digest` verification) and asymmetric `RS256` (PKCS#1 v1.5 RSA-SHA256 signing with private PEM and verification with public PEM). On-demand 2048-bit RSA key generation is provided via `JWTIssuer.generate_rsa_key_pair()`.
2. **Algorithm Confusion Defense (CVE-2015-9235):** Tested that an RS256 verifier strictly rejects HS256 tokens signed with an RSA public key as an HMAC secret. Allowed algorithms default strictly to the instance's configured algorithm (`{self.algorithm}`).
3. **Strict Alg "none" Across Case Variations:** Tested and verified that the `none` algorithm is rejected across all casing combinations (`none`, `NONE`, `None`, `nOnE`), raising `InvalidAlgorithmError`.
4. **Signature & Payload Tampering Rejection:** Rejects tokens with modified payloads, corrupted signature bytes, and tokens signed with the wrong cryptographic key both directly and through the HTTP application pipeline (returning HTTP 401 with `WWW-Authenticate: Bearer` and sanitized error bodies with zero key leakage).
5. **RS256 Key Mismatch Defense:** Verified that an RS256 token signed by RSA Keypair A is strictly rejected by an application configured with RSA Public Key B, returning HTTP 401.
6. **Token Claims & Boundary Math:** Verified token pair issuing with exact TTL math (`exp - iat`), UUID4 format for `jti`, and prevention of custom claims overriding reserved claims (`sub`, `iss`, `aud`, `exp`, `iat`, `jti`, `type`, `nbf`). Verified strict rejection of future `nbf` tokens, non-numeric timestamps (`nbf`, `iat`, `exp`), and missing `sub` claims.
7. **Token Type Enforcement:** Tested that presenting a refresh token to an access-guarded endpoint is rejected with HTTP 401 (`"Invalid token type: expected 'access', got 'refresh'"`).
8. **Redis Blacklist with Auto-Reclaim (`TokenBlacklist`):** Revoked tokens store `jti` in Redis with remaining lifespan as TTL (`ex=remaining`), tested with exact Redis `TTL` assertions. Expired tokens skip redundant Redis allocation. Tests use isolated per-run prefixes (`prefix=f"test_jwt_blacklist_{uuid.uuid4().hex[:8]}:"`) to avoid shared data collisions.
9. **Differentiated Infrastructure Failure Policy (HTTP 503):** Implemented and verified distinct HTTP status policies: invalid, missing, expired, or revoked credentials return HTTP 401 Unauthorized, whereas infrastructure outages (Redis connectivity failure, database connection drop) fail closed and return HTTP 503 Service Unavailable with a standard `Retry-After: 30` header.
10. **Bcrypt-Hashed API Key Guard (`require_api_key()` & `ApiKeyGuard`):** Validates raw API keys against bcrypt hashes in the database using constant-time `bcrypt.checkpw()`. Verified acceptance of valid keys (via `X-API-Key` or `Authorization: ApiKey`), rejection of inactive keys (`is_active = FALSE`), rejection of revoked keys, rejection of expired keys, rejection of missing headers, and verified that a database containing only malformed/corrupted hashes cleanly returns HTTP 401 without 500 server crashes.
11. **Upfront Identifier Validation:** Table and column identifiers are validated upfront at guard instantiation time (`ApiKeyGuard(table=...)`) via regex `^[a-zA-Z_][a-zA-Z0-9_]*$`, raising `ValueError` before any request or query execution.
12. **Duplicate Header Rejection Policy:** Verified explicit handling of duplicate credentials: multiple `Authorization` headers or multiple `X-API-Key` headers are strictly rejected with HTTP 401.
13. **Task-Scoped Container Resolution & Isolation:** `CurrentUser` is registered with `Scope.SCOPED` in the DI container and accessible via `current_user()`. Concurrency stress tests across 30 simultaneous asynchronous worker tasks verified 100% isolation with zero cross-task pollution. Verified that failed requests, handler exceptions, and task cancellations execute `finally: Context.clear()`, preventing scoped user state from leaking to subsequent requests.
14. **Cross-Application DI Isolation:** Verified that independent `Application` instances maintain strictly isolated DI containers and `CurrentUser` resolutions under concurrent requests.
15. **Decoupled Exception Architecture:** Base `HttpException` in `swiftpy/http/exceptions.py` transparently converts authentication errors without circular import coupling across `core`, `http`, and `auth` subsystems.

---

## Week 11 - Validation Engine

### Status: PASSED
* **Test Suites:** `tests/validation/test_validator_rules.py`, `tests/validation/test_db_rules.py`, `tests/validation/test_form_request_pipeline.py`, & `tests/validation/test_concurrency.py`
* **Results:** All 46 validation test suites passed (100% test pass rate across 134 total tests in repository; strict mypy typing verified across 72 source files; ruff compliant and formatted).
* **Verified Guarantees & Architectural Principles:**
1. **Typed Validation Engine (`validate(data, schema) -> ValidationResult`):** Non-blocking async validation coordinator parsing schema definitions and returning a typed `ValidationResult` with `is_valid`, `errors`, `validated_data`, and expressive helper methods (`passes()`, `fails()`, `first_error()`, `errors_for()`, `all_errors()`, `validated()`).
2. **Comprehensive Built-in Rules Suite & Edge Cases:**
   - Implemented `required` (rejects missing, null, empty string, whitespace-only, and empty collections).
   - Implemented `nullable` (skips downstream rule execution if value is null/empty string, but runs all downstream rules when non-null).
   - Implemented `str`, `int` (coercing integer strings while rejecting booleans/floats), `float` (coercing valid numeric strings while strictly rejecting non-finite floats like `NaN`, `inf`, and `-inf`), `bool` (coercing standard truthy/falsy tokens while rejecting arbitrary strings).
   - Implemented `email` (RFC-compliant format validation), `url` (validating http/https scheme and netloc), `min` & `max` (supporting strings, numbers, and collections), `between` (with range validation `min_val <= max_val`), `in_`, `not_in`, and `regex`.
3. **Non-Blocking Async Database Rules (`unique` & `exists`):** Query PostgreSQL asynchronously via `DatabasePool.acquire()` without blocking the event loop. Supports `ignore_id` in `unique` for updating existing records.
4. **SQL Injection Prevention:** Table and column identifiers for `unique` and `exists` are validated upfront against `^[a-zA-Z_][a-zA-Z0-9_]*$`, quoted safely with double quotes, and query values are bound via parameterized query placeholders (`$1`, `$2`), rejecting malicious SQL payloads with `ValueError`.
5. **Database Failure Policy & Information Disclosure Defense:** Database query errors (e.g., missing table, dropped connection) strictly raise exceptions and never silently pass `unique` or `exists`. In the ASGI pipeline, unhandled database errors during validation fail closed, returning HTTP 500 without leaking credentials, connection strings, or raw SQL queries to the client.
6. **Dynamic Table Isolation in Tests:** Database tests dynamically provision isolated table names (`test_acc_{uuid.uuid4().hex[:8]}`) with explicit teardown, preventing parallel test collisions.
7. **Extensible Custom Rules (`ValidatorRule` Protocol):** Defined `@runtime_checkable` `ValidatorRule` protocol supporting both synchronous and asynchronous custom rules, with duck-typing support for `passes()` or `validate()`, and custom formatted error messages.
8. **Flexible Rule Registry & Expression Parser:** `RuleRegistry` parses pipe-separated string expressions (`"required|email|min:5"`), list representations, rule instances, and standard Python types (`str`, `int`, `float`, `bool`), and provides global registration (`register_rule()`).
9. **Typed Request Parameter Validation & Automatic HTTP 422 Responses:** Implemented `FormRequest` base class. Route handlers declare typed parameters (e.g. `body: CreateUserRequest`); the ASGI pipeline inspects parameter annotations, parses the request JSON payload, validates asynchronously, and automatically returns HTTP 422 `Unprocessable Entity` with structured field errors (`{"errors": {"field": [...]}}`) when validation fails.
10. **Pre-Handler Malformed Input Rejection:** Rejects non-object JSON payloads (`b"[]"`, scalar strings, numbers, booleans) and empty/whitespace bodies (`b""`) upfront with HTTP 422 before the handler is ever invoked.
11. **Mass-Assignment Protection:** `form.validated()` returns only schema-defined and coerced fields, stripping untrusted extraneous attributes (e.g. `is_admin`, `role`), while `form.all()` retains the raw input parameters.
12. **Deterministic Concurrency & Non-Blocking Execution Guarantees:**
    - **Deterministic Event Synchronization:** Verified via `asyncio.Event` synchronization that Request A entering an asynchronous validation rule yields to the event loop, allowing concurrent Request B to validate, invoke its handler, and complete its HTTP response before Request A resumes and finishes.
    - **Connection Pool Concurrency:** Verified that 50 concurrent requests simultaneously executing database rules (`unique` and `exists`) complete without deadlocks, pool timeouts, or connection leaks.
    - **Cross-Request Pipeline Isolation:** Verified that 30 concurrent requests with alternating valid and invalid payloads maintain strict state isolation without cross-talk or leakage.

---

## Week 12 - Config, Error Handling, Testing Utilities, and Phase 2 Verification

### Status: PASSED
* **Test Suites:** `tests/core/test_config.py`, `tests/core/test_exceptions.py`, `tests/testing/test_test_client.py`, `tests/testing/test_fixtures.py`, `tests/e2e/test_crud_auth_e2e.py`, & `tests/e2e/test_concurrent_auth.py`
* **Results:** All **165 tests passed** in repository (100% pass rate; **85% statement coverage** across 4,127 statements exceeding the 80% threshold; strict mypy typing verified across 74 source files; ruff compliant and formatted).
* **Verified Guarantees & Architectural Principles:**
1. **Dotted Config Loader & Environment Variable Merging (`swiftpy/core/config.py`):**
   - **Automatic Startup Loading:** Verified that `Application()` instantiation automatically loads all `config/*.py` modules into `self.config` without manual calls.
   - **Dotted Key Access:** Verified dotted key traversal: `config("database.pool.max")`, `cfg.get()`, `__getitem__`, `__contains__`, and `__call__`.
   - **Deterministic Precedence Hierarchy:** Verified explicit multi-tier precedence:
     1. High tier: Framework-specific `SWIFTPY_` prefix (`SWIFTPY_DATABASE__POOL__MAX_SIZE=99`).
     2. Medium tier: Double-underscore nested syntax (`DATABASE__POOL__MAX_SIZE=88`).
     3. Low tier: Normalized single-underscore matching on existing dictionary paths (`DATABASE_POOL_MAX_SIZE=88` mapping to `database.pool.max_size`).
   - **Coercion & Strict Error Handling:** Type coercion for booleans, integers, floats, and JSON arrays/objects. Explicitly defined that malformed integers, non-finite floats, or invalid JSON syntax raise `ConfigurationError` rather than silently failing or leaving invalid values accidental.
2. **Global Exception Handling & Status Code Mapping (`swiftpy/core/exceptions.py`):**
   - **Real Router Method-Not-Allowed (HTTP 405):** Verified that sending a disallowed HTTP method (e.g. `GET /items`) to a route registered for another method (`POST /items`) returns HTTP 405 Method Not Allowed with the allowed methods declared in the `Allow: POST` header.
   - **Unmatched Route Routing (HTTP 404):** Verified that completely unmatched URL paths return HTTP 404 Not Found (`{"detail": "Not Found"}`).
   - **Unified Validation Error Mapping (HTTP 422):** Verified that both `ValidationError` and `ValidationException` map to HTTP 422 with structured field error dictionaries.
   - **Custom Handler Safety:** Guarded custom exception handlers against unhandled errors, ensuring safe fallback to HTTP 500 without crashing the ASGI dispatch loop.
   - **Broad Production Mode Masking (`debug=False`):** Verified that unhandled exceptions originating from database queries (e.g. simulated connection errors), authentication keys (e.g. private key errors), and handler crashes fail closed with a generic message (`{"detail": "Internal Server Error"}`), with zero leakage of passwords, paths, or tracebacks.
3. **Structured Error Mode Formatting:**
   - **Development mode (`debug=True`):** Unhandled exceptions return HTTP 500 with a structured JSON payload containing `"error"`, `"exception"`, `"message"`, and `"traceback"` (list of formatted traceback frames).
   - **Production mode (`debug=False`):** Safe generic JSON response with zero internal error disclosure.
4. **In-Process Testing Utilities (`swiftpy/testing/`):**
   - **`TestClient`:** In-process ASGI test driver executing requests directly through `scope`, `receive`, and `send` without network overhead.
   - **Client Immutability:** Verified that fluent chaining helpers (`.with_token()`, `.with_api_key()`, `.with_headers()`) return new independent `TestClient` instances without mutating the original client's default headers.
   - **Repeated Query Parameters:** Configured `doseq=True` in `urlencode()`, allowing repeated query parameters (`/search?tag=python&tag=asgi`) to be dispatched and parsed via `request.query_param_list`.
   - **Header Isolation & Error Safety:** Verified that concurrent requests on separate client instances maintain header isolation and that an error in one request does not leak into subsequent requests.
5. **Database Transaction Rollback Fixture (`swiftpy/testing/fixtures.py`):**
   - **Zero Residual Records:** Verified rollback across `INSERT`, `UPDATE`, and `DELETE` operations.
   - **Exception Rollback:** Verified that unhandled exceptions inside the test block trigger transaction rollback and restore database state.
   - **Context Cleanup:** Verified that `_active_connection` token is cleanly reset (`get_active_connection() is None`) upon context exit, even after errors.
   - **Direct Connection Binding:** Verified directly that `QueryBuilder._connection()` uses the active transaction connection object rather than acquiring a separate pooled connection.
   - **Concurrent Transaction Isolation:** Verified that two concurrent tasks run independent transactions on distinct physical connections with zero cross-transaction visibility.
6. **End-to-End CRUD and Auth Flow (`tests/e2e/test_crud_auth_e2e.py`):**
   - Verified complete user registration, password hashing via bcrypt, login, JWT issuance, and CRUD operations.
   - **Cross-User Authorization:** Verified a two-user scenario where User A creates an article and User B receives HTTP 403 Forbidden on update and delete attempts, while User A's article remains intact.
   - **Token Boundary Edge Cases:** Verified HTTP 401 Unauthorized for expired tokens, invalid token signatures, malformed bearer tokens, and non-bearer authorization headers, and HTTP 404 for tokens with non-existent user subjects.
7. **High-Concurrency Stress Verification (`tests/e2e/test_concurrent_auth.py`):**
   - **Raw Token Interleaving:** Verified 100 simultaneous authenticated requests with direct token decoding across 10 distinct user identities.
   - **Full Middleware & DI Pipeline Concurrency:** Verified a separate test with 100 concurrent requests executing through SwiftPY's real `JWTGuard` middleware, `AuthManager`, `current_user()`, and container-resolved `CurrentUser` scoped dependency injection, achieving 100% HTTP 200 pass rate with zero task context bleed.
8. **CLI Verification:**
   - Verified CLI entry points: `swiftpy --help`, `swiftpy migrate:status`, `swiftpy migrate`, and `swiftpy migrate:rollback`.

---

## Phase 2 Verification Checklist

- [x] **A complete CRUD API can be built using only SwiftPY**: Verified with native routing, ORM models, validation, and JWT authentication without external web frameworks.
- [x] **JWT and API key authentication both work**: Verified against expiry, signature tampering, casing, and Redis revocation.
- [x] **Validation rules, including async DB rules, are non-blocking**: Verified concurrent execution and `asyncio.Event` synchronization without blocking the event loop.
- [x] **Migrations run forward and backward cleanly on PostgreSQL**: Verified via `swiftpy migrate` and `swiftpy migrate:rollback`.
- [x] **Error responses are structured JSON in both dev and production modes**: Verified full traceback in dev mode and safe generic messages in production mode.
- [x] **The test suite covers all Phase 1 and Phase 2 components, coverage above 80%**: Verified with **85% statement coverage** across 4,127 statements (165/165 tests passed).
- [x] **`swiftpy make:migration`, `swiftpy migrate`, and `swiftpy migrate:rollback` all work from the CLI**: Verified in container environment.

---

# [Phase 03] - Application Infrastructure

---

## Week 13 - Cache Subsystem

### Status: PASSED
* **Test Suites:** `tests/cache/test_memory_cache.py`, `tests/cache/test_redis_cache.py`, `tests/cache/test_cache_manager.py`, & `tests/cache/test_cache_concurrency.py`
* **Results:** All **52 cache test suites passed** (100% test pass rate across **217 total tests** in repository; **85% statement coverage** across 4,535 statements; strict mypy typing verified across 82 source files; ruff compliant).
* **Verified Guarantees & Architectural Principles:**
1. **Abstract Driver Interface (`swiftpy/cache/driver.py`):**
   - Implemented `CacheDriver` abstract base class defining standard cache contracts: `get()`, `set()`, `remember()`, `forget()`, `has()`, `flush()`, `pull()`, `add()`, `increment()`, `decrement()`, and `close()`.
2. **In-Memory Cache Driver (`swiftpy/cache/drivers/memory.py`):**
   - **Monotonic TTL Expiration & Controllable Clock:** Uses `time.monotonic()` for clock-skew immune timestamping. Accepts an injected `clock` callable for deterministic, zero-sleep testing. Expired items are lazily cleaned on retrieval and immediate deletion occurs on non-positive TTLs.
   - **Object Deepcopy Isolation:** Clones values on both insertion and retrieval (`copy.deepcopy`), ensuring external mutation of retrieved mutable collections (dicts, lists) never leaks into or corrupts cached memory.
   - **Concurrency Safety:** Synchronized with `asyncio.Lock` for atomic check-and-set, reads, and writes.
3. **Redis Cache Driver (`swiftpy/cache/drivers/redis.py`):**
   - **Queue-Managed Connection Pooling (`BlockingConnectionPool`):** Configured `aioredis.BlockingConnectionPool` so massive concurrent bursts queue up smoothly without throwing `MaxConnectionsError`.
   - **Safe JSON Serialization:** Values are stored and retrieved using JSON serialization (`json.dumps`/`json.loads`), cleanly handling strings, numbers, booleans, lists, nested dicts, and `None` without unsafe deserialization vulnerabilities (`pickle`). Non-serializable types (`set`) strictly fail with `TypeError`. Raw non-JSON text in Redis cleanly falls back to raw string return without decoding crashes.
   - **Cross-Instance Distributed `remember()` Locking:** Uses Redis distributed locks (`SET lock_key token NX EX=10`) with token-matched Lua release script, automatic lease expiration, and waiter polling.
   - **Native Atomic Operations:** Atomic `SET [EX|PX] [NX]` for `set()` and `add()`; atomic `INCRBY` for `increment()` and `decrement()`; cursor-based scanning (`SCAN`) for scoped `flush()`.
4. **Cache Manager & DI Container Integration (`swiftpy/cache/cache.py`):**
   - **Driver Resolution & Aliasing:** Lazy instantiation of store drivers via `cache.driver(name)` or `cache.store(name)` with fallback to default driver.
   - **Service Provider Scaffolding:** `CacheServiceProvider` registers `Cache` as a singleton in the DI container, accessible via constructor autowiring, `container.resolve(Cache)`, or the global `cache()` helper in `swiftpy.helpers.helper`.
   - **Custom Driver Extensions:** Supports runtime driver extension via `cache.extend(name, factory)`.
5. **High-Concurrency Stress & Verified Guarantees:**
   - **Same-Key Concurrent Writes:** Verified across both `MemoryDriver` and `RedisDriver` that 200 simultaneous `set()` calls to the same key complete successfully, and the final cached value matches one complete submitted payload without corruption.
   - **Atomic Check-and-Set `add()`:** Verified that when 200 concurrent tasks race to insert the same previously missing key, exactly one task wins (`assert sum(results) == 1`) across both Memory and Redis drivers.
   - **Atomic Counters:** Verified that 200 concurrent increments never lose updates, producing all sequential values from 1 to 200. Verified mixed concurrent operations (100 workers `+3`, 100 workers `-1`) net to the exact mathematical sum (200).
   - **Cross-Instance Stampede Protection:** Tested two independent `RedisDriver` instances running 100 workers each (200 total) against the same key; callback executed exactly once globally (`execution_count == 1`) with all 200 workers receiving the identical computed result. Verified exception propagation releases locks cleanly without deadlocking subsequent calls.
   - **Prefix-Isolated Flushing:** Verified that flushing driver instance A only purges keys matching its configured prefix, leaving keys belonging to driver instance B completely untouched on both Memory and Redis backends.
   - **Distinct-Key Writes:** Verified 200 concurrent tasks writing distinct keys with zero cross-key leakage on both drivers.

---

## Week 14 - Events Subsystem

### Status: PASSED
* **Test Modules:** `tests/events/test_event_dispatcher.py`, `tests/events/test_model_events.py`, & `tests/events/test_redis_bus.py`
* **Results:** All **34 event tests passed** across 3 test modules (100% test pass rate across **251 total tests** in repository; strict mypy typing verified across 113 source files; ruff compliant).
* **Verified Guarantees & Architectural Principles:**
1. **In-Process Event Dispatcher (`swiftpy/events/dispatcher.py`):**
   - **Pub/Sub Subsystem:** Implemented `EventDispatcher` providing decoupled event registration via `listen(event_name, handler)` and asynchronous dispatching via `dispatch(event_name, payload)`.
   - **Fail-Fast Error Policy:** Handlers (both synchronous and asynchronous coroutines) adhere to a fail-fast error policy: an exception raised by a handler immediately halts further dispatching and propagates the original exception directly to the caller. Handlers executed before the failure are not re-executed, and subsequent handlers are skipped. Coroutine cancellation (`asyncio.CancelledError`) is strictly propagated without interception.
   - **Interleaved Global Registration Order:** Handlers are tracked with monotonic registration sequence numbers, guaranteeing exact invocation order across exact event names and wildcard matches alike.
   - **Segment-Scoped Wildcard Matching:** Compiles patterns to regex with strict segment boundaries: single segment `*` matches a single dot-delimited segment (`user.*` matches `user.created`, but strictly does NOT match nested multi-segment `user.profile.created`), suffix wildcard `*.created` matches single prefix (`user.created`), and `**` matches multi-segment hierarchies.
   - **Adaptive Signature Invocation:** Introspects listener signatures supporting zero-argument callables, single-argument callables (`payload`), two-argument callables (`event_name, payload`), keyword-only parameters (`*, payload`), and varargs (`*args`) without swallowing handler exceptions during execution.
   - **Lifecycle Management:** Supports listener querying (`has_listeners()`), handler unregistration (`unsubscribe()`), and single-event or bulk listener purging (`forget()`).
   - **Global & IoC Container Singleton:** Accessible as a singleton via `EventDispatcher.get_instance()`, container resolution, or the global `dispatcher()` / `event()` helper functions in `swiftpy.helpers.helper`.
2. **Cross-Process Event Delivery Bus (`swiftpy/events/redis_bus.py`):**
   - **Redis Pub/Sub Ephemeral Delivery Semantics:** Redis Pub/Sub operates on ephemeral, at-most-once delivery semantics (messages published during disconnection are not queued for later delivery).
   - **Strict JSON Envelope Serialization:** Formats cross-process messages as structured JSON envelopes containing `event`, `payload`, `sender_id`, and `timestamp`. Extends serialization to handle ORM model instances (`to_dict()`), timezone-aware datetimes (preserving ISO 8601 UTC offsets), dates, and UUIDs. Unsupported types (e.g. `set`) and circular references strictly raise `TypeError`.
   - **Client Ownership Preservation:** Distinguishes internally created Redis clients from caller-supplied clients. Closing the bus cleanly closes owned clients, while caller-supplied clients remain open for the caller's lifecycle.
   - **Idempotent Resource Teardown & Readiness:** Implements `wait_until_ready()` deterministic subscription synchronization, `close()` / `aclose()` idempotence, and safe cancellation of the background listener task without task leaks or unhandled task exceptions.
   - **Resilient Message Loop:** Background worker task safely logs and skips malformed JSON or invalid envelope messages without terminating. Exceptions thrown by local subscribers do not crash or abort the background loop.
   - **Prefix Isolation & Self-Publish Filtering:** Topic channels are prefixed (`swiftpy_events:*`), ensuring separate bus prefixes never receive each other's messages. Unique UUID `bus_id` filters out self-published events when `ignore_self=True`.
3. **ORM Model Lifecycle Hooks (`swiftpy/database/orm/model.py`):**
   - **Decoupled Lifecycle Dispatching:** Wired `create()`, `save()`, and `delete()` in the ORM `Model` base class to dispatch standard lifecycle events:
     - `create()` -> `{model_name}.created` (passing persisted model instance as payload)
     - `save()` on new instance -> `{model_name}.created` (passing persisted model instance as payload)
     - `save()` on existing instance -> `{model_name}.updated` (passing updated model instance as payload; repeated `save()` correctly transitions from `created` to `updated`)
     - `delete()` on persisted instance -> `{model_name}.deleted` (passing deleted model instance as payload)
   - **Failure Boundaries:** If a database operation fails (e.g., integrity constraint violation), no lifecycle event is emitted. Calling `delete()` on an unsaved model raises `RuntimeError` and emits no event. If a model lifecycle listener raises an exception, the failure propagates immediately to the caller.
   - **Normalized Model Event Naming:** Generates consistent event names via `snake_case(cls.__name__)` (e.g. `User` -> `user.created`, `AuditLogEntry` -> `audit_log_entry.created`).
   - **Zero Listener Coupling:** The ORM has zero internal knowledge of what listeners exist. If no listeners are registered, `dispatch()` returns immediately with zero side-effects.
4. **Service Provider & Configuration:**
   - **Configuration:** Created `config/events.py` for Redis event bus connection options (host, port, password, database, prefix).
   - **Service Provider:** Implemented `EventServiceProvider` registering `EventDispatcher` and `RedisEventBus` singletons in the application DI container.
5. **Cross-Process Verification:**
   - Verified that an event published by Worker Process A (`RedisEventBus.publish("order.cross_process.shipped", payload)`) is received and processed by a listener registered in a completely separate OS Worker Process B running in an independent Python interpreter, with the exact payload confirmed via Redis key storage within SLA.