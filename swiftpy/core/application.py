from __future__ import annotations

import importlib
import inspect
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any, Self, TypeVar

from swiftpy.auth.auth_manager import CurrentUser
from swiftpy.auth.exceptions import AuthenticationError
from swiftpy.auth.token_blacklist import TokenBlacklist
from swiftpy.cache.cache import Cache
from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import Container, set_container
from swiftpy.core.context import Context
from swiftpy.core.exceptions import ExceptionHandler
from swiftpy.core.providers.base import ServiceProvider
from swiftpy.database.pool import DatabasePool
from swiftpy.events.redis_bus import RedisEventBus
from swiftpy.http.exceptions import ClientDisconnect, HttpException
from swiftpy.http.middleware import Middleware, MiddlewarePipeline
from swiftpy.http.request import DEFAULT_MAX_BODY_SIZE, Request
from swiftpy.http.response import (
    Response,
    json_response,
    not_found,
)
from swiftpy.http.router import PathParamCoercionError, Route, RouteGroup, Router
from swiftpy.queue.dispatcher import QueueDispatcher
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

    async def startup(self) -> None:
        if self.container.has(Config) and self.container.has(DatabasePool):
            cfg = self.container.resolve(Config)
            db_cfg = cfg.get("database")
            if isinstance(db_cfg, dict) and (
                db_cfg.get("connections") or db_cfg.get("default")
            ):
                pool = self.container.resolve(DatabasePool)
                await pool.init()

    async def shutdown(self) -> None:
        # 1. Close active WebSocket connections with 1001 (Going Away)
        ws_manager = self.container.get_singleton(WebSocketManager)
        if isinstance(ws_manager, WebSocketManager):
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
        bus = self.container.get_singleton(RedisEventBus)
        if isinstance(bus, RedisEventBus):
            try:
                await bus.close()
            except Exception:
                logger.exception("Error closing RedisEventBus during shutdown")

        # 3. Close Redis clients (Cache, QueueDispatcher, TokenBlacklist)
        for service_cls in (Cache, QueueDispatcher, TokenBlacklist):
            inst = self.container.get_singleton(service_cls)
            if inst is not None and hasattr(inst, "close"):
                try:
                    await inst.close()
                except Exception:
                    logger.exception(
                        "Error closing %s during shutdown", service_cls.__name__
                    )

        # 4. Close DatabasePool
        pool = self.container.get_singleton(DatabasePool)
        if isinstance(pool, DatabasePool):
            try:
                await pool.close()
            except Exception:
                logger.exception("Error closing DatabasePool during shutdown")


