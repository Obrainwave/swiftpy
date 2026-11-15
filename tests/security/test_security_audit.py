from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

import pytest

from swiftpy.ai.agents.memory import DatabaseMemoryBackend
from swiftpy.ai.observability.drivers import DatabaseObservabilityDriver
from swiftpy.auth.api_key_guard import (
    _verify_api_key_hash_async,
    generate_api_key,
    hash_api_key,
    verify_api_key_hash,
)
from swiftpy.auth.jwt_issuer import (
    InvalidAlgorithmError,
    InvalidSignatureError,
    JWTIssuer,
    TokenExpiredError,
)
from swiftpy.auth.token_blacklist import TokenBlacklist
from swiftpy.core.application import Application
from swiftpy.core.config import Config
from swiftpy.core.exceptions import (
    InternalServerError,
    PayloadTooLargeError,
)
from swiftpy.database.query_builder import Query
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.testing.http_client import TestClient

# ============================================================================
# 1. SQL Injection Audit Tests
# ============================================================================


def test_sql_injection_table_identifier_validation() -> None:
    """Verify that table names with SQL injection payloads are strictly rejected."""
    malicious_tables = [
        "users; DROP TABLE users; --",
        "users' OR '1'='1",
        "users\nSELECT * FROM secrets",
        "users\r\n",
        "users--",
        "users/*comment*/",
        "invalid table name with spaces",
    ]
    for bad_table in malicious_tables:
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            Query.table(bad_table)


def test_sql_injection_column_identifier_validation() -> None:
    """Verify that column projections, WHERE clauses, and ORDER BY reject injection."""
    malicious_columns = [
        "id; DROP TABLE users; --",
        "name' OR '1'='1",
        "email\n",
        "password--",
        "col/*comment*/",
    ]
    for bad_col in malicious_columns:
        with pytest.raises(ValueError):
            Query.table("test_users").select(bad_col)

        with pytest.raises(ValueError):
            Query.table("test_users").where(bad_col, "value")

        with pytest.raises(ValueError):
            Query.table("test_users").order_by(bad_col)


def test_sql_injection_parameterization_in_query_builder() -> None:
    """Verify that values in WHERE, INSERT, UPDATE, and DELETE are strictly parameterized."""
    malicious_input = "admin' OR '1'='1'; DROP TABLE users; --"

    # WHERE compilation check
    qb = Query.table("users").where("username", malicious_input)
    sql, bindings = qb.to_sql()
    assert "$1" in sql
    assert malicious_input not in sql
    assert bindings == [malicious_input]

    # LIKE wildcard escaping and parameterization
    qb_like = Query.table("users").where_like("bio", "%hacked_user_input%")
    sql_like, bindings_like = qb_like.to_sql()
    assert "ESCAPE '\\'" in sql_like
    assert "$1" in sql_like
    assert "\\%hacked\\_user\\_input\\%" in bindings_like[0]

    # WHERE IN parameterization
    qb_in = Query.table("users").where_in("id", [1, 2, malicious_input])
    sql_in, bindings_in = qb_in.to_sql()
    assert "($1, $2, $3)" in sql_in
    assert malicious_input not in sql_in
    assert bindings_in == [1, 2, malicious_input]


def test_sql_injection_agent_memory_table_validation() -> None:
    """Verify that DatabaseMemoryBackend validates table identifiers and quotes them."""
    bad_name = "memories; DROP TABLE users; --"
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        DatabaseMemoryBackend(table_name=bad_name)

    valid_memory = DatabaseMemoryBackend(table_name="agent_memories_secure")
    assert valid_memory.table_name == "agent_memories_secure"
    assert valid_memory.messages_table == "agent_memories_secure_messages"
    assert valid_memory.state_table == "agent_memories_secure_state"


def test_sql_injection_observability_driver_table_validation() -> None:
    """Verify that DatabaseObservabilityDriver validates table identifiers and prevents injection."""
    bad_name = "ai_obs; DROP TABLE users; --"
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        DatabaseObservabilityDriver(table_name=bad_name)

    valid_driver = DatabaseObservabilityDriver(table_name="ai_observations_secure")
    assert valid_driver.table_name == "ai_observations_secure"


# ============================================================================
# 2. Authentication Security Audit Tests
# ============================================================================


