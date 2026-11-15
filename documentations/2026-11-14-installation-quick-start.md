# SwiftPY Installation and Quick Start

This guide covers installing SwiftPY, running a minimal ASGI application, and creating and starting a full scaffolded project with the `swiftpy` CLI.

---

## Requirements

- **Python `>= 3.12`** (per [`pyproject.toml`](../pyproject.toml))
- **`uv`** (recommended) or **`pip`** (`python -m venv` + `pip`)
- **PostgreSQL 16** and **Redis 7** when running a full `swiftpy new` project scaffold (the scaffolded `docker-compose.yml` uses `pgvector/pgvector:pg16` and `redis:7-alpine`)
- Optional packages depending on features you enable:
  - `jinja2` for rendering HTML mail templates (`MailTemplateRenderer`) and admin views (`views/`)
  - `aiosmtplib` for sending email over the `smtp` transport

A minimal single-file SwiftPY application without `config/database.py` does **not** initialize `DatabasePool` and runs without PostgreSQL or Redis.

---

## Install SwiftPY

> **Distribution vs. import name:** On PyPI, the package distribution is named **`swiftpy-framework`**. In Python code, you import **`swiftpy`**, and the CLI executable is **`swiftpy`**.

Create and activate a virtual environment:

```bash
python -m venv .venv
```

On macOS or Linux:

```bash
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install `swiftpy-framework` with `pip`:

```bash
python -m pip install --upgrade pip
python -m pip install swiftpy-framework
```

Or add it in a `uv`-managed project:

```bash
uv add swiftpy-framework
```

Verify the CLI is available:

```bash
swiftpy --version
swiftpy --help
```

---

## Run a Minimal Application

In an empty directory (no `config/` directory), create `main.py`:

```python
from __future__ import annotations

from swiftpy import Application, Request, Response, json_response

app = Application()


@app.get("/")
async def home(request: Request) -> Response:
    return json_response({"message": "Hello from SwiftPY"})


@app.get("/health")
async def health(request: Request) -> dict[str, str]:
    return {"status": "ok"}
