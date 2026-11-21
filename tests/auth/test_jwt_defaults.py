from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.core.config import Config, ConfigurationError


def test_jwt_issuer_default_ttls_are_one_hour_and_seven_days() -> None:
    secret = "a-very-secure-jwt-signing-secret-with-at-least-32-bytes"
    issuer = JWTIssuer(secret=secret)

    assert issuer.access_token_ttl == 3600
    assert issuer.refresh_token_ttl == 604800

    pair = issuer.issue_token_pair("user-123")
    assert pair["expires_in"] == 3600
    claims = issuer.decode_token(pair["access_token"], expected_type="access")
    assert int(claims["exp"]) - int(claims["iat"]) == 3600


def test_repo_auth_config_defaults_jwt_access_ttl_to_3600() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config.load(repo_root / "config")
    cfg.set("auth.jwt.secret", "config-secret-with-at-least-32-bytes-for-testing")

    issuer = JWTIssuer.from_config(cfg)
    assert issuer.access_token_ttl == 3600
    assert issuer.refresh_token_ttl == 604800


@pytest.mark.parametrize(
    "invalid_ttl",
    [True, False, 0, -1, -3600, 15.5, "3600", None, [3600]],
)
def test_jwt_issuer_rejects_invalid_access_and_refresh_ttls(invalid_ttl: Any) -> None:
    secret = "a-very-secure-jwt-signing-secret-with-at-least-32-bytes"

    with pytest.raises(ConfigurationError, match="access_token_ttl must be a positive integer"):
        JWTIssuer(secret=secret, access_token_ttl=invalid_ttl)

    with pytest.raises(ConfigurationError, match="refresh_token_ttl must be a positive integer"):
        JWTIssuer(secret=secret, refresh_token_ttl=invalid_ttl)


@pytest.mark.parametrize(
    "invalid_ttl",
    [True, False, 0, -1, -60, 12.34, "900"],
)
def test_jwt_issuer_from_config_rejects_invalid_ttls(invalid_ttl: Any) -> None:
    secret = "a-very-secure-jwt-signing-secret-with-at-least-32-bytes"

    cfg_access = Config(
        {"auth": {"jwt": {"secret": secret, "access_token_ttl": invalid_ttl}}}
    )
    with pytest.raises(ConfigurationError, match="access_token_ttl must be a positive integer"):
        JWTIssuer.from_config(cfg_access)

    cfg_refresh = Config(
        {"auth": {"jwt": {"secret": secret, "refresh_token_ttl": invalid_ttl}}}
    )
    with pytest.raises(ConfigurationError, match="refresh_token_ttl must be a positive integer"):
        JWTIssuer.from_config(cfg_refresh)


def test_jwt_issuer_accepts_valid_positive_integer_ttls() -> None:
    secret = "a-very-secure-jwt-signing-secret-with-at-least-32-bytes"
    issuer = JWTIssuer(secret=secret, access_token_ttl=1, refresh_token_ttl=120)
    assert issuer.access_token_ttl == 1
    assert issuer.refresh_token_ttl == 120

    cfg = Config(
        {
            "auth": {
                "jwt": {
                    "secret": secret,
                    "access_token_ttl": 1800,
                    "refresh_token_ttl": 86400,
                }
            }
        }
    )
    from_cfg = JWTIssuer.from_config(cfg)
    assert from_cfg.access_token_ttl == 1800
    assert from_cfg.refresh_token_ttl == 86400

