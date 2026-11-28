from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, assert_type

from swiftpy import (
    FormRequest,
    Request,
    UniqueRule,
    ValidatedData,
    ValidationResult,
    unique,
    validate,
)
from swiftpy.schemas import BaseSchema, Field


class DocVersionCreate(BaseSchema):
    version: str = Field(..., min_length=1)
    title: str
    notes: str | None = Field(default=None)


class UnparameterizedDocRequest(FormRequest):
    def rules(self) -> dict[str, Any]:
        return {
            "version": "required|str",
            "title": "required|str",
            "notes": "nullable|str",
        }


class TypedDocVersionRequest(FormRequest[DocVersionCreate]):
    def rules(self) -> dict[str, Any]:
        return {
            "version": "required|str|unique:doc_versions,version",
            "title": "required|str",
            "notes": "nullable|str",
        }


class ChildTypedDocVersionRequest(TypedDocVersionRequest):
    pass


class _BaseGenericDocRequest[T](FormRequest[T]):
    pass


class SpecializedDocVersionRequest(_BaseGenericDocRequest[DocVersionCreate]):
    pass


class ExplicitValidatedDataRequest(FormRequest[ValidatedData]):
    pass


class DocVersionService:
    async def create_doc_version(self, data: DocVersionCreate) -> DocVersionCreate:
        _ = (data.version, data.title, data.notes, data["version"], "version" in data)
        _ref = data.get("notes")
        _ = _ref
        return data.validated()


def _inspect_generic_form(cls: type[FormRequest], inst: FormRequest) -> Any:
    return (cls, inst.validated())


async def _verify_form_request_and_schema_agreement(
    service: DocVersionService,
    untyped_req: UnparameterizedDocRequest,
    typed_req: TypedDocVersionRequest,
    child_req: ChildTypedDocVersionRequest,
    specialized_req: SpecializedDocVersionRequest,
    explicit_vd_req: ExplicitValidatedDataRequest,
    inline_generic_req: FormRequest[DocVersionCreate],
    request: Request,
) -> DocVersionCreate:
    # 1. Existing unparameterized FormRequest passes cleanly to DocVersionCreate services
    untyped_payload = untyped_req.validated()
    _ = await service.create_doc_version(untyped_payload)
    _explicit_vd = assert_type(explicit_vd_req.validated(), ValidatedData)
    _ = _explicit_vd

    # 2. Explicit schema conversion works from FormRequest or ValidatedData
    explicit_from_req = assert_type(
        untyped_req.validated(DocVersionCreate),
        DocVersionCreate,
    )
    explicit_via_to_schema = assert_type(
        untyped_req.to_schema(DocVersionCreate),
        DocVersionCreate,
    )
    _ = (explicit_from_req, explicit_via_to_schema)

    # 3. Parameterized FormRequest[DocVersionCreate] infers exact schema types
    typed_payload = assert_type(typed_req.validated(), DocVersionCreate)
    _child_payload = assert_type(child_req.validated(), DocVersionCreate)
    _spec_payload = assert_type(specialized_req.validated(), DocVersionCreate)
    _inline_payload = assert_type(inline_generic_req.validated(), DocVersionCreate)
    _ = (_child_payload, _spec_payload, _inline_payload)

    # 4. Inline request.validate supports schema class and schema_cls parameter
    direct_schema = assert_type(
        await request.validate(DocVersionCreate),
        DocVersionCreate,
    )
    hybrid_schema = assert_type(
        await request.validate(
            {"version": "required|str", "title": "required|str"},
            schema_cls=DocVersionCreate,
        ),
        DocVersionCreate,
    )
    _ = (direct_schema, hybrid_schema)

    # 5. Covariance: helpers typed as FormRequest accept parameterized subclasses
    _ = _inspect_generic_form(UnparameterizedDocRequest, untyped_req)
    _ = _inspect_generic_form(TypedDocVersionRequest, typed_req)

    return assert_type(
        await service.create_doc_version(typed_payload),
        DocVersionCreate,
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
    _dumped = assert_type(validated.to_dict(exclude_unset=True), dict[str, Any])
    _dump_alias = assert_type(validated.model_dump(exclude_none=True), dict[str, Any])
    _fset = assert_type(validated.fields_set, frozenset[str])
    _is_set = assert_type(validated.is_set("email"), bool)
    _converted = assert_type(validated.to_schema(DocVersionCreate), DocVersionCreate)
    _route_val: Any = request.route("id")
    _all_routes: Any = request.route()
    _ = (_email, _dumped, _dump_alias, _fset, _is_set, _converted, _route_val, _all_routes)
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
    _vd = assert_type(result.validated(), ValidatedData)
    _typed = assert_type(result.validated(DocVersionCreate), DocVersionCreate)
    _ = (_vd, _typed)
    return result


def test_validation_typing_contracts_runtime_smoke() -> None:
    """Confirm the validation typing contract test module imports cleanly under pytest."""
    assert callable(_verify_form_request_and_schema_agreement)
    assert callable(_verify_request_validate_contracts)
    assert callable(_verify_standalone_validate_and_unique_helpers)
    assert TypedDocVersionRequest.schema_model is DocVersionCreate
    assert ChildTypedDocVersionRequest.schema_model is DocVersionCreate
    assert SpecializedDocVersionRequest.schema_model is DocVersionCreate
