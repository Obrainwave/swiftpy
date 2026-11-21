# HTTP Requests & Responses

## Introduction

SwiftPY wraps incoming ASGI HTTP scopes into **`Request`** objects and converts handler outputs into **`Response`** or **`StreamingResponse`** objects. Understanding the full `Request` input lifecycle—especially when payloads are loaded automatically versus awaited explicitly—keeps your handlers concise without sacrificing memory safety or upload limits.

Quick imports:

```python
from swiftpy import Application, Request, Response, json_response
from swiftpy.http.request import RequestHeaders
from swiftpy.http.response import (
    ResponseHeaders,
    StreamingResponse,
    created,
    encode_json,
    forbidden,
    not_found,
    stream,
    unauthorized,
    unprocessable,
)
```

---

## Working with `Request`

### 1. Metadata, Headers & Query Parameters

Synchronous request metadata is available immediately without `await`:

| Member | Type | Behavior |
|---|---|---|
| `request.method` | `str` | Uppercase HTTP verb (`"GET"`, `"POST"`, etc.) |
| `request.path` | `str` | Decoded URL path (e.g., `"/users/42"`) |
| `request.scheme` | `str` | URL scheme from ASGI scope (`"http"` or `"https"`, defaults to `"http"`) |
| `request.scope` | `dict[str, Any]` | Raw ASGI scope dictionary |
| `request.state` | `dict[str, Any]` | Per-request mutable dict used by middleware and auth guards |
| `request.headers` | `RequestHeaders` | Case-insensitive immutable `Mapping[str, str]` |
| `request.query_params` | `dict[str, str]` | First value for each URL query key (UTF-8 decoded) |
| `request.query_param_list` | `dict[str, list[str]]` | All values for repeated query keys (`?tag=a&tag=b`) |

`RequestHeaders` decodes header keys/values as `latin-1` per the ASGI spec and supports repeated headers cleanly:

```python
@router.get("/search")
async def search(request: Request) -> dict[str, object]:
    user_agent = request.headers.get("User-Agent", "unknown")
    forwarded = request.headers.getlist("x-forwarded-for")
    query_text = request.query_params.get("q", "")
    tags = request.query_param_list.get("tag", [])
    return {
        "user_agent": user_agent,
        "forwarded": forwarded,
        "q": query_text,
        "tags": tags,
    }
```

### 2. Async Body Methods (`body`, `json`, `form`, `file`, `files`)

Request bodies arrive over the ASGI `receive` channel and are cached on the `Request` instance once parsed:

- **`await request.body(max_size=None) -> bytes`:** Reads and buffers raw request bytes while enforcing `Content-Length` and streaming chunk limits.
- **`await request.json() -> Any`:** Parses UTF-8 JSON (empty body returns `{}`) and validates JSON nesting depth (`max_json_depth`). Malformed JSON or exceeded depth raises `HttpException(status_code=400)`.
- **`await request.form() -> FormData`:** Parses `application/x-www-form-urlencoded` and streaming `multipart/form-data` submissions (`fields_list` / `files_list`).
- **`await request.file(name) -> UploadedFile | None`:** Retrieves the first uploaded file for `name`.
- **`request.has_file(name)`:** Checks whether a non-empty uploaded file with a filename was submitted for field `name`. Inside `Application` routes (with `auto_load_request_input=True`), you can call either `if request.has_file("image"):` synchronously or `if await request.has_file("image"):` asynchronously (`empty` browser file inputs with `filename=""`, text fields, and JSON payloads return `False`).
- **`await request.files(name=None) -> list[UploadedFile] | dict[str, list[UploadedFile]]`:** Retrieves all uploaded files for `name`, or the full mapping of uploaded files when `name` is `None`.

Call `await request.close()` only when working outside the ASGI pipeline; `Application` automatically closes parsed `FormData` temporary files in a `finally` block at the end of every HTTP request.

### 3. Unified Input & Direct Attribute Access (`request.input()`, `request.email`)

Why two mechanisms? HTTP bodies arrive asynchronously, yet writing `payload = await request.json()` followed by `payload.get("email")` in every simple endpoint adds repetitive boilerplate. SwiftPY bridges both:

- **`await request.input(key=None, default=None)`:** Loads and caches a merged dictionary combining URL `query_params` with the parsed request body (body fields take precedence over query parameters).
  - For `application/json`, `*+json`, or empty `Content-Type` with a non-empty JSON object body: parses JSON and merges top-level keys into the input map. Non-dict JSON payloads (such as `[1, 2, 3]`) leave `_non_dict_payload = True`.
  - For `application/x-www-form-urlencoded` and `multipart/form-data`: parses form fields (`str` for single value, `list[str]` when repeated) and uploaded files (`UploadedFile` or `list[UploadedFile]`).
- **`request.<field>` (`__getattr__`), `request.get(key, default=None)`, `request.has(key)`, `request.all()`:** Sync accessors reading from loaded input (or `query_params` if the body has not been loaded). Missing attribute keys raise `AttributeError`; use `request.get("field")` or `request.has("field")` when an input field is optional.

#### How Auto-Loading Works

When your route handler declares a `Request` parameter, `Application` automatically preloads `await request.input()` **before calling your handler**, **only when all of these hold**:

1. `Application(auto_load_request_input=True)` (the default; pass `auto_load_request_input=False` to disable globally).
2. HTTP method is `POST`, `PUT`, `PATCH`, or `DELETE`.
3. `Transfer-Encoding` is **not** `chunked`.
4. `Content-Type` media type is `application/json`, `*+json` (such as `application/problem+json`), or `application/x-www-form-urlencoded`.

`multipart/form-data` uploads are automatically preloaded **only when** `has_request_param` and `auto_load_request_input=True` and the handler calls `request.has_file(...)`; otherwise `multipart/form-data`, arbitrary binary payloads, and chunked streams are **never** auto-read before your handler runs—your handler keeps full control over streaming multipart parsing and file size validation. Furthermore, if an auto-preloaded request body is not valid JSON (for example, a raw webhook payload sent with an `application/json` header), preload buffers the raw bytes (still enforcing `max_body_size` / `413`) and defers any `400 Invalid JSON payload` error until your handler explicitly calls `await request.json()` or `await request.input()`—so handlers that only inspect `await request.body()` continue to run normally.

```python
from swiftpy import Request, Response, json_response, validate
from swiftpy.http.router import Router

router = Router(prefix="/auth")


@router.post("/register")
async def register(request: Request) -> Response:
    # Works immediately because POST + application/json auto-loads input
    email = request.email
    referral = request.get("referral_code")

    result = (
        await validate(
            request,
            {"email": "bail|required|email", "password": "required|str|min:8"},
        )
    ).raise_if_failed().validated()

    return json_response({"email": email, "referral": referral, "ok": bool(result.email)})
```

> [!WARNING]
> **Real `Request` members and reserved security names always take precedence over payload keys.** Attributes and methods defined directly on `Request` (`path`, `method`, `scheme`, `headers`, `scope`, `state`, `body`, `json`, `form`, `file`, `files`, `input`, `all`, `get`, `has`, `query_params`, `query_param_list`, `close`) as well as reserved security/ASGI attribute names (`user`, `auth`, `session`, `admin_user`, `is_authenticated`, `is_admin`, `client`, `server`, `root_path`, `http_version`, `asgi`, `cookies`) are **never** resolved from client input via `request.<field>`. This prevents untrusted query/body parameters like `?user=admin&is_admin=1` from spoofing `getattr(request, "user", None)` or `hasattr(request, "is_admin")`. Always store per-request auth/application state in `request.state` or `Context`, and read input keys with those names via `request.get("user")` or `await request.input("user")`.

---

## Body Limits & Disconnect Protection

Default limits enforced per request (`swiftpy/http/request.py`):

| Setting | Default | Behavior When Exceeded |
|---|---|---|
| `max_body_size` | `2 * 1024 * 1024` (2 MiB) | Raises `PayloadTooLargeError` (`413`) |
| `max_content_length` | `None` (falls back to `max_body_size`; `Application` defaults to `2 MiB` unless overridden) | Rejects upfront from `Content-Length` with `413` |
| `max_json_depth` | `32` levels | Rejects with `HttpException(400)` |

Additional protections:

- Duplicate conflicting `Content-Length` headers or negative/non-integer values return `400 Bad Request`.
- Mid-body client disconnects raise `ClientDisconnect`, which aborts handler execution cleanly without logging false `500` server errors.
- Override limits globally via `Application(max_body_size=..., max_content_length=..., max_json_depth=...)`, `http.*` config keys, or per route:

```python
@router.post("/webhooks", max_body_size=512 * 1024, max_json_depth=16)
async def webhook(request: Request) -> dict[str, bool]:
    payload = await request.json()
    return {"accepted": isinstance(payload, dict)}
```

For `multipart/form-data` uploads, `request.form()` also consults `config/filesystems.py` (`filesystems.uploads.max_file_size`, `max_request_size`, `memory_limit`, `max_field_size`, `temp_dir`).

---

## Returning Responses

### 1. Auto-JSON & `json_response()` / `encode_json()`

Returning a Python `dict` or `list` directly from an HTTP route handler is equivalent to returning `json_response(data)`.

`encode_json()` serializes values strictly (`allow_nan=False`) and natively converts:

- `datetime`, `date`, `time` -> `.isoformat()`
- `UUID`, `Decimal` -> `str(...)`
- `Enum` -> `.value`
- Objects defining `.to_dict()` (ORM `Model`, `UserResponseSchema`, `APIResource`) -> dictionary result, stripping any field names listed in `__hidden__`

Raw `bytes`, `NaN`/`Infinity` floats, and arbitrary objects without `.to_dict()` raise `TypeError` cleanly rather than corrupting JSON.

### 2. Response Helpers

| Helper | Default Status | Response Body & Extra Headers |
|---|---|---|
| `json_response(data, status_code=200, headers=None)` | `200` | JSON payload + `content-type: application/json` |
| `created(data)` | `201` | JSON payload + `201 Created` |
| `not_found(message="Not Found")` | `404` | `{"detail": message}` |
| `unprocessable(errors)` | `422` | `{"errors": errors}` |
| `unauthorized(message="Unauthorized", headers=None)` | `401` | `{"detail": message}` + `WWW-Authenticate: Bearer` |
| `forbidden(message="Forbidden")` | `403` | `{"detail": message}` |
| `stream(iterator, *, status_code=200, content_type="application/octet-stream")` | `200` | Chunked `StreamingResponse` |

### 3. `Response`, `ResponseHeaders` & Cookies

Use `Response(content=b"", status_code=200, headers=None)` for HTML, XML, CSV, plain text (`Response("ok")` sets `text/plain; charset=utf-8`), or `204 No Content`.

`ResponseHeaders` rejects `\r`, `\n`, and `\x00` in header names and values to block CRLF header injection, supports single-value assignment (`response.headers["X-Version"] = "2"`), and preserves repeated headers via `.add(name, value)`, `.append(name, value)`, and `.set_cookie(cookie_value)`:

```python
@router.post("/cookie-demo")
async def cookie_demo() -> Response:
    res = Response(status_code=204)
    res.set_cookie("locale=en; Path=/; SameSite=Lax")
    res.set_cookie("theme=dark; Path=/; HttpOnly")
    return res
```

`StreamingResponse` omits `Content-Length`, streams chunks from any `AsyncIterator[bytes | str]`, and always awaits `.aclose()` (if defined on the iterator) in a `finally` block when streaming completes or aborts. Note that `HEAD` requests automatically suppress sending response body bytes while retaining headers.

---

## Important Defaults & Safeguards

- **`_`-prefixed attributes on `Request`:** Dynamic attribute lookup (`request.<field>`) skips `_`-prefixed names so internal Python/descriptor checks never fall back to request input.
- **No multipart auto-buffering:** Because multipart forms stay out of `_should_auto_load_request_input()`, large file uploads stream through `MultipartParser` to disk cleanly once you call `await request.form()` or bind `UploadedFile`.
- **Disable auto-loading when forwarding raw proxies:** Construct `Application(auto_load_request_input=False)` if proxy routes read raw streams via `await request.body()` and do not want framework pre-parsing on `POST`/`PUT`/`PATCH`/`DELETE`.

---

## Next Step

Continue to [Error Handling & Exceptions](2026-11-20-error-handling.md) to learn `abort()`, declarative `HttpException` subclasses, custom handlers, single vs. multi-field validation errors, and production traceback masking.
