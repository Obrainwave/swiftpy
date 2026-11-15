from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, ClassVar

import pytest

from swiftpy.core.application import Application
from swiftpy.database.pool import DatabasePool
from swiftpy.http.response import Response, json_response
from swiftpy.validation import (
    FormRequest,
    ValidatorRule,
    exists,
    unique,
    validate,
)


class SynchronizedSlowRule(ValidatorRule):
    slow_started: ClassVar[asyncio.Event]
    fast_finished: ClassVar[asyncio.Event]

    async def validate(self, field: str, value: Any, data: dict[str, Any]) -> bool:
        self.slow_started.set()
        await asyncio.wait_for(self.fast_finished.wait(), timeout=5.0)
        return bool(value == "valid_payload")

    def message(self, field: str) -> str:
        return f"The {field} verification failed."


class SlowFormRequest(FormRequest):
    schema: ClassVar[dict[str, Any]] = {
        "token": [SynchronizedSlowRule()],
    }


class FastFormRequest(FormRequest):
    schema: ClassVar[dict[str, Any]] = {
        "token": "required|str",
    }


class RegisterRequest(FormRequest):
    schema: ClassVar[dict[str, Any]] = {
        "name": "required|str|min:3",
        "email": "required|email",
        "age": "required|int|between:18,99",
    }


def _make_http_scope(
    method: str = "GET",
    path: str = "/",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "query_string": b"",
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 50000),
    }


async def _simulate_request(
    app: Application,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    if isinstance(body, dict):
        raw_body = json.dumps(body).encode("utf-8")
        raw_headers = [(b"content-type", b"application/json")]
    else:
        raw_body = b""
        raw_headers = []

    scope = _make_http_scope(method=method, path=path, headers=raw_headers)

    status_code = 0
    resp_body = bytearray()
    body_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": raw_body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code, resp_body
        if message["type"] == "http.response.start":
            status_code = message["status"]
        elif message["type"] == "http.response.body":
            resp_body.extend(message.get("body", b""))

    await app(scope, receive, send)

    try:
        data = json.loads(resp_body.decode("utf-8")) if resp_body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = {"raw": resp_body.decode("utf-8", errors="replace")}

    return status_code, data


@pytest.mark.asyncio
async def test_slow_validation_does_not_block_concurrent_requests() -> None:
    """
    Guarantees deterministically using asyncio.Event that a slow asynchronous validation
    rule executing in Request A yields to the event loop and allows Request B to start,
    validate, execute its handler, and return a complete response before Request A resumes.
    """
    app = Application()

    slow_started = asyncio.Event()
    fast_finished = asyncio.Event()
    SynchronizedSlowRule.slow_started = slow_started
    SynchronizedSlowRule.fast_finished = fast_finished

    order_of_events: list[str] = []

    @app.post("/slow-check")
    async def slow_endpoint(body: SlowFormRequest) -> Response:
        return json_response({"status": "slow_ok", "token": body.token})

    @app.post("/fast-check")
    async def fast_endpoint(body: FastFormRequest) -> Response:
        return json_response({"status": "fast_ok", "token": body.token})

    async def run_slow() -> tuple[int, dict[str, Any]]:
        status, data = await _simulate_request(
            app, "POST", "/slow-check", body={"token": "valid_payload"}
        )
        order_of_events.append("slow_completed")
        return status, data

    async def run_fast() -> tuple[int, dict[str, Any]]:
        # Wait until slow request has actually begun its async validation rule
        await asyncio.wait_for(slow_started.wait(), timeout=2.0)
        order_of_events.append("fast_started_after_slow_started")

        status, data = await _simulate_request(
            app, "POST", "/fast-check", body={"token": "fast_token"}
        )
        order_of_events.append("fast_completed")

        # Now unblock slow validation rule
        fast_finished.set()
        return status, data

    (status_slow, res_slow), (status_fast, res_fast) = await asyncio.gather(
        run_slow(),
        run_fast(),
    )

    assert status_slow == 200
    assert res_slow["status"] == "slow_ok"

    assert status_fast == 200
    assert res_fast["status"] == "fast_ok"

    # Deterministic proof: Fast request started after Slow started, and finished before Slow completed
    assert order_of_events == [
        "fast_started_after_slow_started",
        "fast_completed",
        "slow_completed",
    ]


@pytest.mark.asyncio
async def test_concurrent_database_validation_load(
    ensure_db_pool: DatabasePool,
) -> None:
    """
    Verifies that multiple concurrent validation tasks executing database rules
    (unique and exists) run simultaneously using connection pool without deadlocking
    or leaking connections, using dynamic isolated tables.
    """
    table_name = f"test_conc_users_{uuid.uuid4().hex[:8]}"
    pool = ensure_db_pool.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            CREATE TABLE "{table_name}" (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) NOT NULL,
                email VARCHAR(100) NOT NULL
            );
            """
        )
        # Prepopulate with 25 records
        rows = [(f"user_{i}", f"user_{i}@example.com") for i in range(25)]
        await conn.executemany(
            f'INSERT INTO "{table_name}" (username, email) VALUES ($1, $2);',
            rows,
        )

    try:
        schema = {
            "email": [unique(table_name, "email", pool=ensure_db_pool)],
            "sponsor": [exists(table_name, "username", pool=ensure_db_pool)],
        }

        # 50 total tasks:
        # First 25 have unique emails and valid sponsors -> should PASS
        # Next 25 have existing emails -> should FAIL on unique
        tasks = []
        for i in range(50):
            if i < 25:
                payload = {
                    "email": f"new_user_{i}@example.com",
                    "sponsor": "user_0",
                }
            else:
                payload = {
                    "email": "user_0@example.com",  # Duplicate!
                    "sponsor": "user_0",
                }
            tasks.append(validate(payload, schema))

        results = await asyncio.gather(*tasks)

        assert len(results) == 50
        for i, res in enumerate(results):
            if i < 25:
                assert res.is_valid is True, f"Task {i} unexpectedly failed"
            else:
                assert res.is_valid is False, f"Task {i} unexpectedly passed"
                assert "email" in res.errors

    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP TABLE IF EXISTS "{table_name}";')


@pytest.mark.asyncio
async def test_concurrent_http_pipeline_isolation() -> None:
    """
    Verifies that concurrent requests passing through Application with typed
    FormRequest parameters maintain complete state and response isolation.
    """
    app = Application()

    @app.post("/register")
    async def register(body: RegisterRequest) -> Response:
        return json_response(
            {"registered": True, "name": body.name, "age": body.age},
            status_code=201,
        )

    # 30 concurrent requests: 15 valid, 15 invalid
    tasks = []
    for i in range(30):
        if i % 2 == 0:
            payload = {
                "name": f"ValidUser_{i}",
                "email": f"user_{i}@example.com",
                "age": 20 + (i % 50),
            }
        else:
            payload = {
                "name": "x",  # Too short (min 3)
                "email": "not_an_email",
                "age": 10,  # Below 18
            }
        tasks.append(_simulate_request(app, "POST", "/register", body=payload))

    responses = await asyncio.gather(*tasks)

    assert len(responses) == 30
    for i, (status, data) in enumerate(responses):
        if i % 2 == 0:
            assert status == 201, f"Expected 201 for request {i}, got {status}"
            assert data["registered"] is True
            assert data["name"] == f"ValidUser_{i}"
        else:
            assert status == 422, f"Expected 422 for request {i}, got {status}"
            assert "errors" in data
            assert "name" in data["errors"]
            assert "email" in data["errors"]
            assert "age" in data["errors"]