def discover_providers(
    providers_path: str | Path | None = None,
    *,
    project_root: str | Path | None = None,
    strict: bool = True,
) -> list[type[ServiceProvider]]:
    """
    Dynamically discover all ServiceProvider subclasses in the application providers directory.
    Imports providers by package path (`app.providers.<name>`) relative to `project_root`
    without inserting `app/providers` into `sys.path`.
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

    if target_dir.is_relative_to(root) and str(root) not in sys.path:
        sys.path.insert(0, str(root))

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
                mod = importlib.import_module(package_module_name)
            else:
                synthetic_name = f"_swiftpy_providers.{file.stem}"
                spec = spec_from_file_location(synthetic_name, resolved_file)
                if spec is None or spec.loader is None:
                    continue
                mod = module_from_spec(spec)
                spec.loader.exec_module(mod)
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
        allowed_origins: Sequence[str] | set[str] | None = None,
        max_ws_message_size: int | None = None,
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

        # Merge additional kwargs into config
        if kwargs:
            for k, v in kwargs.items():
                if k not in self.config._data:
                    self.config._data[k] = v

        cfg_max_body = self.config.get("http.max_body_size")
        if max_body_size is not None:
            self.max_body_size: int = int(max_body_size)
        elif cfg_max_body is not None:
            self.max_body_size = int(cfg_max_body)
        else:
            self.max_body_size = DEFAULT_MAX_BODY_SIZE

        # WebSocket origin and message size limits (D-14, P9-W1, P9-W2)
        raw_allowed_origins = (
            allowed_origins
            if allowed_origins is not None
            else (
                self.config.get("websockets.allowed_origins")
                or self.config.get("websocket.allowed_origins")
            )
        )
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
                or self.config.get("websocket.max_message_size")
            )
            self.max_ws_message_size = (
                int(cfg_ws_max) if cfg_ws_max is not None else 1_048_576
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

        set_container(self.container)
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
        set_container(self.container)
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
        param_specs = route.param_specs
        has_var_keyword = route.has_var_keyword
        is_coro = inspect.iscoroutinefunction(handler)

        async def dispatched_handler(
            request: Request, *args: Any, **params: Any
        ) -> Any:
            call_kwargs: dict[str, Any] = {}
            for spec in param_specs:
                if spec.kind == "request":
                    call_kwargs[spec.name] = request
                elif spec.kind == "path":
                    call_kwargs[spec.name] = params[spec.name]
                elif spec.kind == "form":
                    ann: type[FormRequest] = spec.annotation
                    try:
                        raw_body = await request.body()
                        if not raw_body.strip():
                            raise ValidationException(
                                {"body": ["Request body is required."]}
                            )
                        raw_data = await request.json()
                        if not isinstance(raw_data, dict):
                            raise ValidationException(
                                {"body": ["The request payload must be a JSON object."]}
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
                        if exc.status_code == 400:
                            raise ValidationException(
                                {"body": ["Invalid JSON payload"]}
                            ) from None
                        raise

                    rules = ann.get_parsed_rules(container=self.container)
                    result = await validate(data, rules)
                    if result.fails():
                        raise ValidationException(result.errors)

                    try:
                        instance = ann(
                            data=result.validated_data,
                            raw_data=data,
                            request=request,
                            validation_result=result,
                        )
                    except TypeError:
                        instance = self.container.resolve(ann)
                        instance.data = result.validated_data
                        instance.raw_data = data
                        instance.request = request
                        instance.validation_result = result

                    call_kwargs[spec.name] = instance
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

        return dispatched_handler

    def _wrap_websocket_handler(self, route: Route) -> Any:
        handler = route.handler
        param_specs = route.param_specs
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
          - If origin in allowed_origins: allow.
        If allowed_origins is not configured (default None):
          - Enforce same-origin: origin must match the request Host header or server host.
        """
        if origin is None:
            return True

        if self.allowed_origins is not None:
            if "*" in self.allowed_origins:
                return True
            clean_origin = origin.rstrip("/")
            if clean_origin in self.allowed_origins or origin in self.allowed_origins:
                return True
            origin_netloc = clean_origin.split("://", 1)[-1]
            return origin_netloc in self.allowed_origins

        effective_host = host_header
        if not effective_host and scope:
            server_info = scope.get("server")
            if server_info and len(server_info) == 2:
                s_host, s_port = server_info
                effective_host = f"{s_host}:{s_port}" if s_port not in (80, 443, None) else str(s_host)

        if not effective_host:
            return False

        clean_origin = origin.rstrip("/")
        origin_netloc = clean_origin.split("://", 1)[-1].lower()
        clean_host = effective_host.strip().lower()

        def _strip_default_port(h: str) -> str:
            if h.endswith(":80"):
                return h[:-3]
            if h.endswith(":443"):
                return h[:-4]
            return h

        return _strip_default_port(origin_netloc) == _strip_default_port(clean_host)

    def _compile_routes(self) -> None:
        for route in self.router.routes:
            if route.is_websocket or route.method == "WEBSOCKET":
                route.compiled_handler = self._wrap_websocket_handler(route)
            else:
                wrapped = self._wrap_handler(route)
                pipeline = MiddlewarePipeline(self.container, route.middleware)
                route.compiled_handler = pipeline.wrap(wrapped)

    def websocket(self, path: str) -> Callable[[F], F]:
        return self.router.websocket(path)

    def get(
        self, path: str, *, max_body_size: int | None = None
    ) -> Callable[[F], F]:
        return self.router.get(path, max_body_size=max_body_size)

    def post(
        self, path: str, *, max_body_size: int | None = None
    ) -> Callable[[F], F]:
        return self.router.post(path, max_body_size=max_body_size)

    def put(
        self, path: str, *, max_body_size: int | None = None
    ) -> Callable[[F], F]:
        return self.router.put(path, max_body_size=max_body_size)

    def patch(
        self, path: str, *, max_body_size: int | None = None
    ) -> Callable[[F], F]:
        return self.router.patch(path, max_body_size=max_body_size)

    def delete(
        self, path: str, *, max_body_size: int | None = None
    ) -> Callable[[F], F]:
        return self.router.delete(path, max_body_size=max_body_size)

    def head(
        self, path: str, *, max_body_size: int | None = None
    ) -> Callable[[F], F]:
        return self.router.head(path, max_body_size=max_body_size)

    def options(
        self, path: str, *, max_body_size: int | None = None
    ) -> Callable[[F], F]:
        return self.router.options(path, max_body_size=max_body_size)

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

            handler = route.compiled_handler
            if handler is None:
                wrapped = self._wrap_handler(route)
                handler = MiddlewarePipeline(self.container, route.middleware).wrap(
                    wrapped
                )
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

                set_container(self.container)
                connection = WebSocketConnection(
                    scope,
                    receive,
                    send,
                    max_message_size=self.max_ws_message_size,
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

            set_container(self.container)
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
                )
                Context.set("request", request)

                if self._middleware:
                    pipeline = MiddlewarePipeline(self.container, self._middleware)
                    entry = pipeline.wrap(self._dispatch_http)
                    response = await entry(request)
                else:
                    response = await self._dispatch_http(request)

            except ClientDisconnect:
                logger.debug("Client disconnected before request completed")
                return
            except Exception as exc:  # noqa: BLE001
                response = await self._handle_exception_safely(exc, request=request)

            finally:
                if scope.get("type") == "http":
                    await self.container.aclose_scoped()
                    Context.clear()

            if request is not None and request.method.upper() == "HEAD":
                response.content = b""

            try:
                await response.send(tracked_send)
            except Exception:
                logger.exception("Error while sending HTTP response")
                if not response_started:
                    fallback = json_response(
                        {"detail": "Internal Server Error"}, status_code=500
                    )
                    if request is not None and request.method.upper() == "HEAD":
                        fallback.content = b""
                    await fallback.send(tracked_send)

        except Exception:
            logger.exception("Fatal error in ASGI dispatch")
