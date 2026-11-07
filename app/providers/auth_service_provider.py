from __future__ import annotations

from swiftpy.auth import (
    AuthManager,
    CurrentUser,
    JWTIssuer,
    TokenBlacklist,
    get_server_pepper,
)
from swiftpy.core.config import Config
from swiftpy.core.providers.base import ServiceProvider


class AuthServiceProvider(ServiceProvider):
    """
    Register authentication services, token management, and task-scoped user resolution.
    """

    def register(self) -> None:
        self.container.singleton(
            JWTIssuer,
            lambda c: JWTIssuer.from_config(
                c.resolve(Config) if c.has(Config) else None
            ),
        )
        self.container.singleton(
            TokenBlacklist,
            lambda c: TokenBlacklist(
                issuer=c.resolve(JWTIssuer) if c.has(JWTIssuer) else None,
                config=c.resolve(Config) if c.has(Config) else None,
            ),
        )
        self.container.singleton(AuthManager)
        self.container.scoped(
            CurrentUser,
            lambda _: CurrentUser(),
        )

    def boot(self) -> None:
        if self.container.has(Config):
            cfg = self.container.resolve(Config)
            app_env = str(cfg.get("app.env") or cfg.get("env") or "development").lower()
            debug_val = (
                cfg.get("app.debug")
                if cfg.get("app.debug") is not None
                else cfg.get("debug")
            )
            is_debug = (
                bool(debug_val)
                if debug_val is not None
                else app_env not in ("production", "prod")
            )
            default_guard = str(
                cfg.get("auth.defaults.guard") or cfg.get("auth.default") or "jwt"
            ).lower()
            if not is_debug:
                if default_guard == "jwt" and self.container.has(JWTIssuer):
                    self.container.resolve(JWTIssuer)
                elif default_guard == "api_key":
                    get_server_pepper(cfg)
