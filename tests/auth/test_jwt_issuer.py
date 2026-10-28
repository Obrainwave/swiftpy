from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time

import pytest

from swiftpy.auth.jwt_issuer import (
    InvalidAlgorithmError,
    InvalidSignatureError,
    InvalidTokenError,
    JWTIssuer,
    TokenExpiredError,
)
from swiftpy.core.application import Application
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

    def test_forged_token_with_empty_secret_impossible(self) -> None:
        with pytest.raises(ConfigurationError):
            JWTIssuer(secret="")

        header_b64 = (
            base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"access"}')
            .rstrip(b"=")
            .decode("ascii")
        )
        payload_bytes = json.dumps(
            {
                "sub": "admin",
                "type": "access",
                "typ": "access",
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
                "jti": "forged-id",
            }
        ).encode("utf-8")
        payload_b64 = (
            base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
        )
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        forged_sig = (
            base64.urlsafe_b64encode(hmac.new(b"", signing_input, hashlib.sha256).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        forged_token = f"{header_b64}.{payload_b64}.{forged_sig}"

        issuer = JWTIssuer(secret="valid-secret-key-at-least-32-bytes!")
        with pytest.raises(InvalidSignatureError):
            issuer.decode_token(forged_token)

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
                        "issuer": "swiftpy-api",
                        "audience": "swiftpy-client",
                    }
                }
            }
        })
        issuer = JWTIssuer.from_config(cfg)
        assert issuer.access_token_ttl == 600
        assert issuer.refresh_token_ttl == 3600
        assert issuer.issuer == "swiftpy-api"
        assert issuer.audience == "swiftpy-client"
        token = issuer.issue_access_token(subject="42")
        payload = issuer.decode_token(token)
        assert payload["sub"] == "42"
        assert payload["iss"] == "swiftpy-api"
        assert payload["aud"] == "swiftpy-client"

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

    def test_application_boot_missing_secret_non_debug_fails_fast(self) -> None:
        app = Application(
            debug=False,
            config={
                "app": {"debug": False, "env": "production"},
                "auth": {
                    "defaults": {"guard": "jwt"},
                    "guards": {"jwt": {"secret": None}},
                },
            },
        )
        with pytest.raises(ConfigurationError, match=r"(?i)JWT_SECRET"):
            app.boot()

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
            issuer1 = JWTIssuer.from_config(cfg)
            issuer2 = JWTIssuer.from_config(cfg)
        assert any("JWT_SECRET" in record.message for record in caplog.records)
        token1 = issuer1.issue_access_token(subject="debug_user")
        assert issuer1.decode_token(token1)["sub"] == "debug_user"
        # Never reuse a fixed value: second issuer must have a distinct random secret
        with pytest.raises(InvalidSignatureError):
            issuer2.decode_token(token1)

    def test_rs256_requires_keys(self) -> None:
        with pytest.raises(ConfigurationError, match=r"(?i)RS256"):
            JWTIssuer(algorithm="RS256")


