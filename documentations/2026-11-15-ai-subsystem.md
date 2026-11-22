# AI Client, Embeddings, Vector Stores, RAG, Agents & Prompts

## Introduction

SwiftPY includes an asynchronous, production-focused AI subsystem under `swiftpy.ai` that unifies provider requests, streaming, embeddings, vector databases, Retrieval-Augmented Generation (RAG), autonomous tool-calling agents, prompt management, safety middlewares, observability telemetry, and deterministic test fakes:

- **Transport & Providers (`AIHttpPool`, `OpenAIProvider`, `AnthropicProvider`, `ProviderInterface`):** Reuses one shared `httpx.AsyncClient` connection pool (`AIHttpPool`) closed automatically by `Application` on shutdown, normalizes requests/responses into `LLMRequest`, `LLMResponse`, `Message`, `TokenUsage`, `ToolCall`, and `ToolResult`, applies exponential-backoff retries with `Retry-After` handling, and parses Server-Sent Events (SSE) incrementally (`parse_sse_events`, `split_chunk_lines`).
- **Fluent Client & Conversations (`AI`, `ai()`, `AIClient`, `Conversation`, `HistoryStrategy`, `ConversationStore`):** Provides a fluent builder (`.system()`, `.user()`, `.assistant()`, `.prompt()` / `.aprompt()`, `.tools()`, `.auto_execute_tools()`, `.use()`, `.send()`, `.stream()`), explicit or model-inferred provider routing, non-mutating history truncation (`SlidingWindowStrategy`, `TokenBudgetStrategy`), and session stores (`InMemoryConversationStore`, `RedisConversationStore`).
- **Embeddings & Vector Stores (`EmbeddingsClient`, `VectorStore`, adapters):** Generates batched embeddings (`EmbeddingsClient` / `AI.embed()` / `AI.embed_batch()`) and persists searchable vectors through a unified async adapter interface across `PgVectorAdapter` (`pgvector` / `postgres`), `InMemoryVectorStore` (`in_memory`), `QdrantAdapter` (`qdrant`), `PineconeAdapter` (`pinecone`), `ChromaAdapter` (`chroma`), and `LanceDbAdapter` (`lancedb`).
- **RAG Pipelines (`rag()`, `RAGPipelineBuilder`, `Retriever`, rerankers, formatters):** Splits and indexes documents (`chunk_text`, `Retriever.add_documents`), retrieves candidates, optionally reranks (`CohereReranker`, `CrossEncoderReranker`), enforces context token budgets (`truncate_to_token_budget`), wraps untrusted retrieved chunks in cryptographic nonce boundaries (`NumberedListFormatter`, `XMLBlocksFormatter`, `PlainConcatenationFormatter`), and returns structured `RAGResult` objects with source attribution and computed cost.
- **Agents & Tool Loops (`Agent` / `BaseAgent`, `AgentMemory`, `BaseTool`, `@tool`, `FunctionTool`, `ToolLoop`, `AgentTask`, `AgentTracker`):** Runs per-session locked conversational agents backed by `InMemoryMemoryBackend`, `RedisMemoryBackend`, or PostgreSQL `DatabaseMemoryBackend`, exposes built-in AST-safe tools (`CalculatorTool`, `DateTimeTool`, `WebSearchTool`) alongside typed Python `@tool` functions, and tracks cancellable background agent runs via `AgentTracker`.
- **Prompts, Guardrails, Observability & Testing (`PromptLoader`, `AIMiddleware`, `AIObservabilityManager`, `AIFake`):** Loads `prompts/*.md` and `prompts/*.py` templates with `{{ variable }}` interpolation and `cache:build` precompilation, enforces pre-flight moderation (`ContentModerationMiddleware`), sliding-window rate limits (`AIRateLimitMiddleware`), and soft/hard spend budgets (`TokenBudgetMiddleware`), records cost/latency/TTFT telemetry across memory/file/PostgreSQL drivers, and isolates automated tests cleanly with `AIFake`.

```python
from swiftpy.ai import (
    AI,
    Agent,
    AgentMemory,
    AIObservabilityManager,
    AIRateLimitMiddleware,
    CalculatorTool,
    ContentModerationMiddleware,
    Conversation,
    DatabaseMemoryBackend,
    DatabaseObservabilityDriver,
    DateTimeTool,
    EmbeddingsClient,
    InMemoryConversationStore,
    InMemoryVectorStore,
    PgVectorAdapter,
    PromptLoader,
    RedisConversationStore,
    RedisMemoryBackend,
    Retriever,
    SlidingWindowStrategy,
    TokenBudgetMiddleware,
    TokenBudgetStrategy,
    VectorRecord,
    VectorStore,
    WebSearchTool,
    XMLBlocksFormatter,
    ai,
    calculate_token_cost,
    load_prompt,
    rag,
    register_model_pricing,
    tool,
)
from swiftpy.ai.agents import AgentTracker
from swiftpy.ai.prompts import aload_prompt
from swiftpy.testing import AIFake
```

---

## Creating & Organizing Files

A newly generated SwiftPY project scaffolds `config/ai.py` with the core toggle, default provider, and OpenAI/Anthropic credentials. In production apps, expand `config/ai.py` to configure HTTP pooling, vector adapters, Cohere reranking, prompt compilation caching, observability, and (when using Redis-backed AI components) an `ai.redis` section:

```python
# config/ai.py
from __future__ import annotations

from swiftpy.helpers.helper import env, env_bool

CONFIG = {
    "enabled": env_bool("AI_ENABLED", True),
    "default": env("AI_DEFAULT_PROVIDER", "openai"),
    "providers": {
        "openai": {
            "api_key": env("OPENAI_API_KEY", ""),
            "model": env("OPENAI_MODEL", "gpt-4o"),
            "base_url": env("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            "timeout": float(env("AI_TIMEOUT", "60.0")),
            "max_retries": int(env("AI_MAX_RETRIES", "2")),
        },
        "anthropic": {
            "api_key": env("ANTHROPIC_API_KEY", ""),
            "model": env("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"),
            "base_url": env("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
            "version": env("ANTHROPIC_VERSION", "2023-06-01"),
            "timeout": float(env("AI_TIMEOUT", "60.0")),
            "max_retries": int(env("AI_MAX_RETRIES", "2")),
        },
    },
    "http": {
        "max_connections": int(env("AI_HTTP_MAX_CONNECTIONS", "100")),
        "max_keepalive_connections": int(env("AI_HTTP_MAX_KEEPALIVE", "20")),
        "keepalive_expiry": float(env("AI_HTTP_KEEPALIVE_EXPIRY", "30.0")),
        "timeout": float(env("AI_HTTP_TIMEOUT", "60.0")),
    },
    "prompts": {
        "cache": env_bool("AI_PROMPTS_CACHE", False),
    },
    "observability": {
        # "database", "file", "memory", or "all" (defaults to "memory" when APP_ENV=testing)
        "driver": env("AI_OBSERVABILITY_DRIVER", "database"),
        "file_path": env("AI_OBSERVABILITY_FILE", "storage/logs/ai.log"),
    },
    # Optional: configure if using RedisConversationStore, RedisMemoryBackend,
    # AIRateLimitMiddleware(storage="redis"), or TokenBudgetMiddleware(storage="redis").
    "redis": {
        "host": env("REDIS_HOST", "127.0.0.1"),
        "port": int(env("REDIS_PORT", "6379")),
        "password": env("REDIS_PASSWORD") or None,
        "database": int(env("REDIS_AI_DB", "0")),
    },
    "vectorstores": {
        "in_memory": {"max_records": int(env("VECTOR_IN_MEMORY_MAX_RECORDS", "100000"))},
        "pgvector": {"pool_size": int(env("PGVECTOR_POOL_SIZE", "10"))},
        "qdrant": {"base_url": env("QDRANT_URL", "http://127.0.0.1:6333"), "api_key": env("QDRANT_API_KEY", "")},
        "lancedb": {"base_url": env("LANCEDB_URL", "http://127.0.0.1:8000"), "api_key": env("LANCEDB_API_KEY", "")},
        "chroma": {
            "base_url": env("CHROMA_URL", "http://127.0.0.1:8000"),
            "api_key": env("CHROMA_API_KEY", ""),
            "tenant": env("CHROMA_TENANT", "default_tenant"),
            "database": env("CHROMA_DATABASE", "default_database"),
        },
        "pinecone": {
            "api_key": env("PINECONE_API_KEY", ""),
            "environment": env("PINECONE_ENVIRONMENT", "us-east-1-aws"),
            "base_url": env("PINECONE_BASE_URL", ""),
        },
    },
    "rerankers": {
        "cohere": {
            "api_key": env("COHERE_API_KEY", ""),
            "model": env("COHERE_RERANK_MODEL", "rerank-v3.5"),
            "base_url": env("COHERE_BASE_URL", "https://api.cohere.ai/v1/rerank"),
            "timeout": float(env("COHERE_TIMEOUT", "30.0")),
        },
    },
}
```

> **Configuration Detail (`ai.redis`):** `RedisConversationStore`, `RedisMemoryBackend`, `AIRateLimitMiddleware`, and `TokenBudgetMiddleware` resolve Redis settings via `resolve_redis_params(cfg, section="ai")`. Because a freshly generated `config/ai.py` omits `"redis"` by default (and top-level `REDIS_HOST` / `REDIS_PASSWORD` env vars only bind when `"redis"` exists in a loaded config section or when double-underscore vars like `AI__REDIS__HOST` / `REDIS__HOST` are used), explicitly define `"redis"` inside `config/ai.py` as shown above or pass `redis_client=` / `config=` when deploying against password-protected or non-local Redis.

Recommended file organization inside your SwiftPY application (see also [Application Architecture & Project Structure](2026-11-21-application-architecture-and-project-structure.md)):

- `config/ai.py`: Central AI provider, prompt path/cache (`prompts.path`, `prompts.cache`), HTTP pool, observability, vectorstore, and reranker settings.
- `prompts/`: Prompt templates stored as Markdown (`.md`) or Python (`.py`) files, including named prompts (`prompts/summarise.py`, `prompts/classify.py`), nested folders (`prompts/support/triage.md`), and versioned prompts (`prompts/summarise.v2.py` via `swiftpy make:prompt summarise.v2`).
- `app/ai/agents/`: Domain `BaseAgent` subclasses defining model, tools, memory, and system prompt (generated via `swiftpy make:agent <Name>`; includes starter `AssistantAgent`).
- `app/ai/tools/`: Function-calling `BaseTool` subclasses — each tool is one class (generated via `swiftpy make:tool <Name>`; includes starter `SystemStatusTool`).
- `app/ai/pipelines/`: RAG pipeline configurations combining retrievers, rerankers, and generators.
- `app/ai/providers/`: Custom LLM provider adapters beyond built-in providers.
- `app/ai/vectorstores/`: Custom vector store adapters beyond built-in adapters.
- `app/ai/observers/`: Custom AI observability hooks.
- `app/providers/` or `main.py`: Application startup/shutdown hooks for global middlewares, `DatabaseObservabilityDriver.ensure_table()`, background agent cancellation (`AgentTracker.get_instance().cancel_all()`), and explicit vector/Redis handle cleanup. Note that `Application` already closes singleton and global `AIHttpPool` instances automatically during shutdown.
- `bootstrap/cache/prompts.json` (+ sidecar `.meta.json`): Generated automatically alongside `bootstrap/cache/config.json` when running `python -m swiftpy cache:build`.

---

## Step-by-Step Usage Guides & Practical Examples

### Step 1: Multi-Provider Client, Requests & Fluent Builder

You can start a fluent LLM builder in three equivalent ways:

- `AI.chat(model=..., provider=..., config=..., http_pool=...)` — convenient entry point supporting immediate model/provider selection.
- `ai(config=..., http_pool=..., default_provider=...)` — lowercase factory returning an `AIClient`.
- `AI.client(...)` / `AIClient(...)` — explicit client builder instantiation.

#### How Provider Auto-Inference Works

When calling `AIClient.send()` or `AIClient.stream()`, SwiftPY resolves the provider in this order:

1. Explicit `.provider("anthropic")` / `AI.chat(provider=...)`.
2. Model-name prefix inference when `model` is set without an explicit provider:
   - Contains `"claude"` -> `"anthropic"`
   - Contains `"gpt"`, `"o1"`, `"o3"`, `"davinci"`, or `"text-"` -> `"openai"`
3. Default fallback from `ai.default` (default `"openai"`).

