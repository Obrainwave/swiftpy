# SwiftPY

**An ASGI native, Eloquent style, AI powered Python API framework.**

![CI](https://github.com/your-org/swiftpy/actions/workflows/ci.yml/badge.svg?branch=develop)
![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)
![Status](https://img.shields.io/badge/status-pre--alpha-orange.svg)

> **This is pre-alpha software.** SwiftPY is under active, early development and is not ready for production use, or for any use at all yet. Nothing here is published to PyPI. The API shown below is the target design, not something you can currently install and run. If you want to watch this repo grow from its first working request loop, you are in the right place. If you need a production API framework today, use FastAPI, Litestar, or Django.

## What is this

SwiftPY combines three things that exist separately across the Python ecosystem today, but not together in one framework:

- **FastAPI grade developer experience**: type driven request validation, automatic OpenAPI generation, and async first routing.
- **Eloquent caliber ORM**: an Active Record style ORM with a fluent query builder and expressive relationships, closing the gap left by SQLAlchemy's more verbose data mapper pattern.
- **First class AI ecosystem**: LLM clients, streaming, embeddings, vector stores, RAG pipelines, tool calling, and agent loops as framework level citizens, not a LangChain layer bolted on top.

## Status

| Phase | Focus | Status |
|---|---|---|
| 1. Core Foundation | ASGI interface, routing, DI, task isolation, middleware | 🚧 In progress |
| 2. Data and API Essentials | Query Builder, ORM, migrations, auth, validation, CLI | ⬜ Not started |
| 3. Application Infrastructure | Queues, events, cache, WebSockets | ⬜ Not started |
| 4. AI Native Module | LLM clients, RAG, tool calling, agents, admin panel | ⬜ Not started |
| 5. Polish and Launch | Performance, security audit, docs, PyPI release | ⬜ Not started |

## What it will look like

This is the target API. It is the design the PRD and Execution Plan are building toward, not something you can run yet.

```python
from swiftpy.core import Application
from swiftpy.database.orm import Model, HasMany

app = Application()

class User(Model):
    name: str
    email: str
    posts = HasMany("Post")

@app.get("/users/{user_id}")
async def show_user(user_id: int) -> UserOut:
    return await User.find(user_id)

@app.post("/chat/stream")
async def chat_stream(body: ChatRequest):
    async def tokens():
        async for chunk in AI.chat("claude-sonnet-4-6").user(body.message).stream():
            yield chunk
    return stream(tokens())
```

## Installation

Not published yet. There is nothing to `pip install` or `uv add` until Phase 5. To run the project as it currently stands, see [Development Setup](#development-setup) below.

## Development Setup

```bash
git clone https://github.com/your-org/swiftpy.git
cd swiftpy
uv sync
uv run pytest
```

Requires Python 3.12+, PostgreSQL 14+ with the pgvector extension, and Redis for the full dev environment. `docker-compose.yml` provisions both, see [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the complete setup.

## Documentation

Full documentation ships in Phase 5, alongside the first release. Until then, the three planning documents in `docs/` are the closest thing to a spec:

- `SwiftPY_PRD_v1_0.docx`: what SwiftPY is and why
- `SwiftPY_Structure_v1_0.docx`: where everything lives in the codebase
- `SwiftPY_ExecutionPlan_v1_0.docx`: how it's being built, phase by phase

## Contributing

Contribution guidelines live in [`CONTRIBUTING.md`](./CONTRIBUTING.md). Given the pre-alpha state, the most useful contribution right now is reading the PRD and Execution Plan and opening issues against the design itself, not pull requests against code that doesn't exist yet.

## License

MIT. See [`LICENSE`](./LICENSE).
