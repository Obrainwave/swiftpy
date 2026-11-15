# Contributing to SwiftPY

Thank you for your interest in contributing to **SwiftPY** (`swiftpy-framework`)! This guide explains how to set up a local development environment, follow project coding standards, and verify changes before opening a pull request.

---

## Development Setup

### Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** for dependency management, linting, typing, testing, and packaging
- **Docker & Docker Compose** (used for local PostgreSQL + `pgvector` and Redis services)

### 1. Fork, Clone, and Install Dependencies

```bash
git clone https://github.com/obrainwave/swiftpy.git
cd swiftpy
uv sync --group dev
```

### 2. Configure Environment & Backing Services

```bash
cp .env.example .env
```

Update `.env` with valid development/testing values (`APP_KEY`, `DB_PASSWORD`, `REDIS_PASSWORD`, `JWT_SECRET`, and `AUTH_KEY_PEPPER`), then start PostgreSQL (`pgvector`) and Redis:

```bash
docker compose up -d
```

> [!IMPORTANT]
> Never commit `.env` files, real API tokens, database passwords, or cryptographic secrets. Keep secrets in local environment variables or `.env` (gitignored).

---

## Branching & Pull Request Workflow

1. Create a focused feature or bugfix branch from `develop`:
   ```bash
   git checkout develop
   git pull origin develop
   git checkout -b fix/your-short-topic
   ```
2. Keep pull requests concise, coherent, and scoped to a single change. Avoid mixing unrelated refactors with feature work or bug fixes.
3. Preserve backwards compatibility unless a breaking change has been discussed and approved in an issue. When public behavior changes, update docstrings, `CHANGELOG.md`, tests, and relevant documentation under `documentations/`.
4. Do not add new runtime dependencies without explaining why they are needed and why existing dependencies cannot cover the use case.
5. `main` is a protected branch that disallows direct pushes and merge commits—PRs into `main`.

---

## Coding & Architecture Guidelines

SwiftPY targets **Python 3.12+** and enforces strict typing, async safety, and fail-closed security defaults:

- **Top-level imports only**: Place all imports at the top of each module; do not introduce inline imports inside functions or methods.
- **Strict typing**: `pyproject.toml` configures `[tool.mypy]` with `strict = true`. Annotate all public functions, classes, and methods cleanly without relying on `# type: ignore`, arbitrary `cast(...)`, or unnecessary `Any`.
- **Preserve documentation & comments**: Keep existing docstrings and comments intact unless your change alters that behavior.
- **Async & concurrency safety**:
  - Never call blocking synchronous I/O (`time.sleep`, `requests`, sync file reads on large payloads, `subprocess.run`) or CPU-heavy loops directly inside `async def` request paths—use async equivalents or offload work via `asyncio.to_thread` / `loop.run_in_executor`.
  - Never store per-request state in module-level variables or mutable class attributes; use SwiftPY's task-isolated `Context`.
  - Acquire database and Redis connections from the configured pools rather than opening ad-hoc connections per request.
  - Never leave background tasks (`asyncio.create_task`) unobserved; either await them or attach a done callback that logs/handles exceptions.
- **SQL & filesystem safety**:
  - Parameterize user-supplied query values and validate dynamic SQL identifiers via `swiftpy.database.identifiers`.
  - Keep filesystem path normalization and upload containment checks intact; never weaken validation, authentication, or error masking to make a test pass.

---

## Running Checks Locally

Before opening a pull request, run the same checks enforced by [`.github/workflows/ci.yml`](./.github/workflows/ci.yml):

### 1. Linting (`ruff`)

```bash
uv run ruff check .
```

### 2. Strict Type Checking (`mypy`)

```bash
uv run --no-sync --with jinja2 --with aiosmtplib mypy swiftpy
uv run mypy swiftpy
uv run mypy .
uv run mypy --explicit-package-bases .
```

### 3. Automated Test Suite (`pytest`)

Ensure PostgreSQL (`pgvector`) and Redis are running and test environment variables (`APP_KEY`, `JWT_SECRET`, `AUTH_KEY_PEPPER`, `DB_*`, `REDIS_*`) are configured, then run:

```bash
COVERAGE_FILE=/tmp/.coverage uv run pytest -vv -ra --durations=30 --timeout=120 --timeout-method=thread --cov=swiftpy --cov-report=term-missing --cov-fail-under=80
```

For quick targeted iterations while developing:

```bash
uv run pytest tests/path/to/test_file.py -vv
```

### 4. Security Audit & Package Build Check

```bash
uv run --no-sync --with pip-audit pip-audit --desc on
uv build
uv run --with twine twine check dist/*
```

---

## Reporting Bugs & Security Vulnerabilities

- **Bugs & feature requests**: Open an issue at <https://github.com/obrainwave/swiftpy/issues> with reproduction steps, expected vs. actual behavior, Python version, and relevant error output (redacting any secrets).
- **Security vulnerabilities**: Do **not** publish exploit details or sensitive vulnerability reports in public issues. Report security concerns privately by sending a direct email to <akeemsalau@swiftpy.net>.

---

## License

By contributing to SwiftPY, you agree that your contributions will be licensed under the [MIT License](./LICENSE).
