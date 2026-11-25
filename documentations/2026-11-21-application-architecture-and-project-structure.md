# Application Architecture & Project Structure

SwiftPY organizes applications cleanly from small APIs up to domain-driven services without forcing unnecessary layers on simple endpoints.

---

## Quick Reference: Package Root & Top-Level Layout

SwiftPY places HTTP, domain, data, and AI application code inside the top-level **`app/`** Python package:

- `app/http/` (never top-level `http/`) avoids shadowing Python's standard-library `http` module (`http.HTTPStatus`, `http.client`).
- Top-level `config/`, `database/`, `routes/`, `prompts/`, `views/`, `storage/`, and `tests/` keep configuration, migrations, routing, templates, runtime storage, and tests clearly separated from Python application modules.

```text
my_app/
├── app/
│   ├── ai/
│   │   ├── agents/          # Agent class definitions (model, tools, memory, system_prompt)
│   │   ├── tools/           # Function-calling tool definitions (each tool is one class)
│   │   ├── pipelines/       # RAG pipeline configs (retriever + reranker + generator)
│   │   ├── providers/       # Custom LLM provider adapters beyond built-ins
│   │   ├── vectorstores/    # Custom vector store adapters beyond built-ins
│   │   └── observers/       # Custom AI observability hooks
│   ├── events/              # Domain events (dataclasses)
│   ├── exceptions/          # Custom application/domain exceptions (optional)
│   ├── http/
│   │   ├── controllers/     # Thin HTTP orchestration handlers
│   │   ├── handler/         # Global exception handlers (exceptions.py)
│   │   ├── middleware/      # Cross-cutting HTTP middleware (auth, admin guard, etc.)
│   │   ├── requests/        # Typed FormRequest input validators
│   │   └── resources/       # Outbound HTTP response serializers
│   ├── integrations/        # External API / third-party service clients (optional)
│   ├── jobs/                # Background queue jobs
│   ├── listeners/           # Event listeners
│   ├── mail/                # Mailable classes
│   ├── models/              # ORM Active Record models (table mapping, relationships)
│   ├── providers/           # Auto-discovered ServiceProviders (DI bindings & boot hooks)
│   ├── repositories/        # Data access & complex query encapsulation
│   ├── schemas/             # Data contracts & internal structured DTOs
│   └── services/            # Business logic, workflows, transactions
├── config/                  # Framework & environment config modules
├── database/
│   ├── migrations/          # Schema migrations
│   └── seeders/             # Database seeders
├── prompts/
│   ├── summarise.py         # Named prompt (module-level `messages` list)
│   ├── classify.py          # Named classification prompt
│   └── summarise.v2.py      # Versioned prompt loaded via Prompt.load("summarise.v2", ...)
├── public/                  # Public storage symlink target
├── routes/
│   └── api.py               # Starter HTTP routes (/api/health, /api/users)
├── storage/                 # Private/public files, framework cache, logs
├── views/
│   └── mail/                # HTML email templates
├── tests/                   # pytest suite using TestClient
├── main.py                  # ASGI entrypoint & router mounting
└── pyproject.toml
```

---

## Responsibilities of Each Layer

### 1. HTTP Transport Layer (`routes/`, `app/http/`, `main.py`)

- **`main.py` — Composition Root:** Creates the `Application`, registers exception handlers, and mounts route modules (`app.include_router(api_router, prefix="/api")`). Keep business logic, SQL queries, and request validation out of `main.py`.
- **`routes/` — Endpoint Definitions:** Maps HTTP paths and middleware to controller actions (or inline route functions for tiny endpoints):

  ```python
  from __future__ import annotations

  from swiftpy.http.router import Router

  from app.http.controllers.user_controller import UserController

  router = Router()

  # Direct unbound controller method binding with automatic DI instantiation
  router.get("/users", UserController.index)
  ```

- **`app/http/controllers/` — HTTP Orchestration:** Translates HTTP input into service or model calls and returns formatted HTTP responses (`Response`, `json_response(...)`, or plain JSON-serializable `dict`/`list` values). Constructor parameters are automatically resolved through SwiftPY's DI `Container`, and per-request instances are isolated across concurrent requests. Controllers must **not** contain complex SQL queries, raw business logic, or multi-step domain workflows.
- **`app/http/requests/` — Inbound Validation:** Subclasses of `swiftpy.validation.FormRequest` that enforce inbound rules and authorization before a handler runs. See [Validation & FormRequest](2026-11-15-validation-and-form-requests.md).
- **`app/http/resources/` — Outbound Serialization:** Transforms ORM `Model` instances or internal `Schema` DTOs into stable, HTTP-safe dictionaries (`to_dict()`, `Resource.collection(items)`), hiding internal attributes such as `password` or audit fields.
- **`app/http/middleware/` — Cross-Cutting Pipeline Concerns:** Handles JWT verification, role/admin API guards (`app/http/middleware/admin_middleware.py`), rate limits, logging, or request headers.

### 2. Foundation & Domain Layers (`app/providers/`, `app/schemas/`, `app/models/`, `app/repositories/`, `app/services/`)

- **`app/providers/` — Service Providers:** `register()` and `boot()` hooks auto-discovered on startup to bind interfaces, configure clients, and wire shared singletons.
- **`app/schemas/` — Data Contracts & Structured DTOs:** Holds typed `swiftpy.schemas.BaseSchema` / `Schema` DTOs (`make:schema`), such as `UserCreateSchema` and `UserResponseSchema`, with automatic HTTP body validation, `Field(...)` constraints, `from_model()`, and `to_dict()`. See [Application Schemas](2026-11-19-application-schemas.md).
- **`app/models/` — ORM Models:** Defines tables (`__table__`), primary keys, `__fillable__` / `__hidden__` guards, and relationships (`HasOne`, `HasMany`, `BelongsTo`, `BelongsToMany`). See [ORM Models & Relationships](2026-11-15-orm-models-and-relationships.md).
- **`app/repositories/` — Data Access Abstraction:** Isolates complex query compositions, reporting aggregates, eager loading, or swappable persistence logic from services. For straightforward CRUD, calling ORM `Model` methods directly inside a service or simple controller is completely fine.
- **`app/services/` — Business Logic:** Coordinates domain rules, multi-model updates, database transactions, domain event dispatching, background jobs, mailers, and external integrations. Services return domain `Model` instances or explicit `Schema` DTOs — never HTTP `Response` objects.

### 3. Comparing `Request`, `Schema`, `Resource`, and `Model`

| Component | Layer | Primary Responsibility | Typical Input / Output |
| :--- | :--- | :--- | :--- |
| `FormRequest` | `app/http/requests/` | Inbound HTTP validation & authorization | Validates raw HTTP body/query -> `validated()` |
| `Schema` | `app/schemas/` | Internal structured contract / DTO between layers | `Service <-> Controller`, jobs, AI outputs, integrations |
| `Resource` | `app/http/resources/` | Public API JSON formatting & field stripping | Accepts `Model` or `Schema` -> HTTP JSON `dict` / `list` |
| `Model` | `app/models/` | Database table mapping, persistence & relationships | SQL rows <-> ORM records |

---

## AI & Prompts Architecture (`app/ai/` and `prompts/`)

SwiftPY separates **executable AI components** inside the Python package `app/ai/` from **text/message prompt templates** in top-level `prompts/`:

### `app/ai/` Folders

| Folder | Purpose | CLI Generator |
| :--- | :--- | :--- |
| `app/ai/agents/` | `BaseAgent` subclasses defining `model`, `tools`, `memory`, and `system_prompt` (`AssistantAgent` starter included) | `swiftpy make:agent SupportAgent` |
| `app/ai/tools/` | Function-calling `BaseTool` subclasses — each tool is one focused class (`SystemStatusTool` starter included) | `swiftpy make:tool WeatherLookupTool` |
| `app/ai/pipelines/` | RAG pipeline compositions wiring retrievers, rerankers, embeddings, and generators | — |
| `app/ai/providers/` | Custom LLM provider adapters beyond built-in providers (`OpenAIProvider`, `AnthropicProvider`, etc.) | — |
| `app/ai/vectorstores/` | Custom vector store adapters beyond built-in stores (`Qdrant`, `PgVector`, `Chroma`, etc.) | — |
| `app/ai/observers/` | Custom observability and cost/token hooks | — |

