from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import timedelta
from typing import Any, ClassVar

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    RSAPublicKey,
)


class JWTError(Exception):
    """Base exception for all JWT-related errors."""


class InvalidTokenError(JWTError):
    """Raised when a token is structurally malformed or invalid."""


class TokenExpiredError(JWTError):
    """Raised when a token's exp claim is in the past."""


class InvalidSignatureError(InvalidTokenError):
    """Raised when a token's signature cannot be verified."""


class InvalidAlgorithmError(InvalidTokenError):
    """Raised when an algorithm is disallowed, missing, or mismatched."""


def _base64url_encode(data: bytes) -> str:
    """Encode bytes into base64url string without trailing padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _base64url_decode(data: str) -> bytes:
    """Decode a base64url string, adding required padding if necessary."""
    rem = len(data) % 4
    if rem > 0:
        data += "=" * (4 - rem)
    try:
        return base64.urlsafe_b64decode(data.encode("ascii"))
    except Exception as exc:
        raise InvalidTokenError(f"Base64 decoding failed: {exc}") from exc


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
        audience: str | None = None,
    ) -> None:
        self.algorithm = algorithm.upper()
        if self.algorithm not in self.SUPPORTED_ALGORITHMS:
            raise InvalidAlgorithmError(
                f"Unsupported algorithm '{algorithm}'. Must be one of {self.SUPPORTED_ALGORITHMS}."
            )

        self.access_token_ttl = access_token_ttl
        self.refresh_token_ttl = refresh_token_ttl
        self.issuer = issuer
        self.audience = audience

        # Symmetric key setup
        self._secret: bytes | None = None
        if secret is not None:
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
        alg = (algorithm or self.algorithm).upper()
        if alg not in self.SUPPORTED_ALGORITHMS:
            raise InvalidAlgorithmError(f"Unsupported algorithm '{alg}'.")

        now = int(time.time())
        ttl_seconds: int
        if expires_in is not None:
            if isinstance(expires_in, timedelta):
                ttl_seconds = int(expires_in.total_seconds())
            else:
                ttl_seconds = int(expires_in)
        else:
            ttl_seconds = (
                self.access_token_ttl
                if token_type == "access"
                else self.refresh_token_ttl
            )

        payload: dict[str, Any] = {
            "sub": str(subject),
            "type": token_type,
            "token_type": token_type,
            "iat": now,
            "exp": now + ttl_seconds,
            "jti": str(uuid.uuid4()),
        }

        if not_before is not None:
            if isinstance(not_before, timedelta):
                payload["nbf"] = now + int(not_before.total_seconds())
            else:
                payload["nbf"] = int(not_before)

        if self.issuer:
            payload["iss"] = self.issuer
        if self.audience:
            payload["aud"] = self.audience

        if claims:
            # Prevent overwriting core security metadata
            for key, val in claims.items():
                if key not in self.RESERVED_CLAIMS:
                    payload[key] = val

        header = {
            "alg": alg,
            "typ": "JWT",
        }

        header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")

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
            expires_in=expires_in or self.access_token_ttl,
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
            expires_in=expires_in or self.refresh_token_ttl,
        )

    def issue_token_pair(
        self,
        subject: str | int,
        claims: dict[str, Any] | None = None,
        access_expires_in: int | timedelta | None = None,
        refresh_expires_in: int | timedelta | None = None,
    ) -> dict[str, Any]:
        """
        Issue both an access token and a refresh token.
        """
        access_token = self.issue_access_token(
            subject=subject,
            claims=claims,
            expires_in=access_expires_in,
        )
        refresh_token = self.issue_refresh_token(
            subject=subject,
            claims=claims,
            expires_in=refresh_expires_in,
        )
        ttl = access_expires_in or self.access_token_ttl
        ttl_seconds = int(ttl.total_seconds() if isinstance(ttl, timedelta) else ttl)

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": ttl_seconds,
        }

    def decode_token(
        self,
        token: str,
        *,
        verify_exp: bool = True,
        expected_type: str | None = None,
        allowed_algorithms: list[str] | set[str] | None = None,
    ) -> dict[str, Any]:
        """
        Decode and cryptographically verify a JWT.
        """
        if not isinstance(token, str) or not token:
            raise InvalidTokenError("Token must be a non-empty string")

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

        alg_upper = alg.upper()

        # Strict security protection: reject "none" algorithm attacks
        if alg_upper == "NONE":
            raise InvalidAlgorithmError("The 'none' algorithm is strictly disallowed.")

        allowed = (
            {a.upper() for a in allowed_algorithms}
            if allowed_algorithms is not None
            else {self.algorithm}
        )

        if alg_upper not in allowed:
            raise InvalidAlgorithmError(
                f"Algorithm '{alg}' is not allowed for this issuer. Allowed: {allowed}"
            )

        if alg_upper not in self.SUPPORTED_ALGORITHMS:
            raise InvalidAlgorithmError(
                f"Algorithm '{alg}' is not supported. Supported: {self.SUPPORTED_ALGORITHMS}"
            )

        # Verify signature
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        signature_bytes = _base64url_decode(signature_b64)

        self._verify_signature(signing_input, signature_bytes, alg_upper)

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

        # Issued At (iat) check
        iat = payload.get("iat")
        if iat is not None and not isinstance(iat, (int, float)):
            raise InvalidTokenError("Claim 'iat' must be a numeric timestamp")

        # Expiration check
        if verify_exp:
            exp = payload.get("exp")
            if exp is None:
                raise InvalidTokenError("Missing 'exp' claim in token")
            if not isinstance(exp, (int, float)):
                raise InvalidTokenError("Claim 'exp' must be a numeric timestamp")
            if now >= exp:
                raise TokenExpiredError("Token has expired")

        # Not Before (nbf) check
        nbf = payload.get("nbf")
        if nbf is not None:
            if not isinstance(nbf, (int, float)):
                raise InvalidTokenError("Claim 'nbf' must be a numeric timestamp")
            if now < nbf:
                raise InvalidTokenError("Token is not active yet (nbf constraint)")

        # Issuer check
        if self.issuer is not None:
            token_iss = payload.get("iss")
            if token_iss != self.issuer:
                raise InvalidTokenError(
                    f"Invalid issuer: expected '{self.issuer}', got '{token_iss}'"
                )

        # Audience check
        if self.audience is not None:
            token_aud = payload.get("aud")
            if token_aud != self.audience:
                raise InvalidTokenError(
                    f"Invalid audience: expected '{self.audience}', got '{token_aud}'"
                )

        # Token type check
        if expected_type is not None:
            token_type = payload.get("type") or payload.get("token_type")
            if token_type != expected_type:
                raise InvalidTokenError(
                    f"Invalid token type: expected '{expected_type}', got '{token_type}'"
                )

        return payload

    @classmethod
    def decode_unverified(cls, token: str) -> dict[str, Any]:
        """
        Extract token payload without verifying signature (useful for reading jti/exp before revocation).
        """
        parts = token.strip().split(".")
        if len(parts) != 3:
            raise InvalidTokenError("Malformed token structure")
        try:
            payload_json = _base64url_decode(parts[1]).decode("utf-8")
            payload = json.loads(payload_json)
            if not isinstance(payload, dict):
                raise InvalidTokenError("Token payload must be a dictionary")
            return payload
        except Exception as exc:
            raise InvalidTokenError(f"Failed to read payload: {exc}") from exc

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