def test_auth_expired_jwt_rejected() -> None:
    """Verify that expired JWTs are strictly rejected by the decoder."""
    secret = "a" * 32
    issuer = JWTIssuer(secret=secret, access_token_ttl=1)

    # Issue token expired 10 seconds ago
    token = issuer.issue_token(
        subject="user_123",
        token_type="access",
        expires_in=-10,
    )

    with pytest.raises(TokenExpiredError, match="Token has expired"):
        issuer.decode_token(token)


def test_auth_tampered_jwt_signature_rejected() -> None:
    """Verify that tokens with modified payloads or tampered signatures are rejected."""
    secret = "a" * 32
    issuer = JWTIssuer(secret=secret)

    token = issuer.issue_access_token(subject="user_123")
    parts = token.split(".")

    # 1. Tamper with signature
    tampered_sig = parts[2][:-4] + "XXXX"
    tampered_token = f"{parts[0]}.{parts[1]}.{tampered_sig}"
    with pytest.raises(InvalidSignatureError):
        issuer.decode_token(tampered_token)

    # 2. Tamper with payload
    tampered_payload_token = f"{parts[0]}.eyJzdWIiOiAiYWRtaW4ifQ.{parts[2]}"
    with pytest.raises(InvalidSignatureError):
        issuer.decode_token(tampered_payload_token)


def test_auth_none_algorithm_attack_rejected() -> None:
    """Verify that 'none' algorithm attacks are unconditionally rejected."""
    secret = "a" * 32
    issuer = JWTIssuer(secret=secret)

    header = "eyJhbGciOiAibm9uZSIgfQ"  # {"alg": "none"}
    payload = "eyJzdWIiOiAiYWRtaW4iLCAiaWF0IjogMTYwMDAwMDAwMCwgImV4cCI6IDIwMDAwMDAwMDB9"
    token = f"{header}.{payload}."

    with pytest.raises(InvalidAlgorithmError, match="disallowed"):
        issuer.decode_token(token)


@pytest.mark.asyncio
async def test_auth_token_blacklist_revocation() -> None:
    """Verify that revoked tokens are recorded and checked in TokenBlacklist."""
    secret = "a" * 32
    issuer = JWTIssuer(secret=secret)
    token = issuer.issue_access_token(subject="user_revoke_test")
    claims = issuer.decode_token(token)
    jti = claims["jti"]

    class MockPipeline:
        def __init__(self, store: dict[str, str]) -> None:
            self.store = store
            self.ops: list[Any] = []

        def exists(self, *names: str) -> None:
            for key in names:
                self.ops.append(lambda k=key: 1 if k in self.store else 0)

        def get(self, key: str) -> None:
            self.ops.append(lambda: self.store.get(key))

        async def execute(self) -> list[Any]:
            return [op() for op in self.ops]

    class MockRedis:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}

        def pipeline(self) -> MockPipeline:
            return MockPipeline(self.store)

        async def get(self, key: str) -> str | None:
            return self.store.get(key)

        async def set(self, name: str, value: str, ex: int | None = None) -> None:
            self.store[name] = value

        async def scan(
            self,
            cursor: int = 0,
            match: str | None = None,
            count: int | None = None,
        ) -> tuple[int, list[bytes | str]]:
            return (0, list(self.store.keys()))

        async def delete(self, *names: str) -> int:
            deleted = 0
            for name in names:
                if self.store.pop(name, None) is not None:
                    deleted += 1
            return deleted

        async def aclose(self) -> None:
            pass

    mock_redis = MockRedis()
    blacklist = TokenBlacklist(redis_client=mock_redis, issuer=issuer)

    # Before revocation: not revoked
    assert await blacklist.is_revoked(jti) is False

    # Revoke token
    await blacklist.revoke(jti, ttl=300)

    # After revocation: revoked
    assert await blacklist.is_revoked(jti) is True


@pytest.mark.asyncio
async def test_auth_api_key_bcrypt_storage_and_verification() -> None:
    """Verify API keys are stored as bcrypt hashes only and verified asynchronously."""
    full_key, prefix, secret = generate_api_key(prefix="sk_live_sec")
    assert full_key.startswith(prefix)

    # Generate bcrypt hash (cost rounds 4 for fast test)
    hashed = hash_api_key(secret, rounds=4)
    assert hashed.startswith("$2")
    assert secret not in hashed

    # Verify matching key succeeds
    assert verify_api_key_hash(secret, hashed) is True

    # Verify non-matching key fails
    assert verify_api_key_hash("wrong_secret_12345", hashed) is False

    # Verify non-blocking async executor verification
    async_verified = await _verify_api_key_hash_async(secret, hashed)
    assert async_verified is True


