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