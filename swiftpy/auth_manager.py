from __future__ import annotations

from swiftpy.auth.auth_manager import (
    AuthManager,
    AuthProxy,
    AuthUser,
    CurrentUser,
    auth,
    current_user,
    current_user_model,
    resolve_user_model_class,
    set_user_model,
)

__all__ = [
    "AuthManager",
    "AuthProxy",
    "AuthUser",
    "CurrentUser",
    "auth",
    "current_user",
    "current_user_model",
    "resolve_user_model_class",
    "set_user_model",
]
