# SwiftPY Installation and Quick Start

This guide covers two ways to get started with SwiftPY:

1. **Quick Start Without Scaffolding** — run a minimal single-file app with no database, Redis, or Docker.
2. **Full Project Scaffolding (`swiftpy new`)** — scaffold a full API application, then choose **either** the **Fully Dockerized** workflow **or** the **Without Docker at All** workflow.

> **Avoid duplicate commands:** Pick **one** track (and inside Full Project Scaffolding, pick **either** Option A or Option B). You never need to create two `.venv` folders or run both Docker and local `swiftpy start` at the same time.

---

## Requirements

- **Distribution vs. import vs. CLI name:** On PyPI, install **`swiftpy-framework`**. In Python code, import **`swiftpy`**. In the terminal, run **`swiftpy`**.
- **For Quick Start Without Scaffolding (Minimal App):** Python `>= 3.12` only. PostgreSQL, Redis, and Docker are **not** required.
- **For Full Project Scaffolding:**
  - **Option A (Fully Dockerized):** Docker Engine / Docker Desktop with Compose v2, plus `uv` (`uvx`) or Python `>= 3.12` only to run `swiftpy new`.
  - **Option B (Without Docker at All):** Python `>= 3.12`, **PostgreSQL 16** (with `pgvector` if using AI vector stores), and **Redis 7** running natively on your machine.
- Optional extras if needed later: `jinja2` (HTML mail templates) and `aiosmtplib` (`smtp` mail transport).

---

## Track 1: Quick Start Without Scaffolding (Minimal Single-File App)

Use this path when you want to test SwiftPY or build a lightweight service without a database or Redis. A single-file app with no `config/database.py` does **not** initialize `DatabasePool`.

### 1. Create a Folder and Install `swiftpy-framework`

Create a project directory, set up **one** virtual environment, and install `swiftpy-framework`:

```bash
mkdir swiftpy-minimal
cd swiftpy-minimal
python -m venv .venv
```

Activate the virtual environment:

- **macOS / Linux:** `source .venv/bin/activate`
- **Windows PowerShell:** `.venv\Scripts\Activate.ps1`

Install `swiftpy-framework` and verify the CLI:

```bash
python -m pip install --upgrade pip
python -m pip install swiftpy-framework
swiftpy --version
```

> **Using `uv` instead of `venv` + `pip`:** Run `uv init --python 3.12 && uv add swiftpy-framework` inside `swiftpy-minimal`, then prefix commands with `uv run` (or activate `.venv`).

### 2. Create `main.py`

```python
from __future__ import annotations

from swiftpy import Application, Request, Response, json_response

app = Application()


@app.get("/")
async def home(request: Request) -> Response:
    return json_response(
        {
            "status": "ok",
            "message": "Hello from SwiftPY",
            "app": "SwiftPY",
        }
    )


@app.get("/health")
async def health(request: Request) -> dict[str, str]:
    return {"status": "ok"}
```

Route handlers may return an explicit `Response` (such as `json_response(...)`) or a plain `dict` / `list` (automatically serialized to JSON).

### 3. Start and Verify

```bash
swiftpy start --reload
```

(Or directly with Uvicorn: `uvicorn main:app --reload`.)

Visit:

- `http://127.0.0.1:8000/` -> `{"status": "ok", "message": "Hello from SwiftPY", "app": "SwiftPY"}`
- `http://127.0.0.1:8000/health` -> `{"status": "ok"}`

---

## Track 2: Full Project Scaffolding (`swiftpy new`)

Use this path for full-stack APIs with config files, migrations, ORM models, storage, queues, and Docker support.

### 1. Scaffold the Project

Do **not** run `python -m pip install swiftpy-framework` globally outside a virtual environment — user-site installs can clash with global packages (such as `pyopenssl` / `cryptography`) and, on Windows, leave `swiftpy.exe` in `AppData\...\Scripts` off `PATH`.

