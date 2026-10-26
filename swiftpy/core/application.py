from __future__ import annotations

import importlib
import inspect
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any, TypeVar, get_type_hints

from swiftpy.core.config import Config
from swiftpy.core.container import Container, set_container
from swiftpy.core.context import Context
from swiftpy.core.exceptions import ExceptionHandler
from swiftpy.core.providers.base import ServiceProvider
from swiftpy.http.middleware import MiddlewarePipeline
from swiftpy.http.request import Request
from swiftpy.http.response import (
    not_found,
    unprocessable,
)
from swiftpy.http.router import PathParamCoercionError, RouteGroup, Router
from swiftpy.validation.exceptions import ValidationException
from swiftpy.validation.form_request import FormRequest
from swiftpy.validation.validator import validate

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
        auto_discover_providers: bool = True,
        providers_path: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        self.title = title
        self.version = version
        self.debug = debug
        self.extra = kwargs

        self.container = Container()
        self.router = Router()
        self._providers: list[ServiceProvider] = []
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

        # 2. Configure debug mode and exception handler
        if not self.debug:
            self.debug = bool(self.config.get("app.debug", False))
        self.exception_handler = ExceptionHandler(debug=self.debug)

        # 3. Register core singletons into container
        self.container.instance(Application, self)
        self.container.instance(Container, self.container)
        self.container.instance(Router, self.router)
        self.container.instance(Config, self.config)
        self.container.instance(ExceptionHandler, self.exception_handler)

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

    def _wrap_handler(self, handler: Any) -> Any:
        sig = inspect.signature(handler)
        typed_params: list[tuple[str, type[Any]]] = []

        try:
            type_hints = get_type_hints(handler)
        except (NameError, TypeError, ValueError):
            type_hints = {}

        for name, param in sig.parameters.items():
            ann = type_hints.get(name, param.annotation)
            if isinstance(ann, str):
                try:
                    closure_vars = inspect.getclosurevars(handler).nonlocals
                    if ann in closure_vars:
                        ann = closure_vars[ann]
                except (TypeError, ValueError):
                    pass
                if isinstance(ann, str):
                    ann = getattr(handler, "__globals__", {}).get(ann, ann)

            if isinstance(ann, type) and (
                issubclass(ann, FormRequest)
                or hasattr(ann, "schema")
                or hasattr(ann, "rules")
            ):
                typed_params.append((name, ann))

        async def dispatched_handler(
            request: Request, *args: Any, **params: Any
        ) -> Any:
            call_kwargs: dict[str, Any] = {}
            for name, param in sig.parameters.items():
                param_ann = type_hints.get(name, param.annotation)
                if (
                    name == "request"
                    or param_ann is Request
                    or param.annotation is Request
                ):
                    call_kwargs[name] = request
                elif name in params:
                    call_kwargs[name] = params[name]
                elif any(name == tp[0] for tp in typed_params):
                    ann = next(tp[1] for tp in typed_params if tp[0] == name)
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

                    if issubclass(ann, FormRequest):
                        rules = ann.get_rules()
                    elif hasattr(ann, "schema") and isinstance(ann.schema, dict):
                        rules = ann.schema
                    elif hasattr(ann, "rules") and callable(ann.rules):
                        try:
                            rules = ann().rules()
                        except TypeError:
                            rules = ann.rules()
                    else:
                        rules = {}

                    result = await validate(data, rules)
                    if result.fails():
                        raise ValidationException(result.errors)

                    if issubclass(ann, FormRequest):
                        instance = ann(
                            data=result.validated_data,
                            raw_data=data,
                            request=request,
                            validation_result=result,
                        )
                    else:
                        try:
                            instance = ann(**result.validated_data)
                        except TypeError:
                            instance = ann(result.validated_data)
                    call_kwargs[name] = instance

            has_var_keyword = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            if has_var_keyword:
                for k, v in params.items():
                    if k not in call_kwargs:
                        call_kwargs[k] = v

            if not call_kwargs and sig.parameters:
                first_param = next(iter(sig.parameters.values()))
                if first_param.kind in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                ):
                    call_kwargs[first_param.name] = request
                for k, v in params.items():
                    if k in sig.parameters:
                        call_kwargs[k] = v

            if inspect.iscoroutinefunction(handler):
                return await handler(**call_kwargs)
            res = handler(**call_kwargs)
            if inspect.isawaitable(res):
                return await res
            return res

        return dispatched_handler

    def _compile_routes(self) -> None:
        for route in self.router.routes:
            wrapped = self._wrap_handler(route.handler)
            pipeline = MiddlewarePipeline(self.container, route.middleware)
            route.compiled_handler = pipeline.wrap(wrapped)

    def get(self, path: str) -> Callable[[F], F]:
        return self.router.get(path)

    def post(self, path: str) -> Callable[[F], F]:
        return self.router.post(path)

    def put(self, path: str) -> Callable[[F], F]:
        return self.router.put(path)

    def patch(self, path: str) -> Callable[[F], F]:
        return self.router.patch(path)

    def delete(self, path: str) -> Callable[[F], F]:
        return self.router.delete(path)

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
                        self.boot()
                        await send({"type": "lifespan.startup.complete"})
                    elif message["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return

            if scope["type"] != "http":
                return

            if not self._booted:
                self.boot()

            set_container(self.container)
            request: Request | None = None
            try:
                request = Request(scope, receive)
                Context.set("request", request)
                try:
                    route, params = self.router.match(request.method, request.path)
                except PathParamCoercionError as exc:
                    response = unprocessable({exc.param: str(exc)})
                    await response.send(send)
                    return

                if route is None:
                    response = not_found()
                    await response.send(send)
                    return

                handler = route.compiled_handler

                if handler is None:
                    # Route matched before boot() ran, e.g. ad hoc testing.
                    # Not the production hot path, so rebuilding here is acceptable.
                    wrapped = self._wrap_handler(route.handler)
                    handler = MiddlewarePipeline(self.container, route.middleware).wrap(
                        wrapped
                    )

                response = await handler(request, **params)

            except Exception as exc:  # noqa: BLE001
                response = await self.exception_handler.handle(exc, request=request)

            finally:
                if scope.get("type") == "http":
                    Context.clear()

            await response.send(send)

        except Exception:
            logger.exception("Fatal error in ASGI dispatch")
