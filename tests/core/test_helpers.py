from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

from swiftpy import isset as top_level_isset
from swiftpy.core.config import Config
from swiftpy.database.orm.model import Model
from swiftpy.database.orm.relationships import HasOne
from swiftpy.helpers import isset
from swiftpy.helpers.helper import isset as helper_isset
from swiftpy.http.request import Request
from swiftpy.schemas import BaseSchema, Field
from swiftpy.validation.form_request import FormRequest
from swiftpy.validation.validator import ValidatedData


@dataclass
class _Address:
    city: str
    zip_code: str | None = None
    _internal_code: str = "secret-code"


class _SlottedBox:
    __slots__ = ("_hidden_slot", "empty", "value")

    def __init__(self, value: Any, empty: Any = None) -> None:
        self.value = value
        self.empty = empty
        self._hidden_slot = "slot-secret"


class _CustomMapping(Mapping[str, Any]):
    def __init__(self, items: dict[str, Any]) -> None:
        self._items = items

    def __getitem__(self, key: str) -> Any:
        return self._items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)


class _PropertyHolder:
    def __init__(self, first: str) -> None:
        self.first = first

    @property
    def label(self) -> str:
        return f"user:{self.first}"

    def compute(self) -> str:
        return self.first


class _ProfileModel(Model):
    __table__ = "isset_profiles"
    id: int
    bio: str | None = None
    __guarded__: ClassVar[list[str]] = []


class _UserModel(Model):
    __table__ = "isset_users"
    __prevent_lazy_loading__: ClassVar[bool] = True

    id: int
    name: str
    age: int = 0
    nickname: str | None = None
    profile = HasOne("_ProfileModel", foreign_key="user_id")
    __guarded__: ClassVar[list[str]] = []


class _SampleFormRequest(FormRequest):
    def rules(self) -> dict[str, Any]:
        return {"email": "required|email"}


def _make_request(
    *,
    method: str = "POST",
    query_string: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": "/test",
        "query_string": query_string,
        "headers": headers or [],
    }
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def test_isset_exports_and_scalar_checks() -> None:
    assert isset is helper_isset
    assert isset is top_level_isset

    assert isset(None) is False
    assert isset(0) is True
    assert isset(False) is True
    assert isset("") is True
    assert isset([]) is True
    assert isset({}) is True


def test_isset_dict_dot_notation_literal_and_multikey() -> None:
    payload: dict[str, Any] = {
        "name": "Alice",
        "empty_str": "",
        "zero": 0,
        "flag": False,
        "empty_list": [],
        "null_val": None,
        "user.name": "literal-dotted",
        "shadowed.none": None,
        "shadowed": {"none": "nested"},
        "user": {
            "profile": {
                "email": "alice@example.com",
                "bio": None,
            },
            "roles": ["admin", {"scope": "write"}, None],
        },
        "none_branch": None,
    }

    assert isset(payload, "name") is True
    assert isset(payload, "empty_str") is True
    assert isset(payload, "zero") is True
    assert isset(payload, "flag") is True
    assert isset(payload, "empty_list") is True
    assert isset(payload, "null_val") is False
    assert isset(payload, "missing") is False

    # Nested dot-notation
    assert isset(payload, "user.profile.email") is True
    assert isset(payload, "user.profile.bio") is False
    assert isset(payload, "user.roles.0") is True
    assert isset(payload, "user.roles.1.scope") is True
    assert isset(payload, "user.roles.2") is False
    assert isset(payload, "user.roles.9") is False
    assert isset(payload, "none_branch.anything") is False

    # Literal key takes precedence over dot traversal
    assert isset(payload, "user.name") is True
    assert isset(payload, "shadowed.none") is False

    # Malformed dot paths and unhashable keys never crash
    assert isset(payload, "") is False
    assert isset(payload, ".name") is False
    assert isset(payload, "user.") is False
    assert isset(payload, "user..profile") is False
    assert isset(payload, ["unhashable"]) is False

    # Multiple variadic keys require every key to exist and be non-None
    assert isset(payload, "name", "zero", "user.profile.email") is True
    assert isset(payload, "name", "null_val") is False
    assert isset(payload, "name", "missing") is False
    assert isset(None, "name") is False


