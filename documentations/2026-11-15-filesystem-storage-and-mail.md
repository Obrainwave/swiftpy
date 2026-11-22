# Filesystem Storage, Uploads & Mail

## Introduction

SwiftPY provides unified asynchronous APIs for file storage, streaming multipart uploads, and transactional/templated email delivery:

- **Filesystem Storage (`swiftpy.filesystem`):** Manage multiple storage disks (`local`, `public`, `s3`) through `Storage` and `FilesystemManager` (`storage()`), backed by non-blocking `LocalStorageDriver` and native SigV4 `S3StorageDriver`.
- **Multipart Uploads (`UploadedFile`):** Receive bounded-memory uploads via `await request.file(...)` / `await request.files(...)`, enforce extension, MIME, size, and fail-closed magic-byte policies (`UploadedFile.validate()` / `FileValidator`), and store safely with collision-resistant filenames.
- **Transactional & Queued Mail (`swiftpy.mail`):** Compose reusable `Mailable` classes or `MailMessage` payloads with dual HTML/plain-text templates (`MailTemplateRenderer`), inline/file attachments (`Attachment`), background dispatching (`Mail.queue()` / `SendQueuedMailableJob`), pluggable transports (`console`, `log`, `smtp`, `sendmail`, `resend`, `sendgrid`, `mailgun`), and `Mail.fake()` for tests.

```python
from swiftpy.filesystem import (
    FilesystemManager,
    LocalStorageDriver,
    S3StorageDriver,
    Storage,
    UploadedFile,
    Visibility,
)
from swiftpy.filesystem.uploads.validators import FileValidator
from swiftpy.helpers import mail, storage
from swiftpy.mail import Attachment, Mail, Mailable, MailMessage
from swiftpy.mail.templates.renderer import MailTemplateRenderer
```

> **Tip on importing `mail()` and `storage()`:** Always import the helper functions from `swiftpy.helpers` rather than `from swiftpy import mail`. If another module imports the `swiftpy.mail` subpackage first, Python binds that module object onto `swiftpy.mail`, shadowing the callable `mail()` helper.

---

## Creating & Organizing Files

In a scaffolded SwiftPY application:

- **Config files:**
  - `config/filesystems.py` — configures `default` disk, `signing_key`, configured `disks` (`local`, `public`, `s3`), and multipart `uploads` size/memory limits.
  - `config/mail.py` — configures default mailer, global `from` / `reply_to` identities, `mailers`, and attachment/message size `limits`.
- **Service providers:**
  - `app/providers/filesystem_service_provider.py` — registers the `FilesystemManager` singleton in the container (`Application` shutdown closes disk drivers automatically).
  - `app/providers/mail_service_provider.py` — registers the `MailManager` singleton and closes instantiated transports on `shutdown()`.
- **Application code & views:**
  - `app/mail/` — `Mailable` subclasses (includes starter `app/mail/welcome_email.py`).
  - `views/mail/` — email HTML/text templates resolved relative to `views/` (includes starter `views/mail/template.html`).
  - `storage/app/private/` & `storage/app/public/` — default local disk storage roots.

### Generating Mailables & Linking Public Storage

Scaffold a new `Mailable` class in `app/mail/`:

```bash
swiftpy make:mail InvoiceEmail
swiftpy make:mail OrderReceipt --path app/mail --force
```

Create the `public/storage` symlink pointing to `storage/app/public` so web servers or static proxies can serve files stored on the `public` disk under `/storage/...`:

```bash
swiftpy storage:link
swiftpy storage:link --force
# Alternate grouped form:
swiftpy storage link --force
```

> **Docker & Git note:** Generated `.gitignore` and `.dockerignore` files intentionally ignore `public/storage` so host symlinks do not break Docker BuildKit context transfers (`invalid file request public/storage`). Run `swiftpy storage:link` in your target runtime environment or container entrypoint when needed.

### Essential Environment Variables