Before dispatching any call, `AIClient` checks `ai.enabled` (`AI_ENABLED`) and raises `AIModuleDisabledError` if disabled, then resolves missing API keys into `AIAuthenticationError` (or `AIConfigurationError` for unknown provider names).

```python
from swiftpy.ai import AI, AIClient
from swiftpy.ai.providers import LLMRequest, LLMResponse, ProviderInterface, TokenUsage


async def summarize_ticket(ticket_text: str, user_id: int) -> dict[str, object]:
    # Automatically routes to the Anthropic provider because the model contains "claude",
    # or explicitly select .provider("openai").model("gpt-4o").
    response = await (
        AI.chat(model="claude-3-5-sonnet-20241022")
        .system("Summarize support tickets in 2 concise bullet points.")
        .user(ticket_text)
        .temperature(0.2)
        .max_tokens(300)
        # Internal metadata stays local (middlewares, observability); never sent to LLM JSON body
        .metadata(user_id=user_id, tenant_id="acme")
        .send()
    )

    return {
        "summary": response.content,
        "model": response.model,
        "tokens": response.usage.total_tokens,
        "latency_ms": response.latency_ms,
        "finish_reason": response.finish_reason,
    }
```

> **Metadata vs. `extra` Attribution Safety:** Use `.metadata(...)` for local tracing, observability tags, and middleware attribution. Any dictionary passed via `.extra(...)` is merged into the provider HTTP payload **except** `INTERNAL_ATTRIBUTION_KEYS` (`{"user_id", "tenant_id", "user", "tenant"}`), which `OpenAIProvider` and `AnthropicProvider` strip via `strip_internal_extra()` so internal IDs never leak to third-party APIs while still remaining readable by rate-limit and budget middlewares.

#### Registering Custom Providers

Implement `ProviderInterface` (`send(request)` and `stream(request)`) and register it by name:

```python
from collections.abc import AsyncIterator


class LocalEchoProvider(ProviderInterface):
    @property
    def name(self) -> str:
        return "local_echo"

    async def send(self, request: LLMRequest) -> LLMResponse:
        last = request.messages[-1].content if request.messages else ""
        return LLMResponse(
            content=f"Echo: {last}",
            model=request.model or "echo-v1",
            usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[str]:
        yield "Echo: "
        yield request.messages[-1].content if request.messages else ""


AIClient.register_provider("local_echo", LocalEchoProvider)
```

---

### Step 2: Streaming Responses & HTTP Pooling

Both `OpenAIProvider.stream()` and `AnthropicProvider.stream()` share `AIHttpPool`, decode chunks with `split_chunk_lines` (safe across multi-byte UTF-8 boundaries split across network frames), parse SSE events with `parse_sse_events`, record **time-to-first-token (`ttft_ms`)** and total `duration_ms` inside `AIObservabilityManager`, execute pre-flight safety middlewares, and close the underlying `httpx.Response` even if the consumer disconnects early (`CancelledError` / `GeneratorExit`):

```python
from swiftpy import Application
from swiftpy.ai import AI
from swiftpy.http.response import StreamingResponse

app = Application()


@app.get("/ai/stream")
async def stream_answer(question: str = "Explain asyncpg connection pools"):
    async def event_chunks():
        client = (
            AI.chat(model="gpt-4o-mini")
            .system("Answer concisely.")
            .user(question)
            .max_tokens(400)
        )
        async for token in client.stream():
            yield token.encode("utf-8")

    return StreamingResponse(
        event_chunks(), headers={"content-type": "text/plain; charset=utf-8"}
    )
```

Notice how `AIClient.stream()` behaves cleanly with observability and HTTP connections:

- If a client disconnects mid-stream, `GeneratorExit` or `asyncio.CancelledError` logs status `"cancelled"` in `AIObservabilityManager` and runs `await response.aclose()` inside the provider's `finally` block.
- Do not instantiate ad-hoc `httpx.AsyncClient` instances in handlers—rely on the shared `AIHttpPool` singleton configured under `config/ai.py` (`http.*`) and automatically closed by `Application` shutdown.

---

### Step 3: Prompt Templates (`Prompt` & `PromptLoader`), Versioning, Precompilation & Security

Instead of hardcoding long prompt strings in route handlers or services, organize templates under `prompts/` as `.md` or `.py` files (using `swiftpy make:prompt summarise` or `swiftpy make:prompt summarise.v2`).

#### Resolution Order, `messages` Lists & Interpolation Rules

When loading a prompt such as `"support/triage"` or `"summarise.v2"`:

1. `PromptLoader` resolves relative paths safely under `ai.prompts.path` (default `"prompts"`), rejecting traversal attempts (`"../secret"`, symlinks outside `base_dir`, or empty names) with `ValueError`.
2. **Precedence:** It checks `prompts/support/triage.md` first, then `prompts/support/triage.py`. Dotted version names such as `"summarise.v2"` resolve cleanly to `prompts/summarise.v2.py`.
3. Inside `.py` prompt files, `PromptLoader` checks attributes in order: `MESSAGES`, `messages` (a sequence of `{"role": ..., "content": ...}` dicts or `Message` objects, or zero-arg callable returning them), then `PROMPT`, `prompt`, `TEMPLATE`, `template` (`str`, sequence, or zero-arg callable), falling back to the module docstring if present.
4. Variables use double curly braces: `{{ issue }}` (whitespace inside braces is optional: `{{issue}}` or `{{ issue }}`).
5. **Single-pass rendering & escaping:**
   - Missing template variables raise `KeyError`.
   - Use `\{{ literal }}` to output literal `{{ literal }}` without substitution. Preceding literal backslashes such as `C:\\{{ folder }}` are preserved accurately.
   - Variable values containing `{{ ... }}` are never re-interpolated on a second pass.

```python
# prompts/summarise.v2.py
messages: list[dict[str, str]] = [
    {"role": "system", "content": "Summarise concisely in {{ style }} style."},
    {"role": "user", "content": "{{ text }}"},
]
```

```markdown
<!-- prompts/support/triage.md -->
You are triaging ticket #{{ ticket_id }} for tier {{ plan }}.
Ignore literal syntax examples like \{{ placeholder }}.

Customer message:
{{ body }}
```

Use `Prompt.aload(...)` / `aload_prompt(...)` / `.aprompt(...)` inside `async def` request handlers so filesystem reads run via `asyncio.to_thread` rather than blocking the event loop:

```python
from swiftpy.ai import AI, Prompt, PromptLoader, aload_prompt


async def draft_triage_reply(ticket_id: int, plan: str, body: str) -> str:
    # Option A: attach rendered template directly on a fluent AIClient
    # (when the prompt defines a `messages` list, all messages are appended automatically)
    res = await (
        AI.aprompt(
            "support/triage",
            model="gpt-4o-mini",
            ticket_id=ticket_id,
            plan=plan,
            body=body,
        )
        .temperature(0.1)
        .send()
    )
    return res.content


async def render_only(ticket_id: int, plan: str, body: str) -> str | list[dict[str, str]]:
    # Option B: load via the Prompt facade (supports versioned .v2 names and messages lists)
    rendered_v2 = await Prompt.aload("summarise.v2", style="3-bullet", text=body)
    # Or standalone string/template helpers
    rendered = await aload_prompt(
        "support/triage",
        ticket_id=ticket_id,
        plan=plan,
        body=body,
    )
    # Or via a scoped loader instance: PromptLoader(base_dir="prompts")
    loader = PromptLoader.get_instance()
    raw = await loader.aget_template("support/triage")
    assert isinstance(raw, str)
    assert isinstance(rendered_v2, list)
    return loader.render(raw, ticket_id=ticket_id, plan=plan, body=body) or rendered
```

> **`AI.prompt()` / `Prompt.load()` vs. `AI.aprompt()` / `Prompt.aload()`:** Synchronous `AI.prompt()`, `Prompt.load()`, and `client.prompt()` read disk files synchronously unless cached. Inside async request handlers, prefer `await AI.aprompt(...)`, `await client.aprompt(...)`, `await Prompt.aload(...)`, or `await Prompt.aload_messages(...)`.

#### Production Precompilation (`cache:build`)

When `APP_ENV=production` or `ai.prompts.cache=True`, `PromptLoader.is_cache_enabled()` activates in-memory template caching and can load precompiled templates from `bootstrap/cache/prompts.json`:

```bash
# Precompile config/ -> bootstrap/cache/config.json AND prompts/ -> bootstrap/cache/prompts.json
python -m swiftpy cache:build

# Remove compiled config and prompt caches
python -m swiftpy cache:clear
```

`PromptLoader.dump_cache()` writes a companion `.meta.json` recording the `base_dir` path so another `PromptLoader` pointing at a different directory will not accidentally load mismatched cached templates.

---

### Step 4: Conversations, History Trimming & Redis Stores

Use `Conversation` when you want structured multi-turn message state, portable JSON/dict serialization, or sliding-window / token-budget history management:

```python
from swiftpy.ai import (
    AI,
    Conversation,
    InMemoryConversationStore,
    RedisConversationStore,
    SlidingWindowStrategy,
    TokenBudgetStrategy,
)


async def chat_turn(session_id: str, user_text: str, use_redis: bool = True) -> str:
    store = (
        RedisConversationStore(key_prefix="ai:conversation", default_ttl=86400)
        if use_redis
        else InMemoryConversationStore(max_entries=1000, default_ttl=3600)
    )
    try:
        conv = await store.load(session_id)
        if not conv.system_messages:
            conv.add_system("You are a helpful product specialist.")

        # Non-mutating history truncation: returns a new trimmed Conversation
        strategy = TokenBudgetStrategy(max_tokens=2000)
        trimmed = strategy.apply(conv)

        # Attach trimmed conversation; AIClient appends user+assistant turns ONLY if send() succeeds
        response = await (
            AI.chat(model="gpt-4o-mini")
            .conversation(trimmed)
            .user(user_text)
            .send()
        )

        # Sync the successful turn back to the full or trimmed conversation and persist
        conv.add_user(user_text).add_assistant(response.content)
        await store.save(session_id, conv)
        return response.content
    finally:
        if isinstance(store, RedisConversationStore):
            await store.close()
```

Key behaviors verified in `Conversation`, `HistoryStrategy`, and conversation stores:

- **`Conversation` mutates in place & `.clone()` copies:** `.add_system()`, `.add_user()`, `.add_assistant()`, and `.add_message()` mutate that `Conversation` instance and return `self` for chaining. Call `conv.clone()` whenever you need an independent branch.
- **Failed `.send()` rollback guarantee:** Attaching `.conversation(conv)` to `AIClient` will **not** append the pending `user` prompt or an empty turn to `conv` if `send()` raises an error—mutations occur only after a complete `LLMResponse` returns.
- **Orphaned tool-call cleanup in strategies:** Both `SlidingWindowStrategy(max_messages=...)` and `TokenBudgetStrategy(max_tokens=...)` preserve leading `system` messages and automatically strip orphaned leading `role="tool"` or `role="assistant"` (`tool_calls=...`) messages left after trimming older dialogue turns, preventing provider HTTP 400 validation errors.
- **Store TTL semantics (`ttl` parameter):** In both `InMemoryConversationStore` and `RedisConversationStore`, `ttl > 0` sets expiry seconds, `ttl == 0` disables expiration (persists indefinitely), and `ttl < 0` deletes the stored session immediately. `InMemoryConversationStore` also enforces `max_entries` LRU eviction.

---

### Step 5: Embeddings & Vector Stores (`PgVectorAdapter`, Cloud & Local Adapters)

#### Generating Embeddings

`EmbeddingsClient` (or `AI.embed()` / `AI.embed_batch()`) chunks large batches up to the provider limit (`openai` supports up to `2048` texts per request, default `chunk_size=100`), runs chunks with bounded concurrency (`concurrency=5`), verifies finite floats and consistent dimensions, and preserves input ordering by response index:

```python
from swiftpy.ai import AI, EmbeddingsClient


async def embed_faq_entries(articles: list[str]) -> list[list[float]]:
    # Uses OpenAI text-embedding-3-small by default (requires OPENAI_API_KEY)
    # Or pass provider="deterministic" in offline development/tests
    return await AI.embed_batch(
        articles,
        model="text-embedding-3-small",
        chunk_size=100,
        concurrency=5,
    )
```

#### Unified `VectorStore` Adapters

Instantiate any adapter through `AI.vectorstore(adapter=..., collection=..., dimensions=..., metric=..., **kwargs)` or `VectorStore.<driver>(...)`. Every adapter validates collection names, record IDs, vector finiteness (rejecting `NaN`, `Inf`, and zero-norm vectors for `"cosine"`), metadata filter operators (`$eq`, `$ne`, `$gt`, `$gte`, `$lt`, `$lte`, `$in`, `$nin`, `$exists`, `$and`, `$or`), and normalizes `SearchResult.score` so **higher always means closer** across `"cosine"`, `"l2"`, and `"inner_product"`.

