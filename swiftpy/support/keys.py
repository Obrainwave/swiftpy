"""
Application cryptographic key decoding and entropy validation helpers.

Provides unified parsing of ``base64:`` prefixed keys and raw secrets across
subsystems (Queue, Events, Filesystem, Application startup) so every consumer
derives identical key material and enforces minimum entropy.
"""

from __future__ import annotations

import base64
import binascii

from swiftpy.core.config import Config, ConfigurationError, env

MIN_SIGNING_KEY_BYTES = 32


def decode_signing_key(raw_key: str) -> bytes:
    """Decode a raw or ``base64:`` prefixed signing key into bytes."""
    if not isinstance(raw_key, str) or not raw_key.strip():
        raise ConfigurationError("Signing key must be a non-empty string.")

    if raw_key.startswith("base64:"):
        encoded = raw_key[len("base64:") :].strip()
        if not encoded:
            raise ConfigurationError("Malformed base64 signing key: empty payload.")
        try:
            return base64.b64decode(encoded.encode("ascii"), validate=True)
        except (ValueError, binascii.Error, UnicodeEncodeError) as exc:
            raise ConfigurationError(
                f"Malformed base64 application signing key: {exc}"
            ) from exc

    return raw_key.encode("utf-8")


def validate_signing_key(
    raw_key: str | None,
    *,
    min_bytes: int = MIN_SIGNING_KEY_BYTES,
    purpose: str = "APP_KEY",
) -> bytes:
    """Decode and validate that a signing key meets minimum byte entropy."""
    if raw_key is None or not isinstance(raw_key, str) or not raw_key.strip():
        raise ConfigurationError(
            f"{purpose} is required and must be configured (at least {min_bytes} bytes)."
        )

    decoded = decode_signing_key(raw_key)
    if len(decoded) < min_bytes:
        raise ConfigurationError(
            f"{purpose} must decode to at least {min_bytes} bytes (got {len(decoded)} bytes)."
        )
    return decoded


def resolve_configured_app_key(config: Config | None = None) -> str | None:
    """Resolve ``app.key`` / ``key`` from Config (when declared) or ``env('APP_KEY')``."""
    cfg = config
    if cfg is None:
        try:
            cfg = Config.get_instance()
        except (ConfigurationError, RuntimeError):
            cfg = None

    if cfg is not None and (cfg.has("app.key") or cfg.has("key")):
        raw = cfg.get("app.key") if cfg.has("app.key") else cfg.get("key")
        if isinstance(raw, str) and raw.strip():
            return raw
        return None

    env_key = env("APP_KEY")
    if env_key is not None and str(env_key).strip():
        return str(env_key)
    return None


def resolve_configured_previous_keys(config: Config | None = None) -> list[str]:
    """Resolve retired keys for signature rotation from Config or environment."""
    cfg = config
    if cfg is None:
        try:
            cfg = Config.get_instance()
        except (ConfigurationError, RuntimeError):
            cfg = None

    if cfg is not None and cfg.has("app.previous_keys"):
        val = cfg.get("app.previous_keys")
        if isinstance(val, (list, tuple)):
            return [str(k) for k in val if str(k).strip()]
        if isinstance(val, str) and val.strip():
            return [k.strip() for k in val.split(",") if k.strip()]
        return []

    env_prev = env("APP_PREVIOUS_KEYS")
    if env_prev:
        return [k.strip() for k in str(env_prev).split(",") if k.strip()]
    return []


__all__ = [
    "MIN_SIGNING_KEY_BYTES",
    "decode_signing_key",
    "resolve_configured_app_key",
    "resolve_configured_previous_keys",
    "validate_signing_key",
]
