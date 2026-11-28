from __future__ import annotations

import copy
import inspect
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from enum import Enum, IntEnum
from types import UnionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Literal,
    Self,
    TypeVar,
    Union,
    dataclass_transform,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)
from uuid import UUID

from swiftpy.database.orm.fields import _MISSING as _ORM_MISSING
from swiftpy.database.orm.fields import FieldInfo as OrmFieldInfo
from swiftpy.http.exceptions import HttpException
from swiftpy.schemas.fields import _MISSING, Field, SchemaField, SchemaFieldInfo
from swiftpy.validation.exceptions import ValidationException

if TYPE_CHECKING:
    from swiftpy.http.request import Request

F = TypeVar("F", bound=Callable[..., Any])
_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_DEFAULT_FIELD_INFO = SchemaFieldInfo()
_IMMUTABLE_DEFAULT_TYPES = (
    type(None),
    bool,
    int,
    float,
    str,
    bytes,
    Decimal,
    UUID,
    date,
    datetime,
    time,
    Enum,
)


def _clone_default(value: Any) -> Any:
    if isinstance(value, _IMMUTABLE_DEFAULT_TYPES):
        return value
    return copy.deepcopy(value)


class _FieldCoercionError(ValueError):
    """Internal validation failure carrying field-relative error entries."""

    def __init__(self, messages: dict[str, list[str]]) -> None:
        self.messages = messages
        super().__init__(next(iter(messages.values()))[0] if messages else "Invalid value.")


@dataclass(slots=True, frozen=True)
class _CompiledSchemaField:
    name: str
    annotation: Any
    field_info: SchemaFieldInfo
    is_required: bool


@dataclass(slots=True, frozen=True)
class _FieldValidatorSpec:
    attr_name: str
    func: Callable[..., Any]
    is_classmethod: bool
    is_staticmethod: bool
    param_count: int
    takes_cls: bool


@dataclass(slots=True, frozen=True)
class _ModelValidatorSpec:
    attr_name: str
    func: Callable[..., Any]
    is_classmethod: bool
    is_staticmethod: bool


def field_validator(*fields: str) -> Callable[[F], F]:
    """Decorate a method on a BaseSchema subclass to validate/transform specific fields."""
    if not fields or any(not isinstance(f, str) or not f.strip() for f in fields):
        raise ValueError("field_validator() requires one or more non-empty field names.")

    cleaned = tuple(f.strip() for f in fields)

    def decorator(func: F) -> F:
        target: Any = getattr(func, "__func__", func)
        existing = tuple(getattr(target, "_swiftpy_field_validators", ()))
        target._swiftpy_field_validators = (*existing, *cleaned)
        return func

    return decorator


@overload
def model_validator[Fn: Callable[..., Any]](func: Fn) -> Fn: ...


@overload
def model_validator(func: None = None) -> Callable[[F], F]: ...


def model_validator(func: Callable[..., Any] | None = None) -> Any:
    """Decorate a method on a BaseSchema subclass to perform cross-field model validation."""

    def decorator(inner: Callable[..., Any]) -> Callable[..., Any]:
        target: Any = getattr(inner, "__func__", inner)
        target._swiftpy_model_validator = True
        return inner

    if func is not None:
        return decorator(func)
    return decorator


def _collect_caller_type_locals() -> dict[str, Any]:
    """Capture type-like symbols from the immediate subclass definition scope."""
    localns: dict[str, Any] = {}
    frame = inspect.currentframe()
    try:
        curr = frame.f_back if frame is not None else None
        depth = 0
        while curr is not None and depth < 4:
            if not curr.f_code.co_filename.replace("\\", "/").endswith(
                "/swiftpy/schemas/base.py"
            ):
                for key, val in curr.f_locals.items():
                    if isinstance(val, type) or get_origin(val) is not None:
                        localns[key] = val
                break
            curr = curr.f_back
            depth += 1
    finally:
        del frame
    return localns


def _extract_field_info(raw_value: Any) -> SchemaFieldInfo:
    if isinstance(raw_value, SchemaFieldInfo):
        return raw_value
    if isinstance(raw_value, OrmFieldInfo):
        if raw_value.default_factory is not None:
            return SchemaFieldInfo(default_factory=raw_value.default_factory)
        default_val = _MISSING if raw_value.default is _ORM_MISSING else raw_value.default
        return SchemaFieldInfo(default=default_val)
    if raw_value is Ellipsis:
        return SchemaFieldInfo(_MISSING)
    if callable(raw_value) and not isinstance(raw_value, type):
        return SchemaFieldInfo(default_factory=raw_value)
    return SchemaFieldInfo(default=_clone_default(raw_value))


def _merge_annotated_field_info(
    annotated_info: SchemaFieldInfo,
    raw_default: Any,
) -> SchemaFieldInfo:
    if isinstance(raw_default, SchemaFieldInfo):
        return raw_default
    default_info = _extract_field_info(raw_default)
    default_factory = default_info.default_factory
    default_val: Any = (
        ...
        if default_factory is not None or default_info.default is _MISSING
        else default_info.default
    )
    return SchemaFieldInfo(
        default_val,
        default_factory=default_factory,
        alias=annotated_info.alias,
        description=annotated_info.description,
        gt=annotated_info.gt,
        ge=annotated_info.ge,
        lt=annotated_info.lt,
        le=annotated_info.le,
        multiple_of=annotated_info.multiple_of,
        min_length=annotated_info.min_length,
        max_length=annotated_info.max_length,
        pattern=annotated_info.compiled_pattern or annotated_info.pattern,
        strip_whitespace=annotated_info.strip_whitespace,
        to_lower=annotated_info.to_lower,
        to_upper=annotated_info.to_upper,
        exclude=annotated_info.exclude,
    )


