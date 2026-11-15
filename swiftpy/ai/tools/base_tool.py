from __future__ import annotations

import asyncio
import enum
import functools
import inspect
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints, overload

_TOOL_NAME_PATTERN = re.compile(r"\A[a-zA-Z0-9_-]+\Z")


class BaseTool(ABC):
    """
    Abstract contract for executable tools in SwiftPY AI agents and tool loops.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """The unique name of the tool, matching ^[a-zA-Z0-9_-]+$."""

    @property
    @abstractmethod
    def description(self) -> str:
        """A detailed description of what the tool does and when the LLM should invoke it."""

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON Schema dictionary describing the parameters required by this tool."""

    @abstractmethod
    async def handle(self, *args: Any, **kwargs: Any) -> Any:
        """
        Execute the tool asynchronously with arguments provided by the model.
        """

    def to_dict(self) -> dict[str, Any]:
        """
        Format the tool schema as an OpenAI-compatible function definition.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic_dict(self) -> dict[str, Any]:
        """
        Format the tool schema as an Anthropic-compatible tool definition.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


def _infer_param_schema(ann: Any) -> dict[str, Any]:
    """Map a Python type annotation to a detailed JSON Schema dictionary."""
    if ann is inspect.Parameter.empty or ann is Any:
        return {"type": "string"}

    origin = get_origin(ann)
    args = get_args(ann)

    # Literal[...]
    if origin is Literal:
        values = list(args)
        val_type = "string"
        if values and all(isinstance(v, bool) for v in values):
            val_type = "boolean"
        elif values and all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            val_type = "integer"
        elif values and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            val_type = "number"
        return {"type": val_type, "enum": values}

    # Enum subclasses
    if isinstance(ann, type) and issubclass(ann, enum.Enum):
        enum_values = [e.value for e in ann]
        if issubclass(ann, enum.IntEnum) or (
            enum_values and all(isinstance(v, int) and not isinstance(v, bool) for v in enum_values)
        ):
            val_type = "integer"
        elif enum_values and all(isinstance(v, bool) for v in enum_values):
            val_type = "boolean"
        elif enum_values and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in enum_values):
            val_type = "number"
        else:
            val_type = "string"
        return {"type": val_type, "enum": enum_values}

    # Union & Optional (e.g. str | None, Optional[int], Union[int, str])
    if origin in (Union, UnionType):
        non_none_args = [a for a in args if a is not type(None)]
        is_nullable = len(non_none_args) < len(args)
        if len(non_none_args) == 1:
            schema = _infer_param_schema(non_none_args[0])
            if is_nullable:
                schema["nullable"] = True
            return schema
        schemas = [_infer_param_schema(a) for a in non_none_args]
        return {"anyOf": schemas, "nullable": is_nullable} if is_nullable else {"anyOf": schemas}

    # Arrays: list, tuple, set, Sequence
    if ann is list or origin in (list, set, tuple, Sequence):
        item_schema = _infer_param_schema(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": item_schema}

    # Objects: dict, Mapping
    if ann is dict or origin in (dict, Mapping):
        return {"type": "object"}

    # Primitives: Note that bool is a subclass of int in Python, so bool must be checked first!
    if ann is bool:
        return {"type": "boolean"}
    if ann is int:
        return {"type": "integer"}
    if ann is float:
        return {"type": "number"}
    if ann is str:
        return {"type": "string"}

    return {"type": "string"}


def _extract_enum_type(ann: Any) -> type[enum.Enum] | None:
    """Extract Enum subclass from direct annotations or Optional[Enum] unions."""
    if isinstance(ann, type) and issubclass(ann, enum.Enum):
        return ann
    origin = get_origin(ann)
    if origin in (Union, UnionType):
        non_none = [a for a in get_args(ann) if a is not type(None)]
        if len(non_none) == 1 and isinstance(non_none[0], type) and issubclass(non_none[0], enum.Enum):
            return non_none[0]
    return None


def _unwrap_partial_and_callable(fn: Callable[..., Any]) -> tuple[Callable[..., Any], Callable[..., Any]]:
    """Return (underlying_target, hint_and_doc_target) handling partials and callable instances."""
    base: Any = fn
    while isinstance(base, functools.partial):
        base = base.func
    hint_target = base
    if (
        not inspect.isfunction(base)
        and not inspect.ismethod(base)
        and not inspect.isbuiltin(base)
        and not isinstance(base, type)
        and callable(base)
    ):
        hint_target = base.__call__
    return base, hint_target


def is_async_callable(fn: Callable[..., Any]) -> bool:
    """Return True if a function, partial, or callable object defines an async entrypoint."""
    current: Any = fn
    while isinstance(current, functools.partial):
        current = current.func
    if inspect.iscoroutinefunction(current):
        return True
    if callable(current) and not inspect.isfunction(current) and not inspect.ismethod(current):
        return inspect.iscoroutinefunction(type(current).__call__)
    return False


def _validate_arg_type(
    param_name: str,
    value: Any,
    ann: Any,
    schema: Mapping[str, Any],
) -> None:
    """Validate a runtime parameter value recursively against declared type annotation and JSON schema."""
    origin = get_origin(ann)
    args = get_args(ann)
    schema_type = schema.get("type")
    is_nullable = (
        schema.get("nullable", False)
        or schema_type == "null"
        or (isinstance(schema_type, (list, tuple)) and "null" in schema_type)
        or (origin in (Union, UnionType) and type(None) in args)
    )

    if value is None:
        if is_nullable:
            return
        raise TypeError(f"Parameter '{param_name}' cannot be None.")

    # Check enum values (guarding bool == int equivalence in Python)
    if "enum" in schema:
        enum_vals = schema["enum"]
        raw_val = value.value if isinstance(value, enum.Enum) else value
        if not any(v == raw_val and isinstance(raw_val, bool) is isinstance(v, bool) for v in enum_vals):
            raise ValueError(
                f"Parameter '{param_name}' must be one of {enum_vals}, got {value!r}"
            )
        return

    # Check union types
    union_schemas = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(union_schemas, Sequence):
        matched = False
        for s in union_schemas:
            if isinstance(s, Mapping):
                try:
                    _validate_arg_type(param_name, value, Any, s)
                    matched = True
                    break
                except (TypeError, ValueError):
                    continue
        if not matched:
            raise TypeError(f"Parameter '{param_name}' does not match any allowed Union type.")
        return

    expected_type = schema.get("type")
    if expected_type == "boolean":
        if not isinstance(value, bool):
            raise TypeError(f"Parameter '{param_name}' must be a boolean, got {type(value).__name__}")
    elif expected_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"Parameter '{param_name}' must be an integer, got {type(value).__name__}")
    elif expected_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"Parameter '{param_name}' must be a number, got {type(value).__name__}")
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise ValueError(f"Parameter '{param_name}' must be a finite number, got {value!r}")
    elif expected_type == "string":
        if not isinstance(value, str):
            raise TypeError(f"Parameter '{param_name}' must be a string, got {type(value).__name__}")
    elif expected_type == "array":
        if not isinstance(value, (list, tuple, set)):
            raise TypeError(f"Parameter '{param_name}' must be an array, got {type(value).__name__}")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for idx, elem in enumerate(value):
                _validate_arg_type(f"{param_name}[{idx}]", elem, Any, item_schema)
    elif expected_type == "object":
        if not isinstance(value, Mapping):
            raise TypeError(f"Parameter '{param_name}' must be an object/dict, got {type(value).__name__}")
        validate_tool_arguments(schema, value, param_name)


