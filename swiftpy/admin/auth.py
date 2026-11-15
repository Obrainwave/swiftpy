from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Callable
from typing import Any

from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import get_container
from swiftpy.core.context import Context
from swiftpy.http.request import Request
from swiftpy.http.response import Response

logger = logging.getLogger("swiftpy.admin.auth")

ADMIN_COOKIE_NAME = "swiftpy_admin_session"
DEFAULT_SESSION_MAX_AGE = 86400  # 24 hours
DEFAULT_MAX_REVOKED_SESSIONS = 10_000
_CONTEXT_ADMIN_KEY = "__swiftpy_admin_user__"
_LOCAL_DEV_ENVS = frozenset({"local", "development", "dev", "testing", "test"})


def _resolve_admin_config(explicit: Config | None = None) -> Config | None:
    """Resolve the active application Config, falling back cleanly to global Config."""
    if explicit is not None:
        return explicit
    try:
        container = get_container()
        if container.has(Config):
            return container.resolve(Config)
    except RuntimeError:
        pass
    try:
        return Config.get_instance()
    except (RuntimeError, ValueError):
        return None


def _is_dev_debug_mode(cfg: Config | None) -> bool:
    """Return True only when config explicitly declares a local/dev/test env in debug mode."""
    if cfg is None:
        return False
    raw_env = cfg.get("app.env") or cfg.get("APP_ENV") or cfg.get("env")
    if raw_env is None:
        return False
    app_env = str(raw_env).strip().lower()
    if app_env not in _LOCAL_DEV_ENVS:
        return False
    raw_debug = cfg.get("app.debug")
    if isinstance(raw_debug, bool):
        return raw_debug
    if isinstance(raw_debug, str):
        return raw_debug.strip().lower() in ("true", "1", "yes", "on")
    return bool(raw_debug) if raw_debug is not None else False


def _normalize_cookie_path(prefix: str) -> str:
    """Normalize an admin mount prefix into a clean cookie Path attribute."""
    stripped = prefix.strip("/")
    return f"/{stripped}" if stripped else "/"


def _should_use_secure_cookie(
    *,
    request: Request | None = None,
    secure: bool | None = None,
    config: Config | None = None,
) -> bool:
    """Determine whether the Secure cookie flag should be set."""
    if secure is not None:
        return bool(secure)
    cfg = _resolve_admin_config(config)
    if cfg is not None:
        explicit = cfg.get("admin.cookie_secure")
        if isinstance(explicit, bool):
            return explicit
        if isinstance(explicit, str):
            return explicit.strip().lower() in ("true", "1", "yes", "on")
    if request is not None:
        scheme = str(getattr(request, "scheme", "") or "").lower()
        fwd_proto = str(request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if scheme == "https" or fwd_proto == "https":
            return True
    if cfg is not None:
        raw_env = str(cfg.get("app.env") or cfg.get("env") or "").strip().lower()
        app_url = str(cfg.get("app.url") or "").strip().lower()
        if raw_env in ("production", "prod") or app_url.startswith("https://"):
            return True
    return False


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(s: str) -> bytes:
    padding = 4 - (len(s) % 4)
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s.encode("ascii"))


def parse_cookie_header(cookie_header: str | None) -> dict[str, str]:
    """Parse HTTP Cookie header string into a key-value dictionary."""
    if not cookie_header:
        return {}
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


class AdminUnauthorizedError(Exception):
    """Raised when an unauthenticated request attempts to access protected admin resources."""

    def __init__(self, message: str = "Admin authentication required.") -> None:
        super().__init__(message)
        self.message = message