def _unwrap_annotated(annotation: Any) -> tuple[Any, SchemaFieldInfo | None]:
    if get_origin(annotation) is not Annotated:
        return annotation, None
    args = get_args(annotation)
    if not args:
        return annotation, None
    inner = args[0]
    found_info: SchemaFieldInfo | None = None
    for meta in args[1:]:
        if isinstance(meta, SchemaFieldInfo):
            found_info = meta
            break
    unwrapped, nested_info = _unwrap_annotated(inner)
    return unwrapped, found_info or nested_info


def _is_union_type(origin: Any) -> bool:
    return origin is Union or origin is UnionType


def _allows_none(annotation: Any) -> bool:
    unwrapped, _ = _unwrap_annotated(annotation)
    if unwrapped is Any or unwrapped is object or unwrapped is type(None):
        return True
    origin = get_origin(unwrapped)
    if _is_union_type(origin):
        return any(_allows_none(member) for member in get_args(unwrapped))
    return False


def _matches_exact_member(value: Any, member: Any) -> bool:
    unwrapped, _ = _unwrap_annotated(member)
    origin = get_origin(unwrapped)
    if unwrapped is Any or unwrapped is object:
        return True
    if unwrapped is bool:
        return isinstance(value, bool)
    if unwrapped is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if unwrapped is float:
        return isinstance(value, float)
    if unwrapped is datetime:
        return isinstance(value, datetime)
    if unwrapped is date:
        return isinstance(value, date) and not isinstance(value, datetime)
    if origin is Literal:
        return any(
            type(value) is type(opt) and value == opt for opt in get_args(unwrapped)
        )
    if origin is not None and isinstance(origin, type):
        return isinstance(value, origin)
    if isinstance(unwrapped, type):
        return isinstance(value, unwrapped)
    return False


def _apply_string_transforms(value: str, field_info: SchemaFieldInfo) -> str:
    res = value.strip() if field_info.strip_whitespace else value
    if field_info.to_lower:
        res = res.lower()
    elif field_info.to_upper:
        res = res.upper()
    return res


def _coerce_value(
    value: Any,
    annotation: Any,
    field_path: str,
    field_info: SchemaFieldInfo,
) -> Any:
    target, annotated_info = _unwrap_annotated(annotation)
    active_info = annotated_info if annotated_info is not None else field_info
    coerced = _coerce_target_value(value, target, field_path, active_info)
    if annotated_info is not None:
        constraint_errs = _check_field_constraints(
            coerced, field_path, annotated_info
        )
        if constraint_errs:
            raise _FieldCoercionError({field_path: constraint_errs})
    return coerced


