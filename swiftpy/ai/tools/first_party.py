from __future__ import annotations

import ast
import asyncio
import datetime
import math
import operator
import zoneinfo
from collections.abc import Callable
from typing import Any

from swiftpy.ai.exceptions import AIConfigurationError
from swiftpy.ai.tools.base_tool import BaseTool, is_async_callable

# ==============================================================================
# 1. Calculator Tool (AST-Safe Evaluation, Resource Bounds, Zero eval)
# ==============================================================================

_SAFE_OPERATORS: dict[type[ast.AST], Callable[..., Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_sqrt(x: float) -> float:
    if x < 0:
        raise ValueError("Math domain error: square root of negative number is undefined in real numbers")
    return math.sqrt(x)


_SAFE_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "round": round,
    "sqrt": _safe_sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "ceil": math.ceil,
    "floor": math.floor,
    "log": math.log,
    "exp": math.exp,
}


def _safe_eval_node(node: ast.AST, depth: int = 0, max_depth: int = 20) -> Any:
    """Recursively evaluate an AST expression node using strictly whitelisted safe operations and bounds."""
    if depth > max_depth:
        raise ValueError(f"Expression AST depth exceeds maximum allowed limit ({max_depth})")

    if isinstance(node, ast.Expression):
        return _safe_eval_node(node.body, depth + 1, max_depth)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            if math.isnan(node.value) or math.isinf(node.value):
                raise ValueError("Non-finite numeric constants (NaN or Infinity) are not permitted")
            return node.value
        raise ValueError(f"Unsupported constant type in math expression: {type(node.value).__name__}")

    if isinstance(node, ast.UnaryOp):
        op = _SAFE_OPERATORS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
        operand = _safe_eval_node(node.operand, depth + 1, max_depth)
        res = op(operand)
        if isinstance(res, complex):
            raise TypeError("Complex numbers are not supported in math expressions")
        if isinstance(res, float) and (math.isnan(res) or math.isinf(res)):
            raise ValueError("Unary operation resulted in non-finite value (NaN or Infinity)")
        return res

    if isinstance(node, ast.BinOp):
        op = _SAFE_OPERATORS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported binary operator: {type(node.op).__name__}")
        left = _safe_eval_node(node.left, depth + 1, max_depth)
        right = _safe_eval_node(node.right, depth + 1, max_depth)

        # Guard against exponent explosion / resource exhaustion before evaluating pow()
        if isinstance(node.op, ast.Pow):
            if isinstance(left, bool) or isinstance(right, bool):
                raise TypeError("Boolean operands are not permitted in math expressions")
            if right > 1000 or right < -1000:
                raise ValueError("Exponent too large in power calculation (exceeds magnitude limit of 1000)")
            if isinstance(left, (int, float)) and abs(left) > 1 and isinstance(right, (int, float)) and right > 0:
                if abs(left) > 1e100 and right > 1:
                    raise ValueError("Base value too large for exponentiation")
                if math.log10(abs(left)) * float(right) > 1000:
                    raise ValueError("Exponent too large in power calculation (result exceeds digit limit)")

        try:
            res = op(left, right)
        except OverflowError as exc:
            raise ValueError("Numeric calculation overflow") from exc

        if isinstance(res, complex):
            raise TypeError("Complex numbers are not supported in math expressions")
        if isinstance(res, float) and (math.isnan(res) or math.isinf(res)):
            raise ValueError("Binary operation resulted in non-finite value (NaN or Infinity)")
        return res

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise TypeError("Only direct math function calls are permitted (attribute and method access forbidden)")
        if node.keywords:
            raise ValueError("Keyword arguments are not permitted in math function calls")
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            raise ValueError("Starred arguments are not permitted in math function calls")
        fn_name = node.func.id.lower()
        fn = _SAFE_FUNCTIONS.get(fn_name)
        if fn is None:
            raise ValueError(f"Function '{fn_name}' is not in the safe math allowlist")
        args = [_safe_eval_node(arg, depth + 1, max_depth) for arg in node.args]
        try:
            res = fn(*args)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"Function '{fn_name}' failed: {exc}") from exc

        if isinstance(res, complex):
            raise TypeError("Complex numbers are not supported in math expressions")
        if isinstance(res, float) and (math.isnan(res) or math.isinf(res)):
            raise ValueError(f"Function '{fn_name}' produced a non-finite result (NaN or Infinity)")
        return res

    # Explicit rejection of unauthorized constructs (attributes, subscripts, comprehensions, etc.)
    raise ValueError(f"Disallowed expression element '{type(node).__name__}'")


def _evaluate_math_expression(clean_expr: str, max_ast_depth: int) -> int | float:
    """Parse and safely evaluate a math expression synchronously for threadpool offload."""
    tree = ast.parse(clean_expr, mode="eval")
    result = _safe_eval_node(tree, depth=0, max_depth=max_ast_depth)
    if isinstance(result, complex):
        raise TypeError("Complex numbers are not supported in math expressions")
    if isinstance(result, float):
        if result.is_integer():
            return int(result)
        return result
    if isinstance(result, int) and not isinstance(result, bool):
        return result
    raise ValueError(f"Unsupported numeric result type: {type(result).__name__}")