class AdminUser:
    """Represents an authenticated admin user session."""

    def __init__(
        self,
        username: str,
        is_admin: bool = True,
        permissions: list[str] | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.username = username
        self.is_admin = is_admin
        self.permissions = ["all"] if permissions is None else list(permissions)
        self.session_id = session_id if session_id is not None else secrets.token_hex(16)
        self.metadata = dict(metadata or {})

    @property
    def is_authenticated(self) -> bool:
        return self.is_admin is True

    def has_permission(self, permission: str | None = None) -> bool:
        """Check whether this authenticated admin user holds the requested permission."""
        if self.is_admin is not True:
            return False
        if "all" in self.permissions or "*" in self.permissions:
            return True
        if permission is None:
            return False
        return permission in self.permissions

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "is_admin": self.is_admin,
            "permissions": self.permissions,
            "session_id": self.session_id,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdminUser:
        raw_perms = data.get("permissions")
        perms = [str(p) for p in raw_perms] if isinstance(raw_perms, list) else []
        raw_meta = data.get("metadata")
        meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
        raw_sid = data.get("session_id")
        sid = raw_sid if isinstance(raw_sid, str) else ""
        return cls(
            username=str(data.get("username", "")),
            is_admin=data.get("is_admin") is True,
            permissions=perms,
            session_id=sid,
            metadata=meta,
        )

    def __repr__(self) -> str:
        return f"<AdminUser username={self.username!r} is_admin={self.is_admin}>"


class AdminSessionStore:
    """
    Session storage and token issuance manager for admin authentication.
    Signs sessions using HMAC-SHA256 to ensure tamper-proof, stateless verification
    with bounded TTL-pruned in-memory revocation tracking.
    """

    def __init__(
        self,
        secret_key: str | None = None,
        config: Config | None = None,
        max_revoked_sessions: int = DEFAULT_MAX_REVOKED_SESSIONS,
    ) -> None:
        self._config = config
        self._secret_key = secret_key
        self._ephemeral_secret: str | None = None
        self._max_revoked_sessions = max(1, max_revoked_sessions)
        self._revoked_sessions: dict[str, float] = {}

    def get_secret_key(self) -> str:
        if self._secret_key and self._secret_key.strip():
            return self._secret_key
        cfg = _resolve_admin_config(self._config)
        key = cfg.get("admin.secret_key") or cfg.get("app.key") if cfg is not None else None
        if key and str(key).strip():
            return str(key)
        if _is_dev_debug_mode(cfg):
            if self._ephemeral_secret is None:
                logger.warning(
                    "Admin session secret ('admin.secret_key' or 'app.key') is not configured; "
                    "generating an ephemeral key for local debug mode."
                )
                self._ephemeral_secret = secrets.token_hex(32)
            return self._ephemeral_secret
        raise ConfigurationError(
            "Admin session secret key ('admin.secret_key' or 'app.key') is required "
            "outside local debug mode."
        )

    def _prune_revoked_sessions(self, now: float | None = None) -> None:
        ts = time.time() if now is None else now
        expired = [sid for sid, exp_ts in self._revoked_sessions.items() if exp_ts <= ts]
        for sid in expired:
            self._revoked_sessions.pop(sid, None)
        while len(self._revoked_sessions) > self._max_revoked_sessions:
            oldest_sid = next(iter(self._revoked_sessions))
            self._revoked_sessions.pop(oldest_sid, None)

    def create_token(self, user: AdminUser, max_age: int = DEFAULT_SESSION_MAX_AGE) -> str:
        """Create a signed, base64-encoded session token."""
        now = int(time.time())
        payload = {
            "user": user.to_dict(),
            "exp": now + max_age,
            "iat": now,
        }
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        payload_b64 = _b64encode(payload_bytes)

        key = self.get_secret_key().encode("utf-8")
        sig = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
        sig_b64 = _b64encode(sig)

        return f"{payload_b64}.{sig_b64}"

    def verify_token(self, token: str) -> AdminUser | None:
        """Verify signature, expiration, admin status, and session ID of a token."""
        if not token or "." not in token:
            return None
        parts = token.split(".", 1)
        if len(parts) != 2:
            return None
        payload_b64, sig_b64 = parts

        key = self.get_secret_key().encode("utf-8")
        expected_sig = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
        try:
            provided_sig = _b64decode(sig_b64)
        except (binascii.Error, ValueError):
            return None

        if not hmac.compare_digest(expected_sig, provided_sig):
            return None

        try:
            payload_bytes = _b64decode(payload_b64)
            data = json.loads(payload_bytes.decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None

        if not isinstance(data, dict):
            return None

        # Check expiration
        exp = data.get("exp")
        if not isinstance(exp, (int, float)) or time.time() > exp:
            return None

        user_data = data.get("user")
        if not isinstance(user_data, dict):
            return None

        raw_username = user_data.get("username")
        raw_sid = user_data.get("session_id")
        if not isinstance(raw_username, str) or not raw_username.strip():
            return None
        if not isinstance(raw_sid, str) or not raw_sid.strip():
            return None
        if user_data.get("is_admin") is not True:
            return None

        user = AdminUser.from_dict(user_data)
        self._prune_revoked_sessions()
        if user.session_id in self._revoked_sessions:
            return None

        return user

    def revoke_session(self, session_id: str, ttl: int | None = None) -> None:
        """Revoke an active admin session ID with TTL pruning and bounded retention."""
        if not session_id:
            return
        now = time.time()
        ttl_sec = DEFAULT_SESSION_MAX_AGE if ttl is None else ttl
        self._revoked_sessions[session_id] = now + ttl_sec
        self._prune_revoked_sessions(now)

    def generate_csrf_token(self, user: AdminUser) -> str:
        """Generate a cryptographically signed CSRF token bound to the admin session."""
        salt = secrets.token_hex(8)
        msg = f"{user.session_id}:{salt}".encode()
        key = self.get_secret_key().encode()
        sig = hmac.new(key, msg, hashlib.sha256).hexdigest()
        return f"{salt}:{sig}"

    def verify_csrf_token(self, token: str | None, user: AdminUser) -> bool:
        """Verify that a CSRF token is authentic and bound to the current admin session."""
        if not token or ":" not in token:
            return False
        parts = token.split(":", 1)
        if len(parts) != 2:
            return False
        salt, provided_sig = parts
        msg = f"{user.session_id}:{salt}".encode()
        key = self.get_secret_key().encode()
        expected_sig = hmac.new(key, msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected_sig, provided_sig)


class AdminAuthenticator:
    """Validates admin credentials with pluggable custom authenticators."""

    def __init__(self, config: Config | None = None) -> None:
        self._config = config
        self._custom_auth: Callable[[str, str], bool | AdminUser | None] | None = None

    def set_authenticator(self, auth_func: Callable[[str, str], bool | AdminUser | None]) -> None:
        """Register a custom authentication callback: func(username, password) -> bool | AdminUser | None."""
        self._custom_auth = auth_func

    def authenticate(self, username: str, password: str) -> AdminUser | None:
        """Verify credentials against custom authenticator or configured defaults."""
        if self._custom_auth is not None:
            res = self._custom_auth(username, password)
            if isinstance(res, AdminUser):
                return res if res.is_admin is True else None
            if res is True:
                return AdminUser(username=username)
            return None

        # Resolve configured admin credentials
        cfg = _resolve_admin_config(self._config)
        raw_user = cfg.get("admin.username") if cfg is not None else None
        raw_pass = cfg.get("admin.password") if cfg is not None else None

        has_custom_user = bool(raw_user and str(raw_user).strip())
        has_custom_pass = bool(raw_pass and str(raw_pass))
        is_default_pair = (
            (not has_custom_user or str(raw_user) == "admin")
            and (not has_custom_pass or str(raw_pass) == "admin123")
        )

        if (
            (not has_custom_user or not has_custom_pass or is_default_pair)
            and not _is_dev_debug_mode(cfg)
        ):
            raise ConfigurationError(
                "Default administrative credentials ('admin'/'admin123') are strictly prohibited in production mode "
                "and non-debug environments. Set ADMIN_USERNAME and ADMIN_PASSWORD or configure a custom admin authenticator."
            )

        expected_user = str(raw_user) if has_custom_user else "admin"
        expected_pass = str(raw_pass) if has_custom_pass else "admin123"

        # Constant-time comparison
        user_ok = hmac.compare_digest(username.strip(), expected_user.strip())
        pass_ok = hmac.compare_digest(password, expected_pass)

        if user_ok and pass_ok:
            return AdminUser(username=username)
        return None


# Global singleton instances
_admin_session_store = AdminSessionStore()
_admin_authenticator = AdminAuthenticator()


def get_admin_session_store() -> AdminSessionStore:
    return _admin_session_store


def get_admin_authenticator() -> AdminAuthenticator:
    return _admin_authenticator


def create_admin_session(
    response: Response,
    user: AdminUser,
    max_age: int = DEFAULT_SESSION_MAX_AGE,
    *,
    prefix: str = "/admin",
    request: Request | None = None,
    secure: bool | None = None,
) -> str:
    """Create a signed admin session token and attach it as a Set-Cookie header."""
    store = get_admin_session_store()
    token = store.create_token(user, max_age=max_age)
    cookie_path = _normalize_cookie_path(prefix)
    cookie_parts = [
        f"{ADMIN_COOKIE_NAME}={token}",
        f"Path={cookie_path}",
        f"Max-Age={max_age}",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if _should_use_secure_cookie(request=request, secure=secure):
        cookie_parts.append("Secure")
    response.set_cookie("; ".join(cookie_parts))
    return token


def clear_admin_session(
    response: Response,
    request: Request | None = None,
    *,
    prefix: str = "/admin",
    secure: bool | None = None,
) -> None:
    """Clear the admin session cookie and revoke the session in store."""
    if request is not None:
        user = get_current_admin(request)
        if user is not None:
            store = get_admin_session_store()
            store.revoke_session(user.session_id)
            Context.set(_CONTEXT_ADMIN_KEY, None)
            if hasattr(request, "state"):
                request.state.pop("admin_user", None)

    cookie_path = _normalize_cookie_path(prefix)
    cookie_parts = [
        f"{ADMIN_COOKIE_NAME}=",
        f"Path={cookie_path}",
        "Max-Age=0",
        "Expires=Thu, 01 Jan 1970 00:00:00 GMT",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if _should_use_secure_cookie(request=request, secure=secure):
        cookie_parts.append("Secure")
    response.set_cookie("; ".join(cookie_parts))


def get_current_admin(request: Request) -> AdminUser | None:
    """
    Extract and verify the admin session from request cookies.
    Attaches authenticated AdminUser to request.state and task Context.
    """
    # Check if already cached on this request
    cached = getattr(request, "state", {}).get("admin_user")
    if isinstance(cached, AdminUser):
        return cached if cached.is_admin is True else None

    cookie_header = request.headers.get("cookie")
    cookies = parse_cookie_header(cookie_header)
    token = cookies.get(ADMIN_COOKIE_NAME)
    if not token:
        return None

    store = get_admin_session_store()
    admin_user = store.verify_token(token)
    if admin_user is not None and admin_user.is_admin is True:
        request.state["admin_user"] = admin_user
        Context.set(_CONTEXT_ADMIN_KEY, admin_user.to_dict())
        return admin_user
    return None


def require_admin(request: Request) -> AdminUser:
    """
    Enforce authenticated admin session.
    Returns the authenticated AdminUser or raises AdminUnauthorizedError.
    """
    user = get_current_admin(request)
    if user is None or user.is_admin is not True:
        raise AdminUnauthorizedError("Admin session required. Please sign in.")
    return user


def current_admin() -> AdminUser | None:
    """Retrieve the current admin user from task-scoped Context."""
    raw = Context.get(_CONTEXT_ADMIN_KEY)
    if isinstance(raw, dict):
        user = AdminUser.from_dict(raw)
        return user if user.is_admin is True else None
    return None


__all__ = [
    "ADMIN_COOKIE_NAME",
    "AdminAuthenticator",
    "AdminSessionStore",
    "AdminUnauthorizedError",
    "AdminUser",
    "clear_admin_session",
    "create_admin_session",
    "current_admin",
    "get_admin_authenticator",
    "get_admin_session_store",
    "get_current_admin",
    "parse_cookie_header",
    "require_admin",
]
