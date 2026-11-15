from __future__ import annotations

from typing import Any, ClassVar
from unittest.mock import patch

import pytest

from swiftpy.core.application import Application
from swiftpy.core.config import ConfigurationError
from swiftpy.http.response import json_response
from swiftpy.testing import TestClient
from swiftpy.validation.form_request import FormRequest
from swiftpy.validation.rules.registry import RuleRegistry
from swiftpy.validation.validator import Validator, validate


class CachedPostRequest(FormRequest):
    schema: ClassVar[dict[str, Any]] = {
        "title": "required|str|min:3",
        "views": "required|int|min:0",
    }


def test_form_request_parses_rules_once_and_caches() -> None:
    CachedPostRequest.clear_rule_cache()
    registry = RuleRegistry()

    with patch.object(registry, "parse_rules", wraps=registry.parse_rules) as mock_parse:
        # First call parses rules
        parsed1 = CachedPostRequest.get_parsed_rules(registry)
        assert "title" in parsed1
        assert "views" in parsed1
        assert mock_parse.call_count == 2

        # Second call returns cached rules without invoking parse_rules again
        parsed2 = CachedPostRequest.get_parsed_rules(registry)
        assert parsed2 is parsed1
        assert mock_parse.call_count == 2


class BuggyRule:
    """A custom rule that has a bug and raises TypeError during execution."""

    def validate(self, field: str, value: Any) -> bool:
        # Intentionally raise a TypeError inside the rule execution
        raise TypeError("Simulated rule implementation bug")


@pytest.mark.asyncio
async def test_rule_raising_type_error_propagates_and_is_not_swallowed() -> None:
    validator = Validator(
        data={"item": "test"},
        schema={"item": [BuggyRule()]},
    )
    with pytest.raises(TypeError):
        await validator.validate()


@pytest.mark.asyncio
async def test_non_dict_payload_returns_structured_422() -> None:
    # Testing validator directly with non-dict input
    non_dict_vals: list[Any] = [[], "string", 12345, None, True]
    for non_dict_val in non_dict_vals:
        res = await validate(non_dict_val, {"field": "required"})
        assert not res.passes()
        assert res.errors == {"body": ["The request body must be a JSON object."]}


@pytest.mark.asyncio
async def test_rule_order_independence_for_coercion() -> None:
    # Rule order 1: required|int|min:5
    res1 = await validate(
        {"count": "10"},
        {"count": "required|int|min:5"},
    )
    assert res1.passes()
    assert res1.validated_data["count"] == 10

    # Rule order 2: min:5|required|int (reversed order)
    res2 = await validate(
        {"count": "10"},
        {"count": "min:5|required|int"},
    )
    assert res2.passes()
    assert res2.validated_data["count"] == 10

    # Failing case in both orders: "3" is < 5
    res_fail1 = await validate({"count": "3"}, {"count": "required|int|min:5"})
    assert not res_fail1.passes()
    res_fail2 = await validate({"count": "3"}, {"count": "min:5|required|int"})
    assert not res_fail2.passes()


class BrokenRulesRequest(FormRequest):
    def rules(self) -> dict[str, Any]:
        # Buggy rules() method that raises TypeError
        raise TypeError("Intentional TypeError in rules()")


def test_form_request_get_rules_fails_closed_with_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="Failed to resolve validation rules"):
        BrokenRulesRequest.get_rules()


@pytest.mark.asyncio
async def test_broken_form_request_fails_closed_and_never_executes_handler() -> None:
    app = Application()
    handler_executed = False

    @app.post("/test-broken")
    async def broken_endpoint(form: BrokenRulesRequest) -> Any:
        nonlocal handler_executed
        handler_executed = True
        return json_response({"status": "ok"})

    client = TestClient(app)
    response = await client.post("/test-broken", json={"any": "data"})

    # Must fail closed with 500 Internal Server Error (or 500 error pipeline)
    assert response.status_code == 500
    # Handler must NEVER execute
    assert handler_executed is False


class DependencyInjectedService:
    def get_allowed_prefix(self) -> str:
        return "SWIFT_"


class DependencyInjectedFormRequest(FormRequest):
    def __init__(self, service: DependencyInjectedService) -> None:
        super().__init__()
        self.service = service

    def rules(self) -> dict[str, Any]:
        return {
            "code": "required|str|min:3",
        }


@pytest.mark.asyncio
async def test_form_request_with_constructor_di_dependency() -> None:
    app = Application()
    app.container.singleton(DependencyInjectedService)

    @app.post("/test-di-form")
    async def di_form_endpoint(form: DependencyInjectedFormRequest) -> Any:
        assert isinstance(form.service, DependencyInjectedService)
        return json_response({"code": form.get("code"), "prefix": form.service.get_allowed_prefix()})

    client = TestClient(app)
    response = await client.post("/test-di-form", json={"code": "SWIFT_PRO"})
    assert response.status_code == 200
    assert response.json() == {"code": "SWIFT_PRO", "prefix": "SWIFT_"}