| Adapter Key | Class | Storage / Transport | Filtering (`nested`) | Namespaces | HNSW / Index Notes |
| --- | --- | --- | --- | --- | --- |
| `"in_memory"` | `InMemoryVectorStore` | Local process RAM (LRU `max_records=100_000`) | Yes (`True`) | `False` | Thread-safe async lock, deep-copied records |
| `"pgvector"` / `"postgres"` | `PgVectorAdapter` | PostgreSQL + `pgvector` extension (`DatabasePool` or `asyncpg.Pool`) | Yes (`True`) JSONB `@>` / operators | `False` | Auto-creates HNSW index (`vector_cosine_ops` / `l2` / `ip`) & 63-byte safe identifiers |
| `"qdrant"` | `QdrantAdapter` | HTTP REST (`AIHttpPool`) | Yes (`True`) | `False` | Maps arbitrary string IDs deterministically to Qdrant UUIDs while preserving `_orig_id` |
| `"pinecone"` | `PineconeAdapter` | HTTPS REST (`AIHttpPool`) | Yes (`False` — flat filters) | `True` | Validates environment slug + HTTPS URL (`SSRF` protection) |
| `"chroma"` | `ChromaAdapter` | HTTP REST (`AIHttpPool`) | Yes (`False` — flat filters) | `False` | Resolves tenant/database/collection IDs automatically |
| `"lancedb"` | `LanceDbAdapter` | HTTP REST (`AIHttpPool`) | Yes (`True`) | `False` | Batch delete uses ID lists (`delete_batch`) rather than raw SQL predicates |

> **Capabilities Awareness:** Inspect `store.capabilities` (`VectorStoreCapabilities`) at runtime if writing adapter-agnostic code—especially `nested_metadata_filtering` (false on Chroma/Pinecone), `namespaces` (true only on Pinecone), and `pagination` (currently `False` across all built-in adapters).

#### PostgreSQL `pgvector` (`PgVectorAdapter`) Example

`PgVectorAdapter` works directly with SwiftPY's DI-managed `DatabasePool` (including active `db.transaction()` blocks!) or an explicit `DatabasePool` / `asyncpg.Pool`. Tables are named `swiftpy_vectors_<collection>` with automatic identifier hashing if names exceed PostgreSQL's 63-byte limit:

```python
from swiftpy.ai import AI, EmbeddingsClient, VectorRecord, VectorStore


async def setup_and_query_pgvector() -> list[dict[str, object]]:
    # Option A: reuse application DatabasePool from DI, or pass pool=... explicitly
    store = AI.vectorstore(
        adapter="pgvector",
        collection="knowledge_base",
        dimensions=1536,
        metric="cosine",
    )
    embeddings = EmbeddingsClient()

    try:
        # Provisions extension (`CREATE EXTENSION IF NOT EXISTS vector`), table, HNSW index
        await store.create_collection("knowledge_base", dimensions=1536, metric="cosine")

        docs = [
            ("doc-1", "SwiftPY closes AIHttpPool automatically on shutdown.", {"topic": "ai", "tier": "core"}),
            ("doc-2", "PgVectorAdapter uses HNSW indexes and JSONB filters.", {"topic": "vectors", "tier": "pro"}),
        ]
        vecs = await embeddings.embed_batch([text for _, text, _ in docs])

        records = [
            VectorRecord(id=doc_id, vector=vec, content=text, metadata=meta)
            for (doc_id, text, meta), vec in zip(docs, vecs)
        ]
        await store.upsert_batch(records)

        query_vec = await embeddings.embed("How are vector metadata filters queried?")
        hits = await store.search(
            query_vec,
            limit=3,
            filter={"$or": [{"topic": {"$eq": "vectors"}}, {"tier": "core"}]},
        )
        return [{"id": h.id, "score": h.score, "content": h.content, "meta": h.metadata} for h in hits]
    finally:
        # Safe to call even if reusing external DI DatabasePool (only closes owned pools)
        if hasattr(store, "aclose"):
            await store.aclose()
```

---

### Step 6: Retrieval-Augmented Generation (`rag()`, `Retriever`, Rerankers & Formatters)

SwiftPY's RAG stack lives under `swiftpy.ai.rag`.

> **Import Disambiguation (`rag` function vs. `swiftpy.ai.rag` submodule):** `swiftpy.ai` re-exports the builder helper function `rag()`. If you need submodules like `chunk_text`, `truncate_to_token_budget`, `Retriever`, or formatters, import explicitly with `from swiftpy.ai.rag import Retriever, CohereReranker, CrossEncoderReranker, XMLBlocksFormatter` and `from swiftpy.ai.rag.retriever import chunk_text, truncate_to_token_budget` (do not write `import swiftpy.ai.rag as r`, which binds the `rag()` function).

#### Indexing, Retrieval, Reranking & Grounded Generation

1. **Chunk & Index:** Split large documents with `chunk_text(text, chunk_size=1000, overlap=200)` and index via `await retriever.add_documents(docs, metadatas=..., ids=...)`.
2. **Retrieve & Rerank:** `Retriever.retrieve()` embeds the query and searches `VectorStore` with optional `min_score` and `filter`. Add `.reranker(CohereReranker(...))` or `.reranker(CrossEncoderReranker(scoring_fn=...))` (whose sync `scoring_fn` is automatically offloaded via `loop.run_in_executor` so local cross-encoder inference does not freeze the ASGI event loop).
3. **Token Budget & Prompt-Injection Defense:** `.max_context_tokens(n)` invokes `truncate_to_token_budget(...)` before formatting. When rendering prompts, `RAGPipelineBuilder` automatically escapes embedded boundary tokens via `escape_context_boundaries()` and wraps context inside a **per-call cryptographic nonce delimiter**:
   `<<<CONTEXT_<random_hex>>>> ... <<<END CONTEXT_<random_hex>>>>` alongside untrusted-context system instructions. Using `.formatter("xml")` (`XMLBlocksFormatter`) additionally validates XML tag names and escapes document content and metadata attributes.