def test_isset_sequences_sets_and_primitives() -> None:
    items = ["first", None, 0, {"k": "v"}]
    assert isset(items, 0) is True
    assert isset(items, 1) is False
    assert isset(items, 2) is True
    assert isset(items, -1) is True
    assert isset(items, -3) is False
    assert isset(items, 99) is False
    assert isset(items, "3.k") is True
    assert isset(items, True) is False  # bool is not a sequence index

    tags = {"swift", "py", 0, None}
    assert isset(tags, "swift") is True
    assert isset(tags, 0) is True
    assert isset(tags, "missing") is False
    assert isset(tags, None) is False
    assert isset(frozenset({"a"}), "a") is True

    # Primitives do not expose string/int built-in methods as keys
    assert isset("hello", "upper") is False
    assert isset("hello", 0) is False
    assert isset(42, "real") is False
    assert isset({"n": 42}, "n.bit_length") is False


def test_isset_objects_dataclasses_slots_and_properties() -> None:
    addr = _Address(city="Lagos", zip_code=None)
    holder = SimpleNamespace(
        profile=addr,
        count=0,
        absent=None,
        _private_token="top-secret",
    )
    assert isset(holder, "profile.city") is True
    assert isset(holder, "profile.zip_code") is False
    assert isset(holder, "count") is True
    assert isset(holder, "absent") is False
    assert isset(holder, "missing") is False
    # Underscore-prefixed attributes on objects/dataclasses must be blocked
    assert isset(holder, "_private_token") is False
    assert isset(holder, "profile._internal_code") is False
    assert isset(addr, "_internal_code") is False

    slotted = _SlottedBox(value="ok", empty=None)
    assert isset(slotted, "value") is True
    assert isset(slotted, "empty") is False
    assert isset(slotted, "other") is False
    assert isset(slotted, "_hidden_slot") is False

    prop_obj = _PropertyHolder("ada")
    assert isset(prop_obj, "first") is True
    assert isset(prop_obj, "label") is True
    assert isset(prop_obj, "compute") is False
    assert isset(prop_obj, "__class__") is False

    # Underscore-prefixed keys in dicts and Mapping implementations must be preserved
    raw_map = {"_token": "csrf-123", "__meta": {"_id": 42, "_empty": None}}
    assert isset(raw_map, "_token") is True
    assert isset(raw_map, "__meta._id") is True
    assert isset(raw_map, "__meta._empty") is False

    custom_map = _CustomMapping({"_api_key": "k-1", "public": "ok", "_null": None})
    assert isset(custom_map, "_api_key", "public") is True
    assert isset(custom_map, "_null") is False