class TestJWTHardeningP2A5:
    def _issuer(
        self,
        *,
        leeway: int = 0,
        audience: str | list[str] | None = None,
    ) -> JWTIssuer:
        return JWTIssuer(
            secret="hardening-secret-key-at-least-32-bytes!",
            leeway=leeway,
            audience=audience,
        )

    def test_future_iat_and_nbf_with_leeway(self) -> None:
        now = int(time.time())
        issuer_strict = self._issuer(leeway=0)
        issuer_leeway = self._issuer(leeway=30)

        # Craft token with iat 15s in the future
        header_b64 = (
            base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"access"}')
            .rstrip(b"=")
            .decode("ascii")
        )
        payload_b64 = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "sub": "u1",
                        "type": "access",
                        "typ": "access",
                        "iat": now + 15,
                        "exp": now + 300,
                        "jti": "id-future-iat",
                    }
                ).encode("utf-8")
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        sig = (
            base64.urlsafe_b64encode(
                issuer_strict._sign(f"{header_b64}.{payload_b64}".encode("ascii"), "HS256")
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        future_iat_token = f"{header_b64}.{payload_b64}.{sig}"

        with pytest.raises(InvalidTokenError, match="future"):
            issuer_strict.decode_token(future_iat_token)

        # Within 30s leeway -> accepted
        decoded = issuer_leeway.decode_token(future_iat_token)
        assert decoded["sub"] == "u1"

        # Future nbf 15s ahead: rejected with leeway=0, accepted with leeway=30
        nbf_token = issuer_strict.issue_token(subject="u2", not_before=now + 15)
        with pytest.raises(InvalidTokenError, match="nbf"):
            issuer_strict.decode_token(nbf_token)
        assert issuer_leeway.decode_token(nbf_token)["sub"] == "u2"

        # Expired 10s ago: rejected with leeway=0, accepted with leeway=30
        exp_token = issuer_strict.issue_token(subject="u3", expires_in=-10)
        with pytest.raises(TokenExpiredError, match="expired"):
            issuer_strict.decode_token(exp_token)
        assert issuer_leeway.decode_token(exp_token)["sub"] == "u3"

    def test_audience_as_list_supported(self) -> None:
        issuer = self._issuer(audience=["service-a", "service-b"])
        token = issuer.issue_access_token(subject="user_aud")
        payload = issuer.decode_token(token)
        assert payload["aud"] == ["service-a", "service-b"]

        verifier_single = self._issuer(audience="service-b")
        assert verifier_single.decode_token(token)["sub"] == "user_aud"

        verifier_wrong = self._issuer(audience="service-c")
        with pytest.raises(InvalidTokenError, match="Invalid audience"):
            verifier_wrong.decode_token(token)

    @pytest.mark.parametrize("bad_exp", ["NaN", "Infinity", "-Infinity", "true", "false"])
    def test_non_finite_or_boolean_exp_rejected(self, bad_exp: str) -> None:
        issuer = self._issuer()
        header_b64 = (
            base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"access"}')
            .rstrip(b"=")
            .decode("ascii")
        )
        raw_payload = f'{{"sub":"u1","type":"access","iat":{int(time.time())},"exp":{bad_exp},"jti":"j1"}}'.encode()
        payload_b64 = base64.urlsafe_b64encode(raw_payload).rstrip(b"=").decode("ascii")
        sig = (
            base64.urlsafe_b64encode(
                issuer._sign(f"{header_b64}.{payload_b64}".encode("ascii"), "HS256")
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        token = f"{header_b64}.{payload_b64}.{sig}"

        with pytest.raises(InvalidTokenError, match="numeric timestamp"):
            issuer.decode_token(token)

    def test_exact_match_algorithm_enforced(self) -> None:
        issuer = self._issuer()
        with pytest.raises(InvalidAlgorithmError, match="Unsupported algorithm"):
            JWTIssuer(secret="a" * 32, algorithm="hs256")

        header_b64 = (
            base64.urlsafe_b64encode(b'{"alg":"hs256","typ":"access"}')
            .rstrip(b"=")
            .decode("ascii")
        )
        payload_b64 = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "sub": "u1",
                        "type": "access",
                        "iat": int(time.time()),
                        "exp": int(time.time()) + 300,
                        "jti": "j1",
                    }
                ).encode("utf-8")
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        sig = (
            base64.urlsafe_b64encode(
                issuer._sign(f"{header_b64}.{payload_b64}".encode("ascii"), "HS256")
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        token = f"{header_b64}.{payload_b64}.{sig}"
        with pytest.raises(InvalidAlgorithmError, match="not allowed"):
            issuer.decode_token(token)

    def test_reserved_claims_in_claims_dict_raises(self) -> None:
        issuer = self._issuer()
        for reserved in ("sub", "exp", "iat", "jti", "type", "typ", "nbf", "iss", "aud"):
            with pytest.raises(ValueError, match="Cannot override reserved JWT claim"):
                issuer.issue_access_token(subject="u1", claims={reserved: "override"})

    def test_expires_in_zero_treated_explicitly(self) -> None:
        issuer = self._issuer()
        token = issuer.issue_access_token(subject="u1", expires_in=0)
        payload = issuer.decode_token(token, verify_exp=False)
        assert payload["exp"] == payload["iat"]

        pair = issuer.issue_token_pair(
            subject="u1", access_expires_in=0, refresh_expires_in=0
        )
        assert pair["expires_in"] == 0
        acc_payload = issuer.decode_token(pair["access_token"], verify_exp=False)
        ref_payload = issuer.decode_token(pair["refresh_token"], verify_exp=False)
        assert acc_payload["exp"] == acc_payload["iat"]
        assert ref_payload["exp"] == ref_payload["iat"]


