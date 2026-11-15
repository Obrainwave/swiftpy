from __future__ import annotations

import importlib
import inspect
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any, Self, TypeVar
from urllib.parse import urlsplit

from swiftpy.ai.pool import AIHttpPool
from swiftpy.auth.auth_manager import AuthManager, CurrentUser
from swiftpy.auth.exceptions import AuthenticationError
from swiftpy.auth.jwt_issuer import JWTIssuer
from swiftpy.auth.token_blacklist import TokenBlacklist, is_token_blacklist_enabled
from swiftpy.cache.cache import Cache
from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import Container, bind_container, set_container
from swiftpy.core.context import Context
from swiftpy.core.exceptions import ExceptionHandler
from swiftpy.core.providers.base import ServiceProvider
from swiftpy.database.pool import DatabasePool
from swiftpy.events.dispatcher import EventDispatcher
from swiftpy.events.redis_bus import RedisEventBus
from swiftpy.filesystem.manager import FilesystemManager
from swiftpy.http.exceptions import ClientDisconnect, HttpException
from swiftpy.http.middleware import Middleware, MiddlewarePipeline
from swiftpy.http.request import (
    DEFAULT_MAX_BODY_SIZE,
    DEFAULT_MAX_JSON_DEPTH,
    Request,
)
from swiftpy.http.response import (
    Response,
    json_response,
    not_found,
)
from swiftpy.http.router import PathParamCoercionError, Route, RouteGroup, Router
from swiftpy.mail.manager import MailManager
from swiftpy.queue.dispatcher import QueueDispatcher
from swiftpy.support.keys import (
    resolve_configured_app_key,
    validate_signing_key,
)
from swiftpy.validation.exceptions import ValidationException
from swiftpy.validation.form_request import FormRequest
from swiftpy.validation.validator import validate
from swiftpy.websockets.connection import (
    WebSocketConnection,
    WebSocketDisconnect,
    WebSocketState,
)
from swiftpy.websockets.manager import WebSocketManager

logger = logging.getLogger("swiftpy")

F = TypeVar("F", bound=Callable[..., Any])


class FrameworkServiceProvider(ServiceProvider):
    """
    Built-in framework service provider managing core infrastructure lifecycle:
    - Initializes and closes `DatabasePool` (when database configuration is present).
    - Stops `RedisEventBus` and closes Redis clients (`Cache`, `QueueDispatcher`, `TokenBlacklist`).
    - Closes active WebSocket connections with code 1001 (`Going Away`) on shutdown.
    """

    def register(self) -> None:
        if self.container.has(Config):
            cfg = self.container.resolve(Config)
            db_cfg = cfg.get("database")
            if (
                isinstance(db_cfg, dict)
                and (db_cfg.get("connections") or db_cfg.get("default"))
                and not self.container.has(DatabasePool)
            ):
                self.container.singleton(DatabasePool)

        if not self.container.has(JWTIssuer):
            self.container.singleton(
                JWTIssuer,
                lambda c: JWTIssuer.from_config(
                    c.resolve(Config) if c.has(Config) else None
                ),
            )
        auth_cfg: Config | None = (
            self.container.resolve(Config) if self.container.has(Config) else None
        )
        if not self.container.has(TokenBlacklist) and is_token_blacklist_enabled(
            auth_cfg
        ):
            self.container.singleton(
                TokenBlacklist,
                lambda c: TokenBlacklist(
                    config=c.resolve(Config) if c.has(Config) else None,
                    container=c,
                ),
            )
        if not self.container.has(AuthManager):
            self.container.singleton(AuthManager, lambda c: AuthManager(c))
        if not self.container.has(QueueDispatcher):
            self.container.singleton(QueueDispatcher, lambda _: QueueDispatcher())
        if not self.container.has(EventDispatcher):
            self.container.singleton(EventDispatcher, lambda _: EventDispatcher())
        if not self.container.has(RedisEventBus):
            self.container.singleton(
                RedisEventBus,
                lambda c: RedisEventBus(
                    dispatcher=c.resolve(EventDispatcher)
                    if c.has(EventDispatcher)
                    else None
                ),
            )
        if not self.container.has(Cache):
            self.container.singleton(
                Cache,
                lambda c: Cache(config=c.resolve(Config) if c.has(Config) else None),
            )
        if not self.container.has(MailManager):
            self.container.singleton(
                MailManager,
                lambda c: MailManager(
                    config=c.resolve(Config) if c.has(Config) else None
                ),
            )

    async def startup(self) -> None:
        if self.container.has(Config) and self.container.has(DatabasePool):
            cfg = self.container.resolve(Config)
            db_cfg = cfg.get("database")
            if isinstance(db_cfg, dict) and (
                db_cfg.get("connections") or db_cfg.get("default")
            ):
                pool = self.container.resolve(DatabasePool)
                await pool.init()

        if self.container.has(RedisEventBus) and self.container.has(WebSocketManager):
            bus = self.container.resolve(RedisEventBus)
            ws_manager = self.container.resolve(WebSocketManager)
            ws_manager.subscribe_to_bus(bus)

    async def shutdown(self) -> None:
        # 1. Close active WebSocket connections with 1001 (Going Away)
        ws_manager = self.container.get_singleton(WebSocketManager)
        bus = self.container.get_singleton(RedisEventBus)
        if isinstance(ws_manager, WebSocketManager):
            if isinstance(bus, RedisEventBus):
                ws_manager.unsubscribe_from_bus(bus)
            for conn in list(ws_manager.get_connections()):
                try:
                    if conn.is_connected:
                        await conn.close(code=1001, reason="Server shutting down")
                except Exception:
                    logger.exception("Error closing WebSocket connection during shutdown")
                try:
                    await ws_manager.disconnect(conn)
                except Exception:
                    logger.exception(
                        "Error disconnecting WebSocket connection during shutdown"
                    )

        # 2. Stop RedisEventBus and close its Redis connection
        if isinstance(bus, RedisEventBus):
            try:
                await bus.close()
            except Exception:
                logger.exception("Error closing RedisEventBus during shutdown")

        # 3. Close background clients (Cache, QueueDispatcher, TokenBlacklist, FilesystemManager, MailManager)
        for service_cls in (
            Cache,
            QueueDispatcher,
            TokenBlacklist,
            FilesystemManager,
            MailManager,
        ):
            inst = self.container.get_singleton(service_cls)
            if inst is not None and hasattr(inst, "close"):
                try:
                    await inst.close()
                except Exception:
                    logger.exception(
                        "Error closing %s during shutdown", service_cls.__name__
                    )

        # 4. Close AIHttpPool (container singleton and global fallback if any)
        closed_ai_pools: set[int] = set()
        ai_pool = self.container.get_singleton(AIHttpPool)
        if isinstance(ai_pool, AIHttpPool):
            closed_ai_pools.add(id(ai_pool))
            try:
                await ai_pool.aclose()
            except Exception:
                logger.exception("Error closing AIHttpPool during shutdown")
        global_ai_pool = AIHttpPool._instance
        if isinstance(global_ai_pool, AIHttpPool) and id(global_ai_pool) not in closed_ai_pools:
            try:
                await global_ai_pool.aclose()
            except Exception:
                logger.exception("Error closing global AIHttpPool during shutdown")

        # 5. Close DatabasePool
        pool = self.container.get_singleton(DatabasePool)
        if isinstance(pool, DatabasePool):
            try:
                await pool.close()
            except Exception:
                logger.exception("Error closing DatabasePool during shutdown")