def _coerce_target_value(
    value: Any,
    target: Any,
    field_path: str,
    field_info: SchemaFieldInfo,
) -> Any:
    if target is inspect.Parameter.empty or target is Any or target is object:
        if isinstance(value, str):
            return _apply_string_transforms(value, field_info)
        return value

    if target is type(None):
        if value is None:
            return None
        raise _FieldCoercionError({field_path: [f"The {field_path} field must be null."]})

    origin = get_origin(target)
    if _is_union_type(origin):
        members = get_args(target)
        if value is None:
            if any(_allows_none(m) for m in members):
                return None
            raise _FieldCoercionError(
                {field_path: [f"The {field_path} field cannot be null."]}
            )

        non_none = [m for m in members if m is not type(None)]
        if len(non_none) == 1:
            return _coerce_value(value, non_none[0], field_path, field_info)

        for member in non_none:
            if _matches_exact_member(value, member):
                try:
                    return _coerce_value(value, member, field_path, field_info)
                except _FieldCoercionError:
                    pass

        for member in non_none:
            try:
                return _coerce_value(value, member, field_path, field_info)
            except _FieldCoercionError:
                continue

        raise _FieldCoercionError(
            {field_path: [f"The {field_path} field does not match any allowed type."]}
        )

    if value is None:
        raise _FieldCoercionError({field_path: [f"The {field_path} field cannot be null."]})

    if origin is Literal:
        options = get_args(target)
        for opt in options:
            if isinstance(opt, Enum):
                if value == opt or value == opt.value:
                    return opt
            elif isinstance(opt, bool):
                if isinstance(value, bool) and value is opt:
                    return opt
            elif isinstance(opt, int):
                if isinstance(value, int) and not isinstance(value, bool) and value == opt:
                    return opt
            elif value == opt and type(value) is type(opt):
                return opt
        allowed_repr = ", ".join(repr(o.value if isinstance(o, Enum) else o) for o in options)
        raise _FieldCoercionError(
            {field_path: [f"The {field_path} field must be one of: {allowed_repr}."]}
        )

    if isinstance(target, type) and issubclass(target, Enum):
        if isinstance(value, target):
            return value
        try:
            return target(value)
        except (ValueError, TypeError):
            if issubclass(target, IntEnum) and isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
                try:
                    return target(int(value.strip()))
                except ValueError:
                    pass
        raise _FieldCoercionError(
            {field_path: [f"The {field_path} field must be a valid {target.__name__}."]}
        )

    if isinstance(target, type) and issubclass(target, BaseSchema):
        if isinstance(value, target):
            return value
        if isinstance(value, Mapping):
            try:
                return target._validate_mapping(value, field_prefix=field_path)
            except ValidationException as exc:
                raise _FieldCoercionError(exc.errors) from None
        raise _FieldCoercionError({field_path: [f"The {field_path} field must be an object."]})

    if target is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            norm = value.strip().lower()
            if norm in ("true", "1", "yes", "on"):
                return True
            if norm in ("false", "0", "no", "off"):
                return False
        raise _FieldCoercionError({field_path: [f"The {field_path} field must be a boolean."]})

    if target is int:
        if isinstance(value, bool):
            raise _FieldCoercionError({field_path: [f"The {field_path} field must be an integer."]})
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.isascii() and _INT_RE.fullmatch(stripped):
                return int(stripped)
        raise _FieldCoercionError({field_path: [f"The {field_path} field must be an integer."]})

    if target is float:
        if isinstance(value, bool):
            raise _FieldCoercionError({field_path: [f"The {field_path} field must be a number."]})
        if isinstance(value, (int, float)):
            fval = float(value)
            if math.isfinite(fval):
                return fval
            raise _FieldCoercionError({field_path: [f"The {field_path} field must be a finite number."]})
        if isinstance(value, str):
            try:
                fval = float(value.strip())
            except ValueError:
                fval = math.nan
            if math.isfinite(fval):
                return fval
        raise _FieldCoercionError({field_path: [f"The {field_path} field must be a valid number."]})

    if target is Decimal:
        if isinstance(value, bool):
            raise _FieldCoercionError({field_path: [f"The {field_path} field must be a decimal."]})
        try:
            if isinstance(value, Decimal):
                dval = value
            elif isinstance(value, int):
                dval = Decimal(value)
            elif isinstance(value, float):
                if not math.isfinite(value):
                    raise InvalidOperation
                dval = Decimal(str(value))
            elif isinstance(value, str):
                dval = Decimal(value.strip())
            else:
                raise InvalidOperation
            if not dval.is_finite():
                raise InvalidOperation
            return dval
        except (InvalidOperation, ValueError):
            raise _FieldCoercionError(
                {field_path: [f"The {field_path} field must be a valid decimal."]}
            ) from None

    if target is str:
        if isinstance(value, str):
            return _apply_string_transforms(value, field_info)
        raise _FieldCoercionError({field_path: [f"The {field_path} field must be a string."]})

    if target is UUID:
        if isinstance(value, UUID):
            return value
        if isinstance(value, str):
            try:
                return UUID(value.strip())
            except ValueError:
                pass
        raise _FieldCoercionError({field_path: [f"The {field_path} field must be a valid UUID."]})

    if target is datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            raw_dt = value.strip()
            if raw_dt.endswith(("Z", "z")):
                raw_dt = f"{raw_dt[:-1]}+00:00"
            try:
                return datetime.fromisoformat(raw_dt)
            except ValueError:
                pass
        raise _FieldCoercionError(
            {field_path: [f"The {field_path} field must be a valid ISO datetime."]}
        )

    if target is date:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value.strip())
            except ValueError:
                pass
        raise _FieldCoercionError(
            {field_path: [f"The {field_path} field must be a valid ISO date (YYYY-MM-DD)."]}
        )

    if target is time:
        if isinstance(value, time):
            return value
        if isinstance(value, str):
            raw_time = value.strip()
            if raw_time.endswith(("Z", "z")):
                raw_time = f"{raw_time[:-1]}+00:00"
            try:
                return time.fromisoformat(raw_time)
            except ValueError:
                pass
        raise _FieldCoercionError({field_path: [f"The {field_path} field must be a valid ISO time."]})

    if target is bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        raise _FieldCoercionError({field_path: [f"The {field_path} field must be bytes."]})

    if origin in (list, Sequence) or target is list:
        if not isinstance(value, (list, tuple)):
            raise _FieldCoercionError({field_path: [f"The {field_path} field must be a list."]})
        args = get_args(target)
        elem_type = args[0] if args else Any
        out_list: list[Any] = []
        agg_errors: dict[str, list[str]] = {}
        for idx, item in enumerate(value):
            item_path = f"{field_path}.{idx}"
            try:
                out_list.append(_coerce_value(item, elem_type, item_path, _DEFAULT_FIELD_INFO))
            except _FieldCoercionError as exc:
                for err_key, err_msgs in exc.messages.items():
                    agg_errors.setdefault(err_key, []).extend(err_msgs)
        if agg_errors:
            raise _FieldCoercionError(agg_errors)
        return out_list

    if origin is tuple or target is tuple:
        if not isinstance(value, (list, tuple)):
            raise _FieldCoercionError({field_path: [f"The {field_path} field must be a list or tuple."]})
        args = get_args(target)
        if not args:
            return tuple(value)
        if len(args) == 2 and args[1] is Ellipsis:
            elem_type = args[0]
            out_items: list[Any] = []
            agg_errors = {}
            for idx, item in enumerate(value):
                item_path = f"{field_path}.{idx}"
                try:
                    out_items.append(_coerce_value(item, elem_type, item_path, _DEFAULT_FIELD_INFO))
                except _FieldCoercionError as exc:
                    for err_key, err_msgs in exc.messages.items():
                        agg_errors.setdefault(err_key, []).extend(err_msgs)
            if agg_errors:
                raise _FieldCoercionError(agg_errors)
            return tuple(out_items)
        if len(value) != len(args):
            raise _FieldCoercionError(
                {field_path: [f"The {field_path} field must contain exactly {len(args)} items."]}
            )
        out_fixed: list[Any] = []
        agg_errors = {}
        for idx, (item, item_type) in enumerate(zip(value, args, strict=True)):
            item_path = f"{field_path}.{idx}"
            try:
                out_fixed.append(_coerce_value(item, item_type, item_path, _DEFAULT_FIELD_INFO))
            except _FieldCoercionError as exc:
                for err_key, err_msgs in exc.messages.items():
                    agg_errors.setdefault(err_key, []).extend(err_msgs)
        if agg_errors:
            raise _FieldCoercionError(agg_errors)
        return tuple(out_fixed)

    if origin in (set, frozenset) or target in (set, frozenset):
        if not isinstance(value, (set, frozenset, list, tuple)):
            raise _FieldCoercionError({field_path: [f"The {field_path} field must be a set or list."]})
        args = get_args(target)
        elem_type = args[0] if args else Any
        out_set_items: list[Any] = []
        agg_errors = {}
        for idx, item in enumerate(value):
            item_path = f"{field_path}.{idx}"
            try:
                out_set_items.append(_coerce_value(item, elem_type, item_path, _DEFAULT_FIELD_INFO))
            except _FieldCoercionError as exc:
                for err_key, err_msgs in exc.messages.items():
                    agg_errors.setdefault(err_key, []).extend(err_msgs)
        if agg_errors:
            raise _FieldCoercionError(agg_errors)
        return frozenset(out_set_items) if (origin is frozenset or target is frozenset) else set(out_set_items)

    if origin in (dict, Mapping) or target is dict:
        if not isinstance(value, Mapping):
            raise _FieldCoercionError({field_path: [f"The {field_path} field must be an object."]})
        args = get_args(target)
        key_type = args[0] if len(args) >= 1 else Any
        val_type = args[1] if len(args) >= 2 else Any
        out_dict: dict[Any, Any] = {}
        agg_errors = {}
        for raw_k, raw_v in value.items():
            key_path = f"{field_path}.{raw_k}"
            try:
                coerced_k = _coerce_value(raw_k, key_type, key_path, _DEFAULT_FIELD_INFO)
                coerced_v = _coerce_value(raw_v, val_type, key_path, _DEFAULT_FIELD_INFO)
                out_dict[coerced_k] = coerced_v
            except _FieldCoercionError as exc:
                for err_key, err_msgs in exc.messages.items():
                    agg_errors.setdefault(err_key, []).extend(err_msgs)
        if agg_errors:
            raise _FieldCoercionError(agg_errors)
        return out_dict

    if isinstance(target, type):
        if isinstance(value, target):
            return value
        raise _FieldCoercionError(
            {field_path: [f"The {field_path} field must be of type {target.__name__}."]}
        )

    return value


