from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import secrets
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any, ClassVar, Protocol, TypeGuard

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    RSAPublicKey,
)

from swiftpy.auth.exceptions import (
    AuthenticationServiceUnavailableError,
    BaseTokenBlacklist,
    InvalidAlgorithmError,
    InvalidSignatureError,
    InvalidTokenError,
    JWTError,
    TokenExpiredError,
    TokenRevokedError,
    is_token_blacklist_enabled,
)
from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import Container, get_container

logger = logging.getLogger("swiftpy.auth.jwt")


class RefreshTokenBlacklist(Protocol):
    """Structural protocol for token blacklist implementations used during refresh."""

    async def is_revoked(
        self,
        jti: str,
        subject: str | int | None = None,
        issued_at: int | None = None,
    ) -> bool: ...

    async def revoke(self, jti: str, ttl: int | None = None) -> None: ...


def _base64url_encode(data: bytes) -> str:
    """Encode bytes into base64url string without trailing padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _base64url_decode(data: str) -> bytes:
    """Decode a canonical base64url string, rejecting non-canonical padding bits."""
    unpadded = data.rstrip("=")
    rem = len(unpadded) % 4
    if rem == 1:
        raise InvalidTokenError("Invalid base64url length")
    padded = unpadded + ("=" * (4 - rem) if rem > 0 else "")
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:
        raise InvalidTokenError(f"Base64 decoding failed: {exc}") from exc
    if _base64url_encode(decoded) != unpadded:
        raise InvalidSignatureError("Non-canonical base64url encoding")
    return decoded


def _is_valid_timestamp(value: Any) -> TypeGuard[int | float]:
    """Return True if value is a finite int or float (excluding bool)."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


