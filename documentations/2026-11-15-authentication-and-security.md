# Authentication, JWT, Token Blacklist & API Keys

## Introduction

SwiftPY ships a complete, security-hardened authentication subsystem supporting bcrypt password hashing, symmetric (`HS256`) and asymmetric (`RS256`) JSON Web Tokens, Redis-backed token revocation (`TokenBlacklist`), constant-time HMAC-SHA256 `<prefix>.<secret>` API keys (`ApiKeyGuard`), multi-guard dispatch (`AuthManager`), and task-isolated user context (`CurrentUser`).

Core authentication symbols can be imported from `swiftpy.auth` or lazily from top-level `swiftpy`:

```python
from swiftpy.auth import (
    ApiKeyGuard,
    AuthenticationError,
    AuthManager,
    CurrentUser,
    Hash,
    InvalidApiKeyError,
    JWTGuard,
    JWTIssuer,
    TokenBlacklist,
    TokenRevokedError,
    current_user,
    generate_api_key,
    hash_api_key,
    hash_password,
    hash_password_async,
    require_api_key,
    require_jwt,
    verify_api_key_hash,
    verify_password,
    verify_password_async,
)
```

---

## Creating & Organizing Files

In a scaffolded SwiftPY project, authentication configuration, providers, and middleware live in standard locations:

- **`config/auth.py`**: Reads environment variables via `env()` / `env_bool()` for default guards, bcrypt cost rounds, JWT settings, API key lookup columns, and blacklist settings.
- **`app/providers/auth_service_provider.py`**: Registers `JWTIssuer`, `TokenBlacklist`, `AuthManager`, and `Hash` as singletons, binds `CurrentUser` as a task-scoped dependency, and validates production secrets on `boot()`.
- **`app/http/middleware/auth_middleware.py` & `admin_middleware.py`**: Starter ASGI middlewares for `AuthManager` authentication and `is_admin=True` authorization.

### Scaffolding API Key Migrations and Keys with the CLI

Use `swiftpy make:api-key` to generate a cryptographically random `<prefix>.<secret>` API key and its peppered HMAC-SHA256 hash—and optionally create the `api_keys` database migration:

```bash
# Generate an API key pair (requires AUTH_KEY_PEPPER outside debug mode, or pass --pepper)
swiftpy make:api-key --prefix sk_live_

# Generate an API key AND create the api_keys migration file
swiftpy make:api-key --migration

# Customize the migration output path or overwrite an existing migration
swiftpy make:api-key -m --path database/migrations --force
```

Available flags on `swiftpy make:api-key`:

| Flag | Default | Description |
| :--- | :--- | :--- |
| `--prefix` | `sk_live_` | Key prefix before the random hex suffix (e.g. `sk_live_a1b2c3d4`) |
| `--pepper` | `None` | Explicit server pepper (`>= 32` bytes), falling back to `AUTH_KEY_PEPPER` |
| `-m`, `--migration` | `False` | Also generate the `create_api_keys_table` migration file |
| `--path` | `None` | Target directory for `-m` migration output (defaults to `database/migrations`) |
| `--force` | `False` | Overwrite an existing migration file if present |

For login validation and business services, generate request and service classes with:

```bash
swiftpy make:request LoginRequest
swiftpy make:service AuthService
```

### Configuration & Environment Variables (`config/auth.py`)

```env
APP_KEY=base64:<32-byte-base64-key>
AUTH_GUARD=jwt
AUTH_BLACKLIST_ENABLED=true
# Opt-in (default false): load the User model by `sub` on verified tokens (one query per request)
AUTH_HYDRATE_USER=false
# Opt-in (default false): resolve a valid Bearer token on routes without JWTGuard
AUTH_PASSIVE_RESOLUTION=false
BCRYPT_ROUNDS=12

# JWT (HS256 requires at least 32 UTF-8 bytes)
JWT_ALGORITHM=HS256
JWT_SECRET=replace-with-at-least-32-bytes-of-random-entropy
JWT_ACCESS_TTL=3600
JWT_REFRESH_TTL=604800
# Optional strict claim validation
# JWT_ISSUER=https://api.example.com
# JWT_AUDIENCE=https://app.example.com
# JWT_LEEWAY=0

# API Keys (AUTH_KEY_PEPPER requires at least 32 UTF-8 bytes)
AUTH_KEY_PEPPER=replace-with-another-32-byte-random-secret
```

