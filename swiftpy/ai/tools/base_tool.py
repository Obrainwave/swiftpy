from __future__ import annotations

import asyncio
import enum
import inspect
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints, overload

_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


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
        return {"type": "string", "enum": [e.value for e in ann]}

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


def _validate_arg_type(param_name: str, value: Any, ann: Any, schema: dict[str, Any]) -> None:
    """Validate a runtime parameter value against declared type annotation and JSON schema."""
    origin = get_origin(ann)
    args = get_args(ann)
    is_nullable = schema.get("nullable", False) or (
        origin in (Union, UnionType) and type(None) in args
    )

    if value is None:
        if is_nullable:
            return
        raise TypeError(f"Parameter '{param_name}' cannot be None.")

    # Check enum values
    if "enum" in schema:
        enum_vals = schema["enum"]
        if value not in enum_vals:
            raise ValueError(
                f"Parameter '{param_name}' must be one of {enum_vals}, got {value!r}"
            )
        return

    # Check union types
    if "anyOf" in schema:
        # Value must match at least one of the schemas
        matched = False
        for s in schema["anyOf"]:
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
    elif expected_type == "string":
        if not isinstance(value, str):
            raise TypeError(f"Parameter '{param_name}' must be a string, got {type(value).__name__}")
    elif expected_type == "array":
        if not isinstance(value, (list, tuple, set)):
            raise TypeError(f"Parameter '{param_name}' must be an array, got {type(value).__name__}")
    elif expected_type == "object" and not isinstance(value, dict):
        raise TypeError(f"Parameter '{param_name}' must be an object/dict, got {type(value).__name__}")


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
        raw_name: str = str(name or getattr(target_fn, "__name__", "tool") or "tool")
        if not _TOOL_NAME_PATTERN.match(raw_name):
            raise ValueError(f"Invalid tool name '{raw_name}'. Must match ^[a-zA-Z0-9_-]+$")

        self._name: str = raw_name
        self._description = description or (inspect.getdoc(target_fn) or f"Execute {self._name}").strip()
        self._sig = inspect.signature(target_fn)
        try:
            self._hints = get_type_hints(target_fn)
        except (TypeError, NameError, AttributeError):
            self._hints = {}

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

        # 1. Check for missing required parameters
        for req in self._parameters.get("required", []):
            if req not in bound_kwargs and req not in [
                p.name for p in self._sig.parameters.values() if p.default is not inspect.Parameter.empty
            ]:
                raise TypeError(f"Missing required parameter '{req}' for tool '{self.name}'.")

        # 2. Check for unexpected parameters if callable does not accept **kwargs
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in self._sig.parameters.values())
        if not has_var_kw:
            allowed = set(self._sig.parameters.keys())
            for k in bound_kwargs:
                if k not in allowed:
                    raise TypeError(
                        f"Unexpected parameter '{k}' for tool '{self.name}'. Allowed parameters: {sorted(allowed)}"
                    )

        # 3. Validate parameter types against schemas
        for k, v in bound_kwargs.items():
            param = self._sig.parameters.get(k)
            if param is not None:
                ann = self._hints.get(k, param.annotation)
                schema = self._parameters.get("properties", {}).get(k, {})
                _validate_arg_type(k, v, ann, schema)

        # 4. Safely bind arguments (handles positional-only and default parameters)
        try:
            bound = self._sig.bind(*args, **bound_kwargs)
            bound.apply_defaults()
        except TypeError as exc:
            raise TypeError(f"Argument binding error for tool '{self.name}': {exc}") from exc

        # 5. Execute: offload synchronous callables to worker thread pool
        if inspect.iscoroutinefunction(self._fn):
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
