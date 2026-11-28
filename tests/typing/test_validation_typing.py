from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, assert_type

from swiftpy import (
    Request,
    UniqueRule,
    ValidatedData,
    ValidationResult,
    unique,
    validate,
)


async def _verify_request_validate_contracts(
    request: Request,
    user_id: int,
) -> ValidatedData:
    readonly_schema: Mapping[str, Any] = MappingProxyType(
        {
            "email": f"required|email|unique:users,email,{user_id}",
        }
    )
    readonly_messages: Mapping[str, str] = MappingProxyType(
        {
            "email.unique": "Taken.",
        }
    )
    validated = assert_type(
        await request.validate(
            readonly_schema,
            messages=readonly_messages,
        ),
        ValidatedData,
    )
    _email = validated.email
    _route_val: Any = request.route("id")
    _all_routes: Any = request.route()
    _ = (_email, _route_val, _all_routes)
    return validated


async def _verify_standalone_validate_and_unique_helpers(
    request: Request,
) -> ValidationResult:
    rule = assert_type(
        unique("users", "email").ignore(42),
        UniqueRule,
    )
    result = assert_type(
        await validate(request, {"email": ["required", "email", rule]}),
        ValidationResult,
    )
    return result


def test_validation_typing_contracts_runtime_smoke() -> None:
    """Confirm the validation typing contract test module imports cleanly under pytest."""
    assert callable(_verify_request_validate_contracts)
    assert callable(_verify_standalone_validate_and_unique_helpers)