```python
from swiftpy.ai import AI, EmbeddingsClient, InMemoryVectorStore, rag
from swiftpy.ai.rag import CrossEncoderReranker, Retriever, XMLBlocksFormatter
from swiftpy.ai.rag.retriever import chunk_text


def score_pairs(query: str, docs: list[str]) -> list[float]:
    # Synchronous local scorer (safe because CrossEncoderReranker runs it in an executor)
    q_terms = set(query.lower().split())
    return [float(sum(1 for t in q_terms if t in d.lower())) for d in docs]


async def answer_with_rag(question: str, handbook_markdown: str) -> dict[str, object]:
    store = InMemoryVectorStore(dimensions=1536, metric="cosine")
    await store.create_collection("handbook", dimensions=1536, metric="cosine")

    embeddings = EmbeddingsClient()
    retriever = Retriever(
        vectorstore=store,
        embeddings=embeddings,
        provider="deterministic",  # swap to "openai" in production
        top_k=8,
        min_score=0.0,
    )

    chunks = chunk_text(handbook_markdown, chunk_size=500, overlap=80)
    await retriever.add_documents(
        chunks,
        metadatas=[{"source": "handbook.md", "chunk": idx} for idx in range(len(chunks))],
    )

    result = await (
        rag()
        .retriever(retriever)
        .top_k(6)
        .reranker(CrossEncoderReranker(scoring_fn=score_pairs))
        .rerank_top_n(3)
        .max_context_tokens(1200)
        .formatter(XMLBlocksFormatter(root_tag="sources", document_tag="doc"))
        .generator(AI.chat(model="gpt-4o-mini").metadata(feature="handbook_qa"))
        .temperature(0.1)
        .max_tokens(400)
        .run(question)
    )

    return {
        "answer": result.text,
        "model": result.model,
        "tokens": result.tokens.total_tokens,
        "cost_usd": result.cost,
        "sources": [{"id": s.id, "score": s.score, "metadata": s.metadata} for s in result.sources],
    }
```

> **Note on `RAGPipelineBuilder.run()` Kwargs:** Additional keyword arguments passed to `.run(query, filter=..., top_k=..., min_score=...)` (or `.query(...)`) are inspected against `retriever.retrieve`'s signature so retrieval overrides go to retrieval while remaining arguments flow to custom callables.

---

### Step 7: Tools, Autonomous Tool Loops & Agents

#### Defining Tools (`@tool` / `FunctionTool` vs. `BaseTool`)

Use `@tool` for lightweight typed functions—SwiftPY infers JSON Schema parameter types (`str`, `int`, `float`, `bool`, `list`, `dict`, `Literal`, `Enum`, `Optional` / `Union`), extracts docstrings, validates LLM arguments strictly before execution (`validate_tool_arguments`), and automatically runs **synchronous** tool functions inside `asyncio.to_thread` so they do not block the event loop:

```python
from enum import StrEnum
from swiftpy.ai import BaseTool, CalculatorTool, DateTimeTool, WebSearchTool, tool


class Priority(StrEnum):
    LOW = "low"
    HIGH = "high"


@tool
async def create_support_note(ticket_id: int, summary: str, priority: Priority = Priority.LOW) -> dict[str, object]:
    """Attach an internal triage note to an existing support ticket."""
    return {"ticket_id": ticket_id, "priority": priority.value, "summary": summary, "saved": True}


class InventoryLookupTool(BaseTool):
    @property
    def name(self) -> str:
        return "inventory_lookup"

    @property
    def description(self) -> str:
        return "Check stock count for an SKU in the warehouse."

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {"sku": {"type": "string", "description": "Catalog SKU code"}},
            "required": ["sku"],
        }

    async def handle(self, sku: str = "", **kwargs: object) -> dict[str, object]:
        return {"sku": sku.upper(), "in_stock": 14}
```

Built-in first-party tools in `swiftpy.ai`:

- **`CalculatorTool()`:** Evaluates mathematical expressions via a restricted Python AST evaluator (`_safe_eval_node` — zero `eval()`), enforcing expression length (`max_expression_length=1000`) and AST depth (`max_ast_depth=25`) in a worker thread.
- **`DateTimeTool()`:** Returns structured ISO, date, time, weekday, year, and `zoneinfo`-validated timezone fields.
- **`WebSearchTool(search_fn=..., timeout=10.0)`:** Wraps your async or sync web-search callback with a strict timeout; raises `AIConfigurationError` if called without a configured `search_fn` (synthetic fallback results are intentionally disabled).

#### Direct `ToolLoop` via `AIClient.auto_execute_tools()`

You can enable multi-turn autonomous tool execution directly on `AIClient`:

```python
from swiftpy.ai import AI, CalculatorTool, DateTimeTool


async def solve_math_and_date(question: str) -> str:
    res = await (
        AI.chat(model="gpt-4o-mini")
        .system("Use tools for exact arithmetic and current UTC dates.")
        .user(question)
        .tools([CalculatorTool(), DateTimeTool()])
        .auto_execute_tools(
            enable=True,
            max_iterations=6,
            max_tool_calls=8,
            allowed_tools=["calculator", "date_time"],
            tool_timeout=5.0,
            max_total_tokens=15000,
            max_cost=0.10,
        )
        .send()
    )
    return res.content
```

Key safeguards built into `ToolLoop`:

- Tool arguments are validated against JSON Schema before invoking `handle()`; malformed arguments, missing tools, timeouts, and tool exceptions are returned as serialized tool-error payloads so the LLM can self-correct without leaking sensitive error tokens (`redact_sensitive_text`).
- When `max_tool_calls` is reached, `ToolLoop` sets `_suppress_tools_in_request = True` on the synthesis turn or raises `AgentLoopError` if the model exceeds allowed calls/iterations, and `AIBudgetExceededError` if cumulative loop tokens (`max_total_tokens`) or spend (`max_cost`) cross the configured threshold.

#### Stateful Agents (`Agent` / `BaseAgent`), Session Memory & Background Tasks

`Agent` (public alias for `BaseAgent`) binds together an isolated cloned `AIClient`, system instructions, tools, guardrails, and an interchangeable `AgentMemory` backend (`InMemoryMemoryBackend`, `RedisMemoryBackend`, or PostgreSQL `DatabaseMemoryBackend`):

