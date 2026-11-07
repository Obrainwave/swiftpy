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

---

## Week 15 - Queues Subsystem

### Status: PASSED
* **Test Module:** `tests/queue/test_queue.py`
* **Results:** All **18 queue tests passed** (100% test pass rate across **269 total tests** in repository; strict mypy typing verified across 132 source files; ruff compliant).
* **Verified Guarantees & Architectural Principles:**
1. **Base Job Class (`swiftpy/queue/job.py`):**
   - **Async `handle()` Contract:** Subclasses inherit `Job` and implement the coroutine `async def handle(self) -> None`, called directly by worker processes upon dequeuing.
   - **Configurable Retry & Backoff Metadata:** Configurable `tries` (default 3), `attempts` counter, `timeout` execution limit, and `backoff` delay strategies supporting scalar constants (e.g. `backoff = 2.0`), progressive delay schedules (e.g. `backoff = [1.0, 5.0, 15.0]`), and immediate retry with `backoff = 0` (or `[0, 0]`).
   - **Permanent Failure Hook:** Supports `async def failed(self, exception: BaseException) -> None` hook invoked when all retries are exhausted. If the hook itself raises an exception, the worker catches the error, logs it, and still guarantees that the failed job record is safely routed to the Dead Letter Queue.
   - **Trusted Namespace Deserialization Security:** Mitigates arbitrary code execution before module import. Enforces a trusted namespace allowlist (`Job.set_trusted_namespaces(["app.", "tests."])`) that validates the target module path before invoking `importlib.import_module()`. Avoids `pickle` entirely, serializing jobs into structured JSON envelopes containing `job_class` (module:qualname), `job_id`, `queue`, `tries`, `attempts`, `backoff`, `timeout`, `args`, `kwargs`, and `data`. Deserialization dynamically verifies the target is a valid `Job` subclass and restores all instance parameters and persisted metadata.
   - **Instance Dispatch Convenience:** Provides `await job.dispatch(queue=..., delay=...)` instance method delegating to the application's queue dispatcher.
2. **Redis-Backed Queue Dispatcher (`swiftpy/queue/dispatcher.py`):**
   - **Dual Data Structure Architecture:**
     - **Active FIFO Queue:** Immediate jobs pushed via `RPUSH` onto Redis lists (`{prefix}{queue}`).
     - **Delayed & Scheduled ZSET:** Jobs with `delay > 0` stored in Redis sorted sets (`{prefix}{queue}:delayed`) with score set to `time.time() + delay`.
     - **Dead Letter Queue (DLQ):** Failed jobs routed to Redis lists (`{prefix}{queue}:failed`) capturing full diagnostic context (exception type, error message, formatted traceback, attempt counts, and failure timestamp).
   - **Connection Management:** Backed by `aioredis.BlockingConnectionPool` with lazy async client initialization, configurable concurrency limits, and clean lifecycle teardown (`close()` / `aclose()`).
   - **Queue Observability & Management:** Provides `size()`, `delayed_size()`, `failed_size()`, `get_failed_jobs()`, and `clear()` methods for active queue inspection and administration.
   - **Global Dispatcher & Helper:** Accessible as a singleton via `QueueDispatcher.get_instance()`, container resolution, or the global `push()` / `dispatch()` helpers.
3. **Standalone Worker Process (`swiftpy/queue/worker.py`):**
   - **Atomic Lua Delayed Job Promotion:** Atomically migrates scheduled jobs whose `score <= now` from the delayed ZSET to the active FIFO list using a Lua script (`zrangebyscore`, `zrem`, `rpush`). Guaranteed zero race conditions across concurrent workers (verified under 10 concurrent promotion tasks racing for 50 scheduled jobs).
   - **Delivery Contracts & Atomic FIFO Job Consumption:** Consumes jobs using atomic Redis `LPOP`, ensuring every queued job is delivered to exactly one worker process.
     - *Graceful Shutdown / In-Flight Cancellation Recovery:* In-flight tasks interrupted via `SIGINT`/`SIGTERM` or `asyncio.CancelledError` automatically catch cancellation, serialize the in-flight job payload, and re-enqueue it onto the active Redis queue via `LPUSH` before exiting, ensuring zero lost jobs.
     - *Ungraceful Kill Caveat:* If a worker process is terminated abruptly via hard `SIGKILL` mid-execution, atomic `LPOP` yields at-most-once delivery.
   - **Retry with Backoff Orchestration:** If a job fails and `attempts < tries`, the worker computes the backoff delay: if `delay > 0`, it pushes the job to the delayed sorted set; if `delay <= 0`, it immediately pushes it back to the active FIFO queue for immediate re-execution.
   - **Dead Letter Queue Routing:** If a job exhausts its maximum attempts, the worker invokes the job's `failed(exc)` hook and pushes the complete error record to the Dead Letter Queue.
   - **Malformed Envelope Resilience:** Corrupt JSON payloads and unresolvable or untrusted job classes are safely quarantined into the DLQ without crashing or terminating the worker process.
   - **Graceful Termination & POSIX Signal Handling:** Registers handlers for `SIGINT` (Ctrl+C) and `SIGTERM`, allowing in-flight jobs to complete or safely re-enqueue before closing Redis connections and shutting down.
   - **Batch & Testing Execution:** Supports `--stop-when-empty` mode to terminate when all monitored active queues and delayed queues are drained, as well as `--max-jobs N` to stop after processing `N` jobs.
4. **CLI Integration (`swiftpy queue:work`):**
   - Implemented `QueueCommand` in `swiftpy/console/commands/queue.py` registered in `swiftpy/console/cli.py`.
   - Supports CLI options: `--queue`, `--sleep`, `--max-jobs`, `--stop-when-empty`, `--timeout`, `--prefix`, and `--app` (application bootstrapping).
5. **High-Concurrency Multi-Process Stress Verification:**
   - Dispatched **500 distinct jobs** onto a single Redis queue.
   - Spawned **3 concurrent, independent OS worker processes** executing `swiftpy queue:work --stop-when-empty`.
   - Drained worker stdout/stderr asynchronously via `communicate()` inside `wait_for` to guarantee no OS 64KB pipe buffer deadlocks.
   - **Guarantees verified:**
     - Worker concurrency confirmed via OS PIDs: All 3 distinct worker OS processes actively participated and recorded their distinct PIDs in Redis (`unique_pids == 3`).
     - Total executed jobs = **500** (`assert total_executed == 500`).
     - Unique executed jobs = **500** (`assert unique_executed == 500`).
     - Exactly-once execution with **zero duplicates** and **zero dropped jobs**.
     - Active queue and delayed queue completely drained to 0.
     - Dead letter queue size = 0.
     - All 3 worker processes exited cleanly with exit code 0.
---

## Week 16 - WebSockets and Phase 3 Verification

### Status: PASSED
* **Test Modules:** `tests/websockets/test_websocket_connection.py`, `tests/websockets/test_websocket_manager.py`, `tests/websockets/test_websocket_concurrency.py`, `tests/websockets/test_websocket_routing_and_lifecycle.py`, & `tests/integration/test_queue_event_websocket.py`
* **Results:** All **35 WebSocket and Integration tests passed** (100% test pass rate across **304 total tests** in repository; strict mypy typing verified across 118 source files; ruff compliant).
* **Verified Guarantees & Architectural Principles:**
1. **ASGI WebSocket Connection Wrapper (`swiftpy/websockets/connection.py`):**
   - **Full ASGI 3.0 Lifecycle Support:** Implemented `WebSocketConnection` (aliased as `WebSocket`) wrapping ASGI scope, `receive`, and `send` callables. Supports `accept()`, `receive()`, `send()`, and `close()`.
   - **Handshake Protocol Invariants:** Accurately reads `websocket.connect` and sends `websocket.accept` with negotiated subprotocols and custom headers. Calling `accept()` repeatedly raises `WebSocketException`, and calling `close()` before accept cleanly transmits a closing handshake (e.g. 1008 rejection). Repeated `close()` calls are idempotent.
   - **Type-Strict Message Handling:** Enforces frame type integrity: `receive_text()` strictly accepts text frames (raising `WebSocketException` on binary frames), `receive_bytes()` strictly accepts binary frames (raising `WebSocketException` on text frames), and `receive_json()` safely catches malformed JSON and raises `WebSocketException`. Polymorphic `send()` automatically encodes strings, binary bytes, and JSON serializable dictionaries/lists.
   - **Connection State Machine:** Explicit `WebSocketState` (`CONNECTING`, `CONNECTED`, `DISCONNECTED`) with clean tracking of `close_code` and `close_reason`. Transmitting messages after client disconnect raises `WebSocketException` or `WebSocketDisconnect`.
   - **Parameter Extraction & Coercion:** Exposes `path`, `headers` (decoded Latin-1 and normalized), `query_params` (scalar), `query_param_list` (multi-value list), and per-connection `state`. Route parameters are matched and typed using handler type annotations.
