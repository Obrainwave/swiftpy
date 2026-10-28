from __future__ import annotations

import logging

import pytest

from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.core.config import Config, ConfigurationError


class TestJWTIssuerSecretEnforcement:
    def test_empty_secret_raises_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match=r"(?i)secret"):
            JWTIssuer(secret="")

    def test_short_secret_raises_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match=r"(?i)at least 32 bytes"):
            JWTIssuer(secret="too-short-secret")

    def test_short_bytes_secret_raises_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match=r"(?i)at least 32 bytes"):
            JWTIssuer(secret=b"short_bytes_key")

    def test_none_secret_for_hs256_raises_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match=r"(?i)secret"):
            JWTIssuer(secret=None, algorithm="HS256")

    def test_valid_secret_length_succeeds(self) -> None:
        valid_secret = "a" * 32
        issuer = JWTIssuer(secret=valid_secret)
        token = issuer.issue_access_token(subject="user_1")
        payload = issuer.decode_token(token)
        assert payload["sub"] == "user_1"

    def test_from_config_with_valid_secret(self) -> None:
        cfg = Config({
            "auth": {
                "guards": {
                    "jwt": {
                        "secret": "config-secret-key-at-least-32-chars!",
                        "algorithm": "HS256",
                        "access_token_ttl": 600,
                        "refresh_token_ttl": 3600,
                    }
                }
            }
        })
        issuer = JWTIssuer.from_config(cfg)
        assert issuer.access_token_ttl == 600
        token = issuer.issue_access_token(subject="42")
        payload = issuer.decode_token(token)
        assert payload["sub"] == "42"

    def test_from_config_missing_secret_non_debug_fails(self) -> None:
        cfg = Config({
            "app": {"debug": False},
            "auth": {
                "guards": {
                    "jwt": {
                        "secret": None,
                    }
                }
            },
        })
        with pytest.raises(ConfigurationError, match=r"(?i)JWT_SECRET"):
            JWTIssuer.from_config(cfg)

    def test_from_config_missing_secret_debug_mode_generates_ephemeral_secret(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = Config({
            "app": {"debug": True},
            "auth": {
                "guards": {
                    "jwt": {
                        "secret": None,
                    }
                }
            },
        })
        with caplog.at_level(logging.WARNING):
            issuer = JWTIssuer.from_config(cfg)
        assert any("JWT_SECRET" in record.message for record in caplog.records)
        token = issuer.issue_access_token(subject="debug_user")
        assert issuer.decode_token(token)["sub"] == "debug_user"

    def test_rs256_requires_keys(self) -> None:
        with pytest.raises(ConfigurationError, match=r"(?i)RS256"):
            JWTIssuer(algorithm="RS256")