### `prompts/` Convention & Versioning

Named prompts in `prompts/` declare a module-level `messages` list (or `.md` template) using `{{ variable }}` placeholders:

```python
# prompts/summarise.v2.py
messages: list[dict[str, str]] = [
    {
        "role": "system",
        "content": "Produce a {{ style }} summary for {{ audience }}.",
    },
    {
        "role": "user",
        "content": "{{ document }}",
    },
]
```

Generate and load prompts safely without tying model prompts to Python module imports:

```bash
swiftpy make:prompt summarise.v2
```

```python
from swiftpy.ai.client import AIClient
from swiftpy.ai.prompts import Prompt

rendered_messages = Prompt.load(
    "summarise.v2",
    style="3-bullet",
    audience="engineers",
    document="Release notes...",
)

# Async loading (offloads disk read via asyncio.to_thread)
async_messages = await Prompt.aload("summarise.v2", style="executive", audience="leads", document="...")

# Direct integration with AIClient / AI facade (appends all messages in the list)
response = await AIClient().aprompt(
    "summarise.v2",
    style="concise",
    audience="developers",
    document="...",
)
```

> Configure the prompt root directory via `AI_PROMPTS_PATH` (defaults to `"prompts"`) or `config/ai.py` (`"prompts": {"path": ..., "cache": ...}`). Notice `.v2` filenames are loaded safely via `PromptLoader` path validation and do not conflict with dotted Python imports.

---

## Standard Architectural Patterns

### Pattern A — Simple Endpoint

Use direct controller binding (`Route -> Controller -> Model -> Resource`) when there is no complex business rule or multi-step workflow:

```python
# routes/posts.py
from swiftpy.http.router import Router
from app.http.controllers.post_controller import PostController

router = Router()
router.get("/posts", PostController.index)
```

```python
# app/http/controllers/post_controller.py
from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from app.http.resources.post_resource import PostResource
from app.models.post import Post


class PostController:
    async def index(self, request: Request) -> Response:
        posts = await Post.all()
        return json_response(PostResource.collection(posts))
```

### Pattern B — Standard CRUD Endpoint

For validated input and domain orchestration (`Route -> Controller -> FormRequest -> Service -> Model -> Resource`), bind controller actions directly and inject services via `__init__`:

```python
# app/http/requests/create_post_request.py
from typing import Any
from swiftpy.validation import FormRequest


class CreatePostRequest(FormRequest):
    def rules(self) -> dict[str, Any]:
        return {
            "title": ["required", "string", "min:3", "max:200"],
            "body": ["required", "string"],
        }
```

### Pattern C — Structured Service + Schema Flow

SwiftPY's starter user API (`swiftpy new my_app`) demonstrates the explicit **`Service -> Schema -> Resource -> Controller -> routes/api.py`** flow:

1. `UserService.list_users()` returns `list[UserResponseSchema]`.
2. `UserController.__init__(self, user_service: UserService)` receives `UserService` via DI.
3. `UserController.index(self, request: Request)` transforms `UserResponseSchema` items through `UserResource.collection(users)`.
4. `routes/api.py` mounts `router.get("/users", UserController.index)`.

Introduce explicit `Schema` DTOs whenever:

- Data crosses multiple modules, queues, or AI boundaries.
- Service outputs do not map 1:1 to a database `Model` (reports, aggregations, multi-source summaries).
- Multiple endpoints share the same structured payload shape.

Skip `Schema` when a simple endpoint validates directly with `FormRequest` and formats directly with a `Resource`.

### Pattern D — External Integrations

Place external API SDK wrappers under `app/integrations/` (for example `app/integrations/stripe_client.py`) and invoke them from `app/services/`, never directly from controllers or routes.

