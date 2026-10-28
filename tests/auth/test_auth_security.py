from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any

import pytest

from swiftpy.auth import (
    ApiKeyGuard,
    AuthManager,
    CurrentUser,
    InvalidAlgorithmError,
    InvalidSignatureError,
    InvalidTokenError,
    JWTGuard,
    JWTIssuer,
    TokenBlacklist,
    TokenExpiredError,
    current_user,
    generate_api_key,
    hash_api_key,
    require_api_key,
    require_jwt,
)
from swiftpy.core.application import Application
from swiftpy.database.pool import DatabasePool
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response


def _b64url(data: bytes) -> str:
    """Encode bytes into base64url string without trailing padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_http_scope(
    method: str = "GET",
    path: str = "/",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    """Helper to build ASGI HTTP scope."""
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
    headers: dict[str, str] | list[tuple[str, str]] | None = None,
    body: bytes = b"",
) -> tuple[int, list[tuple[str, str]], dict[str, Any]]:
    """
    Simulate an ASGI request through Application pipeline.
    Preserves raw response headers as a list of (name, value) pairs
    and returns parsed json body or raw content.
    """
    if isinstance(headers, dict):
        raw_headers = [
            (k.lower().encode("ascii"), v.encode("ascii")) for k, v in headers.items()
        ]
    elif headers:
        raw_headers = [
            (k.lower().encode("ascii"), v.encode("ascii")) for k, v in headers
        ]
    else:
        raw_headers = []

    scope = _make_http_scope(method=method, path=path, headers=raw_headers)

    status_code = 0
    resp_headers: list[tuple[str, str]] = []
    resp_body = bytearray()
    body_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code, resp_headers, resp_body
        if message["type"] == "http.response.start":
            status_code = message["status"]
            resp_headers.extend(
                (k.decode("ascii"), v.decode("ascii"))
                for k, v in message.get("headers", [])
            )
        elif message["type"] == "http.response.body":
            resp_body.extend(message.get("body", b""))

    await app(scope, receive, send)

    try:
        parsed_body = json.loads(resp_body.decode("utf-8")) if resp_body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed_body = {"raw": resp_body.decode("utf-8", errors="replace")}

    return status_code, resp_headers, parsed_body


# ==============================================================================
# SECTION A: JWT ISSUER & CLAIMS VERIFICATION
# ==============================================================================


@pytest.mark.asyncio
async def test_jwt_token_pair_claims_and_lifetimes() -> None:
    """Verify standard claim boundaries, lifetime math, and UUID jti format."""
    issuer = JWTIssuer(
        secret="test-secret-key-at-least-32-chars-long!",
        algorithm="HS256",
        access_token_ttl=900,
        refresh_token_ttl=604800,
        issuer="swiftpy-test",
    )
    before = int(time.time())
    pair = issuer.issue_token_pair(subject=42)
    after = int(time.time())

    access = issuer.decode_token(pair["access_token"], expected_type="access")
    refresh = issuer.decode_token(pair["refresh_token"], expected_type="refresh")

    assert access["sub"] == "42"
    assert access["iss"] == "swiftpy-test"
    assert access["iat"] >= before
    assert access["iat"] <= after
    assert access["exp"] - access["iat"] == 900

    assert refresh["sub"] == "42"
    assert refresh["iat"] >= before
    assert refresh["iat"] <= after
    assert refresh["exp"] - refresh["iat"] == 604800

    # Verify UUID format of jti
    uuid.UUID(access["jti"])
    uuid.UUID(refresh["jti"])
    assert access["jti"] != refresh["jti"]


@pytest.mark.asyncio
async def test_jwt_reserved_claims_cannot_be_overridden() -> None:
    """Verify that user-supplied claims cannot overwrite security metadata (raises ValueError per P2-A5)."""
    issuer = JWTIssuer(
        secret="test-secret-key-at-least-32-chars-long!",
        algorithm="HS256",
        access_token_ttl=300,
    )

    with pytest.raises(ValueError, match="Cannot override reserved JWT claim"):
        issuer.issue_access_token(
            subject=10,
            claims={
                "sub": "admin",
                "exp": 9999999999,
                "type": "refresh",
                "custom_data": "allowed",
            },
        )

    token = issuer.issue_access_token(
        subject=10,
        claims={"custom_data": "allowed"},
    )
    payload = issuer.decode_token(token, expected_type="access")
    assert payload["sub"] == "10"
    assert payload["type"] == "access"
    assert payload["custom_data"] == "allowed"


@pytest.mark.asyncio
async def test_jwt_issuer_rs256_lifecycle() -> None:
    """Verify RS256 token issuance, RSA signing, and public key verification."""
    private_pem, public_pem = JWTIssuer.generate_rsa_key_pair(key_size=2048)

    issuer = JWTIssuer(
        private_key=private_pem,
        public_key=public_pem,
        algorithm="RS256",
        access_token_ttl=300,
    )

    token = issuer.issue_access_token(
        subject="user-uuid-123",
        claims={"tenant_id": 99},
    )

    payload = issuer.decode_token(token, expected_type="access")
    assert payload["sub"] == "user-uuid-123"
    assert payload["tenant_id"] == 99

    # Verification with verification-only public key instance
    verifier = JWTIssuer(
        public_key=public_pem,
        algorithm="RS256",
    )
    verified_payload = verifier.decode_token(token)
    assert verified_payload["sub"] == "user-uuid-123"

    with pytest.raises(ValueError, match="RSAPrivateKey must be provided"):
        verifier.issue_access_token(subject="should-fail")


@pytest.mark.asyncio
async def test_jwt_wrong_signing_key_rejected() -> None:
    """Verify that tokens signed with Secret A are rejected by Verifier using Secret B."""
    issuer_a = JWTIssuer(secret="secret-key-a-32-bytes-minimum----!")
    issuer_b = JWTIssuer(secret="secret-key-b-32-bytes-minimum----!")

    token_a = issuer_a.issue_access_token(subject=1)
    with pytest.raises(InvalidSignatureError, match="Signature verification failed"):
        issuer_b.decode_token(token_a)


@pytest.mark.asyncio
async def test_jwt_malformed_token_segments_rejected() -> None:
    """Verify that structurally malformed tokens are rejected with InvalidTokenError."""
    issuer = JWTIssuer(secret="secret-key-32-bytes-minimum------!")

    with pytest.raises(InvalidTokenError, match="expected 3 segments"):
        issuer.decode_token("not.enough.parts.here.extra")

    with pytest.raises(InvalidTokenError, match="expected 3 segments"):
        issuer.decode_token("only_one_part")

    with pytest.raises(InvalidTokenError, match="Malformed token header"):
        issuer.decode_token("not-valid-b64.not-valid-b64.sig")


@pytest.mark.asyncio
async def test_jwt_issuer_and_audience_validation() -> None:
    """Verify that mismatching issuer or audience claims raise InvalidTokenError."""
    issuer = JWTIssuer(
        secret="secret-key-32-bytes-minimum------!",
        issuer="expected-issuer",
        audience="expected-audience",
    )

    # Valid issuer and audience
    valid_token = issuer.issue_access_token(subject=1)
    assert issuer.decode_token(valid_token)["iss"] == "expected-issuer"

    # Verifier with different expected issuer
    verifier_wrong_iss = JWTIssuer(
        secret="secret-key-32-bytes-minimum------!",
        issuer="wrong-issuer",
    )
    with pytest.raises(InvalidTokenError, match="Invalid issuer"):
        verifier_wrong_iss.decode_token(valid_token)

    # Verifier with different expected audience
    verifier_wrong_aud = JWTIssuer(
        secret="secret-key-32-bytes-minimum------!",
        audience="wrong-audience",
    )
    with pytest.raises(InvalidTokenError, match="Invalid audience"):
        verifier_wrong_aud.decode_token(valid_token)


# ==============================================================================
# SECTION B: SECURITY ATTACK TESTS
# ==============================================================================


@pytest.mark.asyncio
async def test_rs256_rejects_hs256_algorithm_confusion() -> None:
    """
    Security verification: An RS256 verifier strictly rejects an HS256 token
    signed using the RSA public key as an HMAC secret (CVE-2015-9235 defense).
    """
    _, public_pem = JWTIssuer.generate_rsa_key_pair(key_size=2048)
    verifier = JWTIssuer(
        public_key=public_pem,
        algorithm="RS256",
    )

    header = _b64url(
        json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()
    )
    payload = _b64url(
        json.dumps(
            {
                "sub": "attacker",
                "type": "access",
                "iat": int(time.time()),
                "exp": int(time.time()) + 300,
                "jti": "attacker-token",
            },
            separators=(",", ":"),
        ).encode()
    )

    # Attacker uses RSA public key PEM as HMAC secret
    signing_input = f"{header}.{payload}".encode("ascii")
    attacker_sig = _b64url(
        hmac.new(public_pem.encode("utf-8"), signing_input, hashlib.sha256).digest()
    )
    confusion_token = f"{header}.{payload}.{attacker_sig}"

    # Verifier must reject algorithm mismatch
    with pytest.raises(InvalidAlgorithmError, match="Algorithm 'HS256' is not allowed"):
        verifier.decode_token(confusion_token)


@pytest.mark.asyncio
async def test_security_none_algorithm_attack_rejected() -> None:
    """Security verification: Tokens specifying 'none' algorithm are unconditionally blocked."""
    issuer = JWTIssuer(
        secret="secure-secret-key-for-test-32-bytes!!",
        algorithm="HS256",
    )

    header_bytes = json.dumps({"alg": "none", "typ": "JWT"}).encode("utf-8")
    payload_bytes = json.dumps(
        {
            "sub": "attacker",
            "type": "access",
            "exp": int(time.time()) + 300,
        }
    ).encode("utf-8")

    h_b64 = _b64url(header_bytes)
    p_b64 = _b64url(payload_bytes)
    none_token = f"{h_b64}.{p_b64}."

    with pytest.raises(
        InvalidAlgorithmError, match="The 'none' algorithm is strictly disallowed"
    ):
        issuer.decode_token(none_token)


@pytest.mark.asyncio
async def test_security_expired_tokens_rejected() -> None:
    """Security verification: Expired tokens are strictly rejected with 401."""
    issuer = JWTIssuer(
        secret="secure-secret-key-for-test-32-bytes!!",
        algorithm="HS256",
    )

    expired_token = issuer.issue_token(
        subject=100,
        token_type="access",
        expires_in=-10,
    )

    with pytest.raises(TokenExpiredError, match="Token has expired"):
        issuer.decode_token(expired_token)

    app = Application()
    app.container.instance(JWTIssuer, issuer)

    @app.get("/secure-data")
    async def get_secure_data(request: Request) -> Response:
        await require_jwt(request)
        return json_response({"message": "success"})

    status, headers, body = await _simulate_request(
        app,
        "GET",
        "/secure-data",
        headers={"Authorization": f"Bearer {expired_token}"},
    )

    assert status == 401
    assert "expired" in body.get("detail", "").lower()
    assert any(
        k.lower() == "www-authenticate" and "bearer" in v.lower() for k, v in headers
    )


@pytest.mark.asyncio
async def test_security_tampered_payload_rejected() -> None:
    """Security verification: Tampering with payload claims triggers InvalidSignatureError / 401."""
    issuer = JWTIssuer(
        secret="secure-secret-key-for-test-32-bytes!!",
        algorithm="HS256",
    )

    valid_token = issuer.issue_access_token(subject=5, claims={"role": "member"})
    parts = valid_token.split(".")

    # Tamper with payload segment to escalate privilege to role: "admin"
    tampered_payload = json.dumps(
        {
            "sub": "5",
            "type": "access",
            "role": "admin",
            "iat": int(time.time()),
            "exp": int(time.time()) + 900,
            "jti": "tampered-jti",
        }
    ).encode("utf-8")
    tampered_b64 = _b64url(tampered_payload)
    tampered_token = f"{parts[0]}.{tampered_b64}.{parts[2]}"

    with pytest.raises(InvalidSignatureError):
        issuer.decode_token(tampered_token)

    app = Application()
    app.container.instance(JWTIssuer, issuer)

    @app.get("/admin-zone")
    async def get_admin_data(request: Request) -> Response:
        await require_jwt(request)
        return json_response({"status": "ok"})

    status, _, body = await _simulate_request(
        app,
        "GET",
        "/admin-zone",
        headers={"Authorization": f"Bearer {tampered_token}"},
    )

    assert status == 401
    assert (
        "signature" in body.get("detail", "").lower()
        or "invalid" in body.get("detail", "").lower()
    )


@pytest.mark.asyncio
async def test_security_tampered_signature_rejected() -> None:
    """Security verification: Modified signatures are strictly rejected with 401."""
    issuer = JWTIssuer(
        secret="secure-secret-key-for-test-32-bytes!!",
        algorithm="HS256",
    )

    token = issuer.issue_access_token(subject=7)
    parts = token.split(".")

    corrupted_sig = parts[2][:-1] + ("A" if parts[2][-1] != "A" else "B")
    tampered_token = f"{parts[0]}.{parts[1]}.{corrupted_sig}"

    app = Application()
    app.container.instance(JWTIssuer, issuer)

    @app.get("/protected")
    async def protected_endpoint(request: Request) -> Response:
        await require_jwt(request)
        return json_response({"data": 123})

    status, _, _ = await _simulate_request(
        app,
        "GET",
        "/protected",
        headers={"Authorization": f"Bearer {tampered_token}"},
    )

    assert status == 401


# ==============================================================================
# SECTION C: JWT GUARD PIPELINE TESTS
# ==============================================================================


@pytest.mark.asyncio
async def test_jwt_guard_missing_credentials_returns_challenge() -> None:
    """Verify that requests with missing Authorization header return 401 with WWW-Authenticate challenge."""
    issuer = JWTIssuer(
        secret="secure-secret-key-for-test-32-bytes!!",
        algorithm="HS256",
    )
    app = Application()
    app.container.instance(JWTIssuer, issuer)

    @app.get("/protected")
    async def protected(request: Request) -> Response:
        await require_jwt(request)
        return json_response({"ok": True})

    status, headers, body = await _simulate_request(
        app,
        "GET",
        "/protected",
    )
    assert status == 401
    assert any(
        name.lower() == "www-authenticate" and value.lower().startswith("bearer")
        for name, value in headers
    )
    assert "detail" in body
    assert "missing" in body["detail"].lower()


@pytest.mark.asyncio
async def test_jwt_guard_malformed_bearer_headers_rejected() -> None:
    """Verify that malformed Authorization header formats return 401."""
    app = Application()
    app.container.instance(
        JWTIssuer,
        JWTIssuer(secret="test-secret-32-bytes-minimum-----!"),
    )

    @app.get("/protected")
    async def protected(request: Request) -> Response:
        await require_jwt(request)
        return json_response({"ok": True})

    # Malformed prefix (Basic instead of Bearer)
    status_basic, _, body_basic = await _simulate_request(
        app,
        "GET",
        "/protected",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert status_basic == 401
    assert "bearer" in body_basic["detail"].lower()

    # Empty token after Bearer
    status_empty, _, body_empty = await _simulate_request(
        app,
        "GET",
        "/protected",
        headers={"Authorization": "Bearer   "},
    )
    assert status_empty == 401
    assert (
        "empty" in body_empty["detail"].lower()
        or "invalid" in body_empty["detail"].lower()
    )


@pytest.mark.asyncio
async def test_jwt_guard_rejects_refresh_token() -> None:
    """Verify that presenting a refresh token to an access-protected route is strictly rejected."""
    issuer = JWTIssuer(
        secret="secure-secret-key-for-test-32-bytes!!",
        algorithm="HS256",
    )
    pair = issuer.issue_token_pair(subject=42)
    app = Application()
    app.container.instance(JWTIssuer, issuer)

    @app.get("/protected")
    async def protected(request: Request) -> Response:
        await require_jwt(request)
        return json_response({"ok": True})

    status, _, body = await _simulate_request(
        app,
        "GET",
        "/protected",
        headers={"Authorization": f"Bearer {pair['refresh_token']}"},
    )
    assert status == 401
    assert "ok" not in body
    assert body.get("detail") == "Invalid or expired token"


# ==============================================================================
# SECTION D: REDIS TOKEN BLACKLIST & REVOCATION
# ==============================================================================


@pytest.mark.asyncio
async def test_security_revoked_tokens_rejected() -> None:
    """Security verification: Logged out / revoked tokens in Redis blacklist are rejected with 401."""
    issuer = JWTIssuer(
        secret="test-secret-revocation-32-bytes-key!",
        algorithm="HS256",
    )
    unique_prefix = f"test_jwt_blacklist_{uuid.uuid4().hex[:8]}:"
    blacklist = TokenBlacklist(prefix=unique_prefix)
    await blacklist.clear()

    token = issuer.issue_access_token(subject=77)
    payload = issuer.decode_token(token)
    jti = payload["jti"]

    assert await blacklist.is_revoked(jti) is False

    app = Application()
    app.container.instance(JWTIssuer, issuer)
    app.container.instance(TokenBlacklist, blacklist)

    @app.get("/user/profile")
    async def user_profile(request: Request) -> Response:
        user = await require_jwt(request)
        return json_response({"user_id": user["sub"]})

    # 1. Request succeeds before logout
    status, _, body = await _simulate_request(
        app,
        "GET",
        "/user/profile",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status == 200
    assert body["user_id"] == "77"

    # 2. User logs out: Revoke the token
    await blacklist.revoke(jti, ttl=300)
    assert await blacklist.is_revoked(jti) is True

    # 3. Subsequent request with revoked token must be rejected with 401
    status_revoked, _, body_revoked = await _simulate_request(
        app,
        "GET",
        "/user/profile",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status_revoked == 401
    assert body_revoked.get("detail") == "Invalid or expired token"

    # 4. Issue a new fresh token for the same user; it must succeed
    fresh_token = issuer.issue_access_token(subject=77)
    status_fresh, _, body_fresh = await _simulate_request(
        app,
        "GET",
        "/user/profile",
        headers={"Authorization": f"Bearer {fresh_token}"},
    )
    assert status_fresh == 200
    assert body_fresh["user_id"] == "77"

    await blacklist.close()


@pytest.mark.asyncio
async def test_token_blacklist_ttl_and_expired_behavior() -> None:
    """Verify that blacklist sets remaining TTL in Redis and skips already-expired tokens."""
    unique_prefix = f"test_jwt_blacklist_{uuid.uuid4().hex[:8]}:"
    blacklist = TokenBlacklist(prefix=unique_prefix)
    await blacklist.clear()

    issuer = JWTIssuer(
        secret="test-secret-revocation-32-bytes-key!",
        access_token_ttl=120,
    )
    token = issuer.issue_access_token(subject=88)
    payload = issuer.decode_token(token)
    jti = payload["jti"]

    # Revoke via token
    await blacklist.revoke_token(token, issuer=issuer)
    assert await blacklist.is_revoked(jti) is True

    # Verify TTL in Redis is between 1 and 120
    client = await blacklist.get_client()
    key = f"{blacklist.prefix}{jti}"
    redis_ttl = await client.ttl(key)
    assert 0 < redis_ttl <= 120

    # Test already-expired token: should not be added to Redis
    expired_token = issuer.issue_token(subject=88, expires_in=-50)
    expired_payload = issuer.decode_token(expired_token, verify_exp=False)
    expired_jti = expired_payload["jti"]

    await blacklist.revoke_token(expired_token, issuer=issuer)
    assert await blacklist.is_revoked(expired_jti) is False

    await blacklist.close()


@pytest.mark.asyncio
async def test_token_blacklist_redis_failure_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify that if Redis fails during revocation check, authentication fails closed with generic 503 and logs detail."""
    issuer = JWTIssuer(secret="test-fail-closed-secret-32-bytes!")
    token = issuer.issue_access_token(subject=99)

    class BrokenBlacklist:
        async def is_revoked(self, jti: str, **kwargs: Any) -> bool:
            raise ConnectionError("Redis cluster unreachable at 10.0.0.42:6379")

    app = Application()
    app.container.instance(JWTIssuer, issuer)
    app.container.instance(TokenBlacklist, BrokenBlacklist())

    @app.get("/data")
    async def get_data(request: Request) -> Response:
        await require_jwt(request)
        return json_response({"ok": True})

    with caplog.at_level(logging.ERROR):
        status, headers, body = await _simulate_request(
            app,
            "GET",
            "/data",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert status == 503
    assert any(name.lower() == "retry-after" for name, _ in headers)
    assert body.get("detail") == "Authentication service unavailable"
    assert "redis" not in body.get("detail", "").lower()
    assert "10.0.0.42" not in json.dumps(body)
    assert any("10.0.0.42" in str(r.exc_info) or "Redis" in r.message for r in caplog.records)


# ==============================================================================
# SECTION E: API KEY GUARD TESTS
# ==============================================================================


@pytest.mark.asyncio
async def test_security_api_keys_bcrypt_validation(
    ensure_db_pool: DatabasePool,
) -> None:
    """
    Security verification: Validates prefix.secret API keys against bcrypt hashes
    in the database (offloaded to executor) including active, inactive, revoked,
    and expired states, and rejects legacy unsegmented keys.
    """
    pool = ensure_db_pool.get_pool()
    table_name = "test_auth_api_keys_comprehensive"

    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            DROP TABLE IF EXISTS "{table_name}" CASCADE;
            CREATE TABLE "{table_name}" (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                key_prefix VARCHAR(64) NOT NULL UNIQUE,
                key_hash TEXT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                revoked BOOLEAN DEFAULT FALSE,
                expires_at TIMESTAMPTZ DEFAULT NULL
            );
            """
        )

        valid_raw_key, valid_pfx, valid_sec = generate_api_key(prefix="sk_live_val")
        valid_hash = hash_api_key(valid_sec, rounds=4)

        inactive_raw_key, inactive_pfx, inactive_sec = generate_api_key(prefix="sk_live_inact")
        inactive_hash = hash_api_key(inactive_sec, rounds=4)

        revoked_raw_key, revoked_pfx, revoked_sec = generate_api_key(prefix="sk_live_rev")
        revoked_hash = hash_api_key(revoked_sec, rounds=4)

        expired_raw_key, expired_pfx, expired_sec = generate_api_key(prefix="sk_live_exp")
        expired_hash = hash_api_key(expired_sec, rounds=4)

        await conn.execute(
            f"""
            INSERT INTO "{table_name}" (name, key_prefix, key_hash, is_active, revoked, expires_at) VALUES
            ('Production Key', $1, $2, TRUE, FALSE, NULL),
            ('Inactive Key', $3, $4, FALSE, FALSE, NULL),
            ('Old Staging Key', $5, $6, TRUE, TRUE, NULL),
            ('Expired Key', $7, $8, TRUE, FALSE, NOW() - INTERVAL '1 day'),
            ('Malformed Hash Row', 'sk_live_malformed', 'not_a_bcrypt_hash', TRUE, FALSE, NULL);
            """,
            valid_pfx,
            valid_hash,
            inactive_pfx,
            inactive_hash,
            revoked_pfx,
            revoked_hash,
            expired_pfx,
            expired_hash,
        )

    app = Application()
    app.container.instance(DatabasePool, ensure_db_pool)

    @app.get("/api/v1/metrics")
    async def get_metrics(request: Request) -> Response:
        user = await require_api_key(request, table=table_name)
        return json_response({"name": user["name"]})

    # Positive test: Valid API key via X-API-Key header
    status, _, body = await _simulate_request(
        app,
        "GET",
        "/api/v1/metrics",
        headers={"x-api-key": valid_raw_key},
    )
    assert status == 200
    assert body["name"] == "Production Key"

    # Positive test: Valid API key via Authorization header
    status_auth, _, body_auth = await _simulate_request(
        app,
        "GET",
        "/api/v1/metrics",
        headers={"Authorization": f"ApiKey {valid_raw_key}"},
    )
    assert status_auth == 200
    assert body_auth["name"] == "Production Key"

    # Negative test 1: Inactive key (is_active = FALSE)
    status_inactive, _, body_inactive = await _simulate_request(
        app,
        "GET",
        "/api/v1/metrics",
        headers={"x-api-key": inactive_raw_key},
    )
    assert status_inactive == 401
    assert "inactive" in body_inactive.get("detail", "").lower()

    # Negative test 2: Revoked key
    status_rev, _, body_rev = await _simulate_request(
        app,
        "GET",
        "/api/v1/metrics",
        headers={"x-api-key": revoked_raw_key},
    )
    assert status_rev == 401
    assert "revoked" in body_rev.get("detail", "").lower()

    # Negative test 3: Expired key
    status_exp, _, body_exp = await _simulate_request(
        app,
        "GET",
        "/api/v1/metrics",
        headers={"x-api-key": expired_raw_key},
    )
    assert status_exp == 401
    assert "expired" in body_exp.get("detail", "").lower()

    # Negative test 4: Invalid key (testing against valid & malformed hashes)
    status_bad, _, body_bad = await _simulate_request(
        app,
        "GET",
        "/api/v1/metrics",
        headers={"x-api-key": "sk_live_malformed.wrong_secret"},
    )
    assert status_bad == 401
    assert "invalid" in body_bad.get("detail", "").lower()

    # Negative test 5: Legacy unsegmented key rejected with format error
    status_legacy, _, body_legacy = await _simulate_request(
        app,
        "GET",
        "/api/v1/metrics",
        headers={"x-api-key": "sk_live_legacy_unsegmented_key"},
    )
    assert status_legacy == 401
    assert "format" in body_legacy.get("detail", "").lower()

    # Negative test 6: Missing header
    status_none, _, body_none = await _simulate_request(
        app,
        "GET",
        "/api/v1/metrics",
    )
    assert status_none == 401
    assert "missing" in body_none.get("detail", "").lower()


@pytest.mark.asyncio
async def test_api_key_guard_invalid_table_identifier_blocked() -> None:
    """
    Security verification: SQL injection payloads in table and column names are
    blocked upfront at configuration and guard creation boundaries.
    """
    # 1. Guard configuration validation raises ValueError upfront
    with pytest.raises(ValueError, match="Invalid table identifier"):
        ApiKeyGuard(table="api_keys; DROP TABLE users;")

    with pytest.raises(ValueError, match="Invalid column identifier"):
        ApiKeyGuard(hash_column="key_hash; DROP TABLE users;")

    # 2. Direct call to require_api_key also validates table identifier upfront
    req = Request(_make_http_scope(headers=[(b"x-api-key", b"sk_pfx.secret")]), None)
    with pytest.raises(ValueError, match="Invalid table identifier"):
        await require_api_key(req, table="api_keys; DROP TABLE users;")

    with pytest.raises(ValueError, match="Invalid column identifier"):
        await require_api_key(req, hash_column="key_hash; DROP TABLE users;")


@pytest.mark.asyncio
async def test_api_key_guard_database_failure_fails_closed(
    ensure_db_pool: DatabasePool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify that database query errors during API key verification fail closed with generic 503 and log detail."""
    app = Application()
    app.container.instance(DatabasePool, ensure_db_pool)

    @app.get("/data")
    async def handler(request: Request) -> Response:
        # Table does not exist in database
        await require_api_key(
            request,
            table="non_existent_table_xyz",
            pepper="pepper-at-least-32-bytes-long---!",
        )
        return json_response({"ok": True})

    with caplog.at_level(logging.ERROR):
        status, headers, body = await _simulate_request(
            app,
            "GET",
            "/data",
            headers={"x-api-key": "sk_some_prefix.some_secret_value"},
        )
    assert status == 503
    assert any(name.lower() == "retry-after" for name, _ in headers)
    assert body.get("detail") == "Authentication service unavailable"
    assert "database" not in body.get("detail", "").lower()
    assert "non_existent_table_xyz" not in json.dumps(body)
    assert any(
        "non_existent_table_xyz" in str(r.exc_info) or "database error" in r.message
        for r in caplog.records
    )


# ==============================================================================
# SECTION F: AUTH MANAGER, DI CONTAINER & CONCURRENCY ISOLATION
# ==============================================================================


@pytest.mark.asyncio
async def test_auth_manager_guard_resolution() -> None:
    """Verify AuthManager guard resolution and extension mechanism."""
    auth = AuthManager()

    jwt_guard = auth.guard("jwt")
    assert isinstance(jwt_guard, JWTGuard)

    api_key_guard = auth.guard("api_key")
    assert isinstance(api_key_guard, ApiKeyGuard)

    class CustomGuard:
        def authenticate(self, req: Any) -> str:
            return "custom-ok"

    auth.extend("custom", lambda _: CustomGuard())
    custom = auth.guard("custom")
    assert isinstance(custom, CustomGuard)


@pytest.mark.asyncio
async def test_current_user_di_injection_and_task_isolation() -> None:
    """
    Verify current_user() and CurrentUser DI container resolution
    with strict task-scoped concurrency isolation across 30 workers.
    """
    issuer = JWTIssuer(secret="test-di-isolation-secret-32-bytes!!")
    app = Application()
    app.container.instance(JWTIssuer, issuer)

    auth = app.container.resolve(AuthManager)
    auth.register_container_bindings(app.container)

    @app.get("/whoami")
    async def whoami(request: Request) -> Response:
        await require_jwt(request)
        user = current_user()
        assert user is not None

        di_user = app.container.resolve(CurrentUser)
        assert di_user.id == user["sub"]

        await asyncio.sleep(0.01)

        return json_response(
            {
                "user_id": user["sub"],
                "di_id": di_user.id,
                "email": user["email"],
            }
        )

    async def worker(uid: int) -> tuple[int, str]:
        token = issuer.issue_access_token(
            subject=uid,
            claims={"email": f"user{uid}@swiftpy.dev"},
        )
        status, _, body = await _simulate_request(
            app,
            "GET",
            "/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert status == 200
        assert body["user_id"] == str(uid)
        assert body["di_id"] == str(uid)
        assert body["email"] == f"user{uid}@swiftpy.dev"
        return status, body["user_id"]

    results = await asyncio.gather(*(worker(i) for i in range(1, 31)))
    assert len(results) == 30
    assert all(status == 200 for status, _ in results)
    assert [uid for _, uid in results] == [str(i) for i in range(1, 31)]


@pytest.mark.asyncio
async def test_di_context_cleared_after_failed_request() -> None:
    """Verify that a failed/rejected authentication does not leave lingering user context."""
    app = Application()
    issuer = JWTIssuer(secret="test-secret-32-bytes-minimum-----!")
    app.container.instance(JWTIssuer, issuer)

    auth = app.container.resolve(AuthManager)
    auth.register_container_bindings(app.container)

    @app.get("/guarded")
    async def guarded(request: Request) -> Response:
        await require_jwt(request)
        return json_response({"user": current_user()})

    @app.get("/public")
    async def public_route(request: Request) -> Response:
        user = current_user()
        return json_response({"has_user": user is not None})

    # 1. Request fails authentication
    status_fail, _, _ = await _simulate_request(
        app,
        "GET",
        "/guarded",
        headers={"Authorization": "Bearer bad-token"},
    )
    assert status_fail == 401

    # 2. Subsequent public request must not have lingering user context
    status_ok, _, body_ok = await _simulate_request(
        app,
        "GET",
        "/public",
    )
    assert status_ok == 200
    assert body_ok["has_user"] is False


@pytest.mark.asyncio
async def test_cross_application_isolation() -> None:
    """Verify that two independent Application instances maintain strict container and token isolation."""
    issuer_1 = JWTIssuer(secret="app1-secret-at-least-32-bytes----!")
    issuer_2 = JWTIssuer(secret="app2-secret-at-least-32-bytes----!")

    app1 = Application(title="App 1")
    app1.container.instance(JWTIssuer, issuer_1)

    app2 = Application(title="App 2")
    app2.container.instance(JWTIssuer, issuer_2)

    @app1.get("/protected")
    async def app1_protected(request: Request) -> Response:
        await require_jwt(request)
        return json_response({"app": 1})

    @app2.get("/protected")
    async def app2_protected(request: Request) -> Response:
        await require_jwt(request)
        return json_response({"app": 2})

    token1 = issuer_1.issue_access_token(subject="user1")
    token2 = issuer_2.issue_access_token(subject="user2")

    # Token 1 works on App 1, rejected by App 2
    status1, _, body1 = await _simulate_request(
        app1, "GET", "/protected", headers={"Authorization": f"Bearer {token1}"}
    )
    assert status1 == 200
    assert body1["app"] == 1

    status2, _, _ = await _simulate_request(
        app2, "GET", "/protected", headers={"Authorization": f"Bearer {token1}"}
    )
    assert status2 == 401

    # Token 2 works on App 2, rejected by App 1
    status3, _, body3 = await _simulate_request(
        app2, "GET", "/protected", headers={"Authorization": f"Bearer {token2}"}
    )
    assert status3 == 200
    assert body3["app"] == 2


@pytest.mark.asyncio
async def test_jwt_wrong_signing_key_rejected_http() -> None:
    """Verify that a token signed with the wrong key is rejected with HTTP 401 without leaking internal details."""
    issuer_signer = JWTIssuer(
        secret="key-a-used-for-signing-at-least-32-chars-long!",
        algorithm="HS256",
    )
    issuer_verifier = JWTIssuer(
        secret="key-b-configured-on-app-at-least-32-chars-long!",
        algorithm="HS256",
    )
    token = issuer_signer.issue_access_token(subject=101)

    app = Application()
    app.container.instance(JWTIssuer, issuer_verifier)

    @app.get("/secure-data")
    async def secure_endpoint(request: Request) -> Response:
        await require_jwt(request)
        return json_response({"secret": 999})

    status, headers, body = await _simulate_request(
        app,
        "GET",
        "/secure-data",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status == 401
    assert any(
        name.lower() == "www-authenticate" and value.lower().startswith("bearer")
        for name, value in headers
    )
    assert body.get("detail") == "Invalid or expired token"
    # Ensure no secrets or sensitive internal stack details are leaked
    body_str = json.dumps(body)
    assert "key-a" not in body_str
    assert "key-b" not in body_str
    assert "Traceback" not in body_str


@pytest.mark.asyncio
async def test_jwt_none_algorithm_case_variations() -> None:
    """Security verification: The 'none' algorithm is rejected across all case variations ('none', 'NONE', 'None', 'nOnE')."""
    issuer = JWTIssuer(
        secret="test-secret-32-bytes-minimum-----!",
        algorithm="HS256",
    )
    payload = {
        "sub": "attacker",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
    }
    payload_b64 = _b64url(json.dumps(payload).encode("utf-8"))

    for none_variation in ("none", "NONE", "None", "nOnE"):
        header = {"alg": none_variation, "typ": "JWT"}
        header_b64 = _b64url(json.dumps(header).encode("utf-8"))
        unsigned_token = f"{header_b64}.{payload_b64}."

        with pytest.raises(InvalidAlgorithmError, match="disallowed"):
            issuer.decode_token(unsigned_token)


@pytest.mark.asyncio
async def test_jwt_claims_nbf_and_malformed_rejected() -> None:
    """Security verification: Invalid nbf, malformed claims, and missing sub are strictly rejected."""
    issuer = JWTIssuer(
        secret="test-secret-claims-32-bytes-long!",
        algorithm="HS256",
    )
    now = int(time.time())

    # 1. Future nbf constraint
    future_nbf_token = issuer.issue_token(
        subject="alice",
        not_before=now + 1000,
    )
    with pytest.raises(InvalidTokenError, match="not active yet"):
        issuer.decode_token(future_nbf_token)

    # 2. Non-numeric nbf
    header_b64 = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
    bad_nbf_payload = {
        "sub": "alice",
        "exp": now + 300,
        "iat": now,
        "nbf": "tomorrow",
    }
    bad_nbf_b64 = _b64url(json.dumps(bad_nbf_payload).encode("utf-8"))
    sig_bytes = issuer._sign(f"{header_b64}.{bad_nbf_b64}".encode("ascii"), "HS256")
    token_bad_nbf = f"{header_b64}.{bad_nbf_b64}.{_b64url(sig_bytes)}"
    with pytest.raises(InvalidTokenError, match="numeric timestamp"):
        issuer.decode_token(token_bad_nbf)

    # 3. Non-numeric iat
    bad_iat_payload = {
        "sub": "alice",
        "exp": now + 300,
        "iat": "yesterday",
    }
    bad_iat_b64 = _b64url(json.dumps(bad_iat_payload).encode("utf-8"))
    sig_bytes = issuer._sign(f"{header_b64}.{bad_iat_b64}".encode("ascii"), "HS256")
    token_bad_iat = f"{header_b64}.{bad_iat_b64}.{_b64url(sig_bytes)}"
    with pytest.raises(InvalidTokenError, match="numeric timestamp"):
        issuer.decode_token(token_bad_iat)

    # 4. Non-numeric exp
    bad_exp_payload = {
        "sub": "alice",
        "exp": "never",
        "iat": now,
    }
    bad_exp_b64 = _b64url(json.dumps(bad_exp_payload).encode("utf-8"))
    sig_bytes = issuer._sign(f"{header_b64}.{bad_exp_b64}".encode("ascii"), "HS256")
    token_bad_exp = f"{header_b64}.{bad_exp_b64}.{_b64url(sig_bytes)}"
    with pytest.raises(InvalidTokenError, match="numeric timestamp"):
        issuer.decode_token(token_bad_exp)

    # 5. Missing / empty sub
    missing_sub_payload = {
        "exp": now + 300,
        "iat": now,
    }
    missing_sub_b64 = _b64url(json.dumps(missing_sub_payload).encode("utf-8"))
    sig_bytes = issuer._sign(f"{header_b64}.{missing_sub_b64}".encode("ascii"), "HS256")
    token_missing_sub = f"{header_b64}.{missing_sub_b64}.{_b64url(sig_bytes)}"
    with pytest.raises(InvalidTokenError, match="Missing or empty 'sub'"):
        issuer.decode_token(token_missing_sub)


@pytest.mark.asyncio
async def test_rs256_wrong_public_key_rejected_and_http() -> None:
    """Verify that an RS256 token signed by RSA Keypair A is rejected when verified with RSA Public Key B."""
    priv_a, pub_a = JWTIssuer.generate_rsa_key_pair()
    _, pub_b = JWTIssuer.generate_rsa_key_pair()

    issuer_a = JWTIssuer(private_key=priv_a, public_key=pub_a, algorithm="RS256")
    issuer_b = JWTIssuer(public_key=pub_b, algorithm="RS256")

    token_a = issuer_a.issue_access_token(subject="user_rsa_1")

    # 1. Direct decode_token raises InvalidSignatureError
    with pytest.raises(InvalidSignatureError):
        issuer_b.decode_token(token_a)

    # 2. HTTP application with public key B returns 401 Unauthorized
    app = Application()
    app.container.instance(JWTIssuer, issuer_b)

    @app.get("/rsa-data")
    async def rsa_data(request: Request) -> Response:
        await require_jwt(request)
        return json_response({"ok": True})

    status, headers, body = await _simulate_request(
        app,
        "GET",
        "/rsa-data",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert status == 401
    assert any(
        name.lower() == "www-authenticate" and value.lower().startswith("bearer")
        for name, value in headers
    )
    assert body.get("detail") == "Invalid or expired token"


@pytest.mark.asyncio
async def test_duplicate_authorization_and_api_key_headers_rejected() -> None:
    """Security verification: Duplicate Authorization or duplicate API key headers are rejected with 401."""
    issuer = JWTIssuer(secret="test-duplicate-headers-secret-32-chars!")
    token1 = issuer.issue_access_token(subject=1)
    token2 = issuer.issue_access_token(subject=2)

    app = Application()
    app.container.instance(JWTIssuer, issuer)

    @app.get("/jwt-guarded")
    async def guarded_jwt(request: Request) -> Response:
        await require_jwt(request)
        return json_response({"ok": True})

    @app.get("/apikey-guarded")
    async def guarded_key(request: Request) -> Response:
        async def _val(k: str) -> dict[str, str] | None:
            return {"user": k} if k == "valid_key" else None

        await require_api_key(
            request,
            header_name="x-api-key",
            key_validator=_val,
        )
        return json_response({"ok": True})

    # Duplicate Authorization headers
    status_dup_jwt, _, body_dup_jwt = await _simulate_request(
        app,
        "GET",
        "/jwt-guarded",
        headers=[
            ("Authorization", f"Bearer {token1}"),
            ("Authorization", f"Bearer {token2}"),
        ],
    )
    assert status_dup_jwt == 401
    assert "multiple" in body_dup_jwt.get("detail", "").lower()

    # Duplicate X-API-Key headers
    status_dup_key, _, body_dup_key = await _simulate_request(
        app,
        "GET",
        "/apikey-guarded",
        headers=[
            ("x-api-key", "valid_key"),
            ("x-api-key", "other_key"),
        ],
    )
    assert status_dup_key == 401
    assert "multiple" in body_dup_key.get("detail", "").lower()


@pytest.mark.asyncio
async def test_task_cancellation_and_exception_isolation() -> None:
    """Verify that exceptions after successful authentication do not leak CurrentUser state."""
    issuer = JWTIssuer(secret="test-exception-leak-secret-32-bytes!!")
    app = Application()
    app.container.instance(JWTIssuer, issuer)
    auth = app.container.resolve(AuthManager)
    auth.register_container_bindings(app.container)

    @app.get("/crash-after-auth")
    async def crash_handler(request: Request) -> Response:
        await require_jwt(request)
        # Authentication succeeded, state was set
        assert current_user() is not None
        raise RuntimeError("Handler crashed after authentication!")

    @app.get("/public-check")
    async def public_check(request: Request) -> Response:
        user = current_user()
        return json_response({"has_user": user is not None})

    token = issuer.issue_access_token(subject="crash_victim")

    # 1. Request crashes inside handler
    status_crash, _, body_crash = await _simulate_request(
        app,
        "GET",
        "/crash-after-auth",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status_crash == 500
    assert body_crash.get("detail") == "Internal Server Error"

    # Verify task context in caller is clean
    assert current_user() is None

    # 2. Subsequent public request in same pipeline is completely unaffected
    status_pub, _, body_pub = await _simulate_request(
        app,
        "GET",
        "/public-check",
    )
    assert status_pub == 200
    assert body_pub["has_user"] is False


@pytest.mark.asyncio
async def test_api_key_guard_malformed_hashes_in_database_only(
    ensure_db_pool: DatabasePool,
) -> None:
    """
    Security verification: When database contains ONLY malformed or corrupted hashes,
    requests with API keys are rejected with 401 and never trigger 500 server crashes.
    """
    pool = ensure_db_pool.get_pool()
    table_name = "test_auth_only_malformed_hashes"

    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            DROP TABLE IF EXISTS "{table_name}" CASCADE;
            CREATE TABLE "{table_name}" (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                key_prefix VARCHAR(64) NOT NULL UNIQUE,
                key_hash TEXT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                revoked BOOLEAN DEFAULT FALSE,
                expires_at TIMESTAMPTZ DEFAULT NULL
            );
            INSERT INTO "{table_name}" (name, key_prefix, key_hash, is_active, revoked) VALUES
            ('Plain text hash', 'sk_m1', 'not_a_bcrypt_hash_at_all', TRUE, FALSE),
            ('Bad prefix', 'sk_m2', '$2a$bad_salt_too_short', TRUE, FALSE),
            ('Empty hash', 'sk_m3', '', TRUE, FALSE),
            ('Truncated', 'sk_m4', '$2b$12$', TRUE, FALSE);
            """
        )

    app = Application()
    app.container.instance(DatabasePool, ensure_db_pool)

    @app.get("/api/data")
    async def get_data(request: Request) -> Response:
        await require_api_key(
            request,
            table=table_name,
            pepper="malformed-test-pepper-32-bytes!!",
        )
        return json_response({"ok": True})

    for pfx in ("sk_m1", "sk_m2", "sk_m3", "sk_m4", "sk_nonexistent"):
        status, _, body = await _simulate_request(
            app,
            "GET",
            "/api/data",
            headers={"x-api-key": f"{pfx}.attempt_secret_123"},
        )
        assert status == 401
        assert "invalid" in body.get("detail", "").lower()


@pytest.mark.asyncio
async def test_auth_errors_never_leak_issuer_audience_or_token_p2a4(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    P2-A4: Verifies that 401 response bodies never leak expected issuer or audience,
    while logging the cause server-side without logging the raw JWT token.
    """
    secret = "p2a4-no-leak-secret-at-least-32-bytes!"
    issuer_token = JWTIssuer(
        secret=secret,
        issuer="wrong-issuer",
        audience="wrong-audience",
    )
    issuer_server = JWTIssuer(
        secret=secret,
        issuer="secret-internal-issuer-name",
        audience="secret-internal-audience-name",
    )
    token = issuer_token.issue_access_token(subject="user_p2a4")

    app = Application()
    app.container.instance(JWTIssuer, issuer_server)

    @app.get("/protected")
    async def protected(request: Request) -> Response:
        await require_jwt(request)
        return json_response({"ok": True})

    with caplog.at_level(logging.WARNING):
        status, _, body = await _simulate_request(
            app,
            "GET",
            "/protected",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert status == 401
    assert body.get("detail") == "Invalid or expired token"
    body_text = json.dumps(body)
    assert "secret-internal-issuer-name" not in body_text
    assert "secret-internal-audience-name" not in body_text
    # Log record contains the cause, but NEVER the raw token
    log_messages = " ".join(r.getMessage() for r in caplog.records)
    assert "Invalid issuer" in log_messages
    assert token not in log_messages


@pytest.mark.asyncio
async def test_cross_application_di_current_user_isolation() -> None:
    """
    Security verification: Directly verify independent CurrentUser state
    and DI resolution across separate applications.
    """
    issuer1 = JWTIssuer(secret="app1-isolated-secret-32-chars-long!")
    issuer2 = JWTIssuer(secret="app2-isolated-secret-32-chars-long!")

    app1 = Application(title="Service A")
    app1.container.instance(JWTIssuer, issuer1)
    auth1 = app1.container.resolve(AuthManager)
    auth1.register_container_bindings(app1.container)

    app2 = Application(title="Service B")
    app2.container.instance(JWTIssuer, issuer2)
    auth2 = app2.container.resolve(AuthManager)
    auth2.register_container_bindings(app2.container)

    @app1.get("/me")
    async def app1_me(request: Request) -> Response:
        await require_jwt(request)
        user = app1.container.resolve(CurrentUser)
        return json_response({"app": "A", "user_id": user.id, "email": user.email})

    @app2.get("/me")
    async def app2_me(request: Request) -> Response:
        await require_jwt(request)
        user = app2.container.resolve(CurrentUser)
        return json_response({"app": "B", "user_id": user.id, "email": user.email})

    token_a = issuer1.issue_access_token(
        subject="alice_alpha", claims={"email": "alice@app1.internal"}
    )
    token_b = issuer2.issue_access_token(
        subject="bob_beta", claims={"email": "bob@app2.internal"}
    )

    async def call_app1() -> tuple[int, dict[str, Any]]:
        s, _, b = await _simulate_request(
            app1, "GET", "/me", headers={"Authorization": f"Bearer {token_a}"}
        )
        return s, b

    async def call_app2() -> tuple[int, dict[str, Any]]:
        s, _, b = await _simulate_request(
            app2, "GET", "/me", headers={"Authorization": f"Bearer {token_b}"}
        )
        return s, b

    (s1, b1), (s2, b2) = await asyncio.gather(call_app1(), call_app2())

    assert s1 == 200
    assert b1["app"] == "A"
    assert b1["user_id"] == "alice_alpha"
    assert b1["email"] == "alice@app1.internal"

    assert s2 == 200
    assert b2["app"] == "B"
    assert b2["user_id"] == "bob_beta"
    assert b2["email"] == "bob@app2.internal"