def _normalize_numeric_comparison(
    value: float | Decimal,
    bound: float | Decimal,
) -> tuple[Any, Any]:
    if isinstance(value, Decimal) and isinstance(bound, float):
        return value, Decimal(str(bound))
    if isinstance(value, float) and isinstance(bound, Decimal):
        return Decimal(str(value)), bound
    return value, bound


def _check_field_constraints(
    value: Any,
    field_path: str,
    field_info: SchemaFieldInfo,
) -> list[str]:
    if value is None:
        return []

    errors: list[str] = []
    is_numeric = isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)

    numeric_checks: tuple[tuple[float | Decimal | None, str, str], ...] = (
        (field_info.gt, "gt", f"The {field_path} field must be greater than {{bound}}."),
        (field_info.ge, "ge", f"The {field_path} field must be at least {{bound}}."),
        (field_info.lt, "lt", f"The {field_path} field must be less than {{bound}}."),
        (
            field_info.le,
            "le",
            f"The {field_path} field may not be greater than {{bound}}.",
        ),
    )
    for bound, op, template in numeric_checks:
        if bound is None:
            continue
        if not is_numeric:
            errors.append(f"The {field_path} field must be numeric.")
            continue
        lhs, rhs = _normalize_numeric_comparison(value, bound)
        holds = (
            lhs > rhs
            if op == "gt"
            else lhs >= rhs
            if op == "ge"
            else lhs < rhs
            if op == "lt"
            else lhs <= rhs
        )
        if not holds:
            errors.append(template.format(bound=bound))

    if field_info.multiple_of is not None:
        if not is_numeric:
            errors.append(f"The {field_path} field must be numeric.")
        else:
            divisor = field_info.multiple_of
            if isinstance(value, (float, Decimal)) or isinstance(divisor, (float, Decimal)):
                remainder = Decimal(str(value)) % Decimal(str(divisor))
                is_mult = remainder == 0
            else:
                is_mult = (value % divisor) == 0
            if not is_mult:
                errors.append(
                    f"The {field_path} field must be a multiple of {field_info.multiple_of}."
                )

    if field_info.min_length is not None or field_info.max_length is not None:
        if isinstance(value, (str, bytes, list, tuple, set, frozenset, dict)):
            unit = "characters" if isinstance(value, (str, bytes)) else "items"
            length = len(value)
            if field_info.min_length is not None and length < field_info.min_length:
                errors.append(
                    f"The {field_path} field must be at least {field_info.min_length} {unit}."
                )
            if field_info.max_length is not None and length > field_info.max_length:
                errors.append(
                    f"The {field_path} field may not be greater than {field_info.max_length} {unit}."
                )
        else:
            errors.append(
                f"The {field_path} field must support length validation."
            )

    if field_info.compiled_pattern is not None and (
        not isinstance(value, str) or not field_info.compiled_pattern.search(value)
    ):
        errors.append(f"The {field_path} field format is invalid.")

    return errors