class CalculatorTool(BaseTool):
    """
    Safely calculates mathematical expressions using abstract syntax trees with
    strict resource and security bounds (zero eval).
    """

    def __init__(
        self,
        max_expression_length: int = 500,
        max_ast_depth: int = 20,
    ) -> None:
        self._max_expression_length = max_expression_length
        self._max_ast_depth = max_ast_depth

    @property
    def name(self) -> str:
        return "calculator"

    @property
    def description(self) -> str:
        return (
            "Perform mathematical calculations and evaluate numeric expressions safely. "
            "Supports basic arithmetic (+, -, *, /, //, %, **), powers, and functions like sqrt, abs, round."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "The mathematical expression to evaluate, e.g. '(15 * 4) + 120'.",
                }
            },
            "required": ["expression"],
        }

    async def handle(self, expression: str = "", **kwargs: Any) -> Any:
        # Validate expression argument type
        raw_expr = expression if expression != "" else kwargs.get("expression", "")
        if not isinstance(raw_expr, str):
            raise TypeError(f"expression must be a string, got {type(raw_expr).__name__}")

        clean_expr = raw_expr.strip()
        if not clean_expr:
            raise ValueError("Expression cannot be empty.")

        if len(clean_expr) > self._max_expression_length:
            return {
                "expression": clean_expr[:50] + "...",
                "error": f"Expression length ({len(clean_expr)}) exceeds maximum allowed limit ({self._max_expression_length})",
            }

        try:
            result = await asyncio.to_thread(
                _evaluate_math_expression,
                clean_expr,
                self._max_ast_depth,
            )
            return {"expression": clean_expr, "result": result}
        except (SyntaxError, ZeroDivisionError, ValueError, TypeError, OverflowError) as exc:
            return {"expression": clean_expr, "error": str(exc)}


# ==============================================================================
# 2. Date and Time Tool
# ==============================================================================


class DateTimeTool(BaseTool):
    """
    Provides the current date, time, weekday, and timestamp in UTC or specified timezone.
    """

    @property
    def name(self) -> str:
        return "date_time"

    @property
    def description(self) -> str:
        return (
            "Get the current date and time in UTC or a specified timezone with optional custom format. "
            "Use this whenever you need to know today's date, current year, current time, or day of the week."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "Timezone name (e.g. 'UTC', 'US/Eastern', 'Europe/London'). Defaults to 'UTC'.",
                },
                "format": {
                    "type": "string",
                    "description": "Optional strftime format string (e.g. '%Y-%m-%d', '%A, %B %d, %Y'). Defaults to ISO-8601.",
                },
            },
            "required": [],
        }

    async def handle(
        self,
        timezone: str = "UTC",
        format: str | None = None,
        **kwargs: Any,
    ) -> Any:
        if not isinstance(timezone, str):
            raise TypeError(f"timezone must be a string, got {type(timezone).__name__}")
        if format is not None and not isinstance(format, str):
            raise TypeError(f"format must be a string or None, got {type(format).__name__}")

        tz: datetime.tzinfo = datetime.UTC
        tz_name = timezone or "UTC"

        if tz_name.upper() != "UTC":
            try:
                tz = zoneinfo.ZoneInfo(tz_name)
            except (KeyError, ValueError):
                tz = datetime.UTC
                tz_name = "UTC"

        now = datetime.datetime.now(tz)
        try:
            formatted = now.strftime(format) if format else now.isoformat()
        except (ValueError, TypeError):
            formatted = now.isoformat()

        return {
            "datetime": formatted,
            "iso": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "weekday": now.strftime("%A"),
            "year": now.year,
            "timezone": tz_name,
        }


# ==============================================================================
# 3. Web Search Tool
# ==============================================================================


class WebSearchTool(BaseTool):
    """
    WebSearchTool provides a pluggable search integration with a configurable
    search function and a built-in synthetic fallback for development and testing.
    """

    def __init__(
        self,
        search_fn: Callable[..., Any] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._search_fn = search_fn
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search the web for up-to-date information, news, current events, facts, or technical documentation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to look up on the web.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of search results to return (default is 5).",
                },
            },
            "required": ["query"],
        }

    async def handle(self, query: str = "", max_results: int = 5, **kwargs: Any) -> Any:
        raw_query = query if query != "" else kwargs.get("query", "")
        if not isinstance(raw_query, str):
            raise TypeError(f"query must be a string, got {type(raw_query).__name__}")

        if isinstance(max_results, bool) or not isinstance(max_results, int):
            raise TypeError(f"max_results must be an integer, got {type(max_results).__name__}")
        if max_results <= 0:
            raise ValueError(f"max_results must be a positive integer > 0, got {max_results}")

        clean_query = raw_query.strip()
        if not clean_query:
            return {"query": clean_query, "results": []}

        if self._search_fn is None:
            raise AIConfigurationError(
                "WebSearchTool requires a configured 'search_fn' callable; "
                "synthetic search results are disabled."
            )

        try:
            if is_async_callable(self._search_fn):
                coro = self._search_fn(clean_query, max_results=max_results)
                raw_res = await asyncio.wait_for(coro, timeout=self._timeout)
            else:
                call_fn = self._search_fn
                coro_sync = asyncio.to_thread(call_fn, clean_query, max_results=max_results)
                raw_res = await asyncio.wait_for(coro_sync, timeout=self._timeout)

            # Normalize results
            if isinstance(raw_res, dict):
                return raw_res
            if isinstance(raw_res, list):
                return {"query": clean_query, "results": raw_res[:max_results]}
            return {"query": clean_query, "results": [{"content": str(raw_res)}]}
        except TimeoutError:
            return {
                "query": clean_query,
                "error": f"Search operation timed out after {self._timeout}s",
                "results": [],
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "query": clean_query,
                "error": f"Search function failed: {exc!s}",
                "results": [],
            }
