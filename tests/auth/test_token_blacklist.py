from __future__ import annotations

import asyncio
import time
import uuid
from datetime import timedelta

import pytest

from swiftpy.auth.jwt_issuer import InvalidSignatureError, JWTIssuer
from swiftpy.auth.token_blacklist import TokenBlacklist
from swiftpy.core.container import Container, set_container


def test_decode_unverified_removed_p2a3() -> None:
    """Verifies that JWTIssuer.decode_unverified is removed so unverified decoding is impossible."""
    assert not hasattr(JWTIssuer, "decode_unverified")


@pytest.mark.asyncio
async def test_revoke_token_requires_issuer_and_signature() -> None:
    """Verifies that unsigned or tampered tokens cannot revoke arbitrary JTIs and issuer is required."""
    set_container(Container())
    issuer1 = JWTIssuer(secret="test-secret-revocation-valid-32-bytes!!")
    issuer_bad = JWTIssuer(secret="test-secret-revocation-wrong-32-bytes!")

    prefix = f"test_jwt_bl_{uuid.uuid4().hex[:8]}:"
    blacklist_no_issuer = TokenBlacklist(prefix=prefix)
    token_valid = issuer1.issue_access_token(subject=123)

    with pytest.raises(ValueError, match="JWTIssuer is required"):
        await blacklist_no_issuer.revoke_token(token_valid)
    await blacklist_no_issuer.close()

    blacklist = TokenBlacklist(prefix=prefix, issuer=issuer1)
    await blacklist.clear()

    # Token signed with bad secret
    tampered_token = issuer_bad.issue_access_token(subject=123)

    with pytest.raises(InvalidSignatureError):
        await blacklist.revoke_token(tampered_token)

    await blacklist.close()


@pytest.mark.asyncio
async def test_forged_exp_capped_at_max_ttl() -> None:
    """Verifies that a token with a very long or distant exp cannot create an unbounded Redis TTL."""
    issuer = JWTIssuer(secret="test-secret-revocation-valid-32-bytes!!")
    prefix = f"test_jwt_bl_{uuid.uuid4().hex[:8]}:"
    max_ttl = 3600  # Cap at 1 hour for this blacklist
    blacklist = TokenBlacklist(prefix=prefix, issuer=issuer, max_token_lifetime=max_ttl)
    await blacklist.clear()

    # 10-year token
    long_token = issuer.issue_token(
        subject=123,
        expires_in=timedelta(days=3650),
    )
    payload = issuer.decode_token(long_token)
    jti = payload["jti"]

    await blacklist.revoke_token(long_token)
    assert await blacklist.is_revoked(jti) is True

    client = await blacklist.get_client()
    key = f"{prefix}{jti}"
    redis_ttl = await client.ttl(key)
    # Must be capped at max_token_lifetime (allowing slight drift)
    assert 0 < redis_ttl <= max_ttl

    await blacklist.close()


@pytest.mark.asyncio
async def test_concurrent_get_client_creates_one_client() -> None:
    """Verifies that concurrent get_client calls safely acquire the same Redis client instance under lock."""
    prefix = f"test_jwt_bl_{uuid.uuid4().hex[:8]}:"
    blacklist = TokenBlacklist(prefix=prefix)

    # Launch 25 concurrent tasks calling get_client
    clients = await asyncio.gather(*[blacklist.get_client() for _ in range(25)])

    # All returned clients must be the exact same object
    first = clients[0]
    for c in clients[1:]:
        assert c is first

    await blacklist.close()


@pytest.mark.asyncio
async def test_revoke_all_for_subject_invalidates_older_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies that revoke_all_for_subject sets a per-subject cutoff that invalidates older tokens."""
    issuer = JWTIssuer(secret="test-secret-revocation-valid-32-bytes!!")
    prefix = f"test_jwt_bl_{uuid.uuid4().hex[:8]}:"
    blacklist = TokenBlacklist(prefix=prefix, issuer=issuer)
    await blacklist.clear()

    user_id = 456
    base_time = int(time.time())

    # 1. Issue token 1 at base_time - 10
    monkeypatch.setattr(time, "time", lambda: float(base_time - 10))
    token1 = issuer.issue_access_token(subject=user_id)
    payload1 = issuer.decode_token(token1)

    # 2. Revoke all for subject at base_time - 5
    monkeypatch.setattr(time, "time", lambda: float(base_time - 5))
    await blacklist.revoke_all_for_subject(user_id)

    # 3. Issue token 2 at base_time (after revoke_all)
    monkeypatch.setattr(time, "time", lambda: float(base_time))
    token2 = issuer.issue_access_token(subject=user_id)
    payload2 = issuer.decode_token(token2)

    # Token 1 was issued before cutoff -> revoked
    assert (
        await blacklist.is_revoked(
            payload1["jti"],
            subject=user_id,
            issued_at=payload1["iat"],
        )
        is True
    )

    # Token 2 was issued strictly after cutoff -> not revoked
    assert (
        await blacklist.is_revoked(
            payload2["jti"],
            subject=user_id,
            issued_at=payload2["iat"],
        )
        is False
    )

    await blacklist.close()


def test_issue_token_pair_does_not_leak_access_claims_to_refresh() -> None:
    """Verifies that issue_token_pair does not copy access claims into refresh token and sets typ."""
    issuer = JWTIssuer(secret="test-secret-revocation-valid-32-bytes!!")
    pair = issuer.issue_token_pair(
        subject="user_123",
        claims={"role": "admin", "permissions": ["users:write", "billing:read"]},
    )
    access_payload = issuer.decode_token(pair["access_token"], expected_type="access")
    refresh_payload = issuer.decode_token(pair["refresh_token"], expected_type="refresh")

    assert access_payload["role"] == "admin"
    assert access_payload["permissions"] == ["users:write", "billing:read"]
    assert access_payload["type"] == "access"
    assert access_payload["typ"] == "access"

    assert "role" not in refresh_payload
    assert "permissions" not in refresh_payload
    assert refresh_payload["type"] == "refresh"
    assert refresh_payload["typ"] == "refresh"


