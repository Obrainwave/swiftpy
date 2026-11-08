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
from swiftpy.core.context import Context
from swiftpy.http.request import Request
from swiftpy.http.response import Response

logger = logging.getLogger("swiftpy.admin.auth")

ADMIN_COOKIE_NAME = "swiftpy_admin_session"
DEFAULT_SESSION_MAX_AGE = 86400  # 24 hours
_CONTEXT_ADMIN_KEY = "__swiftpy_admin_user__"


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
        self.permissions = list(permissions or ["all"])
        self.session_id = session_id or secrets.token_hex(16)
        self.metadata = dict(metadata or {})

    @property
    def is_authenticated(self) -> bool:
        return True

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
        return cls(
            username=str(data.get("username", "admin")),
            is_admin=bool(data.get("is_admin", True)),
            permissions=list(data.get("permissions", ["all"])),
            session_id=str(data.get("session_id", "")),
            metadata=dict(data.get("metadata", {})),
        )

    def __repr__(self) -> str:
        return f"<AdminUser username={self.username!r} is_admin={self.is_admin}>"


class AdminSessionStore:
    """
    Session storage and token issuance manager for admin authentication.
    Signs sessions using HMAC-SHA256 to ensure tamper-proof, stateless verification
    with optional in-memory revocation tracking.
    """

    def __init__(
        self,
        secret_key: str | None = None,
        config: Config | None = None,
    ) -> None:
        self._config = config
        self._secret_key = secret_key
        self._revoked_sessions: set[str] = set()

    def get_secret_key(self) -> str:
        if self._secret_key:
            return self._secret_key
        cfg = self._config or Config.get_instance()
        key = cfg.get("admin.secret_key") or cfg.get("app.key")
        if not key or not str(key).strip():
            # Fallback to deterministic pepper or generated secret for testing
            key = "swiftpy-admin-default-secret-change-in-production"
        self._secret_key = str(key)
        return self._secret_key

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
        """Verify signature and expiration of a session token; return AdminUser or None."""
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

        user = AdminUser.from_dict(user_data)
        if user.session_id in self._revoked_sessions:
            return None

        return user

    def revoke_session(self, session_id: str) -> None:
        """Revoke an active admin session ID."""
        self._revoked_sessions.add(session_id)

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
        """Verify credentials against custom authenticator or configuration defaults."""
        if self._custom_auth is not None:
            res = self._custom_auth(username, password)
            if isinstance(res, AdminUser):
                return res
            if res is True:
                return AdminUser(username=username)
            return None

        # Fallback to configured admin credentials
        cfg = self._config or Config.get_instance()
        env = str(cfg.get("app.env") or cfg.get("APP_ENV") or "local").lower()
        raw_user = cfg.get("admin.username")
        raw_pass = cfg.get("admin.password")

        # Disallow default credentials in production
        if env == "production":
            is_default = (
                (not raw_user or str(raw_user) == "admin")
                and (not raw_pass or str(raw_pass) == "admin123")
            )
            if is_default:
                raise ConfigurationError(
                    "Default administrative credentials ('admin'/'admin123') are strictly prohibited in production mode. "
                    "Set ADMIN_USERNAME and ADMIN_PASSWORD or configure a custom admin authenticator."
                )

        expected_user = str(raw_user or "admin")
        expected_pass = str(raw_pass or "admin123")

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
) -> str:
    """Create a signed admin session token and attach it as a Set-Cookie header."""
    store = get_admin_session_store()
    token = store.create_token(user, max_age=max_age)
    cookie_str = (
        f"{ADMIN_COOKIE_NAME}={token}; "
        f"Path=/admin; "
        f"Max-Age={max_age}; "
        f"HttpOnly; "
        f"SameSite=Lax"
    )
    response.set_cookie(cookie_str)
    return token


def clear_admin_session(response: Response, request: Request | None = None) -> None:
    """Clear the admin session cookie and revoke the session in store."""
    if request is not None:
        user = get_current_admin(request)
        if user is not None:
            store = get_admin_session_store()
            store.revoke_session(user.session_id)
            Context.set(_CONTEXT_ADMIN_KEY, None)
            if hasattr(request, "state"):
                request.state.pop("admin_user", None)

    cookie_str = (
        f"{ADMIN_COOKIE_NAME}=; "
        f"Path=/admin; "
        f"Max-Age=0; "
        f"Expires=Thu, 01 Jan 1970 00:00:00 GMT; "
        f"HttpOnly; "
        f"SameSite=Lax"
    )
    response.set_cookie(cookie_str)


def get_current_admin(request: Request) -> AdminUser | None:
    """
    Extract and verify the admin session from request cookies.
    Attaches authenticated AdminUser to request.state and task Context.
    """
    # Check if already cached on this request
    cached = getattr(request, "state", {}).get("admin_user")
    if isinstance(cached, AdminUser):
        return cached

    cookie_header = request.headers.get("cookie")
    cookies = parse_cookie_header(cookie_header)
    token = cookies.get(ADMIN_COOKIE_NAME)
    if not token:
        return None

    store = get_admin_session_store()
    admin_user = store.verify_token(token)
    if admin_user is not None:
        request.state["admin_user"] = admin_user
        Context.set(_CONTEXT_ADMIN_KEY, admin_user.to_dict())
    return admin_user


def require_admin(request: Request) -> AdminUser:
    """
    Enforce authenticated admin session.
    Returns the authenticated AdminUser or raises AdminUnauthorizedError.
    """
    user = get_current_admin(request)
    if user is None:
        raise AdminUnauthorizedError("Admin session required. Please sign in.")
    return user


def current_admin() -> AdminUser | None:
    """Retrieve the current admin user from task-scoped Context."""
    raw = Context.get(_CONTEXT_ADMIN_KEY)
    if isinstance(raw, dict):
        return AdminUser.from_dict(raw)
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