def _serialize_schema_value(
    value: Any,
    *,
    exclude_none: bool,
    exclude_unset: bool,
    by_alias: bool,
) -> Any:
    if isinstance(value, BaseSchema):
        return value.to_dict(
            exclude_none=exclude_none,
            exclude_unset=exclude_unset,
            by_alias=by_alias,
        )
    if isinstance(value, list):
        return [
            _serialize_schema_value(
                item,
                exclude_none=exclude_none,
                exclude_unset=exclude_unset,
                by_alias=by_alias,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _serialize_schema_value(
                item,
                exclude_none=exclude_none,
                exclude_unset=exclude_unset,
                by_alias=by_alias,
            )
            for item in value
        )
    if isinstance(value, dict):
        return {
            k: _serialize_schema_value(
                v,
                exclude_none=exclude_none,
                exclude_unset=exclude_unset,
                by_alias=by_alias,
            )
            for k, v in value.items()
        }
    return value


def _extract_model_value(
    obj: Any,
    field_name: str,
    annotation: Any,
    *,
    alias: str | None = None,
) -> Any:
    raw: Any = _MISSING
    if isinstance(obj, Mapping):
        if field_name in obj:
            raw = obj[field_name]
        elif alias and alias in obj:
            raw = obj[alias]
    else:
        relations_cfg = getattr(obj, "__relations_config__", None)
        if isinstance(relations_cfg, Mapping) and field_name in relations_cfg:
            loaded = getattr(obj, "_loaded_relations", {})
            if isinstance(loaded, Mapping) and field_name in loaded:
                raw = loaded[field_name]
            else:
                return _MISSING
        elif hasattr(obj, field_name):
            raw = getattr(obj, field_name)
        elif alias and hasattr(obj, alias):
            raw = getattr(obj, alias)

    if raw is _MISSING:
        return _MISSING

    target, _ = _unwrap_annotated(annotation)
    origin = get_origin(target)
    if _is_union_type(origin):
        non_none = [m for m in get_args(target) if m is not type(None)]
        if len(non_none) == 1:
            target = non_none[0]
            origin = get_origin(target)

    if isinstance(target, type) and issubclass(target, BaseSchema):
        if raw is None or isinstance(raw, target):
            return raw
        return target.from_model(raw)

    if origin in (list, Sequence) and isinstance(raw, (list, tuple)):
        args = get_args(target)
        elem_type = args[0] if args else Any
        elem_target, _ = _unwrap_annotated(elem_type)
        if isinstance(elem_target, type) and issubclass(elem_target, BaseSchema):
            return [
                item if isinstance(item, elem_target) else elem_target.from_model(item)
                for item in raw
            ]

    return raw


async def _extract_request_mapping(
    request: Request,
    *,
    stop_on_first_error: bool = False,
) -> dict[str, Any]:
    """Parse JSON or form/multipart request body into a dictionary, failing with 422."""
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
                    {"body": ["Request body is required."]},
                    single_error=stop_on_first_error,
                )
        try:
            form_data = await request.form()
        except HttpException as exc:
            if exc.status_code == 400:
                raise ValidationException(
                    {"body": [exc.message]},
                    single_error=stop_on_first_error,
                ) from None
            raise
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationException(
                {"body": [str(exc)]},
                single_error=stop_on_first_error,
            ) from None

        if (
            media_type == "multipart/form-data"
            and not form_data.fields_list
            and not form_data.files_list
        ):
            raise ValidationException(
                {"body": ["Request body is required."]},
                single_error=stop_on_first_error,
            )

        data: dict[str, Any] = {}
        for key, vals in form_data.fields_list.items():
            data[key] = vals[0] if len(vals) == 1 else list(vals)
        for key, flist in form_data.files_list.items():
            if key not in data:
                data[key] = flist[0] if len(flist) == 1 else list(flist)
        return data

    raw_body = await request.body()
    if not raw_body.strip():
        raise ValidationException(
            {"body": ["Request body is required."]},
            single_error=stop_on_first_error,
        )
    try:
        raw_data = await request.json()
        if not isinstance(raw_data, dict):
            raise ValidationException(
                {"body": ["The request payload must be a JSON object."]},
                single_error=stop_on_first_error,
            )
        return dict(raw_data)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise ValidationException(
            {"body": ["Invalid JSON payload"]},
            single_error=stop_on_first_error,
        ) from None
    except HttpException as exc:
        if exc.status_code == 400 and exc.message == "Invalid JSON payload":
            raise ValidationException(
                {"body": ["Invalid JSON payload"]},
                single_error=stop_on_first_error,
            ) from None
        raise