2. **WebSocket Connection Manager (`swiftpy/websockets/manager.py`):**
   - **Active Connection & Room Tracking:** Implemented `WebSocketManager` managing active connections and room subscriptions with `asyncio.Lock` concurrency safety.
   - **Concurrent Broadcast Distribution:** Non-blocking `broadcast()` sends messages concurrently across all target connections using `asyncio.gather(..., return_exceptions=True)`.
   - **Pruning & Room Isolation:** Supports targeting specific rooms (`broadcast(msg, room="...")`) or custom predicate filters (`filter_fn=...`). Dead connections or connections that fail during `send()` are automatically pruned from active connections and room membership without interrupting delivery to other recipients.
   - **Fault-Isolated Filtering:** Custom `filter_fn` predicates that raise exceptions are caught and isolated, preventing an erroneous filter callback from aborting broadcasts to other valid connections.
   - **Invariants & Idempotency:** Connections can belong to multiple rooms simultaneously, leave individual rooms via `leave_room()`, or be completely removed via `disconnect()`. Repeated `connect()` and `disconnect()` calls are idempotent.
   - **IoC Container Integration:** Registered as singleton in the application container, accessible via `app.websocket_manager`, `container.resolve(WebSocketManager)`, or `websocket_manager()` / `ws_manager()` helper functions.
3. **Phase 1 Router & ASGI Application Wiring:**
   - **Unified Router Decorator:** Added `@app.websocket("/ws/chat/{room_id}")` and `@router.websocket(...)` / `@group.websocket(...)` into the Phase 1 router.
   - **Routing Isolation:** HTTP 405 Method Not Allowed responses strictly exclude `WEBSOCKET` from HTTP `Allow` headers, preventing HTTP/WS method confusion.
   - **Dispatched Handler Wrapping:** `_wrap_websocket_handler` dynamically binds `WebSocketConnection` and coerced path parameters based on handler signatures.
   - **Lifespan & Context Isolation:** `Application.__call__` routes `scope["type"] == "websocket"` and `scope["type"] == "lifespan"`. Manages task-scoped `Context.set("websocket", ws)`, handles `WebSocketDisconnect`, logs unhandled exceptions, and guarantees `Context.clear()` in `finally` across all exit paths (including 404 route not found and 1002 path parameter coercion errors). Lifespan startup failure emits standard `lifespan.startup.failed`.
4. **In-Process Testing Driver (`WebSocketTestSession`):**
   - Extended `TestClient` with `websocket_connect(path)` returning a non-blocking in-process `WebSocketTestSession` executing ASGI coroutines without network overhead or socket port exhaustion. Stores accepted subprotocols and headers, and normalizes response headers for case-insensitive access.
5. **High-Concurrency Stress Verification:**
   - Verified **50 concurrent in-process WebSocket sessions** received the expected broadcast with zero dropped messages.
   - Verified concurrent room isolation with **40 in-process sessions** across distinct rooms.
   - Verified concurrent request/acknowledgment exchanges across **30 simultaneous sessions**.
   - Verified concurrent full-duplex messaging with **15 concurrent sessions** executing independent sender and receiver tasks simultaneously on the same connection.
   - All concurrency tests utilize structured `try ... finally` blocks guaranteeing task cancellation and `asyncio.gather(..., return_exceptions=True)` teardown on assertion failures or timeouts.
6. **Queue-to-Event-to-WebSocket Integration:**
   - Verified the queue → event dispatcher → WebSocket manager path using an in-process `QueueWorker`, including delivery to clients in the target room and isolation from another room.
   - Test teardown executes in a guaranteed `try ... finally` block, ensuring Redis key cleanup and singleton resets (`WebSocketManager`, `EventDispatcher`, `QueueDispatcher`, trusted namespaces) even if test assertions fail.

---

## Phase 3 Verification Checklist

- [x] **Cache operations are correct under concurrent access, verified by a stress test.**
  - Verified in `tests/cache/test_cache_concurrency.py` (200 concurrent tasks across `MemoryDriver` and `RedisDriver` verifying atomic `add()`, `increment()`, distributed stampede-protected `remember()`, and same-key writes).
- [x] **Events fire and are received both within a single process and across worker processes via Redis.**
  - Verified in `tests/events/test_event_dispatcher.py` and `tests/events/test_redis_bus.py` (cross-process Redis Pub/Sub delivery between independent OS processes).
- [x] **Queued jobs are processed exactly once under concurrent workers, with retry and dead lettering both verified.**
  - Verified in `tests/queue/test_queue.py` (500 distinct jobs processed exactly once across 3 independent OS worker processes with zero duplicates and zero dropped jobs, plus retry with backoff and Dead Letter Queue routing).
- [x] **WebSocket connections accept, send, receive, and broadcast correctly, verified with multiple simultaneous connections.**
  - Verified in `tests/websockets/test_websocket_connection.py`, `tests/websockets/test_websocket_manager.py`, `tests/websockets/test_websocket_concurrency.py`, and `tests/websockets/test_websocket_routing_and_lifecycle.py` (50 simultaneous connections receiving broadcasts, room isolation, send failure pruning, concurrent disconnect/broadcast races, and full-duplex sender/receiver tasks).
- [x] **The queue-to-event-to-WebSocket integration test passes end to end.**
  - Verified in `tests/integration/test_queue_event_websocket.py` (queued job processed by in-process worker -> fires event -> broadcasts via WebSocket manager to clients in targeted room with guaranteed teardown).

---

## Week 17 - Developer Experience: CLI Scaffolding & Code Generation Stubs

### Status: PASSED
* **Test Modules:** `tests/console/test_stubs_and_make.py`
* **Results:** All **7 scaffolding and make tests passed** (100% test pass rate across **311 total tests** in repository; strict mypy typing verified across 120 source files; ruff compliant).
* **Verified Features & Architectural Design:**
1. **Dedicated Stubs Engine (`swiftpy/console/stubs/`):**
   - Implemented `get_stub(name: str) -> str` supporting dotted paths (`migration.create`), snake_case names (`migration_create`), and explicit extensions (`migration.create.stub`).
   - Reusable template stubs:
     - `migration.create.stub`, `migration.alter.stub`, `migration.update.stub`, `migration.blank.stub`, `migration.stub`.
     - `model.stub`: Active Record model template with `__table__`, `__fillable__`, and standard timestamps.
     - `service.stub`: Business logic service class template.
     - `request.stub`: FormRequest validation class template with typed `rules()` method.
     - `resource.stub`: API resource transformer class template with `to_dict()` and `collection()`.
     - `repository.stub`: Data access repository class template.
     - `schema.stub`: Data schema template with `schema = {}`.
2. **Laravel-Style Generators (`swiftpy/console/commands/make.py`):**
   - **`make:model <name> [-m] [--table] [--path] [--force]`:**
     - Scaffolds Active Record models in `app/models/` (creates directory automatically if absent).
     - Infers snake_cased pluralized table name from model name (or honors explicit `--table`).
     - Supports `-m` / `--migration` flag: automatically generates the corresponding `create_<table_name>_table` migration in `database/migrations/`.
   - **`make:service <name> [--path] [--force]`:** Scaffolds business services into `app/services/`.
   - **`make:request <name> [--path] [--force]`:** Scaffolds form requests into `app/http/requests/`.
   - **`make:resource <name> [--path] [--force]`:** Scaffolds API resources into `app/http/resources/`.
   - **`make:repository <name> [--path] [--force]`:** Scaffolds repositories into `app/repositories/`.
   - **`make:schema <name> [--path] [--force]`:** Scaffolds schemas into `app/schemas/`.
3. **Robust Scaffolding Invariants:**
   - **Nested Namespaces & Subdirectories:** E.g., `make:request Auth/LoginRequest` scaffolds into `app/http/requests/Auth/login_request.py` with `class LoginRequest(FormRequest)`.
   - **Collision Protection:** Prevents unintentional file clobbering by raising an error if target exists, unless `--force` is specified.
   - **Literal Braces Compatibility:** Uses direct placeholder substitution instead of `str.format()` to avoid syntax collisions with Python dictionary braces `{}` in stubs.

---

## Phase 0 - Green Tooling Baseline Verification

### Status: PASSED
* **Tasks Executed:** P0-T1 to P0-T4 (Environment, stray root scripts, test typing declarations, baseline capture)
* **Date:** 2026-10-28
* **Quality Gate Results:**
  - `ruff check .`: **All checks passed!** (0 errors)
  - `mypy swiftpy`: **Success: no issues found in 114 source files** (100% clean under strict mypy)
  - `mypy .`: Down from 82 errors in 14 files to 25 errors in 1 file (`tests/orm/test_eager_loading.py`). All 25 remaining errors are the documented public typing gap for Active Record relationships (`HasMany`, `BelongsToMany`, `HasOne`, `BelongsTo` descriptor typing), scheduled for structural resolution in Phase 7 (P7-M6).
  - `pytest`: **311 passed in 132.78s** (0:02:12) with full test suite green.
* **Environment & Source Changes:**
  - Cleaned root-owned cache artifacts (`.venv`, `.ruff_cache`, `.mypy_cache`, `.pytest_cache`) via Docker and initialized clean host venv using `uv sync --frozen` (Decision D-0).
  - Relocated standalone scripts `asgi_app.py` and `test_asyncio.py` to `examples/` and added `exclude = ["examples/"]` in `pyproject.toml`.
  - Added attribute and return type annotations to test modules across `tests/app`, `tests/auth`, `tests/db`, `tests/http`, `tests/concurrency`, `tests/stress`, `tests/validation`, `tests/test_placeholder.py`, and `tests/orm`.

---

## Phase 1 - Foundation & Cross-Cutting Fixes