```env
APP_KEY=base64:<32-byte-base64-key>          # Used as fallback filesystem signing_key
FILESYSTEM_DISK=local
FILESYSTEM_SIGNING_KEY=                      # Optional dedicated HMAC key for local signed URLs
STORAGE_LOCAL_ROOT=storage/app/private
STORAGE_PUBLIC_ROOT=storage/app/public
STORAGE_PUBLIC_URL=/storage

# S3-compatible disk (AWS S3, MinIO, R2, Spaces)
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_DEFAULT_REGION=us-east-1
AWS_BUCKET=
AWS_URL=
AWS_ENDPOINT=
AWS_USE_PATH_STYLE_ENDPOINT=false

# Multipart upload parser limits (bytes)
UPLOAD_MAX_FILE_SIZE=104857600               # 100 MiB
UPLOAD_MAX_REQUEST_SIZE=209715200            # 200 MiB
UPLOAD_MEMORY_LIMIT=1048576                  # 1 MiB spool threshold

# Mail
MAIL_MAILER=console
MAIL_FROM_ADDRESS=hello@example.com
MAIL_FROM_NAME="SwiftPY Application"
MAIL_HOST=127.0.0.1
MAIL_PORT=587
MAIL_USERNAME=
MAIL_PASSWORD=
MAIL_ENCRYPTION=starttls
MAIL_VALIDATE_CERTS=true
```

---

## Step-by-Step Usage Guides & Practical Examples

### 1. Reading, Writing & Streaming with `Storage` & `FilesystemManager`

Use the `Storage` class facade or the `storage()` helper (`storage()` returns `FilesystemManager`; `storage("public")` returns a `StorageDisk`):

```python
from swiftpy.filesystem import Storage, Visibility
from swiftpy.helpers import storage

# Write and read on the default disk ("local" -> storage/app/private)
await Storage.put("reports/summary.txt", "Q4 revenue summary")
text = await Storage.get_text("reports/summary.txt")

# Store publicly readable files on the "public" disk
await Storage.disk("public").put(
    "banners/launch.txt",
    "Now live!",
    visibility=Visibility.PUBLIC,
)
public_url = await Storage.disk("public").url("banners/launch.txt")  # "/storage/banners/launch.txt"

# Copy, move, inspect metadata, list, and delete
await Storage.copy("reports/summary.txt", "reports/summary-bak.txt")
await Storage.move("reports/summary-bak.txt", "archive/summary.txt")
meta = await Storage.metadata("archive/summary.txt")  # FileMetadata(path, size, mime_type, last_modified, ...)
files = await Storage.files("archive", recursive=True)
digest = await Storage.checksum("archive/summary.txt", algorithm="sha256")
await Storage.delete("archive/summary.txt")
```

#### Returning File Downloads & Streaming Large Payloads

`LocalStorageDriver` offloads all disk I/O to worker threads (`asyncio.to_thread`), writes atomically via temporary files, and excludes symlinks during directory listings:

```python
from swiftpy import Request, Response
from swiftpy.filesystem import Storage

@app.get("/reports/latest")
async def download_report(request: Request) -> Response:
    # Streams chunks with Content-Length, detected Content-Type, and RFC-safe Content-Disposition
    return await Storage.download("reports/summary.txt", name="q4-summary.txt")
```

For large streams without buffering into memory:

```python
async def chunk_source():
    yield b"chunk-one-"
    yield b"chunk-two"

await Storage.write_stream("exports/big.bin", chunk_source())

async for chunk in Storage.read_stream("exports/big.bin", chunk_size=64 * 1024):
    process(chunk)
```

#### S3-Compatible Disks, On-Demand Disks & Custom Drivers

Configure `"s3"` in `config/filesystems.py` (works with AWS S3, MinIO, Cloudflare R2, and Spaces using pure-async `httpx` and native AWS SigV4 signing) or build unmanaged disks / custom drivers on the fly:

```python
from swiftpy.filesystem import LocalStorageDriver, Storage

# Ad-hoc disk with read-only protection or key prefix scoping
archive_disk = Storage.build(
    {
        "driver": "local",
        "root": "storage/archive",
        "read_only": True,
        "prefix": "2026",
    },
    name="archive_2026",
)

# Register a custom driver backend with FilesystemManager.extend() / Storage.extend()
Storage.extend("custom_local", lambda cfg: LocalStorageDriver(root=cfg["root"]))
```

#### Storage Lifecycle Events

`StorageDisk` emits events automatically via `EventDispatcher` when bound in the container:

- `FileUploaded` (`"storage.file.uploaded"`) after `put`, `put_file`, `put_file_as`, `write_stream`, and `copy`
- `FileDeleted` (`"storage.file.deleted"`) after `delete`
- `FileMoved` (`"storage.file.moved"`) after `move`

### 2. Signed Temporary URLs

Generate time-limited, HMAC-SHA256-signed URLs on `LocalStorageDriver` or AWS SigV4 presigned URLs on `S3StorageDriver`:

```python
from datetime import timedelta
from swiftpy import Request, Response, json_response
from swiftpy.filesystem import Storage
from swiftpy.filesystem.exceptions import InvalidSignatureError

# Local disk: uses FILESYSTEM_SIGNING_KEY (falls back to APP_KEY)
signed_url = await Storage.disk("local").temporary_url(
    "invoices/inv-42.pdf",
    expires_in=timedelta(minutes=15),  # or int seconds > 0
)

# Inside your signed download route handler (note: SwiftPY path params must be identifiers):
@app.get("/storage/{folder}/{filename}")
async def serve_signed(request: Request, folder: str, filename: str) -> Response:
    qs = request.scope.get("query_string", b"").decode("utf-8", errors="replace")
    full_path = f"{request.path}?{qs}" if qs else request.path
    try:
        rel_path = Storage.disk("local").verify_temporary_url(full_path, method="GET")
    except InvalidSignatureError:
        return json_response({"error": "Invalid or expired download link."}, status_code=403)
    return await Storage.disk("local").download(rel_path)
```

> **Fail-closed signing key requirement:** `LocalStorageDriver.temporary_url()` and `verify_temporary_url()` raise `ConfigurationError` if neither `FILESYSTEM_SIGNING_KEY` nor `APP_KEY` is configured. On `S3StorageDriver`, `temporary_url()` returns an S3 SigV4 presigned URL (`X-Amz-Signature`); `verify_temporary_url()` is local-only and raises `DriverNotSupportedError` on S3 disks.

### 3. Handling Multipart Uploads & Magic-Byte Validation

`Request.file(name)` and `Request.files(name)` parse `multipart/form-data` requests lazily via `MultipartParser`, spooling uploads cleanly to temporary storage once parts exceed `UPLOAD_MEMORY_LIMIT` (`1 MiB` by default):

```python
from swiftpy import Request, Response
from swiftpy.http.response import json_response

@app.post("/profile/avatar")
async def upload_avatar(request: Request) -> Response:
    avatar = await request.file("avatar")
    if avatar is None:
        return json_response({"error": "Missing 'avatar' file field."}, status_code=422)

    # Validate size, extension, declared MIME, and actual magic file signature
    await avatar.validate(
        {
            "max_size": "5MB",
            "min_size": "100B",
            "extensions": ["jpg", "jpeg", "png", "webp"],
            "mimes": ["image/jpeg", "image/png", "image/webp"],
            "magic_bytes": True,
        }
    )

    # Stores with a cryptographically random UUID filename (e.g. "avatars/<hex>.png")
    stored_path = await avatar.store("avatars", disk="public")
    return json_response(
        {
            "path": stored_path,
            "url": await Storage.disk("public").url(stored_path),
            "bytes": await avatar.get_real_size(),
        },
        status_code=201,
    )
```

Key `UploadedFile` & `FileValidator` rules:

- **Route parameter & upload existence checks:** Besides `await request.file("avatar")` / `await request.files("attachments")`, handlers can inject uploads directly by parameter name using `avatar: UploadedFile` or `attachments: list[UploadedFile]`, or check whether an upload was provided via `if request.has_file("avatar"):` (or `if await request.has_file("avatar"):`).
- **Rules mapping (`dict` required):** Supported keys are `"max_size"`, `"min_size"` (integers or units like `"500B"`, `"10KB"`, `"5MB"`, `"1GB"`), `"extensions"`, `"mimes"`, and `"magic_bytes"`. Passing a list instead of a mapping raises `TypeError`.
- **Fail-closed magic-byte verification:** `magic_bytes=True` reads the file's leading header bytes (`32` bytes) and verifies them against signatures derived from your server policy (`extensions` / `mimes`), supporting `image/jpeg`, `image/png`, `image/gif`, `image/webp`, `application/pdf`, and `application/zip`. Unknown types without registered magic signatures fail closed rather than passing.
- **Anti-spoofing & HTTP exceptions:** `FileValidator` checks `await file.get_real_size()` against actual spooled bytes alongside declared size. Size violations raise `FileTooLargeError` (HTTP `413`); extension, MIME, and magic-byte failures raise `InvalidFileExtensionError` / `InvalidMimeTypeError` / `FileValidationError` (HTTP `422` when handled by SwiftPY's exception handler).
- **Saving options:**
  - `await upload.store(directory, disk=...)` generates a collision-resistant hex filename while keeping a sanitized extension.
  - `await upload.store_as(directory, filename, disk=...)` writes with an explicit normalized relative name.
  - `await upload.save(destination, base_dir=...)` writes directly to a local filesystem path while rejecting `..`, null bytes, symlinks, and escapes outside `base_dir` (or `Path.cwd()` for relative targets).

### 4. Composing & Sending Mail (`Mail`, `Mailable`, `MailMessage` & `Attachment`)

#### Building a `Mailable` with a Template

Starter example from `app/mail/welcome_email.py`:

```python
from __future__ import annotations

from swiftpy.mail import Attachment, Mailable, MailMessage


class WelcomeEmail(Mailable):
    mailer_name: str | None = None
    queue: str = "emails"
    tries: int = 3
    timeout: float | None = 60.0
    backoff: int | float = 5

    def __init__(self, name: str, to: str | list[str] | None = None) -> None:
        super().__init__()
        self.name = name
        self.to_address = [to] if isinstance(to, str) else (to or [])

    def build(self) -> MailMessage:
        return MailMessage(
            subject=f"Welcome to SwiftPY, {self.name}!",
            to=self.to_address,
            template="mail/template.html",  # Resolves views/mail/template.html (+ .txt if present)
            context={
                "title": f"Welcome, {self.name}!",
                "content": "Thank you for joining our platform.",
                "app_name": "SwiftPY",
            },
        )
```

Send immediately via `Mail.send()`:

```python
from swiftpy.mail import Attachment, Mail, MailMessage

# Send a Mailable using the default mailer from config/mail.py
result = await Mail.send(WelcomeEmail("Ada", to="ada@example.com"))

# Or build a MailMessage directly with attachments & inline images
msg = (
    MailMessage(
        subject="Monthly Invoice",
        to=["billing@example.com"],
        cc=["finance@example.com"],
        html_body='<p>See invoice attached.</p><img src="cid:logo">',
        text_body="See invoice attached.",
    )
    .attach(Attachment.inline_from_bytes(b"<svg/>", cid="logo", filename="logo.svg", content_type="image/svg+xml"))
    .attach_from_bytes(b"%PDF-1.7...", filename="invoice.pdf", content_type="application/pdf")
    .add_header("X-Invoice-Id", "inv-42")
)
result = await Mail.send(msg, mailer="smtp")
```

Key `MailMessage` / `Envelope` behavior:

- **Default sender & reply-to:** When `from_address` is omitted, `MailManager` applies `mail.from.address` / `mail.from.name` from `config/mail.py` without mutating the caller's `MailMessage`.
- **HTML fallback:** When `html_body` is provided without `text_body`, `Envelope.build()` automatically generates a stripped plain-text alternative alongside the HTML part.
- **Injection & limit guards:** `MailMessage` and `Attachment` reject CRLF / `\x00` bytes in subjects, addresses, headers, filenames, and `cid` values (`HeaderInjectionError`), validate email formats (`InvalidAddressError`), and enforce `max_attachment_size_bytes` (`25 MiB` default) and `max_message_size_bytes` (`35 MiB` default) with `AttachmentTooLargeError`.

### 5. Dual HTML & Plain-Text Email Templates (`MailTemplateRenderer`)

By default, `MailTemplateRenderer` resolves templates inside `views/` (configurable via `MailTemplateRenderer(template_dir=...)`):

- Passing `template="mail/welcome"` (or `"mail/welcome.html"`) renders **both** `views/mail/welcome.html` and `views/mail/welcome.txt` if present, populating `html_body` and/or `text_body` (explicitly set `html_body` / `text_body` on `MailMessage` take precedence).
- When `jinja2` is installed, SwiftPY uses `jinja2.sandbox.SandboxedEnvironment` with autoescaping enabled for `.html`/`.xml`. Without Jinja2, the built-in fallback renderer substitutes `{{ nested.var }}` with HTML quote escaping for `.html` and unescaped text for `.txt`.
- Both directory traversal (`../...`) and private/dunder attribute lookups (`_...` / `__...`) raise `MailValidationError`.

### 6. Mail Transports, Background Queuing & `Mail.fake()`

#### Configured Mail Transports

Configured under `config/mail.py` `"mailers"` (and accessible via `mail("smtp")` / `Mail.mailer("smtp")`):

| Mailer Key / Transport | Backend & Delivery Notes |
| :--- | :--- |
| `"console"` / `"log"` | `ConsoleTransport` — logs metadata to `swiftpy.mail.<channel>` and retains `sent_messages` in memory for local dev |
| `"smtp"` | `SMTPTransport` — async SMTP supporting `"starttls"`, `"tls"` / `"ssl"`, or plain mode via `aiosmtplib` (when installed) or native `asyncio` RFC 5321 streaming |
| `"sendmail"` | `SendmailTransport` — non-blocking `asyncio.create_subprocess_exec` invocation with configurable `timeout` |
| `"resend"` | `ResendTransport` — Resend REST API (`RESEND_API_KEY`) via `httpx.AsyncClient` |
| `"sendgrid"` | `SendGridTransport` — SendGrid v3 API (`SENDGRID_API_KEY`) via `httpx.AsyncClient` |
| `"mailgun"` | `MailgunTransport` — Mailgun multipart v3 API (`MAILGUN_API_KEY`, `MAILGUN_DOMAIN`) |

Register a custom transport with `Mail.extend("postmark", factory)`.

#### Queueing Mailables with `Mail.queue()`

Dispatch emails off the request path using SwiftPY's queue subsystem (`SendQueuedMailableJob`), honoring `after_commit=True` inside `db.transaction()`:

```python
from swiftpy.database.transaction import db
from swiftpy.mail import Mail

async with db.transaction():
    user = await User.create(name="Ada", email="ada@example.com")
    await Mail.queue(WelcomeEmail(user.name, to=user.email), after_commit=True)
```

Security rules for queued `Mailable` classes:

- `SendQueuedMailableJob` inherits `Job`'s HMAC-SHA256 `APP_KEY` signing and default trusted namespaces (`["app", "swiftpy"]`). Mailables defined outside `app.*` / `swiftpy.*` must be allowlisted via `SendQueuedMailableJob.allow_mailable_class(MyExternalMailable)` (or `Job.set_trusted_namespaces([...])`).
- Public instance attributes extracted into `mailable_state` must be declared on the class (`__init__` params, class annotations/attributes, or `self.<attr>` assignments) and JSON-serializable; private (`_...`) or descriptor attributes are rejected on worker deserialization.
- Transient transport failures (`MailConnectionError`, `MailRateLimitError`, `MailTimeoutError`) retry using the `Mailable`'s `tries` / `backoff`, while permanent errors (`MailValidationError`, `MailAuthenticationError`, `MailConfigurationError`) route directly to the dead-letter queue without retrying.

#### Testing Emails with `Mail.fake()`

`Mail.fake()` swaps delivery with an in-memory `MailFake` isolated via `ContextVar` (safe across concurrent async tests) and restores automatically when used as a context manager:

```python
from swiftpy.mail import Mail

with Mail.fake() as fake:
    await Mail.send(WelcomeEmail("Ada", to="ada@example.com"))
    await Mail.queue(WelcomeEmail("Bob", to="bob@example.com"))

    fake.assert_sent(WelcomeEmail, recipient="ada@example.com", count=1)
    fake.assert_not_sent(recipient="charlie@example.com")
    fake.assert_queued(WelcomeEmail, count=1)
```

---

## Important Defaults & Production Safeguards

- **Configure `FILESYSTEM_SIGNING_KEY` or `APP_KEY` before using local signed URLs:** `LocalStorageDriver.temporary_url()` and `verify_temporary_url()` fail closed with `ConfigurationError` if no signing key is configured.
- **Pair `magic_bytes=True` with explicit `extensions` and `mimes` allowlists:** File extension and `Content-Type` headers are client-controlled; `magic_bytes=True` inspects leading file bytes and fails closed on unknown or spoofed signatures (`InvalidFileExtensionError` / `InvalidMimeTypeError` / `FileValidationError`).
- **Keep private files off the `"public"` disk:** Store sensitive uploads on `"local"` (`storage/app/private`) or `"s3"` with `Visibility.PRIVATE` and issue short-lived `temporary_url()` downloads; use `swiftpy storage:link` only for public assets in `storage/app/public`.
- **Keep SMTP certificate validation enabled & secrets in env vars:** `MAIL_VALIDATE_CERTS` defaults to `true`; REST API transports automatically sanitize tokens (`Bearer ...`, `sk_live_...`, `SG....`, `key-...`) from upstream error messages, and production deployments must provide real `MAIL_*` / provider keys rather than leaving `MAIL_MAILER=console`.
- **Use `Mail.queue(..., after_commit=True)` inside transactions & allowlist external Mailables:** Prevents sending emails for rolled-back database writes, and call `await MailManager.get_instance().close()` / `await FilesystemManager.get_instance().close()` (or let `Application` shutdown run) when managing transports manually outside the app lifecycle.

---

## Next Step

Continue to [WebSockets](2026-11-15-websockets-and-admin.md) to build real-time `@app.websocket` endpoints, manage topic broadcasts, and enforce WebSocket origin validation with `WebSocketManager`.