# ============================================================================
# 3. Input Handling Limits Tests
# ============================================================================


@pytest.mark.asyncio
async def test_input_handling_request_body_size_limit_content_length() -> None:
    """Verify request body exceeding max_body_size via Content-Length is rejected with 413."""
    app = Application(max_body_size=1024, debug=False)

    @app.post("/test-size")
    async def size_route(req: Request) -> Response:
        await req.body()
        return json_response({"ok": True})

    client = TestClient(app)
    # Send 2048 bytes when limit is 1024
    large_payload = b"X" * 2048
    res = await client.post(
        "/test-size",
        data=large_payload,
        headers={"Content-Length": str(len(large_payload))},
    )
    res.assert_status(413)
    assert "exceeds maximum size" in res.json()["detail"]


@pytest.mark.asyncio
async def test_input_handling_request_body_size_limit_streaming_chunks() -> None:
    """Verify request body exceeding max_body_size via streaming chunks without header is rejected."""
    # Simulate ASGI receive yielding chunks without content-length header
    chunks = [b"a" * 600, b"b" * 600]  # total 1200 > limit 1000
    chunk_idx = 0

    async def mock_receive() -> dict[str, Any]:
        nonlocal chunk_idx
        if chunk_idx < len(chunks):
            data = chunks[chunk_idx]
            chunk_idx += 1
            return {
                "type": "http.request",
                "body": data,
                "more_body": chunk_idx < len(chunks),
            }
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/chunked",
        "headers": [],
    }
    req = Request(scope, mock_receive, max_body_size=1000)

    with pytest.raises(PayloadTooLargeError, match="exceeds maximum size of 1000 bytes"):
        await req.body()


@pytest.mark.asyncio
async def test_input_handling_configurable_max_content_length() -> None:
    """Verify max_content_length limit is enforced independently when configured."""
    app = Application(max_content_length=500, max_body_size=2000, debug=False)

    @app.post("/test-cl")
    async def cl_route(req: Request) -> Response:
        await req.body()
        return json_response({"ok": True})

    client = TestClient(app)
    res = await client.post(
        "/test-cl",
        data=b"X" * 600,
        headers={"Content-Length": "600"},
    )
    res.assert_status(413)
    assert "exceeds maximum size of 500 bytes" in res.json()["detail"]


@pytest.mark.asyncio
async def test_input_handling_negative_and_malformed_content_length_rejected() -> None:
    """Verify invalid or negative Content-Length headers return HTTP 400."""
    app = Application(debug=False)

    @app.post("/cl-invalid")
    async def invalid_cl_route(req: Request) -> Response:
        await req.body()
        return json_response({"ok": True})

    client = TestClient(app)
    res1 = await client.post("/cl-invalid", data=b"a", headers={"Content-Length": "-5"})
    res1.assert_status(400)
    assert res1.json()["detail"] == "Invalid Content-Length header"

    res2 = await client.post("/cl-invalid", data=b"a", headers={"Content-Length": "not-a-number"})
    res2.assert_status(400)
    assert res2.json()["detail"] == "Invalid Content-Length header"


@pytest.mark.asyncio
async def test_input_handling_json_depth_limit_enforced() -> None:
    """Verify deeply nested JSON exceeding max_json_depth is rejected with HTTP 400."""
    app = Application(max_json_depth=5, debug=False)

    @app.post("/test-depth")
    async def depth_route(req: Request) -> Response:
        payload = await req.json()
        return json_response({"received": payload})

    client = TestClient(app)

    # 1. Acceptable JSON: depth 3 <= 5
    shallow_obj = {"a": {"b": {"c": 1}}}
    res_ok = await client.post("/test-depth", json=shallow_obj)
    res_ok.assert_status(200)

    # 2. Deeply nested JSON: depth 8 > 5
    deep_obj: dict[str, Any] = {"level": 1}
    curr = deep_obj
    for i in range(2, 9):
        curr["nested"] = {"level": i}
        curr = curr["nested"]

    res_deep = await client.post("/test-depth", json=deep_obj)
    res_deep.assert_status(400)
    assert "exceeds maximum allowed depth" in res_deep.json()["detail"]


@pytest.mark.asyncio
async def test_input_handling_route_level_limit_overrides() -> None:
    """Verify route-level limits override application global defaults."""
    app = Application(max_body_size=1024, max_json_depth=10, debug=False)

    # Strict route: max_json_depth=2
    @app.post("/strict-depth", max_json_depth=2)
    async def strict_route(req: Request) -> Response:
        data = await req.json()
        return json_response({"data": data})

    client = TestClient(app)
    # Depth 3 rejected on strict route
    res = await client.post("/strict-depth", json={"a": {"b": {"c": 1}}})
    res.assert_status(400)
    assert "depth of 2" in res.json()["detail"]