### P1-F1: Unified SQL Identifier Validation
* **Status:** PASSED
* **Date:** 2026-10-28
* **Changes:**
  - Created centralized `swiftpy/database/identifiers.py` module with `MAX_IDENTIFIER_LENGTH = 63`, `validate_identifier()`, `validate_column_identifier()`, and `quote_identifier(..., allow_dotted=True, allow_wildcard=True)`.
  - Enforced strict regex matching (`re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*")`) rejecting trailing newlines (`\n`), spaces, special characters, and identifiers > 63 bytes (Postgres NAMEDATALEN limit).
  - Updated `QueryBuilder` (`from_table`, `join`, `where`, `where_null`, `where_not_null`, `order_by`, `select`, `insert`, `update`) to use centralized identifier validation and rejected `*` in `where`, `where_null`, `where_not_null`, `join`, `order_by`, `insert`, and `update`.
  - Replaced duplicate / permissive identifier regexes in `Blueprint`, `Schema`, `Migrator`, `UniqueRule`, `ExistsRule`, and `ApiKeyGuard`, and removed duplicate `self.id_column` assignment in `UniqueRule`.
  - Added unit test suite `tests/db/test_identifiers.py` and extended `tests/db/test_sql_injections.py` (moving inline `import pytest` to module top).
* **Quality Gate Results:** see the "Phase 1 closing gate" section below (per-item counts for this item were not measured separately; `tests/db/test_identifiers.py` has 10 tests).
* **Behavior note:** `quote_identifier` is now stricter than before. `*` is rejected in `join`, `order_by`, `where_null` and `where_not_null`, and `insert`/`update` column keys may not be dotted or wildcards.

---

### P1-F2: String Helpers Relocation & Acronym Handling
* **Status:** PASSED
* **Date:** 2026-10-28
* **Changes:**
  - Extracted string utilities (`snake_case`, `pluralize`, inflection rules, irregulars, and uncountables) out of `swiftpy/helpers/helper.py` into `swiftpy/support/strings.py`.
  - Re-exported `snake_case` and `pluralize` in `swiftpy/helpers/__init__.py`, `swiftpy/helpers/helper.py`, and `swiftpy/support/__init__.py` for backward compatibility.
  - Fixed acronym handling in `snake_case`: acronym runs followed by lowercase are separated correctly so that `snake_case("APIKey") == "api_key"` (resolving table name mismatch with `ApiKeyGuard`), `snake_case("HTTPServer") == "http_server"`, `snake_case("HTTPRequest") == "http_request"`, and `snake_case("UserAPIKey") == "user_api_key"`.
  - Documented the **Breaking** table name inference change for acronym model names (`APIKey` -> `api_keys`) in `CHANGELOG.md`.
  - Updated ORM (`swiftpy/database/orm/model.py`, `swiftpy/database/orm/relationships.py`) and CLI scaffolding (`swiftpy/console/commands/make.py`) to import directly from `swiftpy.support.strings`, eliminating circular import coupling from `helpers.py`.
  - Added test suite `tests/support/test_strings.py` (33 tests covering `APIKey`, `HTTPServer`, `UserProfile`, `already_snake`, digits, empty string, ORM acronym model table inference, irregulars, and `swiftpy.helpers` package re-export parity).
* **Quality Gate Results:** see the "Phase 1 closing gate" section below (`tests/support/test_strings.py` has 33 tests).

---

### P1-F3: Centralized Redis Connection Configuration Resolver
* **Status:** PASSED
* **Date:** 2026-10-28
* **Changes:**
  - Created `swiftpy/support/redis_config.py` offering `build_redis_url()` and `resolve_redis_params()`.
  - Enforced single-source-of-truth configuration resolution through `Config` exclusively (eliminating scattered raw `os.getenv` calls).
  - Sanitized credentials with RFC-compliant percent-encoding (`urllib.parse.quote(..., safe="")`) for passwords containing special characters (`@`, `:`, `/`, `#`, `?`, etc.) and usernames.
  - Provided automatic fallback to subsystem-specific configs (`cache.stores.redis`, `queue.connections.redis`, `events.redis`) and per-subsystem databases (`REDIS_CACHE_DB`, `REDIS_QUEUE_DB`, `REDIS_EVENTS_DB`), using explicit `None` checks (`_first_not_none`) so `database=0` and per-section DB keys are never shadowed by global `redis.database`.
  - Updated `swiftpy/cache/drivers/redis.py` and `swiftpy/queue/dispatcher.py` to use `resolve_redis_params`.
  - Added unit test suite `tests/support/test_redis_config.py` (13 tests covering URL generation, credential escaping, SSL schemes, keyword overrides, per-section DB overrides, and `database=0` precedence).
* **Quality Gate Results:** see the "Phase 1 closing gate" section below (`tests/support/test_redis_config.py` has 13 tests).

---

### Phase 1 closing gate (measured 2026-10-28, tests run in the `swiftpy_app` Docker container)
  - `ruff check .`: All checks passed!
  - `mypy .`: 25 errors in 1 file (`tests/orm/test_eager_loading.py`). These are the same relation-typing errors recorded in the Phase 0 baseline and scheduled for P7-M6. No new errors. `mypy swiftpy tests/db tests/support`: Success, 124 source files.
  - Targeted: `pytest tests/db/test_identifiers.py tests/db/test_sql_injections.py tests/support/ tests/orm/ tests/console/`: 89 passed in 39.22s.
  - Full suite: `pytest -q`: **386 passed in 140.15s** (0:02:20).
  - Known gaps: the README is not yet updated for the acronym table-name change (planned for P10-D1). The CHANGELOG notes it as Breaking.

---

## Phase 2 - Security Blockers

### P2-A1: JWT Secret Enforcement & Config Wiring (Decision D-1)
* **Status:** PASSED
* **Date:** 2026-10-28
* **Changes:**
  - Enforced strict minimum 32-byte secret validation for symmetric algorithms (`HS256`) in `JWTIssuer.__init__`, raising `ConfigurationError` on empty, `None`, or short secrets (< 32 bytes).
  - Enforced key requirement for asymmetric algorithms (`RS256`), raising `ConfigurationError` when both `private_key` and `public_key` are absent.
  - Added `JWTIssuer.from_config(config: Config | None = None)` factory reading `secret`, `algorithm`, `access_token_ttl`, `refresh_token_ttl`, `issuer`, `audience`, `leeway`, `private_key`, and `public_key` from `Config` (`auth.guards.jwt.*` or `auth.jwt.*`).
  - Wired `AuthServiceProvider.boot()` to resolve `JWTIssuer` eagerly when `not is_debug` and the default guard is `"jwt"`, ensuring `app.boot()` fails fast with `ConfigurationError` in non-debug mode if `JWT_SECRET` is unset.
  - In debug mode, if `JWT_SECRET` is absent, generates a distinct ephemeral random secret (`secrets.token_urlsafe(48)`) per `from_config` call and logs a security warning.
  - Removed the hardcoded default secret from `config/auth.py` and documented `JWT_SECRET=` and `AUTH_KEY_PEPPER=` in `.env.example`.
  - Added unit test suite `tests/auth/test_jwt_issuer.py` covering empty/short secrets, forged empty-secret token rejection, `from_config` round-trip (`issuer`/`audience`), non-debug `app.boot()` fail-fast, and distinct debug-mode ephemeral secrets.
* **Quality Gate Results:** see the "Phase 2 closing gate" section below.

---

### P2-A2: API Key Guard Overhaul (Decision D-2)
* **Status:** PASSED
* **Date:** 2026-10-28 (updated 2026-11-07)
* **Changes:**
  - Redesigned API key format into `<prefix>.<secret>` (`generate_api_key`) with unique indexed column `key_prefix` (Decision D-2).
  - Removed the legacy unsegmented full-table `SELECT *` fallback entirely; keys without `<prefix>.<secret>` (or with empty prefix/secret) are rejected upfront with `InvalidApiKeyError` before any database access.
  - Replaced O(N) full-table scan and event-loop-blocking bcrypt loop with a single indexed query (`SELECT * FROM "{table}" WHERE "{prefix_column}" = $1 LIMIT 1`) and constant-time HMAC-SHA256 verification (`hash_api_key`, `verify_api_key_hash`) using server pepper (`AUTH_KEY_PEPPER`, enforcing `MIN_PEPPER_BYTES = 32` across `get_server_pepper`, `hash_api_key`, `verify_api_key_hash`, `require_api_key`, `ApiKeyGuard`, and non-debug `AuthServiceProvider.boot`).
  - Offloaded legacy `$2...` bcrypt row verification to `loop.run_in_executor` (`_verify_api_key_hash_async`) so the asyncio event loop is never blocked.
  - Added dummy constant-time HMAC comparison when a prefix does not match any row, preventing prefix-enumeration timing side-channels.
  - Wired `get_active_connection()` from `swiftpy.database.transaction` before falling back to `DatabasePool.get_pool().acquire()` so tests using `db.transaction()` rollback fixtures share the active transaction connection.
  - Added `ApiKeyGuard.from_config(config, pool)` and wired `AuthManager._resolve("api_key")` to construct the guard from configuration.
  - Stripped `hash_column`, `key_hash`, `secret`, `password`, and `password_hash` from `request.state["user"]` and `Context` before returning.
  - Added `swiftpy/console/stubs/migration.api_keys.stub` and `swiftpy make:api-key` CLI command (`--prefix`, `--pepper`, `-m`/`--migration`, `--path`).
  - Added `tests/auth/test_api_key_guard.py` (10 tests covering prefix generation, peppered HMAC verification, production/short pepper enforcement, non-debug boot fail-fast, legacy format rejection, `from_config`, single-query count across a 40-row table, concurrent event-loop non-blocking ticker assertion, and active `db.transaction()` connection reuse).