From the workspace parent directory where you want your `my_api` folder created, generate the project with **one** of these isolated options:

- **With `uvx` (recommended — no `.venv` clutter or global installs):**

  ```bash
  uvx --from swiftpy-framework swiftpy new my_api
  cd my_api
  ```

- **With `python -m venv` + `pip`:**

  ```bash
  python -m venv .cli-venv
  source .cli-venv/bin/activate   # Windows PowerShell: .cli-venv\Scripts\Activate.ps1
  python -m pip install --upgrade pip
  python -m pip install swiftpy-framework
  swiftpy --version
  swiftpy new my_api
  deactivate
  cd my_api
  ```

  *(In Option A you can delete `.cli-venv` afterward; in Option B you create `my_api/.venv` next, or you can keep one parent `.venv` active instead. If `swiftpy` was already installed in user site-packages without `Scripts` on `PATH`, invoke `python -m swiftpy.console.cli new my_api`.)*

Pass `--force` only if overwriting an existing non-empty directory (existing `.env` files are preserved when `--force` is used).

### 2. Generated Project Structure

```text
my_api/
├── app/
│   ├── ai/
│   │   ├── agents/         # AI agent classes (AssistantAgent starter)
│   │   ├── tools/          # Function-calling tool classes (SystemStatusTool starter)
│   │   ├── pipelines/      # RAG pipeline configurations
│   │   ├── providers/      # Custom LLM provider adapters
│   │   ├── vectorstores/   # Custom vector store adapters
│   │   └── observers/      # Custom AI observability hooks
│   ├── events/             # Domain events
│   ├── http/
│   │   ├── controllers/    # HTTP controllers (UserController starter)
│   │   ├── handler/        # Exception handlers (exceptions.py -> register_exception_handlers)
│   │   ├── middleware/     # Auth & role/admin API guard middleware templates
│   │   ├── requests/       # Typed FormRequest classes
│   │   └── resources/      # API response serializers (UserResource starter)
│   ├── jobs/               # Background Queue jobs
│   ├── listeners/          # Event listeners
│   ├── mail/               # Mailables (WelcomeEmail)
│   ├── models/             # Active Record ORM models (User)
│   ├── providers/          # Auto-discovered service providers
│   ├── repositories/       # Data access layer
│   ├── schemas/            # Typed dataclasses/schemas (see Application Schemas guide)
│   └── services/           # Business logic services (UserService starter)
├── config/                 # Configuration files loading .env via env() / env_bool()
├── database/
│   ├── migrations/         # Timestamped schema migrations (initial users table included)
│   └── seeders/            # DatabaseSeeders
├── prompts/
│   ├── summarise.py        # Named prompt (module-level `messages` list)
│   └── classify.py         # Named classification prompt
├── public/                 # Public web assets (storage symlink target)
├── routes/
│   └── api.py              # Starter /api/health and /api/users (UserController.index) routes
├── storage/                # Private, public, framework cache, and log directories
├── views/                  # Jinja2 HTML templates (mail/)
├── tests/                  # pytest conftest.py & starter API test
├── .env                    # Generated APP_KEY & JWT_SECRET, DB_DATABASE=my_api
├── .env.example
├── docker-compose.yml      # name: my_api; app, postgres (pgvector:pg16), & redis services
├── Dockerfile
├── main.py                 # Entrypoint: GET /, /api router, register_exception_handlers(app)
└── pyproject.toml          # Configured with swiftpy-framework>=0.3.0, setuptools, & mypy strict settings
```

What `swiftpy new my_api` configures automatically:

- **`.env` and `.env.example`:** Generates random `APP_KEY` and `JWT_SECRET` in `.env`, sets `DB_DATABASE=my_api`, `DB_HOST=127.0.0.1`, `DB_PORT=5432`, `DB_USERNAME=postgres`, `DB_PASSWORD=secret`, and `REDIS_HOST=127.0.0.1`.
- **`docker-compose.yml`:** Sets `name: my_api`, reads `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` directly from `.env` (`${DB_DATABASE}`, `${DB_USERNAME}`, `${DB_PASSWORD}`), and sets `DB_HOST: postgres` and `REDIS_HOST: redis` for the `app` container so container networking works without editing `.env`.
- **Important:** When `Application` boots, `Config.load()` reads `.env` into SwiftPY's active env context without mutating `os.environ` (existing process environment variables still override `.env`).

Now choose **ONE** of the two run modes below (Option A **or** Option B).

---

### Option A: Fully Dockerized (App + PostgreSQL + Redis in Docker)

Use this mode if you want Docker to run everything — **you do not need to create a `.venv`, install Python packages locally, or install Postgres/Redis on your host.**

1. Inside `my_api`, review `.env` (optional — defaults already work out of the box with Compose).
2. Build and start `app`, `postgres`, and `redis`:

   ```bash
   docker compose up -d --build
   ```

3. Run database migrations and link public storage **inside the running `app` container**:

   ```bash
   docker compose exec app swiftpy migrate
   docker compose exec app swiftpy storage:link
   ```

4. Verify the starter endpoints from your browser or `curl`:

   - `GET http://127.0.0.1:8000/` -> `{"status": "ok", "message": "Hello from SwiftPY", "app": "SwiftPY"}`
   - `GET http://127.0.0.1:8000/api/health` -> `{"status": "ok", "app": "SwiftPY"}`
   - `GET http://127.0.0.1:8000/api/users` -> `[{"id": 1, "name": "Alice Doe", "email": "alice@example.com"}]`

Common Docker lifecycle commands:

```bash
# View live application logs
docker compose logs -f app

# Stop containers (keeps database data)
docker compose down

# Stop containers AND delete database/redis volumes (use this if an old volume with different credentials existed)
docker compose down -v
```

> **Troubleshooting `InvalidPasswordError` in Docker:** PostgreSQL only initializes `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` when its data volume is brand new. If a Docker volume named `my_api_postgres_data` already existed from another project or earlier setup with different credentials, run `docker compose down -v` and then `docker compose up -d --build`.

> **Troubleshooting `invalid file request public/storage` on Windows Docker Desktop:** Because `docker-compose.yml` bind-mounts `.:/app`, running `swiftpy storage:link` inside the Linux container creates a symlink at `public/storage`. Projects generated with older templates without `public/storage` in `.dockerignore` can hit a BuildKit error on Windows NTFS during `docker compose up -d --build`. Remove the symlink (`rm -f public/storage` or delete `public\storage`), add `public/storage` to `.dockerignore` and `.gitignore`, rebuild (`docker compose up -d --build`), and re-run `docker compose exec app swiftpy storage:link --force`.

---

### Option B: Without Docker at All (Local Python + Native PostgreSQL & Redis)

Use this mode if you have PostgreSQL 16+ and Redis 7 installed locally on your machine and do **not** want to use Docker at all.

1. Inside `my_api`, install project dependencies into **one** virtual environment:

   - **With `uv` (recommended):**

     ```bash
     uv sync
     source .venv/bin/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1
     ```

   - **Or with `python -m venv` + `pip`:**

     ```bash
     python -m venv .venv
     source .venv/bin/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1
     python -m pip install --upgrade pip
     python -m pip install -e .
     ```

   *(Skip creating `my_api/.venv` only if you intentionally installed `swiftpy-framework` in an activated parent workspace `.venv` — in that case, just run `python -m pip install -e .` inside `my_api`.)*