```python
from swiftpy.ai import (
    Agent,
    AgentMemory,
    CalculatorTool,
    DatabaseMemoryBackend,
    RedisMemoryBackend,
)
from swiftpy.ai.agents import AgentTracker


def build_finance_agent(session_id: str, storage: str = "database") -> Agent:
    backend = (
        DatabaseMemoryBackend(table_name="finance_agent_memory", default_ttl=86400)
        if storage == "database"
        else RedisMemoryBackend(key_prefix="agent:finance", default_ttl=86400)
    )
    memory = AgentMemory(backend=backend, session_id=session_id)

    return Agent(
        name="FinanceAssistant",
        system_prompt="You are a careful financial analyst. Always verify calculations.",
        tools=[CalculatorTool(), InventoryLookupTool()],
        memory=memory,
        model="gpt-4o-mini",
        max_iterations=8,
        max_tool_calls=6,
        tool_timeout=10.0,
        max_total_tokens=20000,
        max_cost=0.25,
    )


async def run_agent_examples() -> dict[str, object]:
    agent = build_finance_agent("tenant-42:user-7", storage="redis")

    # Concurrent calls on the same session_id are automatically serialized via per-session asyncio.Lock
    reply = await agent.run("Calculate 15% of 2400 and check SKU A12 stock.", session_id="tenant-42:user-7")

    # Session key-value state alongside conversation history:
    session_mem = agent.get_session_memory("tenant-42:user-7")
    await session_mem.set_state("last_sku", "A12")

    # Non-blocking background execution tracked by AgentTracker (graceful shutdown cancellable):
    task = agent.run_background("Generate a brief recap of our conversation.", session_id="tenant-42:user-7")
    bg_reply = await task.wait()

    return {
        "reply": reply.content,
        "iterations": reply.iterations,
        "tools_called": [c.name for c in reply.executed_calls],
        "background_reply": bg_reply.content,
    }
```

During application shutdown, cancel any still-running background `AgentTask`s before closing HTTP/DB pools:

```python
await AgentTracker.get_instance().cancel_all(timeout=5.0)
```

> **`DatabaseMemoryBackend` Auto-Provisioning:** `DatabaseMemoryBackend` participates in active `db.transaction()` connections and lazily runs `ensure_table()` creating `<table_name>_messages` and `<table_name>_state` (with bounded 63-byte PostgreSQL index names and `expires_at` TTL support). Remember to call `await backend.close()` when shutting down standalone `RedisMemoryBackend` instances.

---

### Step 8: Middlewares (`AIRateLimitMiddleware`, `TokenBudgetMiddleware`, `ContentModerationMiddleware`), Pricing & Observability

Middlewares wrap every `AIClient.send()` **and** `AIClient.stream()` invocation using the `AIMiddleware` protocol (`async def __call__(request: LLMRequest, next_call: NextHandler) -> LLMResponse`). Attach per client with `.use(mw)` or application-wide via `AI.add_middleware(mw)` (`AIClient.use_global_middleware(mw)`):

```python
import re
from swiftpy.ai import (
    AI,
    AIRateLimitMiddleware,
    ContentModerationMiddleware,
    TokenBudgetMiddleware,
    register_model_pricing,
)


async def check_policy(text: str) -> tuple[bool, str]:
    # Replace with external moderation endpoint if required
    if "drop table" in text.lower():
        return True, "Disallowed destructive SQL instruction"
    return False, ""


# Register custom/fine-tuned model pricing (USD per 1M input / output tokens)
register_model_pricing("gpt-4o-custom", 2.50, 10.00)

AI.add_middleware(
    ContentModerationMiddleware(
        checker=check_policy,
        banned_words=["forbidden-secret-token"],
        patterns=[re.compile(r"\b\d{3}-\d{2}-\d{4}\b")],
        fail_closed=True,
        check_all_roles=True,
    )
)

AI.add_middleware(
    AIRateLimitMiddleware(
        requests_per_minute=60,
        window_seconds=60.0,
        storage="redis",      # "auto" | "redis" | "memory"
        fail_closed=True,     # Reject requests if Redis is unreachable in production
    )
)

AI.add_middleware(
    TokenBudgetMiddleware(
        daily_token_limit=200_000,
        daily_cost_limit=5.00,
        global_daily_token_limit=5_000_000,
        global_daily_cost_limit=100.00,
        per_user=True,
        mode="hard",          # Atomic Lua reservation pre-flight + rollback/reconciliation post-call
        storage="redis",
    )
)
```

How built-in middlewares work under the hood:

- **User & Tenant Attribution:** `resolve_user_id` and `resolve_tenant_id` inspect `request.metadata`, then `request.extra`, then request `Context` (`__swiftpy_current_user__`, `auth.user`, `user`; `tenant_id`, `tenant`, `auth.tenant_id`), defaulting to `"anonymous"` and `"default"`.
- **`TokenBudgetMiddleware` `mode="hard"` vs. `mode="soft"`:** `"soft"` checks current accumulated spend at request entry and adds usage after the response returns. `"hard"` computes a conservative estimate (`prompt_chars // 4` + `min(max_tokens, 2048)` or `default_reservation_tokens=250`), atomically reserves it via Redis Lua (`_LUA_BUDGET_CHECK_AND_RESERVE`) or `asyncio.Lock`, rolls back on provider exception, and reconciles exact token/dollar delta (`actual - reserved`) once the response completes—preventing concurrent requests from bursting past daily caps.
- **`AIRateLimitMiddleware`:** Executes an atomic Redis sliding-window Lua script (`ZREMRANGEBYSCORE` + `ZCARD` + `ZADD`) and raises `AIRateLimitError(retry_after=...)` cleanly when exceeded.

#### Observability Telemetry (`AIObservabilityManager`)

Every `AIClient.send()`, `AIClient.stream()`, `AIFake`, and `ToolLoop` call logs an `AIObservation` containing `model`, `provider`, `input_tokens`, `output_tokens`, `total_tokens`, `cost`, `latency_ms`, `ttft_ms`, `duration_ms`, SHA-256 `prompt_hash`, `status` (`"success"`, `"error"`, or `"cancelled"`), redacted `error_message`, and sanitized `metadata` (stripping keys matching `api_key`, `password`, `secret`, `token`, `authorization`, `private_key`).