* **Quality Gate Results:** see the "Phase 2 closing gate" section below.

---

### P2-A3: Token Revocation Hardening
* **Status:** PASSED
* **Date:** 2026-10-28
* **Changes:**
  - Removed `JWTIssuer.decode_unverified` so unverified JWT payloads cannot be trusted for revocation or Redis writes.
  - Updated `TokenBlacklist.revoke_token` to require a verified signature via an explicit `issuer` parameter, a constructor-injected `JWTIssuer`, or container resolution (raising `ValueError` if no `JWTIssuer` is available), and validated finite numeric `exp` claims via `math.isfinite`.
  - Wired `TokenBlacklist` singleton registration in `AuthServiceProvider` with `JWTIssuer` and `Config`, and updated `TokenBlacklist._get_redis()` to use `resolve_redis_params` from `swiftpy.support.redis_config`.
  - Updated `JWTIssuer.issue_token_pair()` so custom `claims` are attached only to the access token (never leaked onto the refresh token) and set JWT header `typ` to match the token type (`"access"` / `"refresh"`).
  - Updated `tests/auth/test_token_blacklist.py` (5 tests, including deterministic `monkeypatch` clock control replacing `asyncio.sleep`, forged/unverified token rejection, and `decode_unverified` removal check).
* **Quality Gate Results:** see the "Phase 2 closing gate" section below.

---

### P2-A4: Stop Leaking Internals in Auth Errors
* **Status:** PASSED
* **Date:** 2026-10-28
* **Changes:**
  - Standardized client-facing 401 messages across `JWTGuard`, `ApiKeyGuard`, and `TokenRevokedError` (`"Invalid or expired token"` for JWT failures and `"Invalid API key"` for API key failures) so expected `iss`/`aud` values, revocation cutoffs, and raw parser exceptions are never echoed to clients.
  - Standardized 503 messages (`AuthenticationServiceUnavailableError("Authentication service unavailable")`) when Redis or PostgreSQL is unreachable.
  - Logged verification warnings via `logger.warning` (without raw tokens) and backend infrastructure errors via `logger.exception`.
  - Added `test_auth_errors_never_leak_issuer_audience_or_token_p2a4` and updated error message assertions in `tests/auth/test_auth_security.py`.
* **Quality Gate Results:** see the "Phase 2 closing gate" section below.

---

### P2-A5: JWT Hardening & Tidy-Up
* **Status:** PASSED
* **Date:** 2026-10-28
* **Changes:**
  - Added `leeway: int = 0` to `JWTIssuer.__init__`, `from_config`, and `decode_token`; enforced future `iat` (`iat > now + active_leeway`), future `nbf` (`now + active_leeway < nbf`), and `exp` leeway (`now - active_leeway >= exp`).
  - Added `_is_valid_timestamp` rejecting booleans (`True`/`False`, since `isinstance(True, int)` in Python) and non-finite floats (`NaN`, `Infinity`, `-Infinity`) across `exp`, `iat`, `nbf`, `expires_in`, `not_before`, and `leeway`, and passed `allow_nan=False` to `json.dumps`.
  - Added `aud: str | list[str]` support in both token payloads and `JWTIssuer.audience`.
  - Enforced exact-case matching on `alg` (`"hs256"` is rejected while all case variations of `"none"` remain forbidden).
  - Raised `ValueError` in `JWTIssuer.issue_token` if `claims` contains any reserved claim (`RESERVED_CLAIMS`: `sub`, `exp`, `iat`, `jti`, `type`, `typ`, `nbf`, `iss`, `aud`).
  - Treated `expires_in=0` explicitly (`self.access_token_ttl if expires_in is None else expires_in`) across `issue_access_token`, `issue_refresh_token`, and `issue_token_pair`.
  - Validated `expires_at` type in `ApiKeyGuard` (accepting `datetime` or ISO-8601 `str`, raising `InvalidApiKeyError` on invalid column types instead of `AttributeError`).
  - Removed all inline imports from `swiftpy/auth/jwt_guard.py`, `swiftpy/auth/auth_manager.py`, and `tests/auth/test_auth_security.py`.
  - Added `TestJWTHardeningP2A5` in `tests/auth/test_jwt_issuer.py` and `test_api_key_expires_at_type_validation_p2a5` in `tests/auth/test_api_key_guard.py`.
* **Quality Gate Results:** see the "Phase 2 closing gate" section below.

---

### P2-CLI1: `make:*` Path Containment & Identifier Validation
* **Status:** PASSED
* **Date:** 2026-10-28
* **Changes:**
  - Added `_validate_and_split_name` in `swiftpy/console/commands/make.py`, validating every path segment against `^[A-Za-z_][A-Za-z0-9_]*$` both before and after `snake_case`, and rejecting empty names, backslashes, leading slashes, `.` / `..`, spaces, hyphens, and leading digits.
  - Enforced `filepath.resolve().is_relative_to(base_dir.resolve())` before and after directory creation in `_scaffold_file` and `handle_migration`, blocking `..` traversal, absolute paths, and symlink escapes outside the target base directory.
  - Validated `--table`, `--create`, and inferred table names via `validate_identifier` from `swiftpy.database.identifiers` before stub substitution.
  - Extended `tests/console/test_stubs_and_make.py` with `test_make_path_containment_and_validation_p2_cli1` and `test_make_api_key_command_and_migration_stub_p2a2`.
* **Quality Gate Results:** see the "Phase 2 closing gate" section below.

---

### Phase 2 closing gate (measured 2026-11-07, tests run in the `swiftpy_app` Docker container)
  - `ruff check .`: **All checks passed!** (0 errors)
  - `mypy swiftpy app config tests/auth tests/console`: **Success: no issues found in 142 source files**
  - `mypy .`: 25 errors in 1 file (`tests/orm/test_eager_loading.py`). These are the same relation-typing errors recorded in the Phase 0 baseline and scheduled for P7-M6. Zero errors in `swiftpy/` or Phase 2 tests (checked 182 source files).
  - Targeted: `pytest -q tests/auth/ tests/console/test_stubs_and_make.py`: **79 passed in 25.92s**.
  - Full suite: `pytest -q`: **408 passed** (406 baseline + 2 new pepper/boot tests).

---

## Code Review Remediation – Phase 3: Database Correctness and Safety

### P3-D1: `count()` and Terminal Methods Must Not Mutate the Builder
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Added `QueryBuilder.clone()` and `ModelQueryBuilder.clone()`.
  - Updated `QueryBuilder.first()`, `QueryBuilder._aggregate()` (`count`, `sum`, `avg`, `min`, `max`), and `ModelQueryBuilder.first()` to operate on `self.clone()`, preserving `_columns`, `_limit`, and `_order_column` on the original builder across repeated terminal calls.
  - Cleared `_order_column` inside `_aggregate()` and `exists()` so PostgreSQL never raises `GroupingError` when `.order_by()` was chained prior to `.count()`, `.exists()`, or aggregate helpers.
  - Added `.exists()` on `QueryBuilder`, `ModelQueryBuilder`, and `RelationshipQueryBuilder` (`SELECT EXISTS(SELECT 1 FROM ... LIMIT 1)`), and added `.sum()`, `.avg()`, `.min()`, and `.max()` on `ModelQueryBuilder`.
  - Created `tests/db/test_query_builder_terminals.py` (2 tests verifying builder immutability across `order_by` + `count` + `exists` + `first` + `get`, and `ModelQueryBuilder` aggregate/terminal immutability).

---

### P3-D2: Arbitrary Primary Keys (`int`, `UUID`, Custom String Keys)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Added `QueryBuilder.insert_returning(data, *, returning=None) -> dict[str, Any]` and overloaded `QueryBuilder.insert(data, *, returning=None)` so `insert(data)` preserves `int` return semantics for default callers while `insert_returning(data)` returns the full `RETURNING *` row dictionary.
  - Updated `Model.create()` and `Model.save()` (on insert) to hydrate the model instance directly from `insert_returning(data)` in **1 query**, eliminating the redundant second `SELECT` (`cls.find()`) and supporting `UUID` and custom string primary keys (`__primary_key__ = "code"`).
  - Added `PrimaryKeyError(RuntimeError, ValueError)`: `Model.__init_subclass__` rejects composite or non-string `__primary_key__` definitions, and `Model.save()` on an existing model with `pk is None` raises `PrimaryKeyError`.
  - Created `tests/db/test_arbitrary_primary_keys.py` (5 tests covering 1-query `Model.create()`, `UUID` primary key lifecycle, custom string `code` primary key lifecycle, unset primary key on existing model raising `PrimaryKeyError`, and composite primary key rejection).

---