# ============================================================================
# 4. Dependency Vulnerability Audit Test
# ============================================================================


def test_dependency_audit_no_known_vulnerabilities() -> None:
    """Verify that pip-audit confirms 0 known vulnerabilities in installed dependencies."""
    if shutil.which("pip-audit"):
        cmd = ["pip-audit"]
    elif shutil.which("uv"):
        cmd = [
            "uv",
            "run",
            "--with",
            "pip-audit",
            "pip-audit",
        ]
    else:
        pytest.skip("Neither pip-audit nor uv is installed in the local environment")

    env = dict(os.environ)
    env["HOME"] = "/tmp"
    env["XDG_CACHE_HOME"] = "/tmp/.cache"
    env["UV_CACHE_DIR"] = "/tmp/uv_cache"

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )
        assert proc.returncode == 0
        output = proc.stdout + "\n" + proc.stderr
        assert "No known vulnerabilities found" in output
    except FileNotFoundError:
        pytest.skip("Dependency audit tool could not be executed")


# ============================================================================
# 5. Sensitive Data Audit Tests
# ============================================================================


def test_sensitive_data_config_uses_secure_defaults() -> None:
    """Verify default configurations do not contain hardcoded credentials."""
    cfg = Config()
    assert cfg.get("ai.providers.openai.api_key") in (None, "")
    assert cfg.get("ai.providers.anthropic.api_key") in (None, "")
    assert cfg.get("auth.jwt.secret") in (None, "")
    assert cfg.get("auth.api_key.pepper") in (None, "")


def test_sensitive_data_env_file_is_gitignored() -> None:
    """Verify .env files are excluded from version control."""
    gitignore_path = ".gitignore"
    if os.path.exists(gitignore_path):
        with open(gitignore_path, encoding="utf-8") as f:
            content = f.read()
        assert ".env" in content
        assert ".env.*" in content


# ============================================================================
# 6. Error Handling & Traceback Leakage Prevention Tests
# ============================================================================


@pytest.mark.asyncio
async def test_error_handling_production_never_leaks_traceback_or_file_paths() -> None:
    """Verify production error handler never leaks tracebacks, file paths, or internal state."""
    app = Application(debug=False)
    client = TestClient(app)

    @app.get("/crash-runtime")
    async def crash_runtime() -> Response:
        raise RuntimeError("Database query failed: SELECT * FROM secret_table WHERE pass='x'")

    @app.get("/crash-path")
    async def crash_path() -> Response:
        raise FileNotFoundError("/var/run/secrets/internal/auth_token.pem not found")

    @app.get("/crash-500-http")
    async def crash_http_500() -> Response:
        raise InternalServerError("PostgreSQL connection /var/lib/postgresql/data socket died")

    # 1. Unhandled runtime error
    res1 = await client.get("/crash-runtime")
    res1.assert_status(500)
    assert res1.json() == {"detail": "Internal Server Error"}
    assert "SELECT" not in res1.text
    assert "traceback" not in res1.text
    assert "secret_table" not in res1.text

    # 2. File path leakage
    res2 = await client.get("/crash-path")
    res2.assert_status(500)
    assert res2.json() == {"detail": "Internal Server Error"}
    assert "/var/run" not in res2.text
    assert "auth_token.pem" not in res2.text

    # 3. HTTP 500 exception
    res3 = await client.get("/crash-500-http")
    res3.assert_status(500)
    assert res3.json() == {"detail": "Internal Server Error"}
    assert "PostgreSQL" not in res3.text
    assert "/var/lib" not in res3.text


@pytest.mark.asyncio
async def test_error_handling_debug_mode_exposes_traceback_safely() -> None:
    """Verify development mode (debug=True) includes stack trace and exception info."""
    app = Application(debug=True)
    client = TestClient(app)

    @app.get("/debug-crash")
    async def debug_crash() -> Response:
        raise ValueError("Diagnostic debug message for developers")

    res = await client.get("/debug-crash")
    res.assert_status(500)
    data = res.json()
    assert data["error"] == "Internal Server Error"
    assert data["exception"] == "ValueError"
    assert data["message"] == "Diagnostic debug message for developers"
    assert "traceback" in data
    assert isinstance(data["traceback"], list)