def _deep_merge_config(target: dict[str, Any], updates: Mapping[str, Any]) -> None:
    """Merge `updates` into `target` in-place, giving `updates` top precedence."""
    for key, val in updates.items():
        if isinstance(val, Mapping) and isinstance(target.get(key), dict):
            _deep_merge_config(target[key], val)
        elif isinstance(val, Mapping):
            target[key] = dict(val)
        else:
            target[key] = val


def discover_providers(
    providers_path: str | Path | None = None,
    *,
    project_root: str | Path | None = None,
    strict: bool = True,
) -> list[type[ServiceProvider]]:
    """
    Dynamically discover all ServiceProvider subclasses in the application providers directory.
    Imports providers by package path (`app.providers.<name>`) relative to `project_root`
    without leaking `root` into `sys.path`.
    When `strict=True` (the default), import or syntax errors in provider modules raise `ConfigurationError`.
    """
    if project_root is not None:
        root = Path(project_root).resolve()
    elif providers_path is not None:
        resolved_prov = Path(providers_path).resolve()
        if resolved_prov.name == "providers" and resolved_prov.parent.name == "app":
            root = resolved_prov.parent.parent
        else:
            root = Path.cwd().resolve()
    else:
        root = Path.cwd().resolve()

    if providers_path is not None:
        target_dir = Path(providers_path)
        if not target_dir.is_absolute():
            target_dir = (root / target_dir).resolve()
        else:
            target_dir = target_dir.resolve()
    else:
        target_dir = (root / "app" / "providers").resolve()

    if not target_dir.is_dir():
        return []

    discovered: list[type[ServiceProvider]] = []

    for file in sorted(target_dir.glob("*.py")):
        if file.name.startswith(("_", ".")):
            continue

        resolved_file = file.resolve()
        package_module_name: str | None = None
        try:
            rel_path = resolved_file.relative_to(root).with_suffix("")
            if len(rel_path.parts) >= 2:
                package_module_name = ".".join(rel_path.parts)
        except ValueError:
            package_module_name = None

        can_use_package_import = False
        if package_module_name is not None:
            top_pkg = package_module_name.split(".", 1)[0]
            existing_top = sys.modules.get(top_pkg)
            if existing_top is None:
                can_use_package_import = True
            else:
                top_file = getattr(existing_top, "__file__", None)
                top_paths = getattr(existing_top, "__path__", None)
                if (top_file and Path(top_file).resolve().is_relative_to(root)) or (
                    top_paths
                    and any(Path(p).resolve().is_relative_to(root) for p in top_paths)
                ):
                    can_use_package_import = True

        display_name = package_module_name or file.stem
        try:
            if can_use_package_import and package_module_name is not None:
                root_str = str(root)
                inserted_root = False
                if root_str not in sys.path:
                    sys.path.insert(0, root_str)
                    inserted_root = True
                try:
                    mod = importlib.import_module(package_module_name)
                finally:
                    if inserted_root and root_str in sys.path:
                        sys.path.remove(root_str)
            else:
                synthetic_name = f"_swiftpy_providers.{file.stem}"
                spec = spec_from_file_location(synthetic_name, resolved_file)
                if spec is None or spec.loader is None:
                    continue
                mod = module_from_spec(spec)
                sys.modules[synthetic_name] = mod
                try:
                    spec.loader.exec_module(mod)
                except Exception:
                    sys.modules.pop(synthetic_name, None)
                    raise
        except Exception as exc:
            if strict:
                raise ConfigurationError(
                    f"Failed to load service provider '{file.stem}' ({display_name}): {exc}"
                ) from exc
            logger.warning(
                "Could not auto-load provider module %s: %s", display_name, exc
            )
            continue

        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, ServiceProvider)
                and attr is not ServiceProvider
                and attr not in discovered
            ):
                discovered.append(attr)

    # Ensure AppServiceProvider is loaded first if present
    discovered.sort(key=lambda cls: 0 if cls.__name__ == "AppServiceProvider" else 1)
    return discovered