When `APP_DEBUG=false` (or `APP_ENV=production`), `AuthServiceProvider.boot()` checks the active default guard (`auth.defaults.guard`) and fails fast with `ConfigurationError` if `JWT_SECRET` (for `jwt`) or `AUTH_KEY_PEPPER` (for `api_key`) is missing or shorter than 32 bytes.

---

## Step-by-Step Usage Guides & Practical Examples

### 1. Password Hashing (`Hash` & Helper Functions)

SwiftPY provides the Laravel-style `Hash` support class (`swiftpy.support.hashing` / `swiftpy.auth`) alongside module-level helper functions backed by `bcrypt`:

```python
from swiftpy.auth import (
    Hash,
    hash_password,
    hash_password_async,
    verify_password,
    verify_password_async,
)

# Sync API (ideal for CLI scripts, sync tests, or non-async contexts)
hashed = Hash.make("Secret123!")                 # UTF-8 "$2b$12$..." string
is_valid = Hash.check("Secret123!", hashed)      # True
should_upgrade = Hash.needs_rehash(hashed)       # True if cost != configured rounds

# Async API (offloads CPU-heavy bcrypt to asyncio.to_thread for ASGI handlers/seeders)
hashed_async = await Hash.make_async("Secret123!", rounds=12)
ok_async = await Hash.check_async("Secret123!", hashed_async)

# Equivalent convenience functions
hashed = hash_password("Secret123!")
is_valid = verify_password("Secret123!", hashed)
hashed_async = await hash_password_async("Secret123!")
ok_async = await verify_password_async("Secret123!", hashed_async)
```

Key password hashing behaviors:

- **PostgreSQL / ORM ready:** `Hash.make()` and `Hash.make_async()` always return a UTF-8 `str` (`"$2b$..."`) rather than raw `bytes`.
- **Configurable bcrypt rounds:** `rounds` defaults to `auth.hash.rounds` (`BCRYPT_ROUNDS` in `config/auth.py`, default `12`) and validates integer bounds `4..31` (rejecting booleans or out-of-range values with `TypeError`/`ValueError`).
- **Safe >72-byte password support:** Inputs up to 72 UTF-8 bytes use raw UTF-8 bcrypt directly (remaining compatible with existing `$2a$`/`$2b$`/`$2y$` hashes), while inputs longer than 72 UTF-8 bytes are automatically SHA-256 pre-hashed so `bcrypt>=5.0` never truncates or raises length errors.
- **Malformed hash safety:** `Hash.check()` and `Hash.check_async()` return `False` cleanly on `""`, non-bcrypt strings, or non-string inputs instead of crashing handlers.
- **Transparent cost upgrades:** Call `Hash.needs_rehash(user.password)` after a successful login to re-hash and save passwords when you raise `BCRYPT_ROUNDS`.

### 2. Issuing & Verifying JWTs (`JWTIssuer`)

