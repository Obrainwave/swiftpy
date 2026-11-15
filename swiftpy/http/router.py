from __future__ import annotations

import inspect
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar, get_args, get_origin, get_type_hints

from swiftpy.core.config import ConfigurationError
from swiftpy.core.container import Container
from swiftpy.core.exceptions import MethodNotAllowedError, ValidationError
from swiftpy.filesystem.uploads.uploaded_file import UploadedFile
from swiftpy.http.request import Request
from swiftpy.http.response import Response
from swiftpy.validation.form_request import FormRequest
from swiftpy.websockets.connection import WebSocket, WebSocketConnection

RouteHandler = Any
F = TypeVar("F", bound=Callable[..., Any])

_PARAM_TOKEN_RE = re.compile(r"\{([^{}]*)\}")

_BUILTIN_TYPES: dict[str, type[Any]] = {
    "int": int,
    "str": str,
    "float": float,
    "bool": bool,
    "bytes": bytes,
    "dict": dict,
    "list": list,
    "set": set,
    "tuple": tuple,
    "Any": Any,
}

ParamKind = Literal[
    "request", "path", "form", "di", "default", "websocket", "file", "files"
]


class RouteRegistrationError(ConfigurationError, TypeError):
    """Raised at route registration or compilation time when a route or handler is invalid."""


class PathParamCoercionError(ValidationError):
    """Raised when a URL path parameter cannot be coerced to its declared type."""

    def __init__(self, param: str, value: str, expected_type: type[Any]) -> None:
        self.param = param
        self.value = value
        self.expected_type = expected_type
        msg = (
            f"Path parameter '{param}' with value '{value}' could not be "
            f"converted to {expected_type.__name__}"
        )
        super().__init__(errors={param: [msg]}, message=msg)


def _coerce_bool(value: str) -> bool:
    norm = value.strip().lower()
    if norm in ("true", "1", "yes"):
        return True
    if norm in ("false", "0", "no"):
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _coerce_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"Non-finite float value: {value!r}")
    return parsed


def _collect_caller_locals(handler: Callable[..., Any]) -> dict[str, Any]:
    localns: dict[str, Any] = {}
    target_code = getattr(handler, "__code__", None)
    target_file = getattr(target_code, "co_filename", None)
    frame = inspect.currentframe()
    try:
        curr = frame.f_back if frame is not None else None
        depth = 0
        while curr is not None and depth < 8:
            if target_file is None or curr.f_code.co_filename == target_file:
                for k, v in curr.f_locals.items():
                    if k not in localns:
                        localns[k] = v
            curr = curr.f_back
            depth += 1
    finally:
        del frame
    return localns