def validate_tool_arguments(
    schema: Mapping[str, Any],
    arguments: Mapping[str, Any],
    tool_name: str = "",
) -> None:
    """Validate a tool argument mapping against a JSON Schema definition."""
    if not isinstance(arguments, Mapping):
        label = f"tool '{tool_name}'" if tool_name else "tool"
        raise TypeError(f"Expected object/dict arguments for {label}, got {type(arguments).__name__}")

    properties = schema.get("properties")
    prop_map: Mapping[str, Any] = properties if isinstance(properties, Mapping) else {}
    required = schema.get("required")
    if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
        for req in required:
            if isinstance(req, str) and req not in arguments:
                label = f" for '{tool_name}'" if tool_name else ""
                raise TypeError(f"Missing required parameter '{req}'{label}.")

    if schema.get("additionalProperties") is False:
        allowed_keys = set(prop_map.keys())
        for key in arguments:
            if key not in allowed_keys:
                raise TypeError(
                    f"Unexpected parameter '{key}'. Allowed parameters: {sorted(allowed_keys)}"
                )

    for key, val in arguments.items():
        if key in prop_map and isinstance(prop_map[key], Mapping):
            _validate_arg_type(str(key), val, Any, prop_map[key])


class FunctionTool(BaseTool):
    """
    Adapter converting a standard Python callable into a BaseTool instance
    with parameter schema inference, input validation, and asynchronous execution.
    """

    def __init__(
        self,
        fn: Callable[..., Any] | None = None,
        name: str | None = None,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
        *,
        func: Callable[..., Any] | None = None,
    ) -> None:
        target_fn = fn or func
        if target_fn is None:
            raise ValueError("FunctionTool requires a callable function.")
        self._fn = target_fn
        underlying, hint_target = _unwrap_partial_and_callable(target_fn)

        raw_name = str(
            name
            or getattr(target_fn, "__name__", None)
            or getattr(underlying, "__name__", None)
            or type(underlying).__name__
            or "tool"
        )
        if not _TOOL_NAME_PATTERN.fullmatch(raw_name):
            raise ValueError(f"Invalid tool name '{raw_name}'. Must match ^[a-zA-Z0-9_-]+$")

        self._name: str = raw_name
        doc = inspect.getdoc(target_fn) or inspect.getdoc(underlying) or f"Execute {self._name}"
        self._description = description or doc.strip()
        self._sig = inspect.signature(target_fn)
        self._hints = (
            get_type_hints(hint_target)
            if hasattr(hint_target, "__annotations__")
            else {}
        )

        if parameters is not None:
            self._parameters = parameters
        else:
            self._parameters = self._infer_parameters(target_fn, self._sig, self._hints)

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def handle(self, *args: Any, **kwargs: Any) -> Any:
        bound_kwargs = kwargs.copy()
        params_list = list(self._sig.parameters.values())
        positional_params = [
            p
            for p in params_list
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]

        # 1. Check for missing required parameters (accounting for supplied positional args)
        supplied_pos_names = {p.name for p in positional_params[: len(args)]}
        defaulted_names = {
            p.name for p in params_list if p.default is not inspect.Parameter.empty
        }
        for req in self._parameters.get("required", []):
            if (
                req not in bound_kwargs
                and req not in supplied_pos_names
                and req not in defaulted_names
            ):
                raise TypeError(f"Missing required parameter '{req}' for tool '{self.name}'.")

        # 2. Check for unexpected parameters if callable does not accept **kwargs
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params_list)
        if not has_var_kw:
            allowed = set(self._sig.parameters.keys())
            for k in bound_kwargs:
                if k not in allowed:
                    raise TypeError(
                        f"Unexpected parameter '{k}' for tool '{self.name}'. Allowed parameters: {sorted(allowed)}"
                    )

        # 3. Validate parameter types & coerce Enum arguments
        for k, v in list(bound_kwargs.items()):
            param = self._sig.parameters.get(k)
            if param is not None:
                ann = self._hints.get(k, param.annotation)
                schema = self._parameters.get("properties", {}).get(k, {})
                _validate_arg_type(k, v, ann, schema)
                enum_cls = _extract_enum_type(ann)
                if enum_cls is not None and v is not None and not isinstance(v, enum_cls):
                    matched = [
                        member
                        for member in enum_cls
                        if member.value == v and isinstance(member.value, bool) is isinstance(v, bool)
                    ]
                    if matched:
                        bound_kwargs[k] = matched[0]

        # 4. Safely bind arguments, mapping POSITIONAL_ONLY parameters passed via kwargs
        pos_only_params = [
            p for p in params_list if p.kind == inspect.Parameter.POSITIONAL_ONLY
        ]
        bind_pos_args = list(args)
        bind_kwargs = bound_kwargs.copy()
        if len(bind_pos_args) < len(pos_only_params):
            for p in pos_only_params[len(bind_pos_args) :]:
                if p.name in bind_kwargs:
                    bind_pos_args.append(bind_kwargs.pop(p.name))
                elif p.default is not inspect.Parameter.empty:
                    bind_pos_args.append(p.default)
                else:
                    break

        try:
            bound = self._sig.bind(*bind_pos_args, **bind_kwargs)
            bound.apply_defaults()
        except TypeError as exc:
            raise TypeError(f"Argument binding error for tool '{self.name}': {exc}") from exc

        # 5. Execute: await async callables directly, offload sync callables to worker thread
        if is_async_callable(self._fn):
            return await self._fn(*bound.args, **bound.kwargs)
        return await asyncio.to_thread(self._fn, *bound.args, **bound.kwargs)

    @staticmethod
    def _infer_parameters(
        fn: Callable[..., Any],
        sig: inspect.Signature,
        hints: dict[str, Any],
    ) -> dict[str, Any]:
        """Infer JSON schema parameters from signature and type hints."""
        properties: dict[str, Any] = {}
        required: list[str] = []

        for param_name, param in sig.parameters.items():
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue

            ann = hints.get(param_name, param.annotation)
            schema = _infer_param_schema(ann)
            schema["description"] = f"Parameter '{param_name}'"
            properties[param_name] = schema

            # A parameter is required if it has no default and is not explicitly optional/nullable
            if param.default is inspect.Parameter.empty and not schema.get("nullable", False):
                required.append(param_name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }


@overload
def tool(
    fn: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> FunctionTool: ...


@overload
def tool(
    fn: None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> Callable[[Callable[..., Any]], FunctionTool]: ...


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> FunctionTool | Callable[[Callable[..., Any]], FunctionTool]:
    """
    Decorator converting a Python function into a BaseTool instance.
    """
    def decorator(target: Callable[..., Any]) -> FunctionTool:
        return FunctionTool(
            target,
            name=name,
            description=description,
            parameters=parameters,
        )

    if fn is not None:
        return decorator(fn)
    return decorator
    return decorator