`JWTIssuer` signs and validates `HS256` (HMAC-SHA256) and `RS256` (RSA PKCS#1 v1.5 SHA-256) tokens with unique `jti` identifiers and strict claim checks:

```python
from swiftpy.auth import JWTIssuer

# Resolved automatically from Config by AuthServiceProvider, or constructed explicitly:
issuer = JWTIssuer(
    secret="at-least-32-bytes-of-secret-material!",
    algorithm="HS256",
    access_token_ttl=3600,     # 60 minutes (default)
    refresh_token_ttl=604800,  # 7 days (default)
    issuer="https://api.example.com",
    audience="web-client",
    leeway=5,
)

# Issue an access + refresh pair
tokens = issuer.issue_token_pair(
    subject=42,
    claims={"email": "ada@example.com", "is_admin": False},
)
# Returns:
# {
#     "access_token": "<jwt>",
#     "refresh_token": "<jwt>",
#     "token_type": "Bearer",
#     "expires_in": 900,
# }

# Decode and verify signature, exp, nbf, future iat, iss, aud, and expected token_type
payload = issuer.decode_token(tokens["access_token"], expected_type="access")
```

For asymmetric microservice setups where only an auth service signs tokens and downstream APIs verify them with a public key:

```python
private_pem, public_pem = JWTIssuer.generate_rsa_key_pair(key_size=2048)
auth_issuer = JWTIssuer(private_key=private_pem, algorithm="RS256")
verifier = JWTIssuer(public_key=public_pem, algorithm="RS256")
```

Important `JWTIssuer` rules:

- **Reserved claims cannot be overridden:** Passing `sub`, `iat`, `exp`, `jti`, `type`, `token_type`, `typ`, `iss`, `aud`, or `nbf` inside `claims` raises `ValueError`.
- **Isolated refresh claims:** `issue_token_pair()` attaches `claims` to the access token **only**; pass `refresh_claims={...}` if you explicitly want custom claims on the refresh token.
- **`CurrentUser` and `auth()` are available as soon as an access token is issued:** `issue_token()`, `issue_access_token()`, `issue_token_pair()` and `await refresh_token_pair()` bind the user into the request `Context`, so an already-injected `CurrentUser` and `auth().check()` / `auth().id()` / `auth().user()` work in the same request (for example inside `/login`). Refresh tokens never bind a user.
- **What gets bound:** pass the subject as `user.id`, a saved `Model` instance, or a mapping with an `id`/`sub` key. Pass `user=` (a `Model` or mapping) to attach more fields to a plain id. Sensitive keys (`password`, `password_hash`, `key_hash`, `secret`, `token`) and `__hidden__` model fields are never bound. A `user=` whose id does not match the subject raises `ValueError`, and a model without a primary key cannot be used as a subject.
- **Existing sessions win:** issuing a token for a *different* subject while another user is authenticated (for example an admin minting a token) leaves the authenticated user untouched.
- **Strict algorithm enforcement:** `alg="none"` (in any case) and algorithm mismatches (`"hs256"`, HS256 token submitted to an RS256 verifier) raise `InvalidAlgorithmError`.

### 3. Complete Login Endpoint & Service Example

Combine `FormRequest`, `UserRepository`, `Hash.check_async()`, and `JWTIssuer` using SwiftPY's auto-wired dependency injection:

```python
from __future__ import annotations

from typing import Any

from app.repositories.user_repository import UserRepository
from swiftpy.auth import AuthenticationError, Hash, JWTIssuer
from swiftpy.http.response import Response, json_response
from swiftpy.http.router import Router
from swiftpy.validation.form_request import FormRequest

router = Router()


class LoginRequest(FormRequest):
    def rules(self) -> dict[str, str]:
        return {
            "email": "required|email|max:255",
            "password": "required|string",
        }


class AuthService:
    def __init__(self, users: UserRepository, jwt: JWTIssuer) -> None:
        self.users = users
        self.jwt = jwt

    async def login(self, email: str, password: str) -> dict[str, Any]:
        user = await self.users.find_by_email(email.strip().lower())
        if user is None or not await Hash.check_async(password, user.password):
            raise AuthenticationError("Invalid email or password.")

        if Hash.needs_rehash(user.password):
            user.password = await Hash.make_async(password)
            await user.save()

        tokens = self.jwt.issue_token_pair(
            subject=user.id,
            claims={"email": user.email, "is_admin": user.is_admin},
        )
        return {**tokens, "user": user.to_dict()}


@router.post("/auth/login")
async def login(payload: LoginRequest, auth: AuthService) -> Response:
    return json_response(await auth.login(payload.email, payload.password))
```

### 4. Protecting Routes with `JWTGuard`, `require_jwt` & `CurrentUser`

Protect routes or `RouteGroup` blocks with `middleware=[JWTGuard]`, then inject `CurrentUser` (or call `current_user()`) inside handlers:

```python
from app.http.middleware.admin_middleware import AdminMiddleware
from swiftpy.auth import CurrentUser, JWTGuard, current_user
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.http.router import RouteGroup, Router

router = Router()


@router.get("/auth/me", middleware=[JWTGuard])
async def me(request: Request, user: CurrentUser) -> Response:
    # CurrentUser behaves like a live task-scoped dict with .id, .email, .is_authenticated
    return json_response({
        "id": user.id,
        "email": user.email,
        "claims": user.to_dict(),
        "helper_match": current_user() == user.to_dict(),
    })


@router.group("/admin", middleware=[JWTGuard, AdminMiddleware])
def admin_routes(group: RouteGroup) -> None:
    @group.get("/metrics")
    async def metrics(request: Request, user: CurrentUser) -> Response:
        # AdminMiddleware requires user["is_admin"] is True (else HTTP 403)
        return json_response({"admin_id": user.id})
```

```python
@router.post("/auth/login")
async def login(payload: LoginRequest, jwt: JWTIssuer, current: CurrentUser) -> Response:
    user = await authenticate(payload)            # your own lookup/password check
    tokens = jwt.issue_token_pair(user)           # or jwt.issue_token_pair(user.id, user=user)

    # Available immediately, without waiting for the next request:
    assert auth().check() and auth().id() == user.id
    assert current.is_authenticated and current.email == user.email
    return json_response(tokens)
```

**Tokens that carry only `sub`.** `require_jwt()` / `JWTGuard` bind the verified claims. Set `AUTH_HYDRATE_USER=true` (`auth.hydrate_user`) and, when a `DatabasePool` is initialized, they also load the User model by primary key so `CurrentUser` and `auth().user()` expose every non-hidden field. This costs one query per authenticated request and is off by default; if the lookup fails the request continues with the token claims and a warning is logged. `require_jwt()` returns the bound user dictionary (token claims plus model fields) rather than the raw claims.

**Routes without `JWTGuard`.** By default only guards authenticate. Set `AUTH_PASSIVE_RESOLUTION=true` (`auth.passive_resolution`, default guard `jwt`) to let routes without a guard resolve a valid `Authorization: Bearer <access_token>` into `CurrentUser` / `auth()`. Invalid, expired or revoked tokens are ignored and the request stays a guest; use `JWTGuard` when you need a `401`.

> `CurrentUser.id` returns the raw token `sub` (a string) when no model is bound, while `auth().id()` coerces it to the User model's primary key type. Compare with `str(...)` or use `auth().id()` consistently.

For token refresh endpoints, use `await jwt.refresh_token_pair(refresh_token)` rather than calling `issue_token_pair()` manually. It verifies the incoming token with `expected_type="refresh"`, checks `TokenBlacklist`, revokes the previous refresh token's `jti` automatically (`rotate=True` by default) so reused refresh tokens fail with `TokenRevokedError` (`401`), and issues a fresh access/refresh pair:

```python
from swiftpy.auth import JWTIssuer
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.validation.form_request import FormRequest


class RefreshRequest(FormRequest):
    def rules(self) -> dict[str, str]:
        return {"refresh_token": "required|string"}


@router.post("/auth/refresh")
async def refresh(payload: RefreshRequest, jwt: JWTIssuer) -> Response:
    # Verifies refresh token, rejects blacklisted/reused tokens (401 TokenRevokedError),
    # rotates by blacklisting the previous refresh JTI, and returns a new token pair.
    tokens = await jwt.refresh_token_pair(payload.refresh_token, rotate=True)
    return json_response(tokens)
```

> **WebSocket JWT support:** `require_jwt()` also extracts Bearer tokens from `Sec-WebSocket-Protocol` (`["bearer", "<jwt>"]` or `"bearer.<jwt>"`) or `?token=` / `?access_token=` query parameters on WebSocket handshakes (avoid query-string tokens in production where access logs record URLs).

### 5. Token Revocation, `auth().logout()` & `auth().logout_all()`

`TokenBlacklist` uses Redis to track revoked `jti` IDs and subject-wide cutoffs with automatic TTL expiration (`min(exp - now, max_token_lifetime)`, capped at 7 days by default).

The ergonomic `auth().logout(...)` and `auth().logout_all(...)` helpers handle bearer extraction, optional refresh-token revocation, `TokenBlacklist` lookups, and per-request auth context cleanup together:

```python
from swiftpy.auth import JWTGuard, JWTIssuer, TokenBlacklist, auth
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response


@router.post("/auth/logout", middleware=[JWTGuard])
async def logout(request: Request) -> Response:
    data = await request.json() if await request.body() else {}
    refresh_token = data.get("refresh_token") if isinstance(data, dict) else None

    # Extracts Bearer access token, revokes access + optional refresh token in TokenBlacklist,
    # and clears per-request Context/request.state auth state
    await auth().logout(request, refresh_token=refresh_token)
    return json_response({"logged_out": True})


@router.post("/auth/logout-all", middleware=[JWTGuard])
async def logout_all(request: Request) -> Response:
    # Revokes all active access & refresh tokens issued before now for the current user
    await auth().logout_all()
    return json_response({"logged_out_all": True})
```

You can also interact with `TokenBlacklist` directly (`await blacklist.revoke_token(token, issuer=jwt)`, `await blacklist.revoke_if_not_revoked(jti, ttl=...)`, `await blacklist.revoke_all_for_subject(user_id)`).

`require_jwt()`, `JWTGuard`, and `JWTIssuer.refresh_token_pair()` automatically consult `TokenBlacklist` whenever `AUTH_BLACKLIST_ENABLED=true` (the default). If Redis becomes unavailable during a check, SwiftPY **fails closed** with HTTP `503 AuthenticationServiceUnavailableError` rather than letting potentially revoked tokens through. Set `AUTH_BLACKLIST_ENABLED=false` if your application runs stateless JWTs without Redis.

### 6. API Key Authentication (`ApiKeyGuard` & `require_api_key`)

For service-to-service or developer API keys, SwiftPY enforces a two-part `<prefix>.<secret>` format:

- `key_prefix` is stored indexed in PostgreSQL (`WHERE "key_prefix" = $1 LIMIT 1` for O(1) lookup).
- `key_hash` stores the HMAC-SHA256 digest of `<secret>` keyed with `AUTH_KEY_PEPPER` (never storing plaintext secrets).

Programmatic key creation and verification:

```python
from swiftpy.auth import generate_api_key, hash_api_key, verify_api_key_hash

# Return `full_key` once to the caller; persist only `key_prefix` and `key_hash`
full_key, key_prefix, secret = generate_api_key(prefix="sk_live_")
key_hash = hash_api_key(secret)  # Uses AUTH_KEY_PEPPER from Config

assert verify_api_key_hash(secret, key_hash) is True
```

Protecting routes with `ApiKeyGuard`:

```python
from swiftpy.auth import ApiKeyGuard, CurrentUser
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response


@router.get("/v1/webhooks", middleware=[ApiKeyGuard])
async def list_webhooks(request: Request, caller: CurrentUser) -> Response:
    # Reads x-api-key header (or Authorization: ApiKey <key> / Bearer <key>)
    # Sensitive fields (key_hash, secret, password, password_hash) are stripped automatically
    return json_response({"caller_name": caller.get("name")})
```

Built-in `ApiKeyGuard` checks:

- Rejects keys missing the `.` `<prefix>.<secret>` separator immediately with `InvalidApiKeyError` (`HTTP 401`).
- Verifies `is_active` / `active` flags, `revoked` flag, and timezone-aware `expires_at` timestamps.
- Offloads legacy `$2...` bcrypt API key hashes to an executor thread so the event loop is not blocked.
- Supports custom validation callbacks via `await require_api_key(request, key_validator=my_async_lookup)` when API keys live outside SQL tables.

> **Customizing `AUTH_API_KEY_TABLE` / columns with `middleware=[ApiKeyGuard]`:** When used directly as route middleware, `ApiKeyGuard` resolves using its default constructor parameters (`table="api_keys"`, `prefix_column="key_prefix"`, `hash_column="key_hash"`, `header_name="x-api-key"`—matching `swiftpy make:api-key -m`). If you override `auth.guards.api_key.*` in `config/auth.py`, either resolve via `AuthManager` (`await auth.authenticate(request, guard_name="api_key")`, which calls `ApiKeyGuard.from_config()`) or bind `container.singleton(ApiKeyGuard, lambda c: ApiKeyGuard.from_config(c.resolve(Config), c.resolve(DatabasePool)))` in your `ServiceProvider`.

### 7. Multi-Guard Routing (`AuthManager`) & Cryptographic Keys (`APP_KEY`)

Use `AuthManager` (injected or resolved via the container) when endpoints select guards dynamically or when registering custom authentication drivers:

```python
from swiftpy.auth import AuthManager
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response


@router.get("/partner/data")
async def partner_data(request: Request, auth: AuthManager) -> Response:
    record = await auth.authenticate(request, guard_name="api_key")
    return json_response({"partner": record["name"]})
```

For custom guards (e.g. session cookies or third-party tokens), register a factory in a `ServiceProvider`:

```python
auth_manager.extend("custom", lambda container: MyCustomGuard())
```

Elsewhere in the framework (queued jobs, signed URLs, Redis event bus), [keys.py](file:///home/brainwave/pyprojects/swiftpy/swiftpy/support/keys.py) provides `validate_signing_key()` and `decode_signing_key()` supporting both raw UTF-8 strings (`>= 32` bytes) and `base64:`-encoded `APP_KEY` values (`APP_PREVIOUS_KEYS` for key rotation).

---

## Important Defaults & Production Safeguards

- **Fail-closed secrets in production (`debug=False`):**
  - `JWTIssuer.from_config()` requires `JWT_SECRET` (`>= 32` bytes for `HS256`) or valid `RS256` keys (`ConfigurationError` otherwise).
  - `get_server_pepper()` / `ApiKeyGuard` requires `AUTH_KEY_PEPPER` (`>= 32` bytes).
  - In debug/development mode only (`APP_DEBUG=true`), missing secrets fall back to ephemeral random per-config keys with a security warning log—never rely on this in staging or production, as tokens invalidate on every restart.
- **Non-blocking password verification in async handlers:** Always call `await Hash.make_async()` and `await Hash.check_async()` (or `hash_password_async` / `verify_password_async`) inside `async def` routes, middlewares, and seeders so CPU-bound bcrypt never freezes concurrent ASGI requests.
- **Do not put secrets in JWT payloads:** JWT payloads are base64url-encoded, not encrypted. Put non-sensitive identifiers and flags (such as `email`, `is_admin`, or role names) in `claims`, never passwords or API secrets.
- **Generic 401 errors & fail-closed 503 dependencies:** Both `JWTGuard` and `ApiKeyGuard` log detailed failure causes server-side while returning generic `"Invalid or expired token"` / `"Invalid API key"` `401` responses to clients, and `503 AuthenticationServiceUnavailableError` if Redis or PostgreSQL fails unexpectedly.
- **Do not weaken `JWTIssuer` algorithm or claim protections:** `alg="none"` is forbidden across all case variants, `"hs256"` / `"rs256"` lowercase tricks are rejected, and `TokenBlacklist.revoke_token()` refuses unverified tokens.

---

## Next Step

Continue to [Structured Asynchronous Logging](2026-11-20-logging.md) to configure `config/logging.py` (`single`, `daily`, `stderr`, `stack`) and write structured non-blocking logs with `Log` and `LogManager`.