### Optional Feature Grouping for Large Applications

If flat folders become crowded as an application grows into many subdomains, group code cleanly inside `app/modules/<domain>/`:

```text
app/modules/
├── billing/
│   ├── controllers/
│   ├── requests/
│   ├── resources/
│   ├── schemas/
│   ├── services/
│   ├── models/
│   └── routes.py
└── auth/
    └── ...
```

---

## Recommended Combinations at a Glance

| Use Case | Components to Use |
| :--- | :--- |
| Simple read / delete | `Route -> Controller -> Model` |
| Read with public field filtering | `Route -> Controller -> Model -> Resource` |
| Validated action without heavy logic | `Route -> Controller -> FormRequest -> Model` |
| Standard domain CRUD | `Route -> Controller -> FormRequest -> Service -> Model -> Resource` |
| Service with explicit output contract | `Route -> Controller -> Service -> Schema -> Resource` |
| Complex queries / reporting | `Route -> Controller -> Service -> Repository -> Model` |
| External API integration | `Controller -> Service -> app/integrations/` |
| AI agent / tool / RAG feature | `Controller -> Service -> app/ai/{agents,tools,pipelines} + prompts/` |
| Reusable dependency / configuration | `app/providers/` |

> **The Golden Rule:** Use the simplest architecture that keeps the code maintainable. Introduce another layer when there is a real responsibility for it — not merely because the folder exists.

---

## Complete Example: Validated Post Creation (`Post` CRUD)

```python
# app/schemas/post_schema.py
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class CreatePostSchema:
    title: str
    body: str
    author_id: int


@dataclass(slots=True)
class PostResponseSchema:
    id: int
    title: str
    author_id: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

```python
# app/services/post_service.py
from __future__ import annotations

from app.models.post import Post
from app.schemas.post_schema import CreatePostSchema, PostResponseSchema


class PostService:
    async def create_post(self, payload: CreatePostSchema) -> PostResponseSchema:
        post = await Post.create(
            title=payload.title,
            body=payload.body,
            author_id=payload.author_id,
        )
        return PostResponseSchema(id=post.id, title=post.title, author_id=post.author_id)
```

```python
# app/http/resources/post_resource.py
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable


class PostResource:
    def __init__(self, resource: Any) -> None:
        self.resource = resource

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": getattr(self.resource, "id", None),
            "title": getattr(self.resource, "title", None),
            "author_id": getattr(self.resource, "author_id", None),
        }

    @classmethod
    def collection(cls, items: Iterable[Any]) -> list[dict[str, Any]]:
        return [cls(item).to_dict() for item in items]
```

```python
# app/http/controllers/post_controller.py
from __future__ import annotations

from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response

from app.http.requests.create_post_request import CreatePostRequest
from app.http.resources.post_resource import PostResource
from app.schemas.post_schema import CreatePostSchema
from app.services.post_service import PostService


class PostController:
    def __init__(self, post_service: PostService) -> None:
        self.post_service = post_service

    async def store(self, request: Request, payload: CreatePostRequest) -> Response:
        data = await payload.validated()
        author_id = int(request.state.get("user_id", 0))
        created = await self.post_service.create_post(
            CreatePostSchema(
                title=str(data["title"]),
                body=str(data["body"]),
                author_id=author_id,
            )
        )
        return json_response(PostResource(created).to_dict(), status=201)
```

```python
# routes/api.py
from __future__ import annotations

from swiftpy.http.router import Router

from app.http.controllers.post_controller import PostController

router = Router()
router.post("/posts", PostController.store)
```

---

## Next Step

- Review [HTTP Routing, Middleware & Exceptions](2026-11-15-http-routing-middleware-and-exceptions.md) for direct controller binding, route groups, and DI parameter resolution.
- Review [Application Schemas](2026-11-19-application-schemas.md) for schema conventions and DTO usage.
- Review [AI Subsystem: Clients, Embeddings, VectorStores, RAG, Agents & Observability](2026-11-15-ai-subsystem.md) for `app/ai/`, `prompts/`, `Prompt.load()`, agents, and tools.