@dataclass_transform(
    kw_only_default=True,
    field_specifiers=(Field, SchemaField, SchemaFieldInfo),
)
class BaseSchema:
    """
    Declarative, statically typed application schema with automatic type coercion,
    Field(...) constraint validation, custom validators, and HTTP/ORM integration.
    """

    _is_swiftpy_schema: ClassVar[bool] = True
    stop_on_first_error: ClassVar[bool] = False
    extra: ClassVar[Literal["ignore", "forbid", "allow"]] = "ignore"

    __schema_fields__: ClassVar[dict[str, _CompiledSchemaField]] = {}
    __schema_field_order__: ClassVar[tuple[str, ...]] = ()
    __schema_aliases__: ClassVar[dict[str, str]] = {}
    __field_validators__: ClassVar[dict[str, list[_FieldValidatorSpec]]] = {}
    __model_validators__: ClassVar[list[_ModelValidatorSpec]] = []
    __hints_resolved__: ClassVar[bool] = True
    __defining_localns__: ClassVar[dict[str, Any]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        extra_mode = getattr(cls, "extra", "ignore")
        if extra_mode not in ("ignore", "forbid", "allow"):
            raise ValueError(
                f"Invalid schema extra policy {extra_mode!r} on {cls.__name__}; "
                "expected 'ignore', 'forbid', or 'allow'."
            )

        needs_caller_locals = "<locals>" in cls.__qualname__
        merged_locals: dict[str, Any] = {}
        for base in reversed(cls.__mro__[1:]):
            merged_locals.update(getattr(base, "__defining_localns__", {}))
        if needs_caller_locals:
            merged_locals.update(_collect_caller_type_locals())
        merged_locals[cls.__name__] = cls

        hints_resolved = True
        try:
            resolved_hints = get_type_hints(
                cls,
                localns=merged_locals,
                include_extras=True,
            )
        except (NameError, TypeError):
            if not needs_caller_locals:
                caller_locals = _collect_caller_type_locals()
                if caller_locals:
                    merged_locals.update(caller_locals)
                    merged_locals[cls.__name__] = cls
            try:
                resolved_hints = get_type_hints(
                    cls,
                    localns=merged_locals,
                    include_extras=True,
                )
            except (NameError, TypeError):
                resolved_hints = inspect.get_annotations(cls, eval_str=False)
                hints_resolved = False

        cls.__defining_localns__ = merged_locals

        compiled_fields: dict[str, _CompiledSchemaField] = {}
        field_order: list[str] = []
        field_validators: dict[str, list[_FieldValidatorSpec]] = {}
        model_validators: list[_ModelValidatorSpec] = []

        for base in reversed(cls.__mro__[1:]):
            if not issubclass(base, BaseSchema) or base is BaseSchema:
                continue
            for fname, cfield in getattr(base, "__schema_fields__", {}).items():
                compiled_fields[fname] = cfield
                if fname not in field_order:
                    field_order.append(fname)
            for fname, specs in getattr(base, "__field_validators__", {}).items():
                field_validators.setdefault(fname, []).extend(specs)
            model_validators.extend(getattr(base, "__model_validators__", []))

        own_annotations = inspect.get_annotations(cls, eval_str=False)
        for name in own_annotations:
            if name.startswith("_"):
                continue
            hint = resolved_hints.get(name, own_annotations[name])
            if get_origin(hint) is ClassVar or (
                isinstance(hint, str) and hint.startswith("ClassVar")
            ):
                continue

            unwrapped, annotated_info = _unwrap_annotated(hint)
            if name in cls.__dict__:
                raw_default = cls.__dict__[name]
                if annotated_info is not None and not isinstance(
                    raw_default, SchemaFieldInfo
                ):
                    f_info = _merge_annotated_field_info(annotated_info, raw_default)
                else:
                    f_info = _extract_field_info(raw_default)
            elif annotated_info is not None:
                f_info = annotated_info
            else:
                f_info = SchemaFieldInfo(_MISSING)

            compiled_fields[name] = _CompiledSchemaField(
                name=name,
                annotation=unwrapped if annotated_info is not None else hint,
                field_info=f_info,
                is_required=f_info.is_required,
            )
            if name not in field_order:
                field_order.append(name)

        for attr_name, raw_attr in cls.__dict__.items():
            if attr_name.startswith("_"):
                continue
            is_cm = isinstance(raw_attr, classmethod)
            is_sm = isinstance(raw_attr, staticmethod)
            target_fn = getattr(raw_attr, "__func__", raw_attr)
            if not callable(target_fn):
                continue

            fv_targets = getattr(target_fn, "_swiftpy_field_validators", None)
            if fv_targets:
                sig = inspect.signature(target_fn)
                params = list(sig.parameters.values())
                takes_cls = (
                    not is_cm
                    and not is_sm
                    and bool(params)
                    and params[0].name == "cls"
                )
                effective_params = (
                    len(params) - 1 if (is_cm or takes_cls) else len(params)
                )
                spec = _FieldValidatorSpec(
                    attr_name=attr_name,
                    func=target_fn,
                    is_classmethod=is_cm,
                    is_staticmethod=is_sm,
                    param_count=effective_params,
                    takes_cls=takes_cls,
                )
                for target_field in fv_targets:
                    if target_field not in compiled_fields:
                        raise ValueError(
                            f"@field_validator({target_field!r}) on {cls.__name__}.{attr_name} "
                            "references an unknown schema field."
                        )
                    field_validators.setdefault(target_field, []).append(spec)

            if getattr(target_fn, "_swiftpy_model_validator", False):
                model_validators.append(
                    _ModelValidatorSpec(
                        attr_name=attr_name,
                        func=target_fn,
                        is_classmethod=is_cm,
                        is_staticmethod=is_sm,
                    )
                )

        aliases: dict[str, str] = {}
        for fname, cfield in compiled_fields.items():
            if cfield.field_info.alias is not None:
                aliases[cfield.field_info.alias] = fname

        cls.__schema_fields__ = compiled_fields
        cls.__schema_field_order__ = tuple(field_order)
        cls.__schema_aliases__ = aliases
        cls.__field_validators__ = field_validators
        cls.__model_validators__ = model_validators
        cls.__hints_resolved__ = hints_resolved

    @classmethod
    def _ensure_hints_resolved(cls) -> None:
        if cls.__hints_resolved__:
            return
        localns = dict(cls.__defining_localns__)
        localns[cls.__name__] = cls
        resolved = get_type_hints(
            cls,
            localns=localns,
            include_extras=True,
        )
        updated: dict[str, _CompiledSchemaField] = {}
        aliases = dict(cls.__schema_aliases__)
        for fname, cfield in cls.__schema_fields__.items():
            hint = resolved.get(fname, cfield.annotation)
            unwrapped, annotated_info = _unwrap_annotated(hint)
            field_info = cfield.field_info
            if annotated_info is not None:
                owner_dict: Mapping[str, Any] = next(
                    (
                        base.__dict__
                        for base in cls.__mro__
                        if issubclass(base, BaseSchema)
                        and base is not BaseSchema
                        and fname in inspect.get_annotations(base, eval_str=False)
                    ),
                    {},
                )
                if fname in owner_dict:
                    raw_default = owner_dict[fname]
                    if not isinstance(raw_default, SchemaFieldInfo):
                        field_info = _merge_annotated_field_info(
                            annotated_info, raw_default
                        )
                else:
                    field_info = annotated_info
            if field_info.alias is not None:
                aliases[field_info.alias] = fname
            updated[fname] = _CompiledSchemaField(
                name=cfield.name,
                annotation=unwrapped if annotated_info is not None else hint,
                field_info=field_info,
                is_required=field_info.is_required,
            )
        cls.__schema_fields__ = updated
        cls.__schema_aliases__ = aliases
        cls.__hints_resolved__ = True

    def __init__(self, *args: Any, **data: Any) -> None:
        cls = self.__class__
        stop_first = bool(getattr(cls, "stop_on_first_error", False))
        payload: Mapping[str, Any]
        if args:
            if len(args) == 1 and isinstance(args[0], Mapping) and not data:
                raw_mapping = args[0]
                if any(not isinstance(k, str) for k in raw_mapping):
                    raise TypeError(
                        f"{cls.__name__}() mapping keys must be str."
                    )
                payload = raw_mapping
            else:
                order = cls.__schema_field_order__
                if len(args) > len(order):
                    raise TypeError(
                        f"{cls.__name__}() takes at most {len(order)} positional "
                        f"arguments but {len(args)} were given."
                    )
                positional_dict: dict[str, Any] = {}
                for fname, val in zip(order, args, strict=False):
                    alias = cls.__schema_fields__[fname].field_info.alias
                    if fname in data or (alias is not None and alias in data):
                        raise TypeError(
                            f"{cls.__name__}() got multiple values for argument {fname!r}."
                        )
                    positional_dict[fname] = val
                positional_dict.update(data)
                payload = positional_dict
        else:
            payload = data

        cls._populate_instance(
            self,
            payload,
            stop_on_first_error=stop_first,
            field_prefix="",
        )

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        cls = self.__class__
        if name in cls.__schema_fields__:
            object.__setattr__(self, name, value)
            fields_set = self.__dict__.get("__fields_set__")
            if isinstance(fields_set, set):
                fields_set.add(name)
            return
        if cls.extra == "allow":
            extra_fields = self.__dict__.get("__extra_fields__")
            if isinstance(extra_fields, dict):
                extra_fields[name] = value
            if inspect.getattr_static(cls, name, _MISSING) is _MISSING:
                object.__setattr__(self, name, value)
            return
        object.__setattr__(self, name, value)

    @classmethod
    def _populate_instance(
        cls,
        instance: Self,
        data: Mapping[str, Any],
        *,
        stop_on_first_error: bool,
        field_prefix: str,
    ) -> Self:
        cls._ensure_hints_resolved()
        errors: dict[str, list[str]] = {}
        validated_values: dict[str, Any] = {}
        fields_set: set[str] = set()
        extra_values: dict[str, Any] = {}

        def _err_key(name: str) -> str:
            return f"{field_prefix}.{name}" if field_prefix else name

        # Map input keys (supporting both alias and canonical Python attribute names)
        resolved_inputs: dict[str, Any] = {}
        for raw_key, raw_val in data.items():
            if not isinstance(raw_key, str):
                continue
            if raw_key in cls.__schema_fields__:
                resolved_inputs[raw_key] = raw_val
            elif raw_key in cls.__schema_aliases__:
                canonical = cls.__schema_aliases__[raw_key]
                resolved_inputs.setdefault(canonical, raw_val)
            elif cls.extra == "forbid":
                key_path = _err_key(raw_key)
                errors.setdefault(key_path, []).append(
                    "Extra inputs are not permitted."
                )
                if stop_on_first_error:
                    raise ValidationException(errors, single_error=True)
            elif cls.extra == "allow":
                if raw_key.startswith("_"):
                    key_path = _err_key(raw_key)
                    errors.setdefault(key_path, []).append(
                        "Reserved attribute names are not permitted."
                    )
                    if stop_on_first_error:
                        raise ValidationException(errors, single_error=True)
                    continue
                extra_values[raw_key] = raw_val

        for fname in cls.__schema_field_order__:
            cfield = cls.__schema_fields__[fname]
            f_info = cfield.field_info
            key_path = _err_key(fname)

            if fname in resolved_inputs:
                fields_set.add(fname)
                raw_val = resolved_inputs[fname]
                try:
                    coerced = _coerce_value(
                        raw_val,
                        cfield.annotation,
                        key_path,
                        f_info,
                    )
                except _FieldCoercionError as exc:
                    for ek, emsgs in exc.messages.items():
                        errors.setdefault(ek, []).extend(emsgs)
                    if stop_on_first_error:
                        raise ValidationException(errors, single_error=True) from None
                    continue

                constraint_errs = _check_field_constraints(coerced, key_path, f_info)
                if constraint_errs:
                    errors.setdefault(key_path, []).extend(constraint_errs)
                    if stop_on_first_error:
                        raise ValidationException(errors, single_error=True)
                    continue

                for fv in cls.__field_validators__.get(fname, ()):
                    try:
                        if fv.is_classmethod:
                            bound_method = getattr(cls, fv.attr_name)
                            coerced = (
                                bound_method(coerced, validated_values)
                                if fv.param_count >= 2
                                else bound_method(coerced)
                            )
                        elif fv.takes_cls:
                            coerced = (
                                fv.func(cls, coerced, validated_values)
                                if fv.param_count >= 2
                                else fv.func(cls, coerced)
                            )
                        else:
                            coerced = (
                                fv.func(coerced, validated_values)
                                if fv.param_count >= 2
                                else fv.func(coerced)
                            )
                    except ValidationException as exc:
                        for ek, emsgs in exc.errors.items():
                            errors.setdefault(ek, []).extend(emsgs)
                        if stop_on_first_error:
                            raise ValidationException(errors, single_error=True) from None
                        break
                    except ValueError as exc:
                        errors.setdefault(key_path, []).append(str(exc))
                        if stop_on_first_error:
                            raise ValidationException(errors, single_error=True) from None
                        break
                else:
                    validated_values[fname] = coerced
                continue

            # Field omitted from input
            if cfield.is_required:
                errors.setdefault(key_path, []).append(
                    f"The {key_path} field is required."
                )
                if stop_on_first_error:
                    raise ValidationException(errors, single_error=True)
                continue

            if f_info.default_factory is not None:
                validated_values[fname] = f_info.default_factory()
            else:
                validated_values[fname] = _clone_default(f_info.default)

        if errors:
            raise ValidationException(errors, single_error=stop_on_first_error)

        for fname, val in validated_values.items():
            object.__setattr__(instance, fname, val)
        for extra_k, extra_v in extra_values.items():
            if inspect.getattr_static(cls, extra_k, _MISSING) is _MISSING:
                object.__setattr__(instance, extra_k, extra_v)
        object.__setattr__(instance, "__fields_set__", fields_set)
        object.__setattr__(instance, "__extra_fields__", extra_values)

        for mv in cls.__model_validators__:
            try:
                if mv.is_classmethod:
                    getattr(cls, mv.attr_name)(instance)
                elif mv.is_staticmethod:
                    mv.func(instance)
                else:
                    mv.func(instance)
            except ValidationException as exc:
                raise ValidationException(
                    exc.errors,
                    message=exc.message,
                    headers=exc.headers,
                    single_error=stop_on_first_error or exc.is_single,
                ) from None
            except ValueError as exc:
                root_key = field_prefix or "__root__"
                raise ValidationException(
                    {root_key: [str(exc)]},
                    single_error=stop_on_first_error,
                ) from None

        return instance

    @classmethod
    def _validate_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        stop_on_first_error: bool | None = None,
        field_prefix: str = "",
    ) -> Self:
        stop_first = (
            bool(getattr(cls, "stop_on_first_error", False))
            if stop_on_first_error is None
            else bool(stop_on_first_error)
        )
        instance = cls.__new__(cls)
        return cls._populate_instance(
            instance,
            data,
            stop_on_first_error=stop_first,
            field_prefix=field_prefix,
        )

    @classmethod
    def validate(
        cls,
        data: Mapping[str, Any] | Self,
        *,
        stop_on_first_error: bool | None = None,
    ) -> Self:
        """Validate a mapping or schema instance and return a strongly typed instance."""
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            stop_first = (
                bool(getattr(cls, "stop_on_first_error", False))
                if stop_on_first_error is None
                else bool(stop_on_first_error)
            )
            raise ValidationException(
                {"body": ["The request payload must be a JSON object."]},
                single_error=stop_first,
            )
        return cls._validate_mapping(
            data,
            stop_on_first_error=stop_on_first_error,
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        stop_on_first_error: bool | None = None,
    ) -> Self:
        """Construct and validate a schema instance from a dictionary."""
        return cls.validate(data, stop_on_first_error=stop_on_first_error)

    @classmethod
    def model_validate(
        cls,
        data: Mapping[str, Any] | Self,
        *,
        stop_on_first_error: bool | None = None,
    ) -> Self:
        """Compatibility alias for `validate()`."""
        return cls.validate(data, stop_on_first_error=stop_on_first_error)

    @classmethod
    async def from_request(
        cls,
        request: Request,
        *,
        stop_on_first_error: bool | None = None,
    ) -> Self:
        """Extract JSON or form data from an incoming HTTP Request and validate into `cls`."""
        stop_first = (
            bool(getattr(cls, "stop_on_first_error", False))
            if stop_on_first_error is None
            else bool(stop_on_first_error)
        )
        payload = await _extract_request_mapping(
            request,
            stop_on_first_error=stop_first,
        )
        return cls._validate_mapping(payload, stop_on_first_error=stop_first)

    @classmethod
    def from_model(cls, obj: Any) -> Self:
        """Build and validate a schema instance from an ORM Model, mapping, or object."""
        if isinstance(obj, cls):
            return obj
        cls._ensure_hints_resolved()
        extracted: dict[str, Any] = {}
        for fname in cls.__schema_field_order__:
            cfield = cls.__schema_fields__[fname]
            val = _extract_model_value(
                obj,
                fname,
                cfield.annotation,
                alias=cfield.field_info.alias,
            )
            if val is not _MISSING:
                extracted[fname] = val
        return cls._validate_mapping(extracted)

    def to_dict(
        self,
        *,
        exclude_none: bool = False,
        exclude_unset: bool = False,
        by_alias: bool = False,
    ) -> dict[str, Any]:
        """Serialize the schema (and nested schemas) into a dictionary."""
        fields_set: set[str] = getattr(self, "__fields_set__", set())
        out: dict[str, Any] = {}

        for fname in self.__class__.__schema_field_order__:
            cfield = self.__class__.__schema_fields__[fname]
            if cfield.field_info.exclude:
                continue
            if exclude_unset and fname not in fields_set:
                continue
            val = getattr(self, fname)
            if exclude_none and val is None:
                continue
            out_key = (
                cfield.field_info.alias
                if (by_alias and cfield.field_info.alias is not None)
                else fname
            )
            out[out_key] = _serialize_schema_value(
                val,
                exclude_none=exclude_none,
                exclude_unset=exclude_unset,
                by_alias=by_alias,
            )

        extra_fields: dict[str, Any] = getattr(self, "__extra_fields__", {})
        for ek, ev in extra_fields.items():
            if exclude_none and ev is None:
                continue
            out[ek] = _serialize_schema_value(
                ev,
                exclude_none=exclude_none,
                exclude_unset=exclude_unset,
                by_alias=by_alias,
            )
        return out

    def model_dump(
        self,
        *,
        exclude_none: bool = False,
        exclude_unset: bool = False,
        by_alias: bool = False,
    ) -> dict[str, Any]:
        """Compatibility alias for `to_dict()`."""
        return self.to_dict(
            exclude_none=exclude_none,
            exclude_unset=exclude_unset,
            by_alias=by_alias,
        )

    @property
    def fields_set(self) -> frozenset[str]:
        """Return the set of field names explicitly supplied when creating this instance."""
        raw: set[str] = getattr(self, "__fields_set__", set())
        return frozenset(raw)

    def is_set(self, field_name: str) -> bool:
        """Return True if `field_name` was explicitly supplied in input data."""
        raw: set[str] = getattr(self, "__fields_set__", set())
        return field_name in raw

    def __eq__(self, other: object) -> bool:
        if type(self) is not type(other):
            return False
        for fname in self.__class__.__schema_field_order__:
            if getattr(self, fname, _MISSING) != getattr(other, fname, _MISSING):
                return False
        extra_self: dict[str, Any] = getattr(self, "__extra_fields__", {})
        extra_other: dict[str, Any] = getattr(other, "__extra_fields__", {})
        return extra_self == extra_other

    def __repr__(self) -> str:
        parts: list[str] = []
        for fname in self.__class__.__schema_field_order__:
            parts.append(f"{fname}={getattr(self, fname, None)!r}")
        return f"{self.__class__.__name__}({', '.join(parts)})"


Schema = BaseSchema

__all__ = [
    "BaseSchema",
    "Field",
    "Schema",
    "SchemaField",
    "SchemaFieldInfo",
    "field_validator",
    "model_validator",
]
