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
from swiftpy.core.config import Config, ConfigurationError
from swiftpy.core.container import Container, set_container
from swiftpy.core.context import Context
from swiftpy.core.exceptions import ExceptionHandler
from swiftpy.core.providers.base import ServiceProvider
from swiftpy.http.exceptions import ClientDisconnect, HttpException
from swiftpy.http.middleware import Middleware, MiddlewarePipeline
from swiftpy.http.request import DEFAULT_MAX_BODY_SIZE, Request
from swiftpy.http.response import (
    Response,
    json_response,
    not_found,
)
from swiftpy.http.router import PathParamCoercionError, Route, RouteGroup, Router
from swiftpy.validation.exceptions import ValidationException
from swiftpy.validation.form_request import FormRequest
from swiftpy.validation.validator import validate
from swiftpy.websockets.connection import (
    WebSocketConnection,
    WebSocketDisconnect,
)
from swiftpy.websockets.manager import WebSocketManager

logger = logging.getLogger("swiftpy")

F = TypeVar("F", bound=Callable[..., Any])


def discover_providers(
    providers_path: str | Path | None = None,
) -> list[type[ServiceProvider]]:
    """
    Dynamically discover all ServiceProvider subclasses in the application providers directory.
    Defaults to `app/providers` relative to the current working directory.
    """
    project_root = Path.cwd()
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    target_dir = (
        Path(providers_path) if providers_path else project_root / "app" / "providers"
    )
    if not target_dir.is_absolute():
        target_dir = project_root / target_dir

    if not target_dir.is_dir():
        return []

    if str(target_dir) not in sys.path:
        sys.path.insert(0, str(target_dir))

    discovered: list[type[ServiceProvider]] = []

    for file in sorted(target_dir.glob("*.py")):
        if file.name.startswith(("_", ".")):
            continue

        try:
            rel_path = file.relative_to(project_root).with_suffix("")
            module_name = ".".join(rel_path.parts)
        except ValueError:
            module_name = file.stem

        try:
            mod = importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError):
            try:
                spec = spec_from_file_location(module_name, file)
                if spec and spec.loader:
                    mod = module_from_spec(spec)
                    sys.modules[module_name] = mod
                    spec.loader.exec_module(mod)
                else:
                    continue
            except (ImportError, AttributeError, ValueError, RuntimeError) as exc:
                logger.warning(
                    "Could not auto-load provider module %s: %s", module_name, exc
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
        debug: bool = False,
        config: dict[str, Any] | Config | None = None,
        providers: Sequence[type[ServiceProvider]] | None = None,
        middleware: Sequence[type[Middleware]] | None = None,
        auto_discover_providers: bool = True,
        providers_path: str | Path | None = None,
        max_body_size: int | None = None,
        **kwargs: Any,
    ) -> None:
        self.title = title
        self.version = version
        self.debug = debug
        self.extra = kwargs

        self.container = Container()
        self.router = Router(container=self.container)
        self._providers: list[ServiceProvider] = []
        self._middleware: list[type[Middleware]] = list(middleware or [])
        self._booted = False

        # 1. Load or bind configuration
        if config is None:
            self.config = Config.load("config")
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

        # 2. Configure debug mode and exception handler (refuse debug=True in production)
        app_env = (
            str(self.config.get("app.env") or self.config.get("env") or "development")
            .strip()
            .lower()
        )
        if not self.debug:
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
        self.websocket_manager = WebSocketManager.get_instance()
        self.container.instance(Application, self)
        self.container.instance(Container, self.container)
        self.container.instance(Router, self.router)
        self.container.instance(Config, self.config)
        self.container.instance(ExceptionHandler, self.exception_handler)
        self.container.instance(WebSocketManager, self.websocket_manager)
        self.container.scoped(
            CurrentUser,
            lambda _: CurrentUser(Context.get("__swiftpy_current_user__") or {}),
        )

        # 4. Bind active container globally
        set_container(self.container)

        # 4. Auto-discover and register service providers from app/providers
        if auto_discover_providers:
            discovered = discover_providers(providers_path)
            for provider_cls in discovered:
                self.register(provider_cls)

        # 5. Register explicitly passed service providers
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

                    rules = ann.get_rules()
                    result = await validate(data, rules)
                    if result.fails():
                        raise ValidationException(result.errors)

                    instance = ann(
                        data=result.validated_data,
                        raw_data=data,
                        request=request,
                        validation_result=result,
                    )
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
                            self.boot()
                            await send({"type": "lifespan.startup.complete"})
                        except Exception as exc:
                            logger.exception("Lifespan startup failed")
                            await send({"type": "lifespan.startup.failed", "message": str(exc)})
                            return
                    elif message["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return

            if scope["type"] == "websocket":
                if not self._booted:
                    self.boot()

                set_container(self.container)
                connection = WebSocketConnection(scope, receive, send)
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

                    handler = route.compiled_handler
                    if handler is None:
                        handler = self._wrap_websocket_handler(route)
                        route.compiled_handler = handler

                    try:
                        await handler(connection, **params)
                        if connection.is_connected:
                            await connection.close(code=1000)
                    except WebSocketDisconnect:
                        pass
                    except Exception:
                        logger.exception("Unhandled exception in WebSocket handler")
                        if connection.is_connected:
                            await connection.close(code=1011, reason="Internal Server Error")
                finally:
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
