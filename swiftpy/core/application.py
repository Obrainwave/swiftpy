from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any, TypeVar

from swiftpy.core.config import Config
from swiftpy.core.container import Container, set_container
from swiftpy.core.providers.base import ServiceProvider
from swiftpy.http.middleware import MiddlewarePipeline
from swiftpy.http.request import Request
from swiftpy.http.response import json_response, not_found, unprocessable
from swiftpy.http.router import PathParamCoercionError, RouteGroup, Router

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

        # 2. Register core singletons into container
        self.container.instance(Application, self)
        self.container.instance(Container, self.container)
        self.container.instance(Router, self.router)
        self.container.instance(Config, self.config)

        # 3. Bind active container globally
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

    def _compile_routes(self) -> None:
        for route in self.router.routes:
            pipeline = MiddlewarePipeline(self.container, route.middleware)
            route.compiled_handler = pipeline.wrap(route.handler)

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

            request = Request(scope, receive)
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
                handler = MiddlewarePipeline(self.container, route.middleware).wrap(
                    route.handler
                )

            response = await handler(request, **params)

        except Exception:
            logger.exception("Unhandled exception during request processing")

            response = json_response(
                {
                    "detail": "Internal Server Error",
                },
                status_code=500,
            )

        await response.send(send)