async def test_isset_with_framework_types(tmp_path: Path) -> None:
    # ValidatedData
    validated = ValidatedData({"user": ValidatedData({"email": "a@b.com", "bio": None})})
    assert isset(validated, "user.email") is True
    assert isset(validated, "user.bio") is False

    # Config
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "app.py").write_text(
        'CONFIG = {"name": "Swift", "nullable": None, "nested": {"ok": False}}\n',
        encoding="utf-8",
    )
    cfg = Config.load(cfg_dir)
    assert isset(cfg, "app.name") is True
    assert isset(cfg, "app.nested.ok") is True
    assert isset(cfg, "app.nullable") is False
    assert isset(cfg, "app.missing") is False

    # Request (query, JSON body, headers)
    json_req = _make_request(
        query_string=b"page=2",
        headers=[(b"content-type", b"application/json")],
        body=b'{"user": {"name": "Ada", "opt": null}}',
    )
    assert isset(json_req, "page") is True
    assert isset(json_req, "user.name") is False  # JSON not loaded yet
    await json_req.input()
    assert isset(json_req, "page", "user.name") is True
    assert isset(json_req, "user.opt") is False
    assert isset(json_req.headers, "Content-Type") is True
    assert isset(json_req.headers, "X-Missing") is False

    # FormData & multipart Request (parsed through real MultipartParser)
    boundary = "----SwiftPYHelperBoundary"
    multipart_body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="title"\r\n\r\n'
        "Hello\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="avatar"; filename="avatar.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
        "png-data\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    multipart_req = _make_request(
        headers=[
            (b"content-type", f"multipart/form-data; boundary={boundary}".encode())
        ],
        body=multipart_body,
    )
    form = await multipart_req.form()
    assert isset(form, "title") is True
    assert isset(form, "avatar") is True
    assert isset(form, "missing") is False

    assert isset(multipart_req, "title") is True
    assert isset(multipart_req, "avatar") is True
    assert isset(multipart_req, "avatar.filename") is True

    # FormRequest (raw_data before validation, data after validation)
    raw_fr = _SampleFormRequest(raw_data={"email": "raw@example.com"}, request=multipart_req)
    assert isset(raw_fr, "email") is True
    assert isset(raw_fr, "missing") is False
    # Internal FormRequest attributes must not be treated as form input keys
    assert isset(raw_fr, "request") is False
    assert isset(raw_fr, "raw_data") is False
    assert isset(raw_fr, "data") is False
    assert isset(raw_fr, "validation_result") is False
    assert isset(raw_fr, "request.path") is False

    validated_fr = _SampleFormRequest(
        data=ValidatedData({"email": "valid@example.com", "extra": None}),
        raw_data={"email": "raw@example.com", "stripped": "ignored"},
        request=multipart_req,
    )
    assert isset(validated_fr, "email") is True
    assert isset(validated_fr, "extra") is False
    assert isset(validated_fr, "stripped") is False
    assert isset(validated_fr, "request") is False
    assert isset(validated_fr, "raw_data") is False

    # If the form data itself contains 'request' or 'raw_data' keys, isset() returns True
    fr_with_named_inputs = _SampleFormRequest(
        data=ValidatedData({"request": "user-request-field", "raw_data": {"nested": 1}}),
        request=multipart_req,
    )
    assert isset(fr_with_named_inputs, "request") is True
    assert isset(fr_with_named_inputs, "raw_data.nested") is True
    await multipart_req.close()

    # Model: columns + loaded vs unloaded relations without triggering lazy-load error
    user = _UserModel(id=1, name="Alice", age=0, nickname=None)
    assert isset(user, "name", "age") is True
    assert isset(user, "nickname") is False
    assert isset(user, "unknown") is False
    assert isset(user, "profile") is False  # Unloaded relation must NOT raise
    # Underscore-prefixed Model internals must be blocked
    assert isset(user, "_original") is False
    assert isset(user, "_exists") is False
    assert isset(user, "_loaded_relations") is False
    assert isset(user, "_init_keys") is False
    assert isset(user, "_modified_keys") is False
    assert isset(user, "__table__") is False
    assert isset({"user": user}, "user._original") is False
    assert isset({"user": user}, "user.name") is True

    user._loaded_relations["profile"] = _ProfileModel(id=9, bio="Backend dev")
    assert isset(user, "profile") is True
    assert isset(user, "profile.bio") is True
    assert isset(user, "profile._original") is False
    assert isset(user, "profile._exists") is False

    user._loaded_relations["profile"] = None
    assert isset(user, "profile") is False

    # Callable references typing check usage cleanly
    fn: Callable[[], Awaitable[None]] | None = None
    assert isset({"cb": fn}, "cb") is False

    # BaseSchema: canonical fields, aliases, nested schemas, and extra="allow" safety
    class _NestedMeta(BaseSchema):
        extra = "allow"
        region: str

    class _ItemSchema(BaseSchema):
        item_name: str = Field(..., alias="itemName")
        notes: str | None = None
        meta: _NestedMeta

    item = _ItemSchema.validate(
        {
            "itemName": "Feed",
            "meta": {"region": "north", "to_dict": "safe-extra"},
        }
    )
    assert isset(item, "item_name") is True
    assert isset(item, "itemName") is True
    assert isset(item, "notes") is False
    assert isset(item, "meta.region") is True
    assert isset(item, "meta.to_dict") is True
    assert isset(item, "__fields_set__") is False
