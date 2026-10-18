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