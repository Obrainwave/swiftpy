from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from swiftpy import Application, Request, Response, json_response
from swiftpy.core.providers.base import ServiceProvider
from swiftpy.database.query_builder import Query
from swiftpy.testing import (
    TestCase,
    TestClient,
    VectorStoreFake,
    transactional_test,
)


class _SampleProvider(ServiceProvider):
    started: bool = False
    stopped: bool = False

    def register(self) -> None:
        return None

    async def startup(self) -> None:
        self.__class__.started = True
        self.__class__.stopped = False

    async def shutdown(self) -> None:
        self.__class__.stopped = True


@pytest.mark.asyncio
async def test_test_client_merges_url_query_with_query_dict_and_utf8() -> None:
    """TEST-1: Inline '?' params, query dict params, and UTF-8 paths must all be preserved."""
    app = Application()

    @app.get("/users/{name}")
    async def get_user(request: Request, name: str) -> Response:
        return json_response(
            {
                "name": name,
                "a": request.query_param_list.get("a", []),
                "b": request.query_param_list.get("b", []),
                "q": request.query_params.get("q"),
            }
        )

    client = TestClient(app)
    res = await client.get(
        "/users/José?a=1&b=from_url",
        query={"b": ["from_arg"], "q": "café"},
    )
    res.assert_ok()
    assert res.json() == {
        "name": "José",
        "a": ["1"],
        "b": ["from_url", "from_arg"],
        "q": "café",
    }


@pytest.mark.asyncio
async def test_test_client_case_insensitive_header_override_and_default_host() -> None:
    """TEST-1: Overriding headers with different case must deduplicate, and Host defaults cleanly."""
    app = Application()

    @app.get("/headers")
    async def inspect_headers(request: Request) -> Response:
        auth_values = [
            v.decode("latin-1")
            for k, v in request.scope["headers"]
            if k == b"authorization"
        ]
        return json_response(
            {
                "auth_values": auth_values,
                "host": request.headers.get("host"),
            }
        )

    client = (
        TestClient(app, base_url="https://api.example.test:8443")
        .with_token("first-token")
        .with_headers({"authorization": "Bearer second-token"})
    )
    res = await client.get("/headers", headers={"AUTHORIZATION": "Bearer third-token"})
    res.assert_ok()
    assert res.json() == {
        "auth_values": ["Bearer third-token"],
        "host": "api.example.test:8443",
    }


@pytest.mark.asyncio
async def test_test_response_assert_header_handles_multi_value_headers() -> None:
    """TEST-1: assert_header must check all Set-Cookie values rather than overwriting by lower key."""
    app = Application()

    @app.get("/cookies")
    async def cookies() -> Response:
        resp = json_response({"ok": True})
        resp.headers.add("Set-Cookie", "session=abc; Path=/")
        resp.headers.add("Set-Cookie", "theme=dark; Path=/")
        return resp

    res = await TestClient(app).get("/cookies")
    res.assert_ok()
    res.assert_header("Set-Cookie", "session=abc; Path=/")
    res.assert_header("set-cookie", "theme=dark; Path=/")

    with pytest.raises(AssertionError):
        res.assert_header("Set-Cookie", "missing=value")


@pytest.mark.asyncio
async def test_test_client_form_dict_data_invalid_data_type_and_delete_body() -> None:
    """TEST-1: Mapping data encodes as form-urlencoded, unsupported data raises TypeError, delete accepts body."""
    app = Application()

    @app.post("/form")
    async def submit_form(request: Request) -> Response:
        body = (await request.body()).decode("utf-8")
        return json_response(
            {
                "content_type": request.headers.get("content-type"),
                "body": body,
            }
        )

    @app.delete("/items")
    async def delete_items(request: Request) -> Response:
        return json_response(await request.json())

    client = TestClient(app)

    form_res = await client.post("/form", data={"name": "alice", "role": "admin"})
    form_res.assert_ok()
    assert form_res.json() == {
        "content_type": "application/x-www-form-urlencoded",
        "body": "name=alice&role=admin",
    }

    del_res = await client.delete("/items", json={"ids": [1, 2]})
    del_res.assert_ok()
    assert del_res.json() == {"ids": [1, 2]}

    bad_payload: Any = 12345
    with pytest.raises(TypeError, match="Unsupported request data type"):
        await client.post("/form", data=bad_payload)


@pytest.mark.asyncio
async def test_test_case_lifespan_and_reset() -> None:
    """TEST-2: TestCase supports async lifespan context, delete body, and client/app reset."""
    _SampleProvider.started = False
    _SampleProvider.stopped = False

    class _LifecycleTestCase(TestCase):
        def create_app(self) -> Application:
            app = Application(
                providers=[_SampleProvider],
                auto_discover_providers=False,
            )

            @app.delete("/resource")
            async def remove(request: Request) -> Response:
                return json_response(await request.json())

            return app

    case = _LifecycleTestCase()
    async with case:
        assert _SampleProvider.started is True
        res = await case.delete("/resource", json={"id": 42})
        res.assert_ok()
        assert res.json() == {"id": 42}

    assert _SampleProvider.stopped is True
    first_app = case.app
    case.reset()
    assert case.app is None
    assert case._client is None
    assert case.get_app() is not first_app


@pytest.mark.asyncio
async def test_transactional_test_uses_public_close_and_query_builder() -> None:
    """TEST-2: transactional_test exposes public TransactionConnection cleanup methods."""
    async with transactional_test() as tx_conn:
        assert hasattr(tx_conn, "close")
        assert hasattr(tx_conn, "rollback_transaction")
        val = await tx_conn.fetchval("SELECT 1")
        assert val == 1
        rows = await Query().table("pg_tables").limit(1).get()
        assert isinstance(rows, list)


def test_standalone_db_transaction_fixture_without_conftest(tmp_path: Path) -> None:
    """TEST-2: db_transaction fixture must run cleanly in an external project without ensure_db_pool."""
    external_test = tmp_path / "test_external_db_tx.py"
    external_test.write_text(
        (
            "import pytest\n"
            "from swiftpy.database.query_builder import Query\n"
            "from swiftpy.testing import db_transaction\n\n"
            "@pytest.mark.asyncio\n"
            "async def test_standalone_db_transaction(db_transaction) -> None:\n"
            "    val = await db_transaction.fetchval('SELECT 42')\n"
            "    assert val == 42\n"
            "    rows = await Query().table('pg_tables').limit(1).get()\n"
            "    assert isinstance(rows, list)\n\n"
            "@pytest.mark.asyncio\n"
            "async def test_standalone_db_transaction_second_loop(db_transaction) -> None:\n"
            "    val = await db_transaction.fetchval('SELECT 7')\n"
            "    assert val == 7\n"
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env.setdefault("APP_ENV", "testing")
    env.setdefault("DB_HOST", "127.0.0.1")
    env.setdefault("DB_PORT", "5432")
    env.setdefault("DB_DATABASE", "swiftpy")
    env.setdefault("DB_USERNAME", "postgres")
    env.setdefault("DB_PASSWORD", "secret")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--noconftest",
            "-o",
            "asyncio_mode=auto",
            "-o",
            "cache_dir=/tmp/pytest_cache",
            str(external_test),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


@pytest.mark.asyncio
async def test_vectorstore_fake_records_calls_and_resets() -> None:
    """TEST-2: VectorStoreFake implements VectorStore contract cleanly with call assertions."""
    fake = VectorStoreFake(collection_name="docs", dimensions=2)
    await fake.upsert("doc-1", [1.0, 0.0], metadata={"tag": "a"}, content="hello")
    hits = await fake.search([1.0, 0.0], limit=1)
    assert len(hits) == 1
    assert hits[0].id == "doc-1"

    fake.assert_upserted("doc-1")
    fake.assert_searched(times=1)
    fake.reset()
    assert fake.recorded() == []
    assert await fake.count() == 0