```

Plain `dict` and `list` return values (such as `/health`) are automatically serialized to JSON responses alongside explicit `json_response(...)` returns.

Start the development server with auto-reload:

```bash
swiftpy start --reload
```

By default, `swiftpy start` binds to `127.0.0.1:8000` and loads `main:app`. Visit <http://127.0.0.1:8000/> or <http://127.0.0.1:8000/health>.

You can also run the ASGI target directly with Uvicorn:

```bash
uvicorn main:app --reload
```

---

## Create a Full Project Scaffold

For full-stack APIs and background workers, scaffold a complete project layout with `swiftpy new`:

```bash
swiftpy new my_api
cd my_api
```

Pass `--force` to overwrite an existing non-empty directory cleanly (existing `.env` files are preserved when `--force` is used).

### Generated Project Structure

```text
my_api/
├── app/
│   ├── events/             # Domain events
│   ├── http/
│   │   ├── handler/        # Exception handlers (exceptions.py -> register_exception_handlers)
│   │   ├── middleware/     # Auth & Admin middleware templates
│   │   └── requests/       # Typed FormRequest classes
│   ├── jobs/               # Background Queue jobs
│   ├── listeners/          # Event listeners
│   ├── mail/               # Mailables (WelcomeEmail)
│   ├── models/             # Active Record ORM models (User)
│   ├── providers/          # Auto-discovered service providers
│   ├── repositories/       # Data access layer
│   ├── schemas/            # Typed dataclasses/schemas
│   └── services/           # Business logic services
├── config/                 # Configuration files loading .env via env() / env_bool()
├── database/
│   ├── migrations/         # Timestamped schema migrations (initial users table included)
│   └── seeders/            # DatabaseSeeders
├── public/                 # Public web assets (storage symlink target)
├── routes/
│   └── api.py              # Starter /api/health and /api/users routes
├── storage/                # Private, public, framework cache, and log directories
├── views/                  # Jinja2 HTML templates (admin/, mail/)
├── tests/                  # pytest conftest.py & starter API test
├── .env                    # Pre-initialized with generated APP_KEY & JWT_SECRET
├── .env.example
├── docker-compose.yml      # app, postgres (pgvector/pgvector:pg16), and redis services
├── Dockerfile
├── main.py                 # Entrypoint: Application(), register_exception_handlers(app), /api router
└── pyproject.toml          # Configured with swiftpy-framework, setuptools build-system, & mypy strict settings
```

### Install Dependencies in the Scaffolded Project

With `uv` (recommended):

```bash
uv sync
source .venv/bin/activate
```

Or with standard `venv` + `pip`:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

The generated `pyproject.toml` already declares `"swiftpy-framework>=0.1.2"` along with `[build-system]` (`setuptools`), `[tool.setuptools.packages.find]`, and `[tool.mypy]`.

### Configure Environment & Backing Services

`swiftpy new` initializes `.env` with freshly generated `APP_KEY` and `JWT_SECRET` values, sets `DB_DATABASE` in `.env` and `.env.example` to your project name, and sets `name: <project_name>` in `docker-compose.yml` (referencing `${DB_DATABASE}`, `${DB_USERNAME}`, and `${DB_PASSWORD}` directly from `.env`). Review `.env` and adjust PostgreSQL (`DB_*`) and Redis (`REDIS_*`) settings as needed.

> **Important:** All generated `config/*.py` files use `swiftpy.helpers.helper.env()` and `env_bool()`. When `Application` boots, `Config.load()` reads `.env` into SwiftPY's active env context without mutating `os.environ`.

If you want Docker Compose to run PostgreSQL and Redis for local development while running the app from your Python virtualenv:

```bash
docker compose up -d postgres redis
```

### Migrate, Link Storage, and Run

Run database migrations, link `public/storage`, and start the server:

```bash
swiftpy migrate
swiftpy storage:link
swiftpy start --reload
```

Verify the starter endpoints:

- `GET http://127.0.0.1:8000/api/health` -> `{"status": "ok", "app": "SwiftPY"}`
- `GET http://127.0.0.1:8000/api/users` -> starter JSON list from `routes/api.py`

### Run the Docker Stack Instead

To run all three containers (`app`, `postgres`, and `redis`) via Docker Compose:

```bash
docker compose up -d
docker compose exec app swiftpy migrate
```

Ports in the scaffolded `docker-compose.yml` bind to `127.0.0.1` (`8000`, `5432`, `6379`).

---

## Choosing a Server Mode (`swiftpy start`)

`swiftpy start` wraps `uvicorn.run(...)` and resolves defaults in order: CLI flags -> `config/server.*` or `config/app.*` -> built-in defaults (`target="main:app"`, `host="127.0.0.1"`, `port=8000`, `workers=1`, `reload=False`).

Common invocations:

```bash
# Development (single worker + file watching; --reload defaults to False)
swiftpy start --reload

# Explicit ASGI target, host, port, and .env file
swiftpy start main:app --host 127.0.0.1 --port 8080 --env-file .env

# Multi-worker production mode (must NOT pass --reload)
swiftpy start main:app --host 0.0.0.0 --port 8000 --workers 4 --no-access-log --log-level warning
```

Supported `swiftpy start` options:

| Option / Argument | Default | Notes |
|---|---|---|
| `target` (positional) | `main:app` | ASGI module:attribute path (`server.target` / `app.target`) |
| `--host` | `127.0.0.1` | Socket bind host (`server.host` / `app.host`) |
| `--port` | `8000` | Socket bind port (`0`–`65535`; `server.port` / `app.port`) |
| `--workers` | `1` | Worker process count (`>= 1`; `server.workers`) |
| `--reload` | `False` | Enable hot reload (cannot be combined with `--workers > 1`) |
| `--env-file` | `None` | Environment file path passed to Uvicorn (`server.env_file`) |
| `--access-log` / `--no-access-log` | `None` | Override Uvicorn HTTP access logging (`server.access_log`) |
| `--log-level` | `None` | `critical`, `error`, `warning`, `info`, `debug`, or `trace` (`server.log_level`) |

---

## Next Step

Continue with the documentation sequence in [`docs/2026-11-14_documentation_plan.md`](../docs/2026-11-14_documentation_plan.md) (next guide: `documentations/2026-11-15-core-application-di-and-config.md`).