2. Ensure local **PostgreSQL** (`127.0.0.1:5432`) and **Redis** (`127.0.0.1:6379`) services are running on your machine.
3. Make sure your local PostgreSQL database exists and `.env` matches your local Postgres credentials:
   - Default `.env` expects database `my_api`, user `postgres`, password `secret`.
   - Update `DB_USERNAME`, `DB_PASSWORD`, `DB_PORT`, or `REDIS_PASSWORD` in `.env` if your local services use different values, then create the database if it does not exist yet:

     ```bash
     createdb -U postgres my_api
     ```

     *(Or in `psql`: `CREATE DATABASE my_api;`.)*

4. Run migrations, link public storage, and start the dev server **once**:

   ```bash
   swiftpy migrate
   swiftpy storage:link
   swiftpy start --reload
   ```

5. Verify the endpoints:

   - `GET http://127.0.0.1:8000/` -> `{"status": "ok", "message": "Hello from SwiftPY", "app": "SwiftPY"}`
   - `GET http://127.0.0.1:8000/api/health` -> `{"status": "ok", "app": "SwiftPY"}`
   - `GET http://127.0.0.1:8000/api/users` -> `[{"id": 1, "name": "Alice Doe", "email": "alice@example.com"}]`

> **Hybrid alternative (Docker only for PostgreSQL + Redis, local Python for the app):** Instead of Option A or installing Postgres/Redis natively in Option B, you can start only the backing containers with `docker compose up -d postgres redis`, keep `.env` pointing to `127.0.0.1`, and run steps 1, 4, and 5 above on your host. Do **not** run `docker compose up -d` (all services) at the same time as a local `swiftpy start` process, or port `8000` will conflict.

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

## Upgrading `swiftpy-framework` in an Existing Project

### Dockerized Projects (Option A)

The scaffolded `Dockerfile` reads `[project].dependencies` from your project's `pyproject.toml` and builds wheels from PyPI (`pip wheel ...`):

1. **Update `dependencies` — not `project.version`:** Bump the `swiftpy-framework` constraint inside `[project].dependencies` in your app's `pyproject.toml` (changing the top-level `[project].version` only changes your own application's package metadata):

   ```toml
   [project]
   name = "my_api"
   version = "0.1.0"
   dependencies = [
       "swiftpy-framework>=0.3.5",
       # ...
   ]
   ```

2. **Rebuild and recreate the container:** Re-run Compose with `--build` so Docker rebuilds the wheel layer and restarts `app`:

   ```bash
   docker compose up -d --build
   docker compose exec app swiftpy --version
   ```

   - If you did **not** edit `pyproject.toml` (for example, keeping `swiftpy-framework>=0.1.6` to pull a newly published `0.2.1` release from PyPI), Docker's `COPY pyproject.toml .` layer stays cached unless you force a rebuild:

     ```bash
     docker compose build --no-cache app
     docker compose up -d
     ```

   - The target version of `swiftpy-framework` must already be published on PyPI before running `docker compose build`. To test an unreleased local framework build in Docker, build a wheel (`uv build --wheel`), copy the `.whl` into your project's build context, and `pip install` that wheel in `Dockerfile`.

### Local Virtual Environment Projects (Option B / Minimal App)

If running without Docker, upgrade inside your activated `.venv`:

```bash
# With uv (updates uv.lock and .venv)
uv sync --upgrade-package swiftpy-framework

# Or with pip
python -m pip install --upgrade "swiftpy-framework>=0.3.1"
swiftpy --version
```

### App Files Are Never Overwritten

Upgrading `swiftpy-framework` updates the installed framework library only—your project files under `app/`, `config/`, `routes/`, and `.env` are untouched. Check [`CHANGELOG.md`](../CHANGELOG.md) when upgrading if a release introduces optional new configuration keys (such as `auth.hash.rounds` / `BCRYPT_ROUNDS` in `config/auth.py` and `Hash` registration in `app/providers/auth_service_provider.py`).

---

## Next Step

Continue to [Application Lifecycle, Configuration & Dependency Injection](2026-11-15-core-application-di-and-config.md) to configure `Application`, `.env` / `config/`, service providers, scoped DI, and `Context`.