class Application:
    """
    Root SwiftPY application.
    """

    def __init__(
        self,
        title: str = "SwiftPY Application",
        version: str = "0.1.0",
        debug: bool | None = None,
        config: dict[str, Any] | Config | None = None,
        providers: Sequence[type[ServiceProvider]] | None = None,
        middleware: Sequence[type[Middleware]] | None = None,
        auto_discover_providers: bool = True,
        providers_path: str | Path | None = None,
        project_root: str | Path | None = None,
        strict_providers: bool = True,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
        allowed_origins: Sequence[str] | set[str] | None = None,
        max_ws_message_size: int | None = None,
        ws_idle_timeout: float | None = None,
        **kwargs: Any,
    ) -> None:
        self.title = title
        self.version = version
        self.debug = debug
        self.extra = kwargs

        self.container = Container()
        self.router = Router(container=self.container)
        self._providers: list[ServiceProvider] = []
        self._started_providers: list[ServiceProvider] = []
        self._middleware: list[type[Middleware]] = list(middleware or [])
        self._booted = False
        self._started = False

        # 1. Load or bind configuration
        if config is None:
            config_dir = (
                Path(project_root) / "config" if project_root is not None else "config"
            )
            self.config = Config.load(config_dir)
        elif isinstance(config, Config):
            self.config = config
        else:
            self.config = Config(config)

        # Merge additional kwargs into config (explicit kwargs always take top precedence)
        if kwargs:
            _deep_merge_config(self.config._data, kwargs)

        cfg_max_body = self.config.get("http.max_body_size")
        if max_body_size is not None:
            self.max_body_size: int = int(max_body_size)
        elif cfg_max_body is not None:
            self.max_body_size = int(cfg_max_body)
        else:
            self.max_body_size = DEFAULT_MAX_BODY_SIZE

        cfg_max_content = self.config.get("http.max_content_length")
        if max_content_length is not None:
            self.max_content_length: int | None = int(max_content_length)
        elif cfg_max_content is not None:
            self.max_content_length = int(cfg_max_content)
        else:
            self.max_content_length = None

        cfg_max_depth = self.config.get("http.max_json_depth")
        if max_json_depth is not None:
            self.max_json_depth: int = int(max_json_depth)
        elif cfg_max_depth is not None:
            self.max_json_depth = int(cfg_max_depth)
        else:
            self.max_json_depth = DEFAULT_MAX_JSON_DEPTH

        # WebSocket origin, message size, and idle timeout limits (D-14, P9-W1, P9-W2, WS-2)
        if allowed_origins is not None:
            raw_allowed_origins = allowed_origins
        elif self.config.has("websockets.allowed_origins"):
            raw_allowed_origins = self.config.get("websockets.allowed_origins")
        else:
            raw_allowed_origins = self.config.get("websocket.allowed_origins")

        if raw_allowed_origins is not None:
            if isinstance(raw_allowed_origins, str):
                self.allowed_origins: set[str] | None = {
                    o.strip() for o in raw_allowed_origins.split(",") if o.strip()
                }
            else:
                self.allowed_origins = set(raw_allowed_origins)
        else:
            self.allowed_origins = None

        if max_ws_message_size is not None:
            self.max_ws_message_size: int = int(max_ws_message_size)
        else:
            cfg_ws_max = (
                self.config.get("websockets.max_message_size")
                if self.config.has("websockets.max_message_size")
                else self.config.get("websocket.max_message_size")
            )
            self.max_ws_message_size = (
                int(cfg_ws_max) if cfg_ws_max is not None else 1_048_576
            )

        if ws_idle_timeout is not None:
            self.ws_idle_timeout: float | None = float(ws_idle_timeout)
        else:
            cfg_ws_idle = (
                self.config.get("websockets.idle_timeout")
                if self.config.has("websockets.idle_timeout")
                else self.config.get("websocket.idle_timeout")
            )
            self.ws_idle_timeout = (
                float(cfg_ws_idle) if cfg_ws_idle is not None else None
            )

        # 2. Configure debug mode and exception handler (refuse debug=True in production)
        app_env = (
            str(self.config.get("app.env") or self.config.get("env") or "development")
            .strip()
            .lower()
        )
        if debug is not None:
            self.debug = bool(debug)
        else:
            cfg_debug = self.config.get("app.debug")
            if cfg_debug is None:
                cfg_debug = self.config.get("debug", False)
            self.debug = bool(cfg_debug)

        if self.debug and app_env in ("production", "prod"):
            raise ConfigurationError(
                "Cannot enable debug mode (debug=True) when APP_ENV is 'production'."
            )

        self.exception_handler = ExceptionHandler(debug=self.debug)

        # 3. Register core singletons and default scoped bindings into container
        self.websocket_manager = WebSocketManager()
        self.container.instance(Application, self)
        self.container.instance(Container, self.container)
        self.container.instance(Router, self.router)
        self.container.instance(Config, self.config)
        self.container.instance(ExceptionHandler, self.exception_handler)
        self.container.instance(WebSocketManager, self.websocket_manager)
        self.container.scoped(
            CurrentUser,
            lambda _: CurrentUser(),
        )

        # 4. Bind active container globally
        set_container(self.container)

        # 5. Register built-in framework lifecycle provider first (starts first, shuts down last)
        self.register(FrameworkServiceProvider)

        # 6. Auto-discover and register service providers from app/providers
        if auto_discover_providers:
            discovered = discover_providers(
                providers_path,
                project_root=project_root,
                strict=strict_providers,
            )
            for provider_cls in discovered:
                self.register(provider_cls)

        # 7. Register explicitly passed service providers
        if providers:
            for provider_cls in providers:
                if not any(isinstance(p, provider_cls) for p in self._providers):
                    self.register(provider_cls)

    def register(self, provider_class: type[ServiceProvider]) -> ServiceProvider:
        """Instantiate and register a service provider."""
        provider = provider_class(self.container)
        provider.register()
        self._providers.append(provider)

        if self._booted:
            provider.boot()

        return provider

    def boot(self) -> None:
        """
        Boot all registered service providers once all bindings are configured.
        """
        if self._booted:
            return

        for provider in self._providers:
            provider.boot()

        self._booted = True
        self._compile_routes()

    async def startup(self) -> None:
        """
        Execute asynchronous startup hooks on all registered service providers in registration order.
        If any provider fails to start, already-started providers are shut down in reverse order.
        """
        if self._started:
            return

        app_env = (
            str(self.config.get("app.env") or self.config.get("env") or "development")
            .strip()
            .lower()
        )
        configured_app_key = resolve_configured_app_key(self.config)
        if app_env in ("production", "prod") or configured_app_key is not None:
            validate_signing_key(configured_app_key, purpose="APP_KEY")

        with bind_container(self.container):
            if not self._booted:
                self.boot()

            started: list[ServiceProvider] = []
            try:
                for provider in self._providers:
                    await provider.startup()
                    started.append(provider)
            except Exception:
                for provider in reversed(started):
                    try:
                        await provider.shutdown()
                    except Exception:
                        logger.exception(
                            "Error rolling back provider %s after startup failure",
                            type(provider).__name__,
                        )
                raise

            self._started_providers = started
            self._started = True

    async def shutdown(self) -> None:
        """
        Execute asynchronous shutdown hooks in reverse registration order.
        Errors in individual providers are logged and do not block remaining shutdown hooks.
        """
        with bind_container(self.container):
            providers_to_stop = (
                list(reversed(self._started_providers))
                if self._started_providers
                else list(reversed(self._providers))
            )
            self._started_providers = []
            self._started = False

            for provider in providers_to_stop:
                try:
                    await provider.shutdown()
                except Exception:
                    logger.exception(
                        "Error during shutdown of provider %s", type(provider).__name__
                    )

    def _wrap_handler(self, route: Route) -> Any:
        handler = route.handler
        param_specs = self.router.ensure_route_analyzed(route)
        has_var_keyword = route.has_var_keyword
        is_coro = inspect.iscoroutinefunction(handler)

        async def dispatched_handler(
            request: Request, *args: Any, **params: Any
        ) -> Response:
            try:
                call_kwargs: dict[str, Any] = {}
                for spec in param_specs:
                    if spec.kind == "request":
                        call_kwargs[spec.name] = request
                    elif spec.kind == "path":
                        call_kwargs[spec.name] = params[spec.name]
                    elif spec.kind == "form":
                        ann: type[FormRequest] = spec.annotation
                        content_type = request.headers.get("content-type", "")
                        media_type = content_type.split(";", 1)[0].strip().lower()

                        if media_type in (
                            "application/x-www-form-urlencoded",
                            "multipart/form-data",
                        ):
                            if media_type == "application/x-www-form-urlencoded":
                                raw_body = await request.body()
                                if not raw_body.strip():
                                    raise ValidationException(
                                        {"body": ["Request body is required."]}
                                    )
                            try:
                                form_data = await request.form()
                            except HttpException as exc:
                                if exc.status_code == 400:
                                    raise ValidationException(
                                        {"body": [exc.message]}
                                    ) from None
                                raise
                            except (ValueError, UnicodeDecodeError) as exc:
                                raise ValidationException(
                                    {"body": [str(exc)]}
                                ) from None

                            if (
                                media_type == "multipart/form-data"
                                and not form_data.fields_list
                                and not form_data.files_list
                            ):
                                raise ValidationException(
                                    {"body": ["Request body is required."]}
                                )

                            data: dict[str, Any] = {}
                            for k, vals in form_data.fields_list.items():
                                data[k] = vals[0] if len(vals) == 1 else list(vals)
                            for k, flist in form_data.files_list.items():
                                if k not in data:
                                    data[k] = flist[0] if len(flist) == 1 else list(flist)
                        else:
                            raw_body = await request.body()
                            if not raw_body.strip():
                                raise ValidationException(
                                    {"body": ["Request body is required."]}
                                )
                            try:
                                raw_data = await request.json()
                                if not isinstance(raw_data, dict):
                                    raise ValidationException(
                                        {
                                            "body": [
                                                "The request payload must be a JSON object."
                                            ]
                                        }
                                    )
                                data = raw_data
                            except (
                                json.JSONDecodeError,
                                UnicodeDecodeError,
                                ValueError,
                            ):
                                raise ValidationException(
                                    {"body": ["Invalid JSON payload"]}
                                ) from None
                            except HttpException as exc:
                                if (
                                    exc.status_code == 400
                                    and exc.message == "Invalid JSON payload"
                                ):
                                    raise ValidationException(
                                        {"body": ["Invalid JSON payload"]}
                                    ) from None
                                raise

                        form_instance, rules, messages = ann.prepare_validation(
                            request=request, container=self.container
                        )
                        result = await validate(data, rules, messages=messages)
                        if result.fails():
                            raise ValidationException(result.errors)

                        form_instance.data = result.validated_data
                        form_instance.raw_data = data
                        form_instance.request = request
                        form_instance.validation_result = result
                        call_kwargs[spec.name] = form_instance
                    elif spec.kind == "di":
                        call_kwargs[spec.name] = self.container.resolve(spec.annotation)
                    elif spec.kind == "file":
                        call_kwargs[spec.name] = await request.file(spec.name)
                    elif spec.kind == "files":
                        call_kwargs[spec.name] = await request.files(spec.name)
                    elif spec.kind == "default" and spec.name in params:
                        call_kwargs[spec.name] = params[spec.name]

                if has_var_keyword:
                    for k, v in params.items():
                        if k not in call_kwargs:
                            call_kwargs[k] = v

                if is_coro:
                    res = await handler(**call_kwargs)
                else:
                    res = handler(**call_kwargs)
                    if inspect.isawaitable(res):
                        res = await res

                if isinstance(res, Response):
                    return res
                if isinstance(res, (dict, list)):
                    return json_response(res)
                raise TypeError(
                    f"Route handler must return a Response, dict, or list, got {type(res).__name__}"
                )
            except ClientDisconnect:
                raise
            except Exception as exc:  # noqa: BLE001
                return await self._handle_exception_safely(exc, request=request)

        return dispatched_handler

    def _wrap_websocket_handler(self, route: Route) -> Any:
        handler = route.handler
        param_specs = self.router.ensure_route_analyzed(route)
        has_var_keyword = route.has_var_keyword
        is_coro = inspect.iscoroutinefunction(handler)

        async def dispatched_ws_handler(
            websocket: WebSocketConnection, *args: Any, **params: Any
        ) -> Any:
            call_kwargs: dict[str, Any] = {}
            for spec in param_specs:
                if spec.kind == "websocket":
                    call_kwargs[spec.name] = websocket
                elif spec.kind == "path":
                    call_kwargs[spec.name] = params[spec.name]
                elif spec.kind == "di":
                    call_kwargs[spec.name] = self.container.resolve(spec.annotation)
                elif spec.kind == "default" and spec.name in params:
                    call_kwargs[spec.name] = params[spec.name]

            if has_var_keyword:
                for k, v in params.items():
                    if k not in call_kwargs:
                        call_kwargs[k] = v

            if is_coro:
                return await handler(**call_kwargs)
            res = handler(**call_kwargs)
            if inspect.isawaitable(res):
                return await res
            return res

        return dispatched_ws_handler

    @staticmethod
    def _parse_origin(raw: str) -> tuple[str, str, int] | None:
        """Parse an HTTP/HTTPS Origin into (scheme, host, port), rejecting malformed values."""
        cleaned = raw.strip()
        if not cleaned or any(ch.isspace() for ch in cleaned):
            return None
        try:
            parsed = urlsplit(cleaned)
            port = parsed.port
        except ValueError:
            return None
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            return None
        host = parsed.hostname
        if not host:
            return None
        effective_port = port if port is not None else (443 if scheme == "https" else 80)
        return scheme, host.lower(), effective_port

    @staticmethod
    def _parse_host_authority(
        raw_host: str, *, default_port: int
    ) -> tuple[str, int, bool] | None:
        """Parse a Host header authority into (host, port, has_explicit_port)."""
        cleaned = raw_host.strip()
        if not cleaned or any(ch.isspace() for ch in cleaned):
            return None
        try:
            parsed = urlsplit(f"//{cleaned}")
            port = parsed.port
        except ValueError:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        if parsed.path or parsed.query or parsed.fragment:
            return None
        host = parsed.hostname
        if not host:
            return None
        return host.lower(), (port if port is not None else default_port), port is not None

    def _is_origin_allowed(
        self,
        origin: str | None,
        host_header: str | None,
        scope: dict[str, Any] | None = None,
    ) -> bool:
        """
        Validate incoming WebSocket Origin against allowed_origins or same-origin policy (Decision D-14).
        If origin is None (non-browser clients like mobile/CLI/tests without Origin), allow.
        If allowed_origins is configured:
          - If "*" in allowed_origins: allow all.
          - If origin matches a configured origin URL or authority: allow.
        If allowed_origins is not configured (default None):
          - Enforce same-origin: origin scheme, host, and port must match the request scheme and Host.
        """
        if origin is None:
            return True

        parsed_origin = self._parse_origin(origin)
        if parsed_origin is None:
            return False

        if self.allowed_origins is not None:
            if "*" in self.allowed_origins:
                return True
            _, origin_host, origin_port = parsed_origin
            for allowed in self.allowed_origins:
                entry = allowed.strip()
                if not entry:
                    continue
                if "://" in entry:
                    if self._parse_origin(entry) == parsed_origin:
                        return True
                    continue
                parsed_entry = self._parse_host_authority(
                    entry, default_port=origin_port
                )
                if parsed_entry is None:
                    continue
                entry_host, entry_port, has_explicit_port = parsed_entry
                if origin_host == entry_host and (
                    not has_explicit_port or origin_port == entry_port
                ):
                    return True
            return False

        effective_host = host_header
        if not effective_host and scope:
            server_info = scope.get("server")
            if server_info and len(server_info) == 2:
                s_host, s_port = server_info
                s_host_str = str(s_host)
                if ":" in s_host_str and not s_host_str.startswith("["):
                    s_host_str = f"[{s_host_str}]"
                effective_host = (
                    f"{s_host_str}:{s_port}"
                    if s_port not in (80, 443, None)
                    else s_host_str
                )

        if not effective_host:
            return False

        raw_scheme = str((scope or {}).get("scheme", "ws")).lower()
        expected_scheme = "https" if raw_scheme in {"wss", "https"} else "http"
        default_port = 443 if expected_scheme == "https" else 80
        parsed_host = self._parse_host_authority(
            effective_host, default_port=default_port
        )
        if parsed_host is None:
            return False
        host_name, host_port, _ = parsed_host
        return parsed_origin == (expected_scheme, host_name, host_port)

    def _compile_http_handler(self, route: Route) -> Any:
        wrapped = self._wrap_handler(route)
        pipeline = MiddlewarePipeline(self.container, route.middleware)
        return pipeline.wrap(
            wrapped,
            exception_handler=self._handle_exception_safely,
        )

    def _compile_routes(self) -> None:
        for route in self.router.routes:
            self.router.ensure_route_analyzed(route)
            if route.is_websocket or route.method == "WEBSOCKET":
                route.compiled_handler = self._wrap_websocket_handler(route)
            else:
                route.compiled_handler = self._compile_http_handler(route)

    def websocket(self, path: str) -> Callable[[F], F]:
        return self.router.websocket(path)

    def get(
        self,
        path: str,
        *,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        return self.router.get(
            path,
            max_body_size=max_body_size,
            max_content_length=max_content_length,
            max_json_depth=max_json_depth,
        )

    def post(
        self,
        path: str,
        *,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        return self.router.post(
            path,
            max_body_size=max_body_size,
            max_content_length=max_content_length,
            max_json_depth=max_json_depth,
        )

    def put(
        self,
        path: str,
        *,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        return self.router.put(
            path,
            max_body_size=max_body_size,
            max_content_length=max_content_length,
            max_json_depth=max_json_depth,
        )

    def patch(
        self,
        path: str,
        *,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        return self.router.patch(
            path,
            max_body_size=max_body_size,
            max_content_length=max_content_length,
            max_json_depth=max_json_depth,
        )

    def delete(
        self,
        path: str,
        *,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        return self.router.delete(
            path,
            max_body_size=max_body_size,
            max_content_length=max_content_length,
            max_json_depth=max_json_depth,
        )

    def head(
        self,
        path: str,
        *,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        return self.router.head(
            path,
            max_body_size=max_body_size,
            max_content_length=max_content_length,
            max_json_depth=max_json_depth,
        )

    def options(
        self,
        path: str,
        *,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        return self.router.options(
            path,
            max_body_size=max_body_size,
            max_content_length=max_content_length,
            max_json_depth=max_json_depth,
        )

    def middleware(self, middleware_list: Sequence[type[Middleware]]) -> Self:
        """Register global HTTP middleware classes that run for every HTTP request."""
        self._middleware.extend(middleware_list)
        return self

    def group(
        self,
        prefix: str,
        middleware: list[type[Any]] | None = None,
    ) -> Callable[[Callable[[RouteGroup], None]], Callable[[RouteGroup], None]]:
        return self.router.group(prefix, middleware=middleware)

    def include_router(
        self,
        router: Router,
        prefix: str = "",
        middleware: list[type[Any]] | None = None,
    ) -> None:
        """
        Mount a child router under an optional prefix and middleware pipeline.
        """
        self.router.include_router(router, prefix=prefix, middleware=middleware)
        if self._booted:
            self._compile_routes()

    def exception_handler_for(
        self, exc_type: type[Any]
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator to register a custom exception handler."""

        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.exception_handler.register(exc_type, handler)
            return handler

        return decorator

    async def _handle_exception_safely(
        self,
        exc: Exception,
        request: Request | None = None,
    ) -> Response:
        try:
            return await self.exception_handler.handle(exc, request=request)
        except Exception:
            logger.exception("ExceptionHandler.handle raised an unhandled error")
            return json_response(
                {"detail": "Internal Server Error"},
                status_code=500,
            )

    async def _dispatch_http(self, request: Request) -> Response:
        try:
            route, params = self.router.match(request.method, request.path)
            if route is None:
                return not_found()

            if route.max_body_size is not None:
                request.max_body_size = route.max_body_size
            if route.max_content_length is not None:
                request.max_content_length = route.max_content_length
            if route.max_json_depth is not None:
                request.max_json_depth = route.max_json_depth

            handler = route.compiled_handler
            if handler is None:
                handler = self._compile_http_handler(route)
                route.compiled_handler = handler

            res: Response = await handler(request, **params)
            return res
        except ClientDisconnect:
            raise
        except Exception as exc:  # noqa: BLE001
            return await self._handle_exception_safely(exc, request=request)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        try:
            if scope["type"] == "lifespan":
                while True:
                    message = await receive()
                    if message["type"] == "lifespan.startup":
                        try:
                            await self.startup()
                            await send({"type": "lifespan.startup.complete"})
                        except Exception as exc:
                            logger.exception("Lifespan startup failed")
                            await send(
                                {
                                    "type": "lifespan.startup.failed",
                                    "message": str(exc),
                                }
                            )
                            return
                    elif message["type"] == "lifespan.shutdown":
                        await self.shutdown()
                        await send({"type": "lifespan.shutdown.complete"})
                        return

            if scope["type"] == "websocket":
                if not self._booted:
                    self.boot()

                with bind_container(self.container):
                    connection = WebSocketConnection(
                        scope,
                        receive,
                        send,
                        max_message_size=self.max_ws_message_size,
                        idle_timeout=self.ws_idle_timeout,
                    )
                    Context.set("websocket", connection)
                    try:
                        try:
                            route, params = self.router.match_websocket(connection.path)
                        except PathParamCoercionError:
                            await connection.close(code=1002, reason="Invalid path parameter")
                            return

                        if route is None:
                            await connection.close(code=1000, reason="Not Found")
                            return

                        # 1. Origin validation (Decision D-14)
                        host_header = connection.headers.get("host")
                        if not self._is_origin_allowed(connection.origin, host_header, connection.scope):
                            await connection.close(code=1008, reason="Origin not allowed")
                            return

                        # 2. Handshake middleware pipeline (P9-W1)
                        # Global middleware + route group middleware
                        all_middleware = list(self._middleware) + list(route.middleware)
                        if all_middleware:
                            handshake_request = Request(
                                scope,
                                receive,
                                max_body_size=self.max_body_size,
                                max_content_length=self.max_content_length,
                                max_json_depth=self.max_json_depth,
                            )
                            Context.set("request", handshake_request)

                            pipeline = MiddlewarePipeline(self.container, all_middleware)

                            async def _handshake_terminal(req: Request) -> Response:
                                return Response("OK", status_code=200)

                            try:
                                entry = pipeline.wrap(_handshake_terminal)
                                resp = await entry(handshake_request)
                                if resp.status_code >= 400:
                                    await connection.close(code=1008, reason="Unauthorized")
                                    return
                                # Propagate request state to connection state
                                connection.state.update(handshake_request.state)
                            except (AuthenticationError, HttpException):
                                await connection.close(code=1008, reason="Unauthorized")
                                return
                            except Exception:
                                logger.exception("Error in WebSocket handshake middleware")
                                await connection.close(code=1008, reason="Handshake error")
                                return

                        handler = route.compiled_handler
                        if handler is None:
                            handler = self._wrap_websocket_handler(route)
                            route.compiled_handler = handler

                        try:
                            await handler(connection, **params)
                            if connection.is_connected:
                                await connection.close(code=1000)
                        except WebSocketDisconnect as exc:
                            logger.debug("WebSocket disconnected: %s", exc)
                        except Exception:
                            if connection.client_state == WebSocketState.CONNECTING:
                                # Handler raised before accept() -> policy violation / forbidden close code 1008
                                await connection.close(code=1008, reason="Policy Violation")
                            else:
                                logger.exception("Unhandled exception in WebSocket handler")
                                if connection.is_connected:
                                    await connection.close(code=1011, reason="Internal Server Error")
                    finally:
                        try:
                            await self.websocket_manager.disconnect(connection)
                        except Exception:
                            logger.exception("Error disconnecting WebSocket from manager in finally")
                        if connection.is_connected:
                            try:
                                await connection.close(code=1000)
                            except Exception:  # noqa: BLE001, S110
                                pass
                        await self.container.aclose_scoped()
                        Context.clear()
                return

            if scope["type"] != "http":
                return

            if not self._booted:
                self.boot()

            with bind_container(self.container):
                request: Request | None = None
                response_started = False

                async def tracked_send(message: dict[str, Any]) -> None:
                    nonlocal response_started
                    if message.get("type") == "http.response.start":
                        response_started = True
                    await send(message)

                try:
                    request = Request(
                        scope,
                        receive,
                        max_body_size=self.max_body_size,
                        max_content_length=self.max_content_length,
                        max_json_depth=self.max_json_depth,
                    )
                    Context.set("request", request)
                    send_body = request.method.upper() != "HEAD"

                    if self._middleware:
                        pipeline = MiddlewarePipeline(self.container, self._middleware)
                        entry = pipeline.wrap(
                            self._dispatch_http,
                            exception_handler=self._handle_exception_safely,
                        )
                        response = await entry(request)
                    else:
                        response = await self._dispatch_http(request)

                    try:
                        await response.send(tracked_send, send_body=send_body)
                    except ClientDisconnect:
                        raise
                    except Exception as exc:
                        logger.exception("Error while sending HTTP response")
                        if not response_started:
                            fallback = await self._handle_exception_safely(
                                exc, request=request
                            )
                            for hdr_name, hdr_val in response.headers.raw_items():
                                lower_hdr = hdr_name.lower()
                                if (
                                    lower_hdr == "vary"
                                    or lower_hdr.startswith("access-control-")
                                ) and hdr_name not in fallback.headers:
                                    fallback.headers[hdr_name] = hdr_val
                            await fallback.send(tracked_send, send_body=send_body)

                except ClientDisconnect:
                    logger.debug("Client disconnected before request completed")
                    return
                except Exception as exc:  # noqa: BLE001
                    if not response_started:
                        fallback = await self._handle_exception_safely(
                            exc, request=request
                        )
                        try:
                            fallback_send_body = (
                                request.method.upper() != "HEAD"
                                if request is not None
                                else True
                            )
                            await fallback.send(
                                tracked_send, send_body=fallback_send_body
                            )
                        except ClientDisconnect:
                            logger.debug(
                                "Client disconnected before fallback response completed"
                            )

                finally:
                    if request is not None:
                        await request.close()
                    if scope.get("type") == "http":
                        await self.container.aclose_scoped()
                        Context.clear()

        except Exception:
            logger.exception("Fatal error in ASGI dispatch")