```python
from swiftpy.ai import AIObservabilityManager, DatabaseObservabilityDriver


async def bootstrap_ai_observability() -> dict[str, object]:
    driver = DatabaseObservabilityDriver()
    # IMPORTANT: DatabaseObservabilityDriver does NOT run implicit DDL inside record();
    # always ensure/migrate `ai_observations` during application startup.
    await driver.ensure_table()

    manager = AIObservabilityManager(drivers=[driver])
    AIObservabilityManager.set_instance(manager)

    stats = await manager.get_stats()
    recent = await manager.get_observations(limit=20, provider="openai")
    return {"stats": stats, "recent_count": len(recent)}
```

> **Startup DDL Requirement (`DatabaseObservabilityDriver`):** Unlike `DatabaseMemoryBackend`, `DatabaseObservabilityDriver.record()` deliberately avoids implicit schema DDL on hot paths. Call `await driver.ensure_table()` during application startup (or include `driver.get_ddl()` in your migrations) so `ai_observations` and its timestamp/prompt-hash indexes exist before live LLM traffic arrives. Also remember that `AIObservabilityManager` selects `InMemoryObservabilityDriver` automatically when `APP_ENV=testing` (unless overridden by `ai.observability.driver`).

---

### Step 9: Deterministic Testing with `AIFake`

Use `AIFake` to intercept every LLM and embedding call without hitting external vendor APIs. `AIFake` uses a task-local `ContextVar` stack (`async with AIFake(...) as fake:`), making parallel async tests isolated from one another while still exercising middlewares and `AIObservabilityManager` telemetry:

```python
from swiftpy.ai import AI, CalculatorTool
from swiftpy.testing import AIFake


async def test_ai_workflows_with_fake() -> None:
    fixtures = {
        "gpt-4o": "Flagged for tier-1 escalation.",
        "claude": "Reviewed by Claude.",
        "*": "Default fallback answer.",
    }

    async with AIFake(fixtures=fixtures) as fake:
        res = await (
            AI.chat(model="gpt-4o-mini")
            .user("Billing double charge on invoice #901")
            .send()
        )
        assert res.content == "Flagged for tier-1 escalation."

        fake.assert_called_once()
        fake.assert_called_with(model="gpt-4o-mini", user="invoice #901")

        # Embeddings are automatically deterministic by default inside AIFake
        vec = await AI.embed("semantic query")
        assert len(vec) == 1536
        fake.assert_embedding_called_once()
        fake.assert_embedding_called_with(text="semantic query")
```

You can also pass a sequence queue (each call pops the next fixture, ideal for multi-step `ToolLoop` responses emitting `LLMResponse(tool_calls=[...])` followed by a final text response), a callable `(LLMRequest) -> str | LLMResponse`, or `embedding_fixtures=...`. If you use the imperative `AI.fake(...)` helper outside an `async with` block, always call `AI.restore()` in teardown.

---

## Important Defaults & Production Safeguards

| Subsystem | Default Value / Behavior | Production Safeguard |
| --- | --- | --- |
| **Enable Switch** | `ai.enabled = True`, `ai.default = "openai"` | Set `AI_ENABLED=false` to fail closed with `AIModuleDisabledError` during maintenance windows |
| **HTTP Connection Pool** | `max_connections=100`, `max_keepalive_connections=20`, `timeout=60.0` | Closed automatically by `Application` shutdown; avoid creating per-request `httpx.AsyncClient` instances |
| **Rate Limit Middleware** | `storage="auto"`, `fail_closed=False`, `requests_per_minute=60` | Set `storage="redis"` and `fail_closed=True` across multi-worker deployments + add `ai.redis` in `config/ai.py` |
| **Token Budget Middleware** | `per_user=True`, `mode="soft"`, `default_reservation_tokens=250` | Use `mode="hard"` + `storage="redis"` to reserve spend atomically before concurrent requests run |
| **Content Moderation** | `fail_closed=True`, `check_all_roles=True` | Keep `fail_closed=True` so moderation outages block rather than silently bypassing policy |
| **RAG Context Security** | `NumberedListFormatter`, untrusted context system & prompt templates | `RAGPipelineBuilder` adds per-call cryptographic `<<<CONTEXT_<hex>>>>` markers and escapes boundaries automatically |
| **Prompt Templates** | `.md` preferred over `.py`, uncached in local/dev unless `cache=True` | Run `python -m swiftpy cache:build` with `APP_ENV=production` or `AI_PROMPTS_CACHE=true`; use `aload_prompt` / `.aprompt()` in async handlers |
| **Tools & Web Search** | `ToolLoop` defaults to `max_iterations=10`, `max_concurrency=10` | Always set `max_tool_calls`, `allowed_tools`, `tool_timeout`, `max_cost`; pass an explicit `search_fn` to `WebSearchTool` |
| **Observability Driver** | `"memory"` in `testing` env, `"database"` otherwise | Run `await DatabaseObservabilityDriver().ensure_table()` on app startup; redact secrets automatically via `AIObservation` |
| **Background Agents & Vector Pools** | `AgentTracker` singleton tracks `AgentTask`s | Call `await AgentTracker.get_instance().cancel_all()` and `await pgvector_store.aclose()` / `await redis_store.close()` on shutdown |

- **Never leak internal user/tenant identifiers to third-party LLM APIs:** Always pass attribution (`user_id`, `tenant_id`) in `.metadata(...)` (or request `Context`) rather than trusting `.extra(...)` alone, and remember that `INTERNAL_ATTRIBUTION_KEYS` are stripped automatically before vendor HTTP dispatch.
- **Avoid blocking the asyncio loop:** Always use `await aload_prompt(...)` / `await client.aprompt(...)` instead of synchronous disk prompt loads inside request paths, rely on `CrossEncoderReranker`'s built-in executor offloading for local ML scoring functions, and type custom synchronous tools cleanly so `FunctionTool` offloads them with `asyncio.to_thread`.
- **Keep external vector adapter configuration explicit:** `PineconeAdapter` enforces HTTPS URLs and validates environment slugs against SSRF injection; when connecting Qdrant, Chroma, or LanceDB behind internal networks, lock down network ingress and pass explicit `config` or API keys rather than exposing unauthenticated ports.

---

## Next Step

Continue to [Developer CLI & Testing Utilities](2026-11-19-cli-and-testing.md) for the complete `swiftpy` console command reference (`new`, `start`, `make:*`, `migrate*`, `cache:*`, `queue:*`, `storage:link`) and SwiftPY's test harness (`TestClient`, `TestResponse`, `TestCase`, `db_transaction`, `Mail.fake()`, `AIFake`, `VectorStoreFake`).
