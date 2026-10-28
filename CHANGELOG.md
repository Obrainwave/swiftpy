# Changelog

All notable changes to SwiftPY will be documented in this file.

## [Unreleased]

### Phase 1 – Shared Foundations
- **P1-F1 (Security / Database):** Consolidated SQL identifier validation into `swiftpy.database.identifiers` (`validate_identifier`, `validate_column_identifier`, `quote_identifier`) using `re.fullmatch` and enforcing PostgreSQL's 63-character limit (`MAX_IDENTIFIER_LENGTH = 63`). Rejected trailing newlines (`"users\n"`), whitespace, and wildcards (`"*"`) in `WHERE`, `JOIN`, `ORDER BY`, `INSERT`, and `UPDATE` column identifiers across `QueryBuilder`, migrations (`Blueprint`, `Schema`, `Migrator`), validation rules (`UniqueRule`, `ExistsRule`), and `ApiKeyGuard`.
- **P1-F2 (Support / ORM):** Extracted `snake_case` and `pluralize` into `swiftpy.support.strings` (re-exported from `swiftpy.helpers` and `swiftpy.helpers.helper` for backward compatibility) to eliminate circular import coupling with the ORM.
  - **Breaking:** Fixed acronym handling in `snake_case` so consecutive uppercase prefixes stay grouped (`snake_case("APIKey") == "api_key"`, `snake_case("HTTPServer") == "http_server"`). Models with acronym class names that do not declare an explicit `__table__` now infer pluralized snake_case table names accordingly (e.g. `APIKey` infers `api_keys` instead of `a_p_i_keys`). Set `__table__` explicitly on models that need the legacy table name.
- **P1-F3 (Support / Redis Config):** Added `swiftpy.support.redis_config` (`build_redis_url`, `resolve_redis_params`) to resolve Redis connection settings exclusively from `Config` with RFC-compliant URL percent-encoding (`urllib.parse.quote`) for passwords and usernames, while preserving subsystem-specific database overrides (`REDIS_QUEUE_DB`, `REDIS_CACHE_DB`, `REDIS_EVENTS_DB`).

### Phase 2 – Security Blockers
- **P2-A1 (Security / Auth – JWT Secret Enforcement):**
  - **Breaking:** `JWTIssuer` now requires an explicit `secret` of at least 32 bytes for symmetric algorithms (`HS256`) or a valid key pair for `RS256`, raising `ConfigurationError` otherwise. Removed the hardcoded fallback secret from `config/auth.py`. In non-debug mode (`APP_DEBUG=false`), `app.boot()` and `JWTIssuer.from_config()` fail fast with `ConfigurationError` when `JWT_SECRET` is unset; in debug mode, an ephemeral per-call random secret is generated with a security warning.
  - Wired `issuer`, `audience`, and `leeway` into `config/auth.py` and `JWTIssuer.from_config()`.
- **P2-A2 (Security / Auth – API Key Guard Redesign):**
  - **Breaking:** Redesigned API keys around a `<prefix>.<secret>` format (`generate_api_key`, `hash_api_key`, `verify_api_key_hash`) backed by a single indexed `WHERE "<prefix_column>" = $1 LIMIT 1` lookup and constant-time HMAC-SHA256 verification with `AUTH_KEY_PEPPER`. Legacy unsegmented API keys without a `<prefix>.<secret>` separator are rejected upfront with `InvalidApiKeyError` (HTTP 401), and the full-table scan fallback has been removed. Existing unsegmented keys must be re-issued using `swiftpy make:api-key`.
  - Legacy `$2...` bcrypt hashes on prefix-matched rows are verified inside `loop.run_in_executor` so the asyncio event loop is never blocked.
  - Added `ApiKeyGuard.from_config()`, active transaction connection reuse via `get_active_connection()`, stripping of `key_hash`/`secret`/`password` fields from `request.state["user"]` and `Context`, `swiftpy make:api-key` CLI command, and `migration.api_keys.stub`.
- **P2-A3 (Security / Auth – Token Revocation Hardening):**
  - **Breaking:** Removed `JWTIssuer.decode_unverified()`. `TokenBlacklist.revoke_token()` now requires signature verification (accepting an explicit `issuer`, a constructor-injected `JWTIssuer`, or resolving `JWTIssuer` from the container) and rejects unverified or tampered tokens before writing to Redis.
  - Fixed `issue_token_pair()` so custom access-token `claims` are no longer copied onto the refresh token and header `typ` matches the token type (`"access"` / `"refresh"`).
  - Updated `TokenBlacklist` to resolve Redis settings via `swiftpy.support.redis_config.resolve_redis_params` and validate finite numeric `exp` claims.
- **P2-A4 (Security / Auth – Generic Auth Error Messages):**
  - **Breaking:** Client-facing `JWTGuard`, `ApiKeyGuard`, and `TokenRevokedError` HTTP 401 responses now return generic messages (`"Invalid or expired token"` / `"Invalid API key"`) without leaking expected `iss`/`aud` values, revocation timestamps, or raw error internals. Backend Redis/DB outages raise `AuthenticationServiceUnavailableError("Authentication service unavailable")` (HTTP 503) while logging details server-side via `logger.warning` / `logger.exception` without raw tokens.
- **P2-A5 (Security / Auth – JWT Hardening & Tidy-Up):**
  - **Breaking:** `JWTIssuer.issue_token()` now raises `ValueError` if `claims` attempts to override any reserved claim (`sub`, `exp`, `iat`, `jti`, `type`, `typ`, `nbf`, `iss`, `aud`). Algorithm validation enforces exact case matching (`"hs256"` is rejected while all case variants of `"none"` remain forbidden).
  - Enforced future `iat` and `nbf` validation with configurable `leeway` (seconds), rejected boolean (`True`/`False`) and non-finite (`NaN`, `Infinity`) timestamps, added `aud` list support, treated `expires_in=0` explicitly, validated `expires_at` column types in `ApiKeyGuard`, and removed inline imports across `swiftpy/auth/`.
- **P2-CLI1 (Security / CLI – `make:*` Path Containment & Identifier Validation):**
  - Hardened `swiftpy make:*` commands (`make:model`, `make:migration`, `make:service`, `make:request`, `make:resource`, `make:repository`, `make:schema`, `make:api-key`) against path traversal, absolute paths, symlink escapes (`Path.resolve().is_relative_to(...)`), invalid Python identifier segments, and invalid SQL table identifiers (`validate_identifier`).