def _resolve_handler_type_hints(
    handler: Callable[..., Any],
    sig: inspect.Signature,
    extra_locals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged_locals: dict[str, Any] = dict(_BUILTIN_TYPES)
    if extra_locals:
        merged_locals.update(extra_locals)
    try:
        closure_vars = inspect.getclosurevars(handler).nonlocals
        merged_locals.update(closure_vars)
    except (TypeError, ValueError):
        pass

    globalns: dict[str, Any] = getattr(handler, "__globals__", {})

    try:
        hints = get_type_hints(handler, globalns=globalns, localns=merged_locals)
    except (NameError, TypeError, ValueError):
        hints = {}

    resolved: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        ann = hints.get(name, param.annotation)
        if isinstance(ann, str):
            if ann in merged_locals:
                ann = merged_locals[ann]
            elif ann in globalns:
                ann = globalns[ann]
        resolved[name] = ann
    return resolved


@dataclass(slots=True, frozen=True)
class ParamSpec:
    name: str
    kind: ParamKind
    annotation: Any
    default: Any = inspect.Parameter.empty


@dataclass(slots=True)
class Route:
    method: str
    path: str
    handler: RouteHandler
    middleware: list[type[Any]]
    is_websocket: bool = False
    order: int = 0
    max_body_size: int | None = None
    max_content_length: int | None = None
    max_json_depth: int | None = None
    localns: dict[str, Any] | None = field(default=None, repr=False)
    pattern: re.Pattern[str] = field(init=False)
    param_names: tuple[str, ...] = field(init=False)
    normalized_path: str = field(init=False)
    precedence_key: tuple[int, tuple[int, ...], int, int] = field(init=False)
    signature: inspect.Signature = field(init=False, repr=False)
    type_hints: dict[str, Any] = field(init=False, repr=False)
    param_coercers: dict[str, tuple[type[Any], Callable[[str], Any]]] = field(
        init=False, repr=False
    )
    param_specs: tuple[ParamSpec, ...] = field(default=(), init=False, repr=False)
    has_var_keyword: bool = field(default=False, init=False, repr=False)
    compiled_handler: RouteHandler | None = field(default=None, init=False, repr=False)
    analysis_deferred: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.method = self.method.upper()
        if not self.path.startswith("/"):
            raise RouteRegistrationError(
                f"Route path must start with '/': {self.path!r}"
            )

        regex_parts: list[str] = []
        normalized_parts: list[str] = []
        param_names: list[str] = []
        last_idx = 0

        for match in _PARAM_TOKEN_RE.finditer(self.path):
            literal = self.path[last_idx : match.start()]
            if "{" in literal or "}" in literal:
                raise RouteRegistrationError(
                    f"Malformed route path '{self.path}': unmatched brace."
                )
            regex_parts.append(re.escape(literal))
            normalized_parts.append(literal)

            param_name = match.group(1)
            if not param_name or not param_name.isidentifier():
                raise RouteRegistrationError(
                    f"Invalid path parameter name '{param_name}' in route '{self.path}'."
                )
            if param_name in param_names:
                raise RouteRegistrationError(
                    f"Duplicate path parameter '{param_name}' in route '{self.path}'."
                )
            param_names.append(param_name)
            regex_parts.append(f"(?P<{param_name}>[^/]+)")
            normalized_parts.append("{}")
            last_idx = match.end()

        tail = self.path[last_idx:]
        if "{" in tail or "}" in tail:
            raise RouteRegistrationError(
                f"Malformed route path '{self.path}': unmatched brace."
            )
        regex_parts.append(re.escape(tail))
        normalized_parts.append(tail)

        self.pattern = re.compile(f"^{''.join(regex_parts)}$")
        self.param_names = tuple(param_names)
        self.normalized_path = "".join(normalized_parts)

        segments = [seg for seg in self.path.split("/") if seg]
        segment_kinds = tuple(
            1 if _PARAM_TOKEN_RE.search(seg) else 0 for seg in segments
        )
        num_dynamic = sum(segment_kinds)
        static_chars = sum(len(_PARAM_TOKEN_RE.sub("", seg)) for seg in segments)
        self.precedence_key = (
            num_dynamic,
            segment_kinds,
            -static_chars,
            self.order,
        )

        self.signature = inspect.signature(self.handler)
        self.type_hints = _resolve_handler_type_hints(
            self.handler,
            self.signature,
            self.localns,
        )
        self.has_var_keyword = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in self.signature.parameters.values()
        )

        coercers: dict[str, tuple[type[Any], Callable[[str], Any]]] = {}
        for p_name in self.param_names:
            ann = self.type_hints.get(p_name, inspect.Parameter.empty)
            if ann is bool:
                coercers[p_name] = (bool, _coerce_bool)
            elif ann is int:
                coercers[p_name] = (int, int)
            elif ann is float:
                coercers[p_name] = (float, _coerce_float)
            elif ann is str or ann is inspect.Parameter.empty or ann is Any:
                coercers[p_name] = (str, str)
            elif isinstance(ann, type) and callable(ann):
                coercers[p_name] = (ann, ann)
            else:
                coercers[p_name] = (str, str)
        self.param_coercers = coercers