### P3-D3: Unguarded Mass Writes and Invalid SQL Compilation (Decision D-10)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Added `QueryError(ValueError, RuntimeError)` and `escape_like(value, escape_char="\\")`.
  - Added `.allow_all()` to `QueryBuilder` and `ModelQueryBuilder`; `update()` and `delete()` without any `WHERE` clause now raise `QueryError` unless `.allow_all()` was explicitly called.
  - Raised `QueryError` on `insert({})` and `update({})`.
  - Compiled `IN []` to `FALSE` and `NOT IN []` to `TRUE`, while rejecting non-list/non-`QueryBuilder` operands for `IN`/`NOT IN` with `QueryError`.
  - Rejected `None` with comparison/pattern/set operators (`<`, `>`, `<=`, `>=`, `LIKE`, `ILIKE`, `IN`, `NOT IN`) with `QueryError`.
  - Added `where_in()`, `where_not_in()`, `where_like()`, and `where_ilike()` (emitting `ESCAPE '\'` and escaping `%`, `_`, and `\` literals).
  - Created `tests/db/test_mass_writes.py` (5 tests covering unguarded `update`/`delete` vs `.allow_all()`, empty `insert({})`/`update({})`, empty/invalid `IN`/`NOT IN`, `None` comparison operators, `where_like`/`where_ilike` literal escaping, and SQL parameter binding assertions).

---

### P3-D4: Transaction and Connection Ownership
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Added `TransactionClosedError(RuntimeError)` and `TransactionConnection` wrapper in `swiftpy/database/transaction.py` (and wrapped `transactional_test` in `swiftpy/testing/fixtures.py`), enforcing a `closed` state flag when a transaction block exits and serializing concurrent operations (`fetch`, `fetchrow`, `fetchval`, `execute`, `executemany`, and nested savepoint `transaction()`) on the same connection via a per-transaction `asyncio.Lock`.
  - Reset `_active_connection` via `ContextVar.reset(token)` after marking the handle closed.
  - Wired `UniqueRule` (`swiftpy/validation/rules/unique.py`) and `ExistsRule` (`swiftpy/validation/rules/exists.py`) with `requires_db = True` and `get_active_connection()` reuse before acquiring from `DatabasePool`.
  - Updated `Validator.validate()` (`swiftpy/validation/validator.py`) to execute field and rule validation sequentially when `get_active_connection() is not None`.
  - Eliminated circular imports by importing `get_container` from `swiftpy.core.container` instead of `swiftpy.core.bootstrap`.
  - Created `tests/db/test_transaction_isolation.py` (5 tests covering child task post-exit `TransactionClosedError`, serialized `asyncio.gather` queries inside `transaction()`, `Validator` with `unique` + `exists` seeing uncommitted rows inside `transaction()`, `max_size=1` pool + `transaction()` + `unique` non-deadlock, and exception/cancellation connection release).

---

### P3-D5: Pool Configuration, SSL Modes & Safe Shutdown
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Guarded `DatabasePool.init()` with an `asyncio.Lock` so concurrent callers share a single pool initialization.
  - Added `_build_ssl_context()` supporting `disable`, `require`, `verify-ca`, and `verify-full` plus `ssl_ca_file`, mapping boolean `True` / `"true"` to `require` with a logged warning for backward compatibility, and raising `ConfigurationError` on unsupported SSL modes.
  - Wired `database.timeouts.statement` (`server_settings={"statement_timeout": ...}`) and `database.pool.command_timeout` into `asyncpg.create_pool()`.
  - Updated `DatabasePool.close(timeout=5.0)` to use `asyncio.wait_for` and fall back to `pool.terminate()` on timeout so shutdown never hangs with in-flight connections.
  - Lowered default `database.pool.max_size` from `50` to `20` in `swiftpy/database/pool.py` and `config/database.py`, and enforced non-empty database passwords in production/non-debug mode (`ConfigurationError` raised outside debug mode; warning logged in debug mode).
  - Created `tests/db/test_pool_config.py` (5 tests covering SSL mode mapping, concurrent `init()`, `statement_timeout`/`max_size` wiring, production empty-password rejection, and `close(timeout=0.1)` with an in-flight connection).

---

### P3-D6: Migrations Hardening
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Whitelisted `ALLOWED_COLUMN_TYPES` in `ColumnDefinition` and `SchemaGrammar._compile_column`, and validated `length` (`1..10_485_760`), `precision` (`1..1000`), and `scale` (`0..precision`) as non-boolean integers in range.
  - Validated all generated constraint and index names (`idx_`, `uq_`, `pk_`, `fk_`) via `validate_identifier` (enforcing the 63-character limit).
  - Added `DefaultRaw` / `raw(sql)` helper for SQL default expressions (`Blueprint.timestamps()` now uses `raw("CURRENT_TIMESTAMP")`); legacy magic strings in `.default("NOW()")` emit a `DeprecationWarning`, while arbitrary strings and objects are single-quote escaped and non-finite floats raise `ValueError`.
  - Updated `Migrator` to store SHA-256 checksums in `"checksum" VARCHAR(64)` (auto-upgrading existing migration tables with `ADD COLUMN IF NOT EXISTS`), fail with `RuntimeError` on checksum drift or out-of-order unapplied migrations, and validate `step >= 1` / `steps >= 1` with `ValueError`.
  - Updated `swiftpy migrate`, `swiftpy migrate:rollback`, and added `swiftpy migrate:reset` in `swiftpy/console/commands/migrate.py` with `--step >= 1` validation and a production guard refusing execution when `APP_ENV=production` unless `--force` is passed.
  - Extended `tests/migrations/test_migrations.py` with `test_blueprint_validation_and_raw_defaults`, `test_migrator_checksum_drift_and_out_of_order`, and `test_cli_step_validation_and_production_guard` (17 migration tests total).
* **Quality Gate Results:** see the "Phase 3 closing gate" section below.

---

### Phase 3 closing gate (measured 2026-11-07, tests run in the `swiftpy_app` Docker container)
  - `ruff check .`: **All checks passed!** (0 errors)
  - `mypy swiftpy app config tests/db tests/migrations`: **Success: no issues found in 147 source files**
  - `mypy .`: 25 errors in 1 file (`tests/orm/test_eager_loading.py`). These are the same relation-typing errors recorded in the Phase 0 baseline and scheduled for P7-M6. Zero errors in `swiftpy/` or Phase 3 tests (checked 187 source files).
  - Targeted: `pytest -q tests/db/ tests/migrations/ tests/orm/ tests/validation/`: **121 passed in 63.47s**.
  - Full suite: `pytest -q`: **433 passed in 182.79s** (408 post-Phase-2 + 25 new Phase 3 tests).

---

## Code Review Remediation – Phase 4: HTTP Correctness

### P4-H1: `Response` Body, Multi-Value Headers (`Set-Cookie`) & CRLF Injection Prevention
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Added `ResponseHeaders(MutableMapping[str, str])` in `swiftpy/http/response.py` backed by an ordered list of `(name, value)` pairs, supporting case-insensitive dict-style access while preserving duplicate headers via `.add(name, value)`, `.getlist(name)`, and `.raw_items()`.
  - Added `Response.set_cookie(...)` appending a `Set-Cookie` header without overwriting earlier cookies.
  - Added `_validate_header_pair()` rejecting `\r`, `\n`, and `\x00` in header names and values with `ValueError`.
  - Encoded `str` bodies as UTF-8, defaulted `Content-Type: text/plain; charset=utf-8` for `str` bodies when unset, and set `Content-Length` automatically on non-streaming responses (except `1xx`, `204`, `304`).
  - Updated `TestResponse` and `TestClient` in `swiftpy/testing/http_client.py` to expose `ResponseHeaders` (`.getlist()`) and `.head()` / `.options()` helpers.

---

### P4-H2: JSON Encoding (`encode_json`) & `Model.__hidden__`
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Added `encode_json()` and `_json_default()` (`allow_nan=False`) in `swiftpy/http/response.py` supporting `datetime`/`date`/`time` (ISO-8601), `UUID` (`str`), `Decimal` (`str`), `Enum` (`.value`), and objects with `.to_dict()`, while rejecting `bytes` and `NaN`/`Infinity` with `TypeError`/`ValueError`.
  - Added `__hidden__: ClassVar[set[str] | list[str] | tuple[str, ...]] = ()` on `Model` (`swiftpy/database/orm/model.py`) and updated `Model.to_dict()` to omit `__hidden__` fields and relations while preserving raw Python types on `.to_dict()`.
  - Created `tests/http/test_response.py` (24 tests covering `P4-H1` and `P4-H2`).

---

### P4-H3: Router Precedence, Regex Escaping, Strict Path Coercion, WebSocket Isolation & Auto `HEAD`/`OPTIONS`
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Escaped literal path segments with `re.escape` in `Route.__post_init__` (`swiftpy/http/router.py`).
  - Implemented static-first route precedence key `(num_dynamic, segment_kinds, -static_chars, order)` and per-method route lookup index `_routes_by_method`.
  - Added duplicate `(method, normalized_path)` detection raising `RouteRegistrationError(ConfigurationError, TypeError)`.
  - Added strict `_coerce_bool` (`true/false/1/0/yes/no`, case-insensitive) and finite `_coerce_float` (rejecting `NaN`/`Infinity`), raising `PathParamCoercionError(ValidationError)` (HTTP 422).
  - Isolated `WEBSOCKET` routes to `match_websocket()`; HTTP requests with method `"WEBSOCKET"` never match WebSocket routes, and `add_route("WEBSOCKET", ...)` without `is_websocket=True` raises `RouteRegistrationError`.
  - Added automatic `HEAD` fallback to `GET` (preserving `Content-Length` and stripping the response body) and automatic `OPTIONS` returning `204 No Content` with the `Allow` header.

---

### P4-H4: Dependency Injection into Handlers & Registration-Time Signature Caching
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Added `Container.can_resolve(target)` in `swiftpy/core/container.py` and updated `CurrentUser.__init__` in `swiftpy/auth/auth_manager.py` to default to reading `Context.get("__swiftpy_current_user__")`.
  - Precomputed `inspect.signature`, `get_type_hints` (including caller-frame `localns`), `param_coercers`, and `param_specs` (`"request"`, `"path"`, `"form"`, `"di"`, `"default"`, `"websocket"`) once at route registration time in `swiftpy/http/router.py` and `swiftpy/core/application.py` (zero `inspect.signature` calls per request).
  - Removed `.schema`/`.rules` duck-typing and the untyped first-parameter `Request` fallback; only `issubclass(ann, FormRequest)` is a body validator and only `issubclass(ann, Request)` injects `Request`. Unsatisfiable or untyped parameters without defaults raise `RouteRegistrationError` at registration time naming both the handler and the parameter.
  - Created `tests/http/test_router.py` (29 tests covering `P4-H3` and `P4-H4`).

---

### P4-H5: Request Body Size Limits, Disconnect Handling, Header `getlist` & Cached Query Params
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Added `PayloadTooLargeError(HttpException)` (status 413) and `ClientDisconnect(Exception)` in `swiftpy/http/exceptions.py`.
  - Enforced `max_body_size` (default `DEFAULT_MAX_BODY_SIZE = 2 MiB`, configurable on `Application` and per-route) in `Request.body()` (`swiftpy/http/request.py`) both up-front via `Content-Length` and while streaming chunks.
  - Handled `{"type": "http.disconnect"}` mid-stream by raising `ClientDisconnect` and catching it in `Application.__call__` so the handler is not invoked and no 500 error is logged or sent.
  - Added `RequestHeaders(Mapping[str, str])` decoding headers as `latin-1`, caching the result, and supporting `.getlist(name)` (preserving duplicates) and last-wins case-insensitive dict access.
  - Cached `query_params` and `query_param_list` (decoded as UTF-8 with `errors="replace"`), moved `from urllib.parse import parse_qs` to top of `swiftpy/http/request.py`, and mapped direct `await request.json()` syntax/encoding/recursion errors to `HttpException(status_code=400)`.
  - Created `tests/http/test_request.py` (9 tests covering `P4-H5`).

---

### P4-H6: Error Handling Pipeline, Production Debug Guard & Built-In Security Middleware
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Removed duck-typing on `.errors` in `ExceptionHandler` (`swiftpy/core/exceptions.py`); only `ValidationError` and `ValidationException` map to HTTP 422.
  - Selected custom exception handlers by MRO specificity (`for cls in type(exc).__mro__:`), logged 4xx `HttpException` at `logger.info` and 5xx at `logger.error` without bodies, and guarded custom handlers and `ExceptionHandler.handle` with a safe 500 fallback.
  - Refused `Application(debug=True)` when `APP_ENV=production` (`ConfigurationError`).
  - Added `app.middleware([...])` global middleware pipeline wrapping `_dispatch_http` so global middleware executes before route-group middleware and post-processes 200, 404, 405, 422, and 500 responses.
  - Populated `config/cors.py` with deny-all defaults (`allowed_origins: []`) and implemented `SecurityHeadersMiddleware` (`SecurityHeaders`), `RequestIdMiddleware` (`RequestId`), and `CORSMiddleware` (`CORS`) in `swiftpy/http/middleware.py`.
  - Created `tests/http/test_error_pipeline.py` (10 tests covering `P4-H6`).
* **Quality Gate Results:** see the "Phase 4 closing gate" section below.

---

### Phase 4 closing gate (measured 2026-11-07, tests run in the `swiftpy_app` Docker container)
  - `ruff check .`: **All checks passed!** (0 errors)
  - `mypy swiftpy app config tests/http tests/core tests/validation tests/websockets tests/testing`: **Success: no issues found in 154 source files**
  - `mypy .`: 25 errors in 1 file (`tests/orm/test_eager_loading.py`). These are the same relation-typing errors recorded in the Phase 0 baseline and scheduled for P7-M6. Zero errors in `swiftpy/` or Phase 4 tests (checked 191 source files).
  - Targeted: `pytest -q -p no:cacheprovider tests/http/ tests/core/ tests/validation/ tests/websockets/ tests/testing/`: **148 passed in 71.61s**.
  - Full suite: `pytest -q -p no:cacheprovider`: **473 passed in 250.95s** (433 post-Phase-3 + 40 new Phase 4 tests).

---

## [Code Review Fix Plan – Phase 5: Lifecycle, Container, and Config (`P5-C1`…`P5-C6`)]

### P5-C1: Async Startup/Shutdown Lifecycle (`C-1`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Added optional `async def startup(self) -> None` and `async def shutdown(self) -> None` default no-op lifecycle hooks to `ServiceProvider` (`swiftpy/core/providers/base.py`).
  - Added `FrameworkServiceProvider` (`swiftpy/core/application.py`) registered first on `Application` (starts first, shuts down last): initializes `DatabasePool` on `startup()` when database config exists, and on `shutdown()` closes active WebSocket connections with code `1001` (`Going Away`), stops and closes `RedisEventBus`, closes Redis clients (`Cache`, `QueueDispatcher`, `TokenBlacklist`), and closes `DatabasePool`.
  - Added `Application.startup()` (runs provider `startup()` in registration order and rolls back already-started providers in reverse order on failure before sending `lifespan.startup.failed`) and `Application.shutdown()` (runs provider `shutdown()` in reverse order, catching/logging individual errors, before sending `lifespan.shutdown.complete`).
  - Added `startup()`, `shutdown()`, `__aenter__()`, and `__aexit__()` to `TestClient` (`swiftpy/testing/http_client.py`).
  - Created `tests/core/test_lifespan.py` (5 tests).

---

### P5-C2: Container Semantics (`C-2`, `C-3`, `D-7`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Updated `Container.bind()`, `Container.singleton()`, and `Container.scoped()` (`swiftpy/core/container.py`) to invalidate any cached singleton or current-task scoped entry for that interface (Decision `D-7`).
  - Wrapped `NameError`/`TypeError`/`ValueError` from `get_type_hints()` and `inspect.signature()` in `_resolve_callable_dependencies` as `ResolutionError` naming the target class/callable.
  - Cached `None` singleton results via `_NONE_SENTINEL` and added `Container.get_singleton(interface)`.
  - Keyed `Scope.SCOPED` cache entries in `Context` with `(cur_task, instance)` so child tasks spawned via `asyncio.create_task` or `asyncio.gather` do not inherit the parent task's scoped instances.
  - Added `await container.aclose_scoped()` and `async with container.scope():` disposing scoped instances implementing `aclose()`, `__aexit__()`, `close()`, or `__exit__()` in reverse creation order at scope exit (wired into HTTP and WebSocket request scopes in `Application.__call__`).
  - Created `tests/concurrency/test_container_semantics.py` (6 tests).

---

### P5-C3: Auth Manager Honors Configured Guards (`C-3`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Added `authenticate(request)` methods to `JWTGuard` (`swiftpy/auth/jwt_guard.py`) and `ApiKeyGuard` (`swiftpy/auth/api_key_guard.py`).
  - Registered default `"jwt"` and `"api_key"` guard factories in `AuthManager._custom_creators` (`swiftpy/auth/auth_manager.py`), invalidated `self._guards.pop(name, None)` on `AuthManager.extend(name, creator)`, and updated `AuthManager.authenticate()` to dispatch to the registered guard instance (`g.authenticate(request)`).
  - Updated `CurrentUser` to sync lazily from `Context.get("__swiftpy_current_user__")` when initialized with `data=None` and expose `_cache_in_scope = False` while unauthenticated so `Container.resolve(CurrentUser)` never caches an unauthenticated `CurrentUser` in scope.
  - Enforced canonical base64url verification in `_base64url_decode` (`swiftpy/auth/jwt_issuer.py`) to reject non-zero padding bits in tampered JWT signatures.
  - Created `tests/auth/test_auth_manager.py` (3 tests).

---

### P5-C4: Instance-Scoped Singletons (`C-4`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Removed `Config._instance = self` from `Config.__init__` (`swiftpy/core/config.py`).
  - Scoped `Config`, `WebSocketManager`, `EventDispatcher`, `QueueDispatcher`, and `RedisEventBus` per `Application` / `Container` instance (`get_instance()` resolves from the active `Container` when bound).
  - Updated `helpers.dispatcher()`, `helpers.queue()`, `helpers.cache()`, and `helpers.websocket_manager()` (`swiftpy/helpers/helper.py`) to raise `BindingNotFoundError` (`ResolutionError`) when the service is not registered in the active container instead of auto-wiring a new leaked instance per call.
  - Added `snapshot_model_registry()` and `restore_model_registry()` in `swiftpy/database/orm/registry.py`.
  - Created `tests/core/test_instance_singletons.py` (4 tests).

---

### P5-C5: Config Correctness (`C-5`, `D-8`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Updated `Config._coerce_value()` (`swiftpy/core/config.py`) to coerce `bool`, `int`, and `float` only when the target key has an existing typed default (or is `app.debug`/`debug`), preserving strings such as `APP__SECRET="0012345678"` (Decision `D-8`).
  - Ignored malformed environment variable keys with empty path segments (`__WEIRD__KEY`, `APP____EMPTY`, `TRAILING__`).
  - Updated `Config.load(..., strict=True)` to raise `ConfigurationError` on broken config files (`strict=False` preserves warning+skip mode for CLI tooling).
  - Updated `Config.get()` to distinguish missing keys from explicit `None` values via `_MISSING`, and updated `Config.all()` and `Config.__init__()` to use `copy.deepcopy`.
  - Replaced raw `os.getenv` reads in `swiftpy/events/redis_bus.py` and `swiftpy/queue/dispatcher.py` with `resolve_redis_params` and `Config.get_instance()`.
  - Extended `tests/core/test_config.py` with 4 new tests (11 total).

---

### P5-C6: Provider Discovery (`C-6`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Updated `discover_providers()` (`swiftpy/core/application.py`) to stop inserting `app/providers` (`target_dir`) into `sys.path` (preventing provider files named `queue.py` or `logging.py` from shadowing Python stdlib modules), import providers by package path (`app.providers.<name>`) relative to `project_root`, and raise `ConfigurationError` on broken provider modules when `strict=True` (default).
  - Added `project_root` and `strict_providers` parameters to `Application.__init__`.
  - Extended `tests/app/test_application_providers.py` with 3 new tests (9 total).
* **Quality Gate Results:** see the "Phase 5 closing gate" section below.

---

### Phase 5 closing gate (measured 2026-11-07, tests run in the `swiftpy_app` Docker container)
  - `ruff check .`: **All checks passed!** (0 errors)
  - `mypy swiftpy app config tests/http tests/core tests/validation tests/websockets tests/testing tests/concurrency tests/auth tests/app`: **Success: no issues found in 166 source files**
  - `mypy .`: 25 errors in 1 file (`tests/orm/test_eager_loading.py`). These are the same relation-typing errors recorded in the Phase 0 baseline and scheduled for P7-M6. Zero errors in `swiftpy/` or Phase 5 tests.
  - Full suite: `pytest -q -p no:cacheprovider`: **498 passed in 198.53s** (473 post-Phase-4 + 25 new Phase 5 tests).

---

## Phase 6 – Queue, Events, and Cache

### P6-Q1: Reliable Queue Consumption (`Q-1`, `D-3`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Designed and implemented list-plus-lease architecture in `swiftpy/queue/dispatcher.py` and `swiftpy/queue/worker.py`.
  - Popping moves jobs from `queue:<name>` to `queue:<name>:processing:<worker_id>` with per-job deadline lease in sorted set `queue:<name>:leases`.
  - Added atomic Lua scripts for `pop_and_lease`, `ack`, `release` (with backoff), and `reap_expired_leases`.
  - Worker automatically reaps expired leases, preserves attempt counts, and moves jobs exceeding maximum `tries` to DLQ.
  - Cancelled jobs are re-queued at the head with attempt count preserved.
  - Added `tests/queue/test_reliability.py` (4 tests).

---

### P6-Q2: Poison-Message Handling (`Q-2`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Hardened `Job.deserialize()` (`swiftpy/queue/job.py`) to validate envelope field types (`args`, `kwargs`, `tries`, `attempts`, `data`) and wrap deserialization/instantiation failures in `JobSerializationError`.
  - Updated worker loop to catch `JobSerializationError`, push to Dead Letter Queue with diagnostic failure metadata, acknowledge the message from processing, and continue processing subsequent jobs.
  - Configured `encoding_errors="replace"` on Redis connections and hardened Lua scripts to remove and ack non-UTF-8 payload strings.
  - Added `tests/queue/test_poison_messages.py` (2 tests).

---

### P6-Q3: Trusted Payloads & HMAC Signing (`Q-3`, `D-3`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Added HMAC-SHA256 envelope signing with `app.key` (`APP_KEY`) in `swiftpy/queue/job.py`.
  - Enforced constant-time `hmac.compare_digest` verification **before** `importlib.import_module()` is invoked.
  - Added key rotation support (`set_previous_keys()`, `get_previous_keys()`).
  - Added `_trusted_namespaces` whitelist guard (defaulting to application packages and `swiftpy`) to reject arbitrary module imports when signing is unconfigured.
  - Added `tests/queue/test_signing.py` (6 tests).

---

### P6-Q4: Worker Loop Efficiency & CLI Commands (`Q-4`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Switched worker loop to block on `BLMOVE` (honoring `--sleep`), cached delayed-job promotion Lua script via SHA.
  - Added `--concurrency N` support to `QueueWorker` and `swiftpy queue:work` using bounded semaphore and task tracking with done-callbacks per the Task Rule.
  - Restored signal handlers on worker exit.
  - Added queue name regex validation (`^[A-Za-z0-9_.-]{1,64}$`).
  - Added DLQ capping (`failed_max_size`, default 1000) with `LTRIM`.
  - Added CLI commands `swiftpy queue:failed`, `swiftpy queue:retry <id|all>`, and `swiftpy queue:flush` in `swiftpy/console/commands/queue.py`.
  - Added `tests/queue/test_worker_behavior.py` (6 tests).

---

### P6-Q5: After-Commit Dispatch (`Q-4`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Implemented transaction commit hooks `on_commit(callback)` in `swiftpy/database/transaction.py` (`TransactionConnection`, `Database`, and module-level).
  - Outermost successful commit triggers callbacks in registration order, inner savepoint commits promote callbacks to parent level, and rollbacks cleanly discard registered callbacks.
  - Added `Job.dispatch(after_commit=True)` (`swiftpy/queue/job.py`): defers job dispatch to transaction commit; drops job if transaction rolls back; dispatches immediately if no transaction is active.
  - Added `tests/queue/test_after_commit.py` (8 tests).

---

### P6-Q6: Redis Ownership and the Event Bus (`Q-5`, `Q-6`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Added `owns_pool` flag to `QueueDispatcher`, `RedisDriver`, and `RedisEventBus` so external shared connection pools are never disconnected by consumers that did not create them.
  - Guarded lazy `get_client()` with `asyncio.Lock` across all drivers.
  - Hardened `RedisEventBus` (`swiftpy/events/redis_bus.py`): done-callback on `_listener_task`, re-raised `CancelledError`, exponential backoff with jitter and rate-limited logging during Redis reconnects, and bounded tracked background task dispatching (`max_concurrency`) so slow listeners never stall consumption.
  - Added HMAC-SHA256 message signing to `RedisEventBus` with key rotation support.
  - Documented at-most-once pub/sub delivery semantics and local vs. remote payload contracts.
  - Optimized `EventDispatcher` (`swiftpy/events/dispatcher.py`): precompiled invocation strategies at listener registration (eliminating repeated `inspect.signature` during dispatch), indexed exact registrations in O(1) dictionary and wildcards separately, and added `remove_listener` / `forget`.
  - Added `tests/events/test_redis_bus_reliability.py` (5 tests).

---

### P6-K1: Cache Driver Correctness (`K-1`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Hardened `remember()` in `CacheDriver` and `RedisDriver`: on lock timeout, executes callback directly without caching and logs a warning (never returns `None` silently).
  - Added reference-counted `_remember_locks` dictionary in `CacheDriver` so locks are cleaned up immediately when unused.
  - Implemented atomic `increment(key, value=1, ttl=None)` with Lua script setting TTL on initial creation and never extending it on subsequent increments. Implemented atomic `add(key, value, ttl=None)` using `SET ... NX`.
  - Scoped `flush()` using `SCAN` with store prefix and batched `UNLINK` in chunks of 500 keys, scoping default prefixes with `app.name`.
  - Added pluggable `CacheSerializer` interface with default `JsonCacheSerializer` supporting `datetime`, `date`, `time`, `UUID`, `Decimal`, and models with `to_dict()` and `__hidden__`.
  - Added `max_items` LRU eviction and lazy expiry sweeping to `MemoryDriver`, performing `copy.deepcopy` outside the lock to minimize lock contention.
  - Updated `Cache._resolve_driver` (`swiftpy/cache/cache.py`) to honor `redis_url`, `max_connections`, and `timeout`, sharing connection pools across stores with identical connection configurations.
  - Added `tests/cache/test_driver_correctness.py` (8 tests).

---

### Phase 6 closing gate (measured 2026-11-07, tests run in the `swiftpy_app` Docker container)
  - `ruff check .`: **All checks passed!** (0 errors)
  - `mypy --exclude "tests/orm/test_eager_loading.py" .`: **Success: no issues found in 201 source files**
  - `mypy .`: 25 errors in 1 file (`tests/orm/test_eager_loading.py`). These are the same relation-typing errors recorded in the Phase 0 baseline and scheduled for P7-M6. Zero errors in `swiftpy/` or Phase 6 tests.
  - Full suite: `pytest -q -p no:cacheprovider`: **537 passed in 304.83s** (498 post-Phase-5 + 39 new Phase 6 tests).

---

## Phase 7 – ORM Safety and Typing

### P7-M1: Hidden Attributes and Safe Serialization (`D-7`, `D-5`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Added `__hidden__: ClassVar[tuple[str, ...]] = ()` support on `Model` in `swiftpy/database/orm/model.py`.
  - Added `_temp_visible` and `_temp_hidden` sets per instance, along with `make_visible(*fields)` and `make_hidden(*fields)`.
  - Updated `to_dict(include_hidden=False)` to recursively filter hidden fields and loaded relations while preserving raw Python types.
  - Changed `Model.__repr__` to output `<ClassName pk_name=pk_val!r>` only, preventing accidental credential/token leakage in logs, interactive shells, and exception traces (`D-5`).
  - Added `tests/orm/test_hidden.py` (6 tests).

---

### P7-M2: Mass Assignment Protection (`D-7`, `D-4`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Enforced deny-all default for mass assignment on `Model` unless `__fillable__` or `__guarded__` is explicitly declared (`D-4`).
  - Added `MassAssignmentError(ValueError)`.
  - Rejects attributes starting with `_` and undeclared attribute keys in `__init__`, `create()`, and `fill()`.
  - Added `force_fill(attributes)` and `force_create(**attributes)` to explicitly bypass mass assignment checks for trusted internal code.
  - Added `_hydrate(row)` on `Model` and wired `ModelQueryBuilder.get()` to hydrate models from trusted DB rows without mass-assignment validation overhead.
  - Updated test suite models with explicit `__guarded__: ClassVar[list[str]] = []` where mass assignment is expected.
  - Added `tests/orm/test_mass_assignment.py` (9 tests).

---

### P7-M3: Defaults and Dirty Tracking (`D-7`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Added `Field(default=..., default_factory=..., primary_key=..., nullable=...)` returning `FieldInfo` metadata in `swiftpy/database/orm/fields.py`.
  - Deep-copies mutable class defaults (`copy.deepcopy`), evaluates callable defaults, and resolves `default_factory` callbacks per instance.
  - Implemented deep-copy snapshotting of `_original` in `_hydrate()` and `save()`, allowing in-place mutations of lists and dicts to be detected as dirty.
  - Added `is_dirty(*fields)` and `get_dirty()` on `Model`.
  - Added `_modified_keys` tracking in `Model.__setattr__` so partial-select models never clobber unselected columns during `save()`.
  - In `Model.save()`, skips `UPDATE` queries and suppresses `.updated` events when dirty tracking detects no changes (no-op save).
  - Added `_coerce_db_value` to automatically coerce JSON/JSONB string columns back to typed Python lists/dicts.
  - Added `tests/orm/test_defaults_dirty.py` (5 tests).

---

### P7-M4: Events and Transaction Ordering (`D-7`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Refactored model lifecycle dispatch (`_dispatch_lifecycle_event`): `*ing` (before) events (`creating`, `updating`, `deleting`) execute inline and abort database operations if an exception is raised or a listener returns `False`.
  - Deferred `*ed` (after) events (`created`, `updated`, `deleted`) to execute post-commit via `on_commit` when inside an active database transaction.
  - Added `Model.strict_events: ClassVar[bool] = False`. When False (default), listener exceptions on `*ed` events are logged without turning committed database writes into apparent failures; when True, listener exceptions propagate to the caller.
  - Added `tests/orm/test_model_events_transaction.py` (5 tests).

---

### P7-M5: Relationships Hardening (`D-8`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Chunked `IN` queries during eager loading in batches of 1,000 keys (`_chunk_list`) across `HasMany`, `HasOne`, `BelongsTo`, and `BelongsToMany` to prevent hitting database parameter limits.
  - Implemented multi-level nested eager loading (e.g. `with_("books.reviews")`, `with_("author.books.tags")`) issuing exactly 1 query per relation level via recursive relation dependency grouping.
  - Guarded `RelationshipQueryBuilder.__getattr__` against private/dunder attribute lookups to prevent recursion during `copy.copy` or inspection.
  - Added pivot manipulation methods on `BelongsToMany` (`attach(model_or_id, **pivot_extra)`, `detach(model_or_id=None)`, `sync(ids_or_models)`).
  - Added `Model.prevent_lazy_loading: ClassVar[bool] = False`. When enabled, accessing un-eager-loaded relationships raises `LazyLoadingDisabledError`.
  - Enforced symmetric `__eq__` and `__hash__` semantics (`type(self) is type(other)` and identical primary key).
  - Added `tests/orm/test_relationships_hardening.py` (5 tests).

---

### P7-M6: Public Typing for Models & Relations (`D-7`, `P0-T3`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Added descriptor protocol (`@overload def __get__(...)`) to `Relation` and `RelationDescriptor` in `swiftpy/database/orm/relationships.py`, allowing Mypy to recognize relationship properties on model classes.
  - Added `tests/typing/test_orm_typing.py` validating static and runtime contracts for attributes, relationships, dirty tracking, hidden fields, and BelongsToMany operations.
  - Completely resolved the 25 legacy Mypy errors in `tests/orm/test_eager_loading.py`. Mypy now passes with **0 errors across 208 source files** in strict mode.

---

### Phase 7 closing gate (measured 2026-11-07, tests run in the `swiftpy_app` Docker container)
  - `ruff check .`: **All checks passed!** (0 errors)
  - `mypy .`: **Success: no issues found in 208 source files** (all 25 previous Mypy relation-typing errors resolved!).
  - Full suite: `pytest -q -p no:cacheprovider`: **568 passed in 359s** (537 post-Phase-6 + 31 new Phase 7 tests).

---

## Phase 8 – Validation

### P8-V1: Rule Correctness (`V-1`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Hardened `IntRule` with ASCII-only regex `^[+-]?[0-9]+$` in `swiftpy/validation/rules/builtin.py`, rejecting unicode superscript digits (`"²"`, `"³"`, `"¹"`) and booleans, coercing valid numeric strings into integers.
  - Hardened `FloatRule` to reject `NaN`, `Infinity`, and `-Infinity`, coercing finite numeric strings into floats.
  - Hardened `EmailRule` with tighter RFC-compliant checks (local <= 64, total <= 254, rejecting consecutive dots `..` and leading/trailing dots in local part). Normalizes domain to lowercase and strips whitespace on validation and storage, preserving local part case (or full lowercase when `email:lowercase` is configured).
  - Hardened `UrlRule` enforcing a 2048-character maximum length limit and valid HTTP/HTTPS schemes.
  - Added support in `swiftpy/validation/rules/registry.py` for escaping `\|` and `\:` in rule strings (e.g. `regex:^(a\|b)+$`) and list-based rule declarations `["required", "regex:^(a|b)+$"]`. Caught `re.error` so invalid regex patterns return validation errors rather than 500 errors.
  - `BetweenRule` parsing (`between:1`) now raises `ConfigurationError` at parse time when missing the required range boundaries.
  - Hardened `InRule` and `NotInRule` with strict type matching, ensuring boolean values (`True`/`False`) do not match integer `1`/`0` or string `"True"`.
  - Added case-insensitive support for database rules (`unique:table,column,ci` and `UniqueRule(..., case_insensitive=True)`) using `LOWER("{column}") = LOWER($1)`.
  - Database data errors (`asyncpg.DataError`) during `unique` or `exists` evaluation now fail validation gracefully with field errors rather than crashing the request with HTTP 500.
  - Added `tests/validation/test_rules_hardening.py` (10 tests).

---

### P8-V2: Validator Engine Optimization & Order Independence (`V-2`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Precomputed and cached rule invocation metadata and signatures (`(param_count, is_async)`) in `swiftpy/validation/validator.py`, eliminating per-field `inspect.signature` overhead on every request.
  - Removed `except TypeError` retry fallback in `_run_rule`, ensuring rule implementation bugs propagate cleanly as framework errors instead of being re-executed.
  - Enforced structured 422 `Unprocessable Entity` response with `{"body": ["The request body must be a JSON object."]}` when non-dict payloads (`[]`, `"x"`, `null`, `123`) are sent.
  - Achieved rule order independence for coercion: sorted rule execution so type coercion rules (`IntRule`, `FloatRule`, `BoolRule`, `EmailRule`) run before magnitude and length constraint rules (`MinRule`, `MaxRule`, `BetweenRule`).

---

### P8-V3: Fail-Closed Validation & DI Resolution (`V-3`, `D-9`)
* **Status:** PASSED
* **Date:** 2026-11-07
* **Changes:**
  - Hardened `FormRequest.get_rules()` in `swiftpy/validation/form_request.py` to fail closed with `ConfigurationError` (HTTP 500) if rule instantiation or definition raises `TypeError` or any exception, removing the unsafe fallback that previously returned `{}` and allowed unvalidated requests to execute route handlers (`D-9`).
  - Added parse-once caching (`_cached_parsed_rules`) per `FormRequest` class so static rule definitions are parsed once and reused across requests.
  - Integrated container dependency injection for `FormRequest`: constructors requiring registered services or dependencies are automatically resolved via `container.resolve(ann)`.
  - Added `tests/validation/test_form_request_fail_closed.py` (7 tests).

---

### Phase 8 closing gate (measured 2026-11-07, tests run in the `swiftpy_app` Docker container)
  - `ruff check .`: **All checks passed!** (0 errors)
  - `mypy .`: **Success: no issues found in 210 source files** (0 errors).
  - Full suite: `pytest -q -p no:cacheprovider`: **585 passed in 381.01s** (568 post-Phase-7 + 17 new Phase 8 tests).