class JWTIssuer:
    """
    High-performance, secure JWT engine supporting RS256 and HS256 algorithms
    with built-in access/refresh token pair lifecycle and claim validation.
    """

    SUPPORTED_ALGORITHMS: ClassVar[set[str]] = {"HS256", "RS256"}
    RESERVED_CLAIMS: ClassVar[set[str]] = {
        "iat",
        "exp",
        "jti",
        "type",
        "token_type",
        "typ",
        "sub",
        "iss",
        "aud",
        "nbf",
    }

    def __init__(
        self,
        secret: str | bytes | None = None,
        private_key: str | bytes | RSAPrivateKey | None = None,
        public_key: str | bytes | RSAPublicKey | None = None,
        algorithm: str = "HS256",
        access_token_ttl: int = 900,  # 15 minutes
        refresh_token_ttl: int = 604800,  # 7 days
        issuer: str | None = None,
        audience: str | list[str] | set[str] | tuple[str, ...] | None = None,
        leeway: int = 0,
        blacklist: RefreshTokenBlacklist | None = None,
        blacklist_resolver: Callable[[], RefreshTokenBlacklist | None] | None = None,
    ) -> None:
        if not isinstance(algorithm, str) or algorithm not in self.SUPPORTED_ALGORITHMS:
            raise InvalidAlgorithmError(
                f"Unsupported algorithm '{algorithm}'. Must be one of {self.SUPPORTED_ALGORITHMS}."
            )
        self.algorithm = algorithm

        if isinstance(leeway, bool) or not isinstance(leeway, (int, float)) or not math.isfinite(leeway) or leeway < 0:
            raise ConfigurationError("JWT leeway must be a non-negative number.")

        self.access_token_ttl = access_token_ttl
        self.refresh_token_ttl = refresh_token_ttl
        self.issuer = issuer
        self.audience = audience
        self.leeway = int(leeway)
        self._blacklist = blacklist
        self._blacklist_resolver = blacklist_resolver

        # Symmetric key setup
        self._secret: bytes | None = None
        if self.algorithm == "HS256":
            if secret is None:
                raise ConfigurationError(
                    "JWT secret must be provided for symmetric algorithm HS256."
                )
            raw_secret = secret.encode("utf-8") if isinstance(secret, str) else secret
            if len(raw_secret) < 32:
                raise ConfigurationError(
                    f"JWT secret must be at least 32 bytes (got {len(raw_secret)} bytes) for HS256."
                )
            self._secret = raw_secret
        elif secret is not None:
            self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret

        # Asymmetric key setup
        self._private_key: RSAPrivateKey | None = None
        self._public_key: RSAPublicKey | None = None

        if private_key is not None:
            if isinstance(private_key, RSAPrivateKey):
                self._private_key = private_key
            else:
                raw_bytes = (
                    private_key.encode("utf-8")
                    if isinstance(private_key, str)
                    else private_key
                )
                loaded_key = serialization.load_pem_private_key(
                    raw_bytes,
                    password=None,
                )
                if not isinstance(loaded_key, RSAPrivateKey):
                    raise TypeError("Loaded private key is not an RSAPrivateKey")
                self._private_key = loaded_key

            if public_key is None and self._private_key is not None:
                self._public_key = self._private_key.public_key()

        if public_key is not None:
            if isinstance(public_key, RSAPublicKey):
                self._public_key = public_key
            else:
                raw_pub_bytes = (
                    public_key.encode("utf-8")
                    if isinstance(public_key, str)
                    else public_key
                )
                loaded_pub = serialization.load_pem_public_key(raw_pub_bytes)
                if not isinstance(loaded_pub, RSAPublicKey):
                    raise TypeError("Loaded public key is not an RSAPublicKey")
                self._public_key = loaded_pub

        if self.algorithm == "RS256" and self._private_key is None and self._public_key is None:
            raise ConfigurationError(
                "RS256 algorithm requires a private_key (for signing) or public_key (for verification)."
            )

    @classmethod
    def from_config(
        cls,
        config: Config | None = None,
        *,
        blacklist: RefreshTokenBlacklist | None = None,
        blacklist_resolver: Callable[[], RefreshTokenBlacklist | None] | None = None,
    ) -> JWTIssuer:
        """
        Build and configure a JWTIssuer from application Config.

        Reads settings from 'auth.guards.jwt.*' or 'auth.jwt.*'.
        In production (debug=False), fails fast if JWT_SECRET is missing or <32 bytes.
        In debug mode (debug=True), if JWT_SECRET is absent, generates a fresh ephemeral
        random secret per call (cached per application via the container singleton)
        and logs a loud warning.
        """
        if config is None:
            try:
                cfg = Config.get_instance()
            except (RuntimeError, ValueError):
                cfg = Config()
        else:
            cfg = config

        # Look up jwt guard config dict
        jwt_cfg: dict[str, Any] = {}
        guards = cfg.get("auth.guards")
        if isinstance(guards, dict) and isinstance(guards.get("jwt"), dict):
            jwt_cfg = guards["jwt"]
        elif isinstance(cfg.get("auth.jwt"), dict):
            jwt_cfg = cfg.get("auth.jwt")

        algorithm = str(jwt_cfg.get("algorithm") or cfg.get("auth.jwt.algorithm") or "HS256")
        raw_secret = jwt_cfg.get("secret") if "secret" in jwt_cfg else cfg.get("auth.jwt.secret")
        if raw_secret is None:
            raw_secret = cfg.get("jwt_secret")

        app_env = str(cfg.get("app.env") or cfg.get("env") or "development").lower()
        debug_val = cfg.get("app.debug") if cfg.get("app.debug") is not None else cfg.get("debug")
        if debug_val is not None:
            is_debug = bool(debug_val)
        else:
            is_debug = app_env not in ("production", "prod")

        secret: str | bytes | None = None
        if algorithm == "HS256":
            if raw_secret is not None and str(raw_secret).strip():
                secret = str(raw_secret)
            elif is_debug:
                secret = secrets.token_urlsafe(48)
                logger.warning(
                    "SECURITY WARNING: No JWT_SECRET configured. Generated a random ephemeral "
                    "secret for debug mode. All tokens will be invalidated on server restart. "
                    "Set JWT_SECRET in your environment for persistence."
                )
            else:
                raise ConfigurationError(
                    "JWT_SECRET is not configured. A secret of at least 32 bytes is required in production."
                )

        access_ttl = int(jwt_cfg.get("access_token_ttl") or cfg.get("auth.jwt.access_token_ttl") or 900)
        refresh_ttl = int(jwt_cfg.get("refresh_token_ttl") or cfg.get("auth.jwt.refresh_token_ttl") or 604800)
        raw_leeway = jwt_cfg.get("leeway") if "leeway" in jwt_cfg else cfg.get("auth.jwt.leeway")
        leeway = int(raw_leeway) if raw_leeway is not None else 0
        issuer = jwt_cfg.get("issuer") or cfg.get("auth.jwt.issuer")
        raw_audience = jwt_cfg.get("audience") or cfg.get("auth.jwt.audience")
        audience: str | list[str] | None
        if isinstance(raw_audience, (list, tuple, set)):
            audience = [str(a) for a in raw_audience]
        elif raw_audience:
            audience = str(raw_audience)
        else:
            audience = None

        private_key = jwt_cfg.get("private_key") or cfg.get("auth.jwt.private_key")
        public_key = jwt_cfg.get("public_key") or cfg.get("auth.jwt.public_key")

        return cls(
            secret=secret,
            private_key=private_key,
            public_key=public_key,
            algorithm=algorithm,
            access_token_ttl=access_ttl,
            refresh_token_ttl=refresh_ttl,
            issuer=str(issuer) if issuer else None,
            audience=audience,
            leeway=leeway,
            blacklist=blacklist,
            blacklist_resolver=blacklist_resolver,
        )

    @classmethod
    def generate_rsa_key_pair(cls, key_size: int = 2048) -> tuple[str, str]:
        """
        Generate a fresh RSA private and public key pair in PEM format.
        """
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=key_size,
        )
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")

        public_pem = (
            private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )

        return private_pem, public_pem

    def issue_token(
        self,
        subject: str | int,
        token_type: str = "access",
        claims: dict[str, Any] | None = None,
        expires_in: int | timedelta | None = None,
        algorithm: str | None = None,
        not_before: int | timedelta | None = None,
    ) -> str:
        """
        Issue a signed JSON Web Token with standard and custom claims.
        """
        alg = self.algorithm if algorithm is None else algorithm
        if not isinstance(alg, str) or alg not in self.SUPPORTED_ALGORITHMS:
            raise InvalidAlgorithmError(f"Unsupported algorithm '{alg}'.")

        if claims:
            conflicting = set(claims.keys()) & self.RESERVED_CLAIMS
            if conflicting:
                raise ValueError(
                    f"Cannot override reserved JWT claim(s): {', '.join(sorted(conflicting))}"
                )

        now = int(time.time())
        ttl_seconds: int
        if expires_in is not None:
            if isinstance(expires_in, timedelta):
                total_sec = expires_in.total_seconds()
                if not math.isfinite(total_sec):
                    raise ValueError("expires_in must be a finite duration")
                ttl_seconds = int(total_sec)
            elif _is_valid_timestamp(expires_in):
                ttl_seconds = int(expires_in)
            else:
                raise ValueError("expires_in must be a finite integer or timedelta")
        else:
            ttl_seconds = (
                self.refresh_token_ttl
                if token_type == "refresh"
                else self.access_token_ttl
            )

        payload: dict[str, Any] = {
            "sub": str(subject),
            "type": token_type,
            "token_type": token_type,
            "typ": token_type,
            "iat": now,
            "exp": now + ttl_seconds,
            "jti": str(uuid.uuid4()),
        }

        if not_before is not None:
            if isinstance(not_before, timedelta):
                payload["nbf"] = now + int(not_before.total_seconds())
            elif _is_valid_timestamp(not_before):
                payload["nbf"] = int(not_before)
            else:
                raise ValueError("not_before must be a finite integer or timedelta")

        if self.issuer:
            payload["iss"] = self.issuer
        if self.audience:
            if isinstance(self.audience, (set, tuple)):
                payload["aud"] = list(self.audience)
            else:
                payload["aud"] = self.audience

        if claims:
            for key, val in claims.items():
                payload[key] = val

        header = {
            "alg": alg,
            "typ": token_type if token_type in {"access", "refresh"} else "JWT",
        }

        header_bytes = json.dumps(
            header, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        payload_bytes = json.dumps(
            payload, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")

        header_b64 = _base64url_encode(header_bytes)
        payload_b64 = _base64url_encode(payload_bytes)

        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        signature_bytes = self._sign(signing_input, alg)
        signature_b64 = _base64url_encode(signature_bytes)

        return f"{header_b64}.{payload_b64}.{signature_b64}"

    def issue_access_token(
        self,
        subject: str | int,
        claims: dict[str, Any] | None = None,
        expires_in: int | timedelta | None = None,
    ) -> str:
        """Issue a short-lived access token."""
        return self.issue_token(
            subject=subject,
            token_type="access",
            claims=claims,
            expires_in=self.access_token_ttl if expires_in is None else expires_in,
        )

    def issue_refresh_token(
        self,
        subject: str | int,
        claims: dict[str, Any] | None = None,
        expires_in: int | timedelta | None = None,
    ) -> str:
        """Issue a long-lived refresh token."""
        return self.issue_token(
            subject=subject,
            token_type="refresh",
            claims=claims,
            expires_in=self.refresh_token_ttl if expires_in is None else expires_in,
        )

    def issue_token_pair(
        self,
        subject: str | int,
        claims: dict[str, Any] | None = None,
        access_expires_in: int | timedelta | None = None,
        refresh_expires_in: int | timedelta | None = None,
        refresh_claims: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Issue both an access token and a refresh token.
        Access token carries caller claims; refresh token does not inherit
        access claims unless explicitly provided in refresh_claims.
        """
        access_token = self.issue_access_token(
            subject=subject,
            claims=claims,
            expires_in=access_expires_in,
        )
        refresh_token = self.issue_refresh_token(
            subject=subject,
            claims=refresh_claims,
            expires_in=refresh_expires_in,
        )
        ttl = self.access_token_ttl if access_expires_in is None else access_expires_in
        ttl_seconds = int(ttl.total_seconds() if isinstance(ttl, timedelta) else ttl)

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": ttl_seconds,
        }

    def _resolve_refresh_blacklist(
        self,
        explicit: RefreshTokenBlacklist | None,
    ) -> RefreshTokenBlacklist | None:
        """Resolve an active token blacklist for refresh rotation and reuse checks."""
        if explicit is not None:
            return explicit

        container: Container | None
        try:
            container = get_container()
        except RuntimeError:
            container = None

        if container is not None:
            cfg = container.resolve(Config) if container.has(Config) else None
            if not is_token_blacklist_enabled(cfg):
                return None

        if self._blacklist is not None:
            return self._blacklist

        if self._blacklist_resolver is not None:
            resolved = self._blacklist_resolver()
            if resolved is not None:
                return resolved

        if container is not None:
            if container.has(BaseTokenBlacklist):
                return container.resolve(BaseTokenBlacklist)
            for binding_cls in tuple(container._bindings):
                if isinstance(binding_cls, type) and issubclass(
                    binding_cls, BaseTokenBlacklist
                ):
                    return container.resolve(binding_cls)

        return None

    async def refresh_token_pair(
        self,
        refresh_token: str,
        *,
        blacklist: RefreshTokenBlacklist | None = None,
        claims: dict[str, Any] | None = None,
        refresh_claims: dict[str, Any] | None = None,
        access_expires_in: int | timedelta | None = None,
        refresh_expires_in: int | timedelta | None = None,
        rotate: bool = True,
    ) -> dict[str, Any]:
        """
        Verify a refresh token, reject blacklisted/reused tokens, optionally rotate
        (revoke old refresh `jti`), and issue a fresh access/refresh token pair.
        """
        payload = self.decode_token(
            refresh_token,
            verify_exp=True,
            expected_type="refresh",
        )
        subject = str(payload["sub"])
        jti = payload.get("jti")
        raw_iat = payload.get("iat")
        iat = (
            int(raw_iat)
            if isinstance(raw_iat, (int, float)) and not isinstance(raw_iat, bool)
            else None
        )

        active_blacklist = self._resolve_refresh_blacklist(blacklist)
        if active_blacklist is not None:
            if not isinstance(jti, str) or not jti:
                raise InvalidTokenError("Refresh token is missing required 'jti' claim")

            try:
                is_rev = await active_blacklist.is_revoked(
                    jti,
                    subject=subject,
                    issued_at=iat,
                )
            except Exception as exc:
                logger.exception("Refresh token revocation check failed")
                raise AuthenticationServiceUnavailableError(
                    "Authentication service unavailable"
                ) from exc

            if is_rev:
                logger.warning(
                    "Rejected revoked or reused refresh token (jti=%s, sub=%s)",
                    jti,
                    subject,
                )
                raise TokenRevokedError("Refresh token has been revoked")

            if rotate:
                raw_exp = payload.get("exp")
                remaining = (
                    max(1, int(raw_exp - time.time()))
                    if _is_valid_timestamp(raw_exp)
                    else max(1, self.refresh_token_ttl)
                )
                claimed = True
                try:
                    atomic_fn = getattr(
                        active_blacklist, "revoke_if_not_revoked", None
                    )
                    if callable(atomic_fn):
                        claimed = bool(await atomic_fn(jti, ttl=remaining))
                    else:
                        await active_blacklist.revoke(jti, ttl=remaining)
                except Exception as exc:
                    logger.exception(
                        "Failed to revoke rotated refresh token (jti=%s, sub=%s)",
                        jti,
                        subject,
                    )
                    raise AuthenticationServiceUnavailableError(
                        "Authentication service unavailable"
                    ) from exc

                if not claimed:
                    logger.warning(
                        "Rejected concurrently reused refresh token (jti=%s, sub=%s)",
                        jti,
                        subject,
                    )
                    raise TokenRevokedError("Refresh token has been revoked")

        inherited_refresh_claims = {
            key: value
            for key, value in payload.items()
            if key not in self.RESERVED_CLAIMS
        }
        resolved_refresh_claims = (
            refresh_claims
            if refresh_claims is not None
            else (inherited_refresh_claims or None)
        )

        return self.issue_token_pair(
            subject=subject,
            claims=claims,
            access_expires_in=access_expires_in,
            refresh_expires_in=refresh_expires_in,
            refresh_claims=resolved_refresh_claims,
        )

    def decode_token(
        self,
        token: str,
        *,
        verify_exp: bool = True,
        verify_nbf: bool = True,
        expected_type: str | None = None,
        allowed_algorithms: list[str] | set[str] | None = None,
        leeway: int | None = None,
    ) -> dict[str, Any]:
        """
        Decode and cryptographically verify a JWT.
        """
        if not isinstance(token, str) or not token:
            raise InvalidTokenError("Token must be a non-empty string")

        active_leeway = self.leeway if leeway is None else leeway
        if not _is_valid_timestamp(active_leeway) or active_leeway < 0:
            raise ValueError("leeway must be a non-negative finite number")

        parts = token.strip().split(".")
        if len(parts) != 3:
            raise InvalidTokenError(
                f"Invalid token format: expected 3 segments separated by dots, got {len(parts)}"
            )

        header_b64, payload_b64, signature_b64 = parts

        try:
            header_json = _base64url_decode(header_b64).decode("utf-8")
            header = json.loads(header_json)
        except Exception as exc:
            raise InvalidTokenError(f"Malformed token header: {exc}") from exc

        if not isinstance(header, dict):
            raise InvalidTokenError("Token header must be a JSON object")

        alg = header.get("alg")
        if not alg or not isinstance(alg, str):
            raise InvalidAlgorithmError("Missing or invalid 'alg' in token header")

        # Strict security protection: reject "none" algorithm attacks across all case variations
        if alg.upper() == "NONE":
            raise InvalidAlgorithmError("The 'none' algorithm is strictly disallowed.")

        # Exact-case matching for allowed and supported algorithms (P2-A5)
        allowed = (
            set(allowed_algorithms)
            if allowed_algorithms is not None
            else {self.algorithm}
        )

        if alg not in allowed:
            raise InvalidAlgorithmError(
                f"Algorithm '{alg}' is not allowed for this issuer. Allowed: {allowed}"
            )

        if alg not in self.SUPPORTED_ALGORITHMS:
            raise InvalidAlgorithmError(
                f"Algorithm '{alg}' is not supported. Supported: {self.SUPPORTED_ALGORITHMS}"
            )

        # Verify signature
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        signature_bytes = _base64url_decode(signature_b64)

        self._verify_signature(signing_input, signature_bytes, alg)

        # Parse payload
        try:
            payload_json = _base64url_decode(payload_b64).decode("utf-8")
            payload = json.loads(payload_json)
        except Exception as exc:
            raise InvalidTokenError(f"Malformed token payload: {exc}") from exc

        if not isinstance(payload, dict):
            raise InvalidTokenError("Token payload must be a JSON object")

        now = int(time.time())

        # Subject check
        if "sub" not in payload or not payload["sub"]:
            raise InvalidTokenError("Missing or empty 'sub' claim in token")

        # Issued At (iat) check (including future check with leeway)
        iat = payload.get("iat")
        if iat is not None:
            if not _is_valid_timestamp(iat):
                raise InvalidTokenError("Claim 'iat' must be a numeric timestamp")
            if iat > now + active_leeway:
                raise InvalidTokenError("Token 'iat' claim is in the future")

        # Expiration check (always validate finiteness if present; check expiry when verify_exp=True)
        exp = payload.get("exp")
        if exp is not None and not _is_valid_timestamp(exp):
            raise InvalidTokenError("Claim 'exp' must be a numeric timestamp")
        if verify_exp:
            if exp is None:
                raise InvalidTokenError("Missing 'exp' claim in token")
            if now - active_leeway >= exp:
                raise TokenExpiredError("Token has expired")

        # Not Before (nbf) check with leeway
        nbf = payload.get("nbf")
        if nbf is not None:
            if not _is_valid_timestamp(nbf):
                raise InvalidTokenError("Claim 'nbf' must be a numeric timestamp")
            if verify_nbf and now + active_leeway < nbf:
                raise InvalidTokenError("Token is not active yet (nbf constraint)")

        # Issuer check
        if self.issuer is not None:
            token_iss = payload.get("iss")
            if token_iss != self.issuer:
                raise InvalidTokenError(
                    f"Invalid issuer: expected '{self.issuer}', got '{token_iss}'"
                )

        # Audience check (supports str or list[str] on both token and issuer, P2-A5)
        if self.audience is not None:
            token_aud = payload.get("aud")
            if isinstance(token_aud, str) and token_aud:
                token_aud_set = {token_aud}
            elif (
                isinstance(token_aud, list)
                and token_aud
                and all(isinstance(item, str) and bool(item) for item in token_aud)
            ):
                token_aud_set = set(token_aud)
            else:
                raise InvalidTokenError(
                    f"Invalid audience: expected '{self.audience}', got '{token_aud}'"
                )

            expected_aud_set = (
                {self.audience}
                if isinstance(self.audience, str)
                else set(self.audience)
            )
            if expected_aud_set.isdisjoint(token_aud_set):
                raise InvalidTokenError(
                    f"Invalid audience: expected '{self.audience}', got '{token_aud}'"
                )

        # Token type check
        if expected_type is not None:
            token_type = (
                payload.get("type")
                or payload.get("token_type")
                or payload.get("typ")
            )
            if token_type != expected_type:
                raise InvalidTokenError(
                    f"Invalid token type: expected '{expected_type}', got '{token_type}'"
                )
            header_typ = str(header.get("typ") or "").lower()
            if header_typ and header_typ not in {"jwt", expected_type.lower()}:
                raise InvalidTokenError(
                    f"Invalid token type in header: expected '{expected_type}', got '{header.get('typ')}'"
                )

        return payload

    def _sign(self, data: bytes, algorithm: str) -> bytes:
        if algorithm == "HS256":
            if self._secret is None:
                raise ValueError("Secret key must be provided for HS256 signing")
            return hmac.new(self._secret, data, hashlib.sha256).digest()

        if algorithm == "RS256":
            if self._private_key is None:
                raise ValueError("RSAPrivateKey must be provided for RS256 signing")
            return self._private_key.sign(
                data,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )

        raise InvalidAlgorithmError(f"Unsupported algorithm '{algorithm}'")

    def _verify_signature(
        self,
        signing_input: bytes,
        signature: bytes,
        algorithm: str,
    ) -> None:
        if algorithm == "HS256":
            if self._secret is None:
                raise ValueError("Secret key must be provided for HS256 verification")
            expected_signature = hmac.new(
                self._secret, signing_input, hashlib.sha256
            ).digest()
            if not hmac.compare_digest(expected_signature, signature):
                raise InvalidSignatureError("Signature verification failed")
            return

        if algorithm == "RS256":
            if self._public_key is None:
                raise ValueError("RSAPublicKey must be provided for RS256 verification")
            try:
                self._public_key.verify(
                    signature,
                    signing_input,
                    padding.PKCS1v15(),
                    hashes.SHA256(),
                )
            except InvalidSignature as exc:
                raise InvalidSignatureError("Signature verification failed") from exc
            return

        raise InvalidAlgorithmError(f"Unsupported algorithm '{algorithm}'")


__all__ = [
    "InvalidAlgorithmError",
    "InvalidSignatureError",
    "InvalidTokenError",
    "JWTError",
    "JWTIssuer",
    "RefreshTokenBlacklist",
    "TokenExpiredError",
]