def _normalize_route_prefix(prefix: str) -> str:
    """Validate and normalize a router/group prefix (empty string for root `'/'`)."""
    clean_prefix = prefix.strip()
    if not clean_prefix or clean_prefix == "/":
        return ""
    if any(ch.isspace() for ch in clean_prefix):
        raise RouteRegistrationError(
            f"Router prefix must not contain whitespace: {prefix!r}"
        )
    if any(token in clean_prefix for token in ("{", "}")):
        raise RouteRegistrationError(
            f"Router prefix must be a literal path, not a dynamic pattern: {prefix!r}"
        )
    if "//" in clean_prefix:
        raise RouteRegistrationError(
            f"Router prefix must not contain empty path segments: {prefix!r}"
        )
    stripped = clean_prefix.strip("/")
    return f"/{stripped}" if stripped else ""


def _join_route_path(prefix: str, child_path: str) -> str:
    """Join a normalized prefix with a child route path while preserving trailing slashes."""
    clean_prefix = prefix.strip("/")
    base = f"/{clean_prefix}" if clean_prefix else ""

    if child_path in ("", "/"):
        return base or "/"

    stripped_child = child_path.strip("/")
    if not stripped_child:
        return base or "/"

    suffix = f"{stripped_child}/" if child_path.endswith("/") else stripped_child
    return f"{base}/{suffix}"


