# SwiftPY installation and quick start

This guide covers installing SwiftPY, running a small ASGI application, and creating a fuller project with the SwiftPY CLI.

## Requirements

- Python 3.12 or newer
- `pip` for installing the released package, or [uv](https://docs.astral.sh/uv/) for installing from a source checkout
- PostgreSQL 16 and Redis 7 when using the generated project scaffold, which configures database and Redis services

The minimal application below does not configure a database or Redis connection.

## Install SwiftPY

Create and activate a virtual environment, then install the distribution:

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

Install SwiftPY:

```bash
python -m pip install --upgrade pip
python -m pip install swiftpy-framework
```

The distribution name is `swiftpy-framework`; Python code imports the `swiftpy` package.


## Run a minimal application

Create `main.py`:

```python
from swiftpy import Application, Request, Response, json_response

app = Application()


@app.get("/")
async def home(request: Request) -> Response:
    return json_response({"message": "Hello from SwiftPY"})
```

Start the application with SwiftPY's CLI and enable reload for local development:

```bash
swiftpy start --reload
```

The CLI defaults to the ASGI target `main:app`, host `127.0.0.1`, and port `8000`. Open <http://127.0.0.1:8000/> to see the response.

You can also run the same ASGI target directly with Uvicorn:

```bash
uvicorn main:app --reload
```

## Create a project scaffold

The CLI can generate a larger project structure:

```bash
swiftpy new my_api
cd my_api
```

The generated project includes `.env` with a generated application key, configuration, routes, service providers, and database and Redis settings. Review `.env` and set your database and Redis credentials before starting the full scaffold. Its generated README lists Python 3.12+, PostgreSQL 16, and Redis 7 as prerequisites.

### Correct the generated dependency name

The scaffold currently writes `"swiftpy"` in its `pyproject.toml` dependencies, but the framework distribution is named `swiftpy-framework`. Before installing the generated project, edit its `pyproject.toml` and replace:

```toml
"swiftpy",
```

with:

```toml
"swiftpy-framework",
```

Then install the generated project in its virtual environment:

```bash
python -m venv .venv
```

Activate the environment using the platform-specific command above, then run:

```bash
python -m pip install -e .
```

If PostgreSQL and Redis are available locally with the credentials in `.env`, apply the scaffold's database migrations and start the development server:

```bash
swiftpy migrate
swiftpy start --reload
```

The generated `docker-compose.yml` defines app, PostgreSQL, and Redis services. For local development with the app running from your activated Python environment, you can start only its backing services with:

```bash
docker compose up -d postgres redis
```

## Choose a server mode

For local development, use auto-reload:

```bash
swiftpy start --reload
```

For a multi-worker deployment, specify the target and bind address explicitly:

```bash
swiftpy start main:app --host 0.0.0.0 --port 8000 --workers 4
```

`--reload` is off by default and cannot be combined with more than one worker. The CLI also accepts an ASGI target as its first argument; when omitted, it uses `main:app`. Host and port can be provided with `--host` and `--port`.