class RouteGroup:
    def __init__(
        self,
        prefix: str,
        router: Router,
        middleware: list[type[Any]] | None = None,
    ) -> None:
        normalized = _normalize_route_prefix(prefix)
        self.prefix = normalized or "/"
        self._normalized_prefix = normalized
        self.router = router
        self.middleware = middleware or []

    def _add_route(
        self,
        method: str,
        path: str,
        handler: RouteHandler,
        *,
        is_websocket: bool = False,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
        localns: dict[str, Any] | None = None,
    ) -> None:
        full_path = _join_route_path(self._normalized_prefix, path)
        self.router.add_route(
            method=method,
            path=full_path,
            handler=handler,
            middleware=list(self.middleware),
            is_websocket=is_websocket,
            max_body_size=max_body_size,
            max_content_length=max_content_length,
            max_json_depth=max_json_depth,
            localns=localns,
        )

    def get(
        self,
        path: str,
        *,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            self._add_route(
                "GET",
                path,
                func,
                max_body_size=max_body_size,
                max_content_length=max_content_length,
                max_json_depth=max_json_depth,
                localns=_collect_caller_locals(func),
            )
            return func

        return decorator

    def post(
        self,
        path: str,
        *,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            self._add_route(
                "POST",
                path,
                func,
                max_body_size=max_body_size,
                max_content_length=max_content_length,
                max_json_depth=max_json_depth,
                localns=_collect_caller_locals(func),
            )
            return func

        return decorator

    def put(
        self,
        path: str,
        *,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            self._add_route(
                "PUT",
                path,
                func,
                max_body_size=max_body_size,
                max_content_length=max_content_length,
                max_json_depth=max_json_depth,
                localns=_collect_caller_locals(func),
            )
            return func

        return decorator

    def patch(
        self,
        path: str,
        *,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            self._add_route(
                "PATCH",
                path,
                func,
                max_body_size=max_body_size,
                max_content_length=max_content_length,
                max_json_depth=max_json_depth,
                localns=_collect_caller_locals(func),
            )
            return func

        return decorator

    def delete(
        self,
        path: str,
        *,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            self._add_route(
                "DELETE",
                path,
                func,
                max_body_size=max_body_size,
                max_content_length=max_content_length,
                max_json_depth=max_json_depth,
                localns=_collect_caller_locals(func),
            )
            return func

        return decorator

    def head(
        self,
        path: str,
        *,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            self._add_route(
                "HEAD",
                path,
                func,
                max_body_size=max_body_size,
                max_content_length=max_content_length,
                max_json_depth=max_json_depth,
                localns=_collect_caller_locals(func),
            )
            return func

        return decorator

    def options(
        self,
        path: str,
        *,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            self._add_route(
                "OPTIONS",
                path,
                func,
                max_body_size=max_body_size,
                max_content_length=max_content_length,
                max_json_depth=max_json_depth,
                localns=_collect_caller_locals(func),
            )
            return func

        return decorator

    def websocket(self, path: str) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            self._add_route(
                "WEBSOCKET",
                path,
                func,
                is_websocket=True,
                localns=_collect_caller_locals(func),
            )
            return func

        return decorator


class Router:
    """
    HTTP and WebSocket router with static-first precedence, precomputed handler
    signatures, and strict path parameter coercion.
    Trailing-slash policy: redirect-free exact match (no silent normalization).
    """

    def __init__(self, container: Container | None = None) -> None:
        self.container = container
        self.routes: list[Route] = []
        self._http_routes: list[Route] = []
        self._routes_by_method: dict[str, list[Route]] = {}
        self._websocket_routes: list[Route] = []
        self._static_routes: dict[str, dict[str, Route]] = {}
        self._static_websocket_routes: dict[str, Route] = {}
        self._dynamic_routes_by_method: dict[str, list[Route]] = {}
        self._dynamic_websocket_routes: list[Route] = []
        self._registered_keys: set[tuple[str, str]] = set()
        self._counter = 0

    def _can_resolve_di(
        self,
        annotation: Any,
        container: Container | None = None,
    ) -> bool:
        target_container = container if container is not None else self.container
        if target_container is None:
            return False
        return target_container.can_resolve(annotation)

    def _analyze_handler(
        self,
        route: Route,
        *,
        container: Container | None = None,
    ) -> tuple[ParamSpec, ...]:
        specs: list[ParamSpec] = []
        ws_assigned = False

        for name, param in route.signature.parameters.items():
            if param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue

            ann = route.type_hints.get(name, param.annotation)

            if route.is_websocket:
                is_ws_type = (
                    ann is WebSocketConnection
                    or ann is WebSocket
                    or (isinstance(ann, type) and issubclass(ann, WebSocketConnection))
                )
                if is_ws_type or (
                    not ws_assigned
                    and name in ("websocket", "ws", "conn", "connection")
                    and (ann is inspect.Parameter.empty or ann is Any)
                ):
                    ws_assigned = True
                    specs.append(
                        ParamSpec(name=name, kind="websocket", annotation=ann)
                    )
                elif name in route.param_names:
                    specs.append(ParamSpec(name=name, kind="path", annotation=ann))
                elif self._can_resolve_di(ann, container=container):
                    specs.append(ParamSpec(name=name, kind="di", annotation=ann))
                elif param.default is not inspect.Parameter.empty:
                    specs.append(
                        ParamSpec(
                            name=name,
                            kind="default",
                            annotation=ann,
                            default=param.default,
                        )
                    )
                else:
                    raise RouteRegistrationError(
                        f"WebSocket handler '{route.handler.__qualname__}' has "
                        f"unsatisfiable parameter '{name}' (annotation: {ann!r})."
                    )
                continue

            if isinstance(ann, type) and issubclass(ann, Request):
                specs.append(ParamSpec(name=name, kind="request", annotation=ann))
            elif isinstance(ann, type) and issubclass(ann, UploadedFile):
                specs.append(ParamSpec(name=name, kind="file", annotation=ann))
            elif (
                get_origin(ann) is list
                and get_args(ann)
                and isinstance(get_args(ann)[0], type)
                and issubclass(get_args(ann)[0], UploadedFile)
            ):
                specs.append(ParamSpec(name=name, kind="files", annotation=ann))
            elif name in route.param_names:
                specs.append(ParamSpec(name=name, kind="path", annotation=ann))
            elif isinstance(ann, type) and issubclass(ann, FormRequest):
                specs.append(ParamSpec(name=name, kind="form", annotation=ann))
            elif self._can_resolve_di(ann, container=container):
                specs.append(ParamSpec(name=name, kind="di", annotation=ann))
            elif param.default is not inspect.Parameter.empty:
                specs.append(
                    ParamSpec(
                        name=name,
                        kind="default",
                        annotation=ann,
                        default=param.default,
                    )
                )
            else:
                raise RouteRegistrationError(
                    f"Handler '{route.handler.__qualname__}' has unsatisfiable "
                    f"parameter '{name}' (annotation: {ann!r}). Route handler "
                    "parameters must be path parameters, Request, FormRequest "
                    "subclasses, or container-resolvable types."
                )

        return tuple(specs)

    def ensure_route_analyzed(
        self,
        route: Route,
        *,
        container: Container | None = None,
    ) -> tuple[ParamSpec, ...]:
        """Resolve deferred handler parameter specs once a DI container is available."""
        if route.analysis_deferred:
            target = container or self.container or Container()
            route.param_specs = self._analyze_handler(route, container=target)
            route.analysis_deferred = False
        return route.param_specs

    def add_compiled_route(self, route: Route) -> Route:
        """Register an already-compiled Route object into all routing indexes."""
        key = (route.method, route.normalized_path)
        self._registered_keys.add(key)
        if route not in self.routes:
            self.routes.append(route)

        is_static = not bool(route.param_names)
        if route.is_websocket:
            if route not in self._websocket_routes:
                self._websocket_routes.append(route)
                self._websocket_routes.sort(key=lambda r: r.precedence_key)
            if is_static:
                self._static_websocket_routes[route.path] = route
            else:
                if route not in self._dynamic_websocket_routes:
                    self._dynamic_websocket_routes.append(route)
                    self._dynamic_websocket_routes.sort(key=lambda r: r.precedence_key)
        else:
            if route not in self._http_routes:
                self._http_routes.append(route)
                self._http_routes.sort(key=lambda r: r.precedence_key)
            method_list = self._routes_by_method.setdefault(route.method, [])
            if route not in method_list:
                method_list.append(route)
                method_list.sort(key=lambda r: r.precedence_key)
            if is_static:
                self._static_routes.setdefault(route.method, {})[route.path] = route
            else:
                dyn_list = self._dynamic_routes_by_method.setdefault(route.method, [])
                if route not in dyn_list:
                    dyn_list.append(route)
                    dyn_list.sort(key=lambda r: r.precedence_key)

        return route

    def add_route(
        self,
        method: str,
        path: str,
        handler: RouteHandler,
        middleware: list[type[Any]] | None = None,
        *,
        is_websocket: bool = False,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
        localns: dict[str, Any] | None = None,
    ) -> Route:
        norm_method = method.upper().strip()
        if not norm_method:
            raise RouteRegistrationError("HTTP method must not be empty.")
        if norm_method == "WEBSOCKET" and not is_websocket:
            raise RouteRegistrationError(
                "Method 'WEBSOCKET' is internal-only; use websocket() to register WebSocket routes."
            )

        effective_locals = (
            localns if localns is not None else _collect_caller_locals(handler)
        )
        route = Route(
            method="WEBSOCKET" if is_websocket else norm_method,
            path=path,
            handler=handler,
            middleware=middleware or [],
            is_websocket=is_websocket,
            order=self._counter,
            max_body_size=max_body_size,
            max_content_length=max_content_length,
            max_json_depth=max_json_depth,
            localns=effective_locals,
        )

        key = (route.method, route.normalized_path)
        if key in self._registered_keys:
            raise RouteRegistrationError(
                f"Duplicate route registered for {route.method} {path}"
            )

        if self.container is None:
            # Standalone routers may declare app-bound DI dependencies before
            # mounting onto an Application; defer strict DI checks until include_router().
            route.analysis_deferred = True
        else:
            route.param_specs = self._analyze_handler(route)
        self._counter += 1
        return self.add_compiled_route(route)

    def route(
        self,
        method: str,
        path: str,
        *,
        middleware: list[type[Any]] | None = None,
        is_websocket: bool = False,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        if method.upper().strip() == "WEBSOCKET" and not is_websocket:
            raise RouteRegistrationError(
                "Method 'WEBSOCKET' is internal-only; use websocket() to register WebSocket routes."
            )

        def decorator(func: F) -> F:
            self.add_route(
                method,
                path,
                func,
                middleware=middleware,
                is_websocket=is_websocket,
                max_body_size=max_body_size,
                max_content_length=max_content_length,
                max_json_depth=max_json_depth,
                localns=_collect_caller_locals(func),
            )
            return func

        return decorator

    def get(
        self,
        path: str,
        *,
        middleware: list[type[Any]] | None = None,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        return self.route(
            "GET",
            path,
            middleware=middleware,
            max_body_size=max_body_size,
            max_content_length=max_content_length,
            max_json_depth=max_json_depth,
        )

    def post(
        self,
        path: str,
        *,
        middleware: list[type[Any]] | None = None,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        return self.route(
            "POST",
            path,
            middleware=middleware,
            max_body_size=max_body_size,
            max_content_length=max_content_length,
            max_json_depth=max_json_depth,
        )

    def put(
        self,
        path: str,
        *,
        middleware: list[type[Any]] | None = None,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        return self.route(
            "PUT",
            path,
            middleware=middleware,
            max_body_size=max_body_size,
            max_content_length=max_content_length,
            max_json_depth=max_json_depth,
        )

    def patch(
        self,
        path: str,
        *,
        middleware: list[type[Any]] | None = None,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        return self.route(
            "PATCH",
            path,
            middleware=middleware,
            max_body_size=max_body_size,
            max_content_length=max_content_length,
            max_json_depth=max_json_depth,
        )

    def delete(
        self,
        path: str,
        *,
        middleware: list[type[Any]] | None = None,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        return self.route(
            "DELETE",
            path,
            middleware=middleware,
            max_body_size=max_body_size,
            max_content_length=max_content_length,
            max_json_depth=max_json_depth,
        )

    def head(
        self,
        path: str,
        *,
        middleware: list[type[Any]] | None = None,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        return self.route(
            "HEAD",
            path,
            middleware=middleware,
            max_body_size=max_body_size,
            max_content_length=max_content_length,
            max_json_depth=max_json_depth,
        )

    def options(
        self,
        path: str,
        *,
        middleware: list[type[Any]] | None = None,
        max_body_size: int | None = None,
        max_content_length: int | None = None,
        max_json_depth: int | None = None,
    ) -> Callable[[F], F]:
        return self.route(
            "OPTIONS",
            path,
            middleware=middleware,
            max_body_size=max_body_size,
            max_content_length=max_content_length,
            max_json_depth=max_json_depth,
        )

    def websocket(
        self,
        path: str,
        *,
        middleware: list[type[Any]] | None = None,
    ) -> Callable[[F], F]:
        return self.route(
            "WEBSOCKET",
            path,
            middleware=middleware,
            is_websocket=True,
        )

    @staticmethod
    def _coerce_matched_params(
        route: Route, raw_params: dict[str, str]
    ) -> dict[str, Any]:
        coerced: dict[str, Any] = {}
        for name, raw_val in raw_params.items():
            coercer_entry = route.param_coercers.get(name)
            if coercer_entry is None:
                coerced[name] = raw_val
                continue
            expected_type, coercer = coercer_entry
            try:
                coerced[name] = coercer(raw_val)
            except (ValueError, TypeError):
                raise PathParamCoercionError(name, raw_val, expected_type) from None
        return coerced

    def match_websocket(
        self,
        path: str,
    ) -> tuple[Route | None, dict[str, Any]]:
        static_route = self._static_websocket_routes.get(path)
        if static_route is not None:
            return static_route, {}

        for route in self._dynamic_websocket_routes:
            m = route.pattern.match(path)
            if m is not None:
                return route, self._coerce_matched_params(route, m.groupdict())
        return None, {}

    def match(
        self,
        method: str,
        path: str,
    ) -> tuple[
        Route | None,
        dict[str, Any],
    ]:
        norm_method = method.upper()

        # 1. Never match WEBSOCKET routes over HTTP
        if norm_method != "WEBSOCKET":
            # Fast path: O(1) exact static lookup
            static_candidates = self._static_routes.get(norm_method)
            if static_candidates is not None and path in static_candidates:
                return static_candidates[path], {}

            dynamic_candidates = self._dynamic_routes_by_method.get(norm_method, ())
            for route in dynamic_candidates:
                m = route.pattern.match(path)
                if m is not None:
                    return route, self._coerce_matched_params(route, m.groupdict())

            # 2. Automatic HEAD fallback to GET
            if norm_method == "HEAD":
                static_get = self._static_routes.get("GET")
                if static_get is not None and path in static_get:
                    return static_get[path], {}
                get_candidates = self._dynamic_routes_by_method.get("GET", ())
                for route in get_candidates:
                    m = route.pattern.match(path)
                    if m is not None:
                        return route, self._coerce_matched_params(route, m.groupdict())

        # 3. Check if any HTTP methods match this path (for automatic OPTIONS or 405)
        matched_methods: set[str] = set()
        matched_routes: list[Route] = []
        for meth, static_map in self._static_routes.items():
            if meth != "WEBSOCKET" and path in static_map:
                matched_methods.add(meth)
                matched_routes.append(static_map[path])

        for route in self._http_routes:
            if route.param_names and route.pattern.match(path) is not None:
                matched_methods.add(route.method)
                matched_routes.append(route)

        if matched_methods:
            if "GET" in matched_methods:
                matched_methods.add("HEAD")

            if norm_method == "OPTIONS":
                options_methods = sorted(matched_methods | {"OPTIONS"})
                allow_header = ", ".join(options_methods)
                inherited_middleware = list(
                    dict.fromkeys(
                        mw for matched_route in matched_routes for mw in matched_route.middleware
                    )
                )

                async def _options_handler(request: Request) -> Response:
                    return Response(
                        b"",
                        status_code=204,
                        headers={"Allow": allow_header},
                    )

                synthetic_route = Route(
                    method="OPTIONS",
                    path="/",
                    handler=_options_handler,
                    middleware=inherited_middleware,
                )
                synthetic_route.param_specs = self._analyze_handler(
                    synthetic_route, container=self.container
                )
                return synthetic_route, {}

            unique_methods = sorted(matched_methods)
            allow_header = ", ".join(unique_methods)
            raise MethodNotAllowedError(
                f"Method '{method}' not allowed for path '{path}'. Allowed: {allow_header}",
                headers={"Allow": allow_header},
            )

        return None, {}

    def group(
        self,
        prefix: str,
        middleware: list[type[Any]] | None = None,
    ) -> Callable[[Callable[[RouteGroup], None]], Callable[[RouteGroup], None]]:
        def decorator(
            func: Callable[[RouteGroup], None],
        ) -> Callable[[RouteGroup], None]:
            group = RouteGroup(
                router=self,
                prefix=prefix,
                middleware=middleware,
            )
            func(group)
            return func

        return decorator

    def include_router(
        self,
        router: Router,
        prefix: str = "",
        middleware: list[type[Any]] | None = None,
    ) -> None:
        """
        Include routes from another router instance under an optional path prefix and middleware.
        """
        clean_prefix = _normalize_route_prefix(prefix)

        parent_mw = list(middleware or [])

        for child_route in router.routes:
            full_path = _join_route_path(clean_prefix, child_route.path)

            combined_middleware = parent_mw + list(child_route.middleware)

            self.add_route(
                method=child_route.method,
                path=full_path,
                handler=child_route.handler,
                middleware=combined_middleware,
                is_websocket=child_route.is_websocket,
                max_body_size=child_route.max_body_size,
                max_content_length=child_route.max_content_length,
                max_json_depth=child_route.max_json_depth,
                localns=child_route.localns,
            )
