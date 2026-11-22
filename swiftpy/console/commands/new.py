from __future__ import annotations

import argparse
import base64
import os
import re
import secrets
import sys
from pathlib import Path

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

DOCKERFILE_CONTENT = """FROM python:3.12-slim-bookworm AS builder

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential libpq-dev && \\
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip && \\
    python -c 'import tomllib, subprocess, sys; subprocess.check_call([sys.executable, "-m", "pip", "wheel", "--no-cache-dir", "--wheel-dir", "/app/wheels", *tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]])'

FROM python:3.12-slim-bookworm

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \\
    libpq5 curl && \\
    rm -rf /var/lib/apt/lists/*

RUN groupadd -r swiftpy && useradd -r -g swiftpy swiftpy

COPY --from=builder /app/wheels /wheels
RUN pip install --no-cache-dir /wheels/*

COPY . .
RUN chown -R swiftpy:swiftpy /app

USER swiftpy
EXPOSE 8000

CMD ["swiftpy", "start", "--host", "0.0.0.0", "--port", "8000"]
"""

DOCKER_COMPOSE_CONTENT = """services:
  app:
    build: .
    ports:
      - "127.0.0.1:8000:8000"
    env_file:
      - .env
    environment:
      DB_HOST: postgres
      REDIS_HOST: redis
    volumes:
      - .:/app
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  postgres:
    image: pgvector/pgvector:pg16
    ports:
      - "127.0.0.1:5432:5432"
    environment:
      POSTGRES_DB: ${DB_DATABASE}
      POSTGRES_USER: ${DB_USERNAME}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USERNAME} -d ${DB_DATABASE}"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    ports:
      - "127.0.0.1:6379:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 5

volumes:
  postgres_data:
  redis_data:
"""

DOCKERIGNORE_CONTENT = """__pycache__
*.pyc
.venv
.env
.git
.gitignore
.pytest_cache
.mypy_cache
.ruff_cache
dist
build
storage/logs/*
storage/app/public/*
public/storage
tests
"""

GITIGNORE_CONTENT = """__pycache__/
*.py[cod]
*$py.class
.env
.venv/
env/
venv/
dist/
build/
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/
storage/logs/*.log
storage/app/public/*
!storage/app/public/.gitkeep
public/storage
"""

ENV_EXAMPLE_CONTENT = """# Application Configuration
APP_NAME=SwiftPY
APP_ENV=local
APP_KEY=
# Keep APP_DEBUG=false in staging/production; enable only for local debugging.
APP_DEBUG=false
APP_URL=http://localhost:8000

# Server (Uvicorn)
SERVER_HOST=127.0.0.1
SERVER_PORT=8000
SERVER_WORKERS=1
SERVER_RELOAD=true

# Database (PostgreSQL / asyncpg)
DB_CONNECTION=postgresql
DB_HOST=127.0.0.1
DB_PORT=5432
DB_DATABASE=swiftpy
DB_USERNAME=postgres
DB_PASSWORD=secret
DB_SSL=disable
DB_SSL_CA_FILE=
DB_POOL_MIN_SIZE=5
DB_POOL_MAX_SIZE=20
DB_POOL_MAX_QUERIES=50000
DB_POOL_MAX_INACTIVE_LIFETIME=300.0
DB_COMMAND_TIMEOUT=60.0
DB_CONNECT_TIMEOUT=10.0
DB_STATEMENT_TIMEOUT=30.0
DB_TRANSACTION_TIMEOUT=60.0

# Redis (Shared or dedicated instances)
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_PASSWORD=
REDIS_CACHE_DB=0
REDIS_QUEUE_DB=1
REDIS_EVENTS_DB=2

# Cache
CACHE_DRIVER=redis
CACHE_PREFIX=swiftpy_cache:
CACHE_DEFAULT_TTL=3600

# Events
EVENTS_DRIVER=redis
EVENTS_PREFIX=swiftpy_events:

# Queue
QUEUE_DRIVER=redis
QUEUE_PREFIX=swiftpy_queue:
QUEUE_NAME=default

# Filesystems & Storage
FILESYSTEM_DISK=local
STORAGE_LOCAL_ROOT=storage/app/private
STORAGE_PUBLIC_ROOT=storage/app/public
STORAGE_PUBLIC_URL=/storage
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_DEFAULT_REGION=us-east-1
AWS_BUCKET=
AWS_ENDPOINT=

# Mail Subsystem
MAIL_MAILER=smtp
MAIL_HOST=127.0.0.1
MAIL_PORT=587
MAIL_USERNAME=
MAIL_PASSWORD=
MAIL_ENCRYPTION=starttls
MAIL_TIMEOUT=30.0
MAIL_VALIDATE_CERTS=true
MAIL_FROM_ADDRESS=noreply@example.com
MAIL_FROM_NAME="${APP_NAME}"
RESEND_API_KEY=
SENDGRID_API_KEY=
MAILGUN_API_KEY=
MAILGUN_DOMAIN=

# Authentication & Security
AUTH_GUARD=jwt
AUTH_BLACKLIST_ENABLED=true
AUTH_HYDRATE_USER=false
AUTH_PASSIVE_RESOLUTION=false
JWT_SECRET=
JWT_ALGORITHM=HS256
JWT_ACCESS_TTL=3600
JWT_REFRESH_TTL=604800
AUTH_KEY_PEPPER=

# CORS (opt-in explicit comma-separated origins, e.g. http://localhost:3000,http://localhost:8000)
# Never pair '*' with CORS_ALLOW_CREDENTIALS=true
CORS_ALLOWED_ORIGINS=
CORS_ALLOW_CREDENTIALS=false
CORS_MAX_AGE=600

# Logging
LOG_CHANNEL=stack
LOG_LEVEL=info

# AI Subsystem
AI_ENABLED=true
AI_DEFAULT_PROVIDER=openai
AI_PROMPTS_PATH=prompts
AI_PROMPTS_CACHE=false
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-3-5-sonnet-20241022
"""

MAIN_PY_CONTENT = """from __future__ import annotations

from swiftpy import Application, Request, Response, json_response

from app.http.handler.exceptions import register_exception_handlers
from routes.api import router as api_router

app = Application()
register_exception_handlers(app)

# Mount default API routes under /api
app.include_router(api_router, prefix="/api")


@app.get("/")
async def home(request: Request) -> Response:
    return json_response(
        {
            "status": "ok",
            "message": "Hello from SwiftPY",
            "app": "SwiftPY",
        }
    )
"""

ROUTES_API_PY_CONTENT = """from __future__ import annotations

from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response
from swiftpy.http.router import Router

from app.http.controllers.user_controller import UserController

router = Router()


@router.get("/health")
async def health_check(request: Request) -> Response:
    \"\"\"Health check endpoint for container orchestrators and monitoring.\"\"\"
    return json_response({"status": "ok", "app": "SwiftPY"})


router.get("/users", UserController.index)
"""

USER_MODEL_CONTENT = """from __future__ import annotations

from datetime import datetime

from swiftpy.database.orm import Model


class User(Model):
    \"\"\"
    Default User ORM model representing application accounts.
    Maps to the `users` database table.
    \"\"\"

    __table__ = "users"
    __primary_key__ = "id"
    __fillable__ = ["name", "email", "password", "is_admin"]
    __hidden__ = ["password"]

    id: int
    name: str
    email: str
    password: str
    is_admin: bool
    created_at: datetime
    updated_at: datetime
"""

USER_SCHEMA_CONTENT = """from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models.user import User


@dataclass(slots=True)
class UserCreateSchema:
    \"\"\"Input validation DTO for user creation requests.\"\"\"

    name: str
    email: str
    password: str


@dataclass(slots=True)
class UserResponseSchema:
    \"\"\"Output serialization schema for user responses.\"\"\"

    id: int
    name: str
    email: str

    @classmethod
    def from_model(cls, user: User) -> UserResponseSchema:
        return cls(id=user.id, name=user.name, email=user.email)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


UserSchema = UserResponseSchema
"""

USER_SERVICE_CONTENT = """from __future__ import annotations

from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.user_schema import UserResponseSchema


class UserService:
    \"\"\"
    Domain business logic layer for user entity management.
    Coordinates between repositories, schemas, transactions, events, and background notifications.
    \"\"\"

    def __init__(self, repository: UserRepository | None = None) -> None:
        self.repository = repository or UserRepository()

    async def list_users(self) -> list[UserResponseSchema]:
        \"\"\"Return starter user response DTOs.\"\"\"
        return [
            UserResponseSchema(
                id=1,
                name="Alice Doe",
                email="alice@example.com",
            )
        ]

    async def create_user(
        self, name: str, email: str, password_hash: str
    ) -> User:
        \"\"\"Create a new user account with hashed credentials.\"\"\"
        user = await self.repository.create(
            name=name,
            email=email,
            password=password_hash,
            is_admin=False,
        )
        return user

    async def create_user_schema(
        self, name: str, email: str, password_hash: str
    ) -> UserResponseSchema:
        \"\"\"Create a new user account and return an explicit response schema.\"\"\"
        user = await self.create_user(name, email, password_hash)
        return UserResponseSchema.from_model(user)
"""

USER_RESOURCE_CONTENT = """from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable


class UserResource:
    \"\"\"HTTP API resource transformer for User entities and schemas.\"\"\"

    def __init__(self, resource: Any) -> None:
        self.resource = resource

    def to_dict(self) -> dict[str, Any]:
        \"\"\"Transform a User model or UserResponseSchema into an HTTP-safe dictionary.\"\"\"
        if isinstance(self.resource, dict):
            return {
                "id": self.resource.get("id"),
                "name": self.resource.get("name"),
                "email": self.resource.get("email"),
            }
        return {
            "id": getattr(self.resource, "id", None),
            "name": getattr(self.resource, "name", None),
            "email": getattr(self.resource, "email", None),
        }

    @classmethod
    def collection(cls, items: Iterable[Any]) -> list[dict[str, Any]]:
        \"\"\"Transform a collection of user items into HTTP-safe dictionaries.\"\"\"
        return [cls(item).to_dict() for item in items]
"""

USER_CONTROLLER_CONTENT = """from __future__ import annotations

from swiftpy.http.request import Request
from swiftpy.http.response import Response, json_response

from app.http.resources.user_resource import UserResource
from app.services.user_service import UserService


class UserController:
    \"\"\"
    HTTP controller for user API endpoints.

    Resolves constructor dependencies via the SwiftPY DI container and delegates
    domain logic to UserService before formatting responses with UserResource.
    \"\"\"

    def __init__(self, user_service: UserService) -> None:
        self.user_service = user_service

    async def index(self, request: Request) -> Response:
        \"\"\"List users serialized through UserResource.\"\"\"
        users = await self.user_service.list_users()
        return json_response(UserResource.collection(users))
"""

ASSISTANT_AGENT_CONTENT = """from __future__ import annotations

from typing import TYPE_CHECKING

from swiftpy.ai.agents import AgentMemory, BaseAgent

from app.ai.tools.system_status_tool import SystemStatusTool

if TYPE_CHECKING:
    from collections.abc import Sequence

    from swiftpy.ai.agents import BaseMemoryBackend
    from swiftpy.ai.client import AIClient
    from swiftpy.ai.tools import BaseTool
    from swiftpy.core.config import Config


class AssistantAgent(BaseAgent):
    \"\"\"Default conversational assistant agent wired with starter tools.\"\"\"

    def __init__(
        self,
        *,
        model: str | None = None,
        system_prompt: str = (
            "You are a helpful, concise SwiftPY application assistant."
        ),
        tools: Sequence[BaseTool | type[BaseTool]] | None = None,
        memory: AgentMemory | BaseMemoryBackend | None = None,
        client: AIClient | None = None,
        config: Config | None = None,
    ) -> None:
        default_tools: list[BaseTool | type[BaseTool]] = (
            list(tools) if tools is not None else [SystemStatusTool()]
        )
        super().__init__(
            name="AssistantAgent",
            model=model,
            system_prompt=system_prompt,
            tools=default_tools,
            memory=memory if memory is not None else AgentMemory(),
            client=client,
            config=config,
        )
"""

SYSTEM_STATUS_TOOL_CONTENT = """from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from swiftpy.ai.tools import BaseTool


class SystemStatusTool(BaseTool):
    \"\"\"Function-calling tool reporting basic application runtime status.\"\"\"

    @property
    def name(self) -> str:
        return "system_status"

    @property
    def description(self) -> str:
        return "Return the current UTC timestamp and application health status."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def handle(self, *args: Any, **kwargs: Any) -> Any:
        \"\"\"Return structured UTC timestamp and ok status.\"\"\"
        return {
            "status": "ok",
            "utc_time": datetime.now(tz=UTC).isoformat(),
        }
"""

PROMPT_SUMMARISE_CONTENT = """\"\"\"Named prompt template loaded via Prompt.load("summarise", ...).\"\"\"

from __future__ import annotations

messages: list[dict[str, str]] = [
    {
        "role": "system",
        "content": (
            "You are a precise technical editor. Summarise the provided content "
            "clearly in {{ style }} style."
        ),
    },
    {
        "role": "user",
        "content": "{{ text }}",
    },
]
"""

PROMPT_CLASSIFY_CONTENT = """\"\"\"Named classification prompt template loaded via Prompt.load("classify", ...).\"\"\"

from __future__ import annotations

messages: list[dict[str, str]] = [
    {
        "role": "system",
        "content": (
            "Classify the user input into exactly one of the allowed categories: "
            "{{ categories }}. Return only the chosen category label."
        ),
    },
    {
        "role": "user",
        "content": "{{ text }}",
    },
]
"""

USER_REPOSITORY_CONTENT = """from __future__ import annotations

from app.models.user import User


class UserRepository:
    \"\"\"
    Data access repository for User entities.
    Encapsulates database operations and keeps query logic decoupled from business services.
    \"\"\"

    async def find_by_id(self, user_id: int) -> User | None:
        \"\"\"Find a single user by primary key ID.\"\"\"
        return await User.find(user_id)

    async def find_by_email(self, email: str) -> User | None:
        \"\"\"Find a single user by email address.\"\"\"
        return await User.where("email", email).first()

    async def create(self, **attributes: object) -> User:
        \"\"\"Persist a new User model instance.\"\"\"
        return await User.create(**attributes)
"""

USER_REGISTERED_EVENT_CONTENT = """from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class UserRegistered:
    \"\"\"
    Domain event triggered when a new user registers an account.
    \"\"\"

    user_id: int
    email: str
    name: str
"""

SEND_WELCOME_NOTIFICATION_CONTENT = """from __future__ import annotations

import logging

from app.events.user_registered import UserRegistered

logger = logging.getLogger(__name__)


class SendWelcomeNotification:
    \"\"\"
    Event listener that sends a welcome notification or queued job
    when a UserRegistered event is dispatched.
    \"\"\"

    async def handle(self, event: UserRegistered) -> None:
        logger.info(
            "Handling UserRegistered event for %s (%s)",
            event.name,
            event.email,
        )
"""

SEND_NOTIFICATION_JOB_CONTENT = """from __future__ import annotations

import logging

from swiftpy.queue.job import Job

logger = logging.getLogger(__name__)


class SendNotificationJob(Job):
    \"\"\"
    Sample background queued job demonstrating asynchronous execution,
    retries, exponential backoff, and transactional dispatching.
    \"\"\"

    tries: int = 3
    backoff: int = 5

    def __init__(self, user_id: int, message: str) -> None:
        self.user_id = user_id
        self.message = message

    async def handle(self) -> None:
        \"\"\"Execute the notification dispatch logic.\"\"\"
        logger.info(
            "Dispatching background notification to user %s: %s",
            self.user_id,
            self.message,
        )
"""

WELCOME_EMAIL_CONTENT = """from __future__ import annotations

from swiftpy.mail.mailable import Mailable
from swiftpy.mail.message import MailMessage


class WelcomeEmail(Mailable):
    \"\"\"
    Sample transactional email sent to newly registered users.
    Renders views/mail/template.html with user context data.
    \"\"\"

    def __init__(self, name: str, to: str | list[str] | None = None) -> None:
        super().__init__()
        self.name = name
        self.to_address = [to] if isinstance(to, str) else (to or [])

    def build(self) -> MailMessage:
        return MailMessage(
            subject=f"Welcome to SwiftPY, {self.name}!",
            to=self.to_address,
            template="mail/template",
            context={
                "title": f"Welcome, {self.name}!",
                "content": "Thank you for joining our platform. We are thrilled to have you here!",
                "app_name": "SwiftPY",
            },
        )
"""

EXCEPTIONS_CONTENT = """from __future__ import annotations

from swiftpy.core.application import Application
from swiftpy.core.exceptions import HttpException


class ResourceConflictException(HttpException):
    \"\"\"Raised when an operation conflicts with existing server state (HTTP 409).\"\"\"

    status_code = 409
    message = "Resource conflict"


def register_exception_handlers(app: Application) -> None:
    \"\"\"Register custom exception renderers onto `app` when non-default JSON shapes are needed.\"\"\"
    _ = app
"""

AUTH_MIDDLEWARE_CONTENT = """from __future__ import annotations

from swiftpy.auth.auth_manager import AuthManager
from swiftpy.core.container import get_container
from swiftpy.core.context import Context
from swiftpy.core.exceptions import HttpException
from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response


class AuthMiddleware(Middleware):
    \"\"\"
    Authentication ASGI middleware for SwiftPY.

    Validates incoming credentials through the application's AuthManager guard,
    binds the authenticated user entity to `request.state["user"]` and
    task-isolated `Context`, and raises HTTP 401 when authentication fails.
    \"\"\"

    async def handle(self, request: Request, next_handler: Handler) -> Response:
        container = get_container()
        auth_manager = (
            container.resolve(AuthManager)
            if container.has(AuthManager)
            else AuthManager(container)
        )
        user = await auth_manager.authenticate(request)
        if user is None:
            raise HttpException(
                status_code=401,
                message="Unauthenticated: Missing or invalid credentials",
            )

        Context.set("__swiftpy_logged_out__", False)
        Context.set("auth.user", user)
        Context.set("__swiftpy_current_user__", user)
        if hasattr(user, "__table__"):
            Context.set("__swiftpy_current_user_model__", user)
        request.state.pop("__swiftpy_logged_out__", None)
        request.state["user"] = user

        return await next_handler(request)
"""

ADMIN_MIDDLEWARE_CONTENT = """from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from swiftpy.core.context import Context
from swiftpy.core.exceptions import HttpException
from swiftpy.http.middleware import Handler, Middleware
from swiftpy.http.request import Request
from swiftpy.http.response import Response


def _resolve_authenticated_user(request: Request) -> Any:
    \"\"\"Resolve the active authenticated user across Context and request.state, respecting logout state.\"\"\"
    if Context.get("__swiftpy_logged_out__") or request.state.get(
        "__swiftpy_logged_out__"
    ):
        return None
    user = Context.get("auth.user")
    if user is None:
        user = Context.get("__swiftpy_current_user__")
    if user is None:
        user = Context.get("__swiftpy_current_user_model__")
    if user is None:
        user = request.state.get("user")
    return user


def _is_admin_user(user: Any) -> bool:
    \"\"\"Return True only when user carries an explicit boolean `is_admin is True` in a mapping or model.\"\"\"
    if isinstance(user, Mapping):
        raw_flag = user.get("is_admin")
        if raw_flag is not None:
            return raw_flag is True
        if getattr(user, "is_admin", None) is True:
            return True
        model = getattr(user, "model", None) or Context.get(
            "__swiftpy_current_user_model__"
        )
        return getattr(model, "is_admin", None) is True
    return getattr(user, "is_admin", None) is True


class AdminMiddleware(Middleware):
    \"\"\"
    Authorization ASGI middleware for administrative access control.

    Retrieves the authenticated user from task-isolated `Context` or `request.state`.
    Verifies that the user exists and carries boolean `is_admin is True`, raising
    HTTP 401 Unauthorized if unauthenticated, or HTTP 403 Forbidden if not an administrator.
    \"\"\"

    async def handle(self, request: Request, next_handler: Handler) -> Response:
        user = _resolve_authenticated_user(request)
        if user is None:
            raise HttpException(
                status_code=401,
                message="Unauthenticated: Please authenticate first",
            )

        if not _is_admin_user(user):
            raise HttpException(
                status_code=403,
                message="Forbidden: Administrator access required",
            )

        return await next_handler(request)
"""

CREATE_USER_REQUEST_CONTENT = """from __future__ import annotations

from swiftpy.validation.form_request import FormRequest


class CreateUserRequest(FormRequest):
    \"\"\"
    FormRequest validating inbound user registration payloads.
    Encapsulates field rules, sanitization, and custom error messages.
    \"\"\"

    def rules(self) -> dict[str, str]:
        return {
            "name": "required|string|min:2|max:100",
            "email": "required|email|max:255",
            "password": "required|string|min:8",
        }

    def messages(self) -> dict[str, str]:
        return {
            "name.required": "Full name is required.",
            "email.email": "Please provide a valid email address.",
            "password.min": "Password must be at least 8 characters long.",
        }
"""

APP_SERVICE_PROVIDER_CONTENT = """from __future__ import annotations

from swiftpy.core.providers.base import ServiceProvider


class AppServiceProvider(ServiceProvider):
    \"\"\"
    Core application service provider.
    Registers application singletons and utilities in the DI container.
    \"\"\"

    def register(self) -> None:
        pass

    def boot(self) -> None:
        pass
"""

AUTH_SERVICE_PROVIDER_CONTENT = """from __future__ import annotations

from swiftpy.auth import (
    AuthManager,
    CurrentUser,
    Hash,
    JWTIssuer,
    TokenBlacklist,
    get_server_pepper,
)
from swiftpy.core.config import Config
from swiftpy.core.providers.base import ServiceProvider


class AuthServiceProvider(ServiceProvider):
    \"\"\"
    Register authentication services, token management, and task-scoped user resolution.
    \"\"\"

    def register(self) -> None:
        self.container.singleton(
            JWTIssuer,
            lambda c: JWTIssuer.from_config(
                c.resolve(Config) if c.has(Config) else None
            ),
        )
        self.container.singleton(
            TokenBlacklist,
            lambda c: TokenBlacklist(
                issuer=c.resolve(JWTIssuer) if c.has(JWTIssuer) else None,
                config=c.resolve(Config) if c.has(Config) else None,
            ),
        )
        self.container.singleton(AuthManager)
        self.container.singleton(Hash)
        self.container.scoped(
            CurrentUser,
            lambda _: CurrentUser(),
        )

    def boot(self) -> None:
        if self.container.has(Config):
            cfg = self.container.resolve(Config)
            app_env = str(cfg.get("app.env") or cfg.get("env") or "development").lower()
            debug_val = (
                cfg.get("app.debug")
                if cfg.get("app.debug") is not None
                else cfg.get("debug")
            )
            is_debug = (
                bool(debug_val)
                if debug_val is not None
                else app_env not in ("production", "prod")
            )
            default_guard = str(
                cfg.get("auth.defaults.guard") or cfg.get("auth.default") or "jwt"
            ).lower()
            if not is_debug:
                if default_guard == "jwt" and self.container.has(JWTIssuer):
                    self.container.resolve(JWTIssuer)
                elif default_guard == "api_key":
                    get_server_pepper(cfg)
"""

CACHE_SERVICE_PROVIDER_CONTENT = """from __future__ import annotations

from swiftpy.cache import Cache
from swiftpy.core.providers.base import ServiceProvider


class CacheServiceProvider(ServiceProvider):
    \"\"\"
    Register the Cache manager singleton into the application DI container.
    \"\"\"

    def register(self) -> None:
        self.container.singleton(Cache)

    def boot(self) -> None:
        pass
"""

DATABASE_SERVICE_PROVIDER_CONTENT = """from __future__ import annotations

from swiftpy.core.providers.base import ServiceProvider
from swiftpy.database.pool import DatabasePool


class DatabaseServiceProvider(ServiceProvider):
    \"\"\"
    Governs database connection pools, transaction events, and ORM lifecycle hooks.
    \"\"\"

    def register(self) -> None:
        self.container.singleton(DatabasePool)

    def boot(self) -> None:
        pass
"""

EVENT_SERVICE_PROVIDER_CONTENT = """from __future__ import annotations

from typing import ClassVar

from swiftpy.core.providers.base import ServiceProvider
from swiftpy.events import EventDispatcher, RedisEventBus

from app.events.user_registered import UserRegistered
from app.listeners.send_welcome_notification import SendWelcomeNotification


class EventServiceProvider(ServiceProvider):
    \"\"\"
    Wires domain events to their asynchronous listener classes.
    \"\"\"

    LISTENERS: ClassVar[dict[type, list[type]]] = {
        UserRegistered: [SendWelcomeNotification],
    }

    def register(self) -> None:
        self.container.singleton(
            EventDispatcher, lambda _: EventDispatcher()
        )
        self.container.singleton(
            RedisEventBus,
            lambda c: RedisEventBus(
                dispatcher=c.resolve(EventDispatcher),
            ),
        )

    def boot(self) -> None:
        dispatcher = self.container.resolve(EventDispatcher)
        for event_cls, listener_classes in self.LISTENERS.items():
            for listener_cls in listener_classes:
                listener_inst = listener_cls()
                dispatcher.listen(event_cls, listener_inst.handle)
"""

FILESYSTEM_SERVICE_PROVIDER_CONTENT = """from __future__ import annotations

from swiftpy.core.providers.base import ServiceProvider
from swiftpy.filesystem.manager import FilesystemManager


class FilesystemServiceProvider(ServiceProvider):
    \"\"\"
    Registers the FilesystemManager singleton into the application DI container.
    \"\"\"

    def register(self) -> None:
        self.container.singleton(FilesystemManager)

    def boot(self) -> None:
        pass
"""

QUEUE_SERVICE_PROVIDER_CONTENT = """from __future__ import annotations

from swiftpy.core.providers.base import ServiceProvider
from swiftpy.queue.dispatcher import QueueDispatcher


class QueueServiceProvider(ServiceProvider):
    \"\"\"
    Register the QueueDispatcher singleton into the application DI container.
    \"\"\"

    def register(self) -> None:
        self.container.singleton(
            QueueDispatcher, lambda _: QueueDispatcher()
        )

    def boot(self) -> None:
        pass
"""

MAIL_SERVICE_PROVIDER_CONTENT = """from __future__ import annotations

from swiftpy.core.config import Config
from swiftpy.core.providers.base import ServiceProvider
from swiftpy.mail.manager import MailManager


class MailServiceProvider(ServiceProvider):
    \"\"\"
    Register the MailManager singleton into the application DI container.
    \"\"\"

    def register(self) -> None:
        self.container.singleton(
            MailManager,
            lambda c: MailManager(
                config=c.resolve(Config) if c.has(Config) else None
            ),
        )

    async def shutdown(self) -> None:
        manager = self.container.get_singleton(MailManager)
        if isinstance(manager, MailManager):
            await manager.close()
"""

CREATE_USERS_MIGRATION_CONTENT = """from __future__ import annotations

from swiftpy.database.migrations import Blueprint, Migration, Schema


class CreateUsersTable(Migration):
    async def up(self) -> None:
        def define(table: Blueprint) -> None:
            table.id()
            table.string("name")
            table.string("email").unique()
            table.string("password")
            table.boolean("is_admin").default(False)
            table.timestamps()

        await Schema.create("users", define)

    async def down(self) -> None:
        await Schema.drop_if_exists("users")
"""

DATABASE_SEEDER_CONTENT = """from __future__ import annotations

from swiftpy.database.seeder import Seeder


class DatabaseSeeder(Seeder):
    \"\"\"
    Root database seeder.
    Run via `swiftpy db:seed`. Call additional seeders using `await self.call(...)`.
    \"\"\"

    async def run(self) -> None:
        # Example:
        # await self.call(UserSeeder)
        pass
"""

CONFIG_AI_CONTENT = """from __future__ import annotations

from swiftpy.helpers.helper import env, env_bool

CONFIG = {
    "enabled": env_bool("AI_ENABLED", True),
    "default": env("AI_DEFAULT_PROVIDER", "openai"),
    "prompts": {
        "path": env("AI_PROMPTS_PATH", "prompts"),
        "cache": env_bool("AI_PROMPTS_CACHE", False),
    },
    "providers": {
        "openai": {
            "api_key": env("OPENAI_API_KEY", ""),
            "model": env("OPENAI_MODEL", "gpt-4o"),
        },
        "anthropic": {
            "api_key": env("ANTHROPIC_API_KEY", ""),
            "model": env("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"),
        },
    },
    "http": {
        "max_connections": int(env("AI_HTTP_MAX_CONNECTIONS", "100")),
        "max_keepalive_connections": int(env("AI_HTTP_MAX_KEEPALIVE", "20")),
        "keepalive_expiry": float(env("AI_HTTP_KEEPALIVE_EXPIRY", "30.0")),
        "timeout": float(env("AI_HTTP_TIMEOUT", "60.0")),
    },
}
"""

CONFIG_APP_CONTENT = """from __future__ import annotations

from swiftpy.helpers.helper import env, env_bool

CONFIG = {
    "name": env("APP_NAME", "SwiftPY"),
    "env": env("APP_ENV", "local"),
    "debug": env_bool("APP_DEBUG", False),
    "enable_benchmarks": env_bool("APP_ENABLE_BENCHMARKS", False),
    "key": env("APP_KEY", ""),
    "url": env("APP_URL", "http://localhost:8000"),
    "cipher": "AES-256-GCM",
}
"""

CONFIG_AUTH_CONTENT = """from __future__ import annotations

from swiftpy.helpers.helper import env, env_bool

CONFIG = {
    "defaults": {
        "guard": env("AUTH_GUARD", "jwt"),
    },
    "hash": {
        "rounds": int(env("BCRYPT_ROUNDS", "12") or "12"),
    },
    "blacklist_enabled": env_bool("AUTH_BLACKLIST_ENABLED", True),
    "hydrate_user": env_bool("AUTH_HYDRATE_USER", False),
    "passive_resolution": env_bool("AUTH_PASSIVE_RESOLUTION", False),
    "guards": {
        "jwt": {
            "secret": env("JWT_SECRET", ""),
            "algorithm": env("JWT_ALGORITHM", "HS256"),
            "access_token_ttl": int(env("JWT_ACCESS_TTL", "3600") or "3600"),
            "refresh_token_ttl": int(env("JWT_REFRESH_TTL", "604800") or "604800"),
            "leeway": int(env("JWT_LEEWAY", "0") or "0"),
            "issuer": env("JWT_ISSUER") or None,
            "audience": env("JWT_AUDIENCE") or None,
            "private_key": env("JWT_PRIVATE_KEY") or None,
            "public_key": env("JWT_PUBLIC_KEY") or None,
        },
        "api_key": {
            "table": env("AUTH_API_KEYS_TABLE", "api_keys"),
            "prefix_column": "prefix",
            "hash_column": "key_hash",
            "header": "X-API-Key",
            "pepper": env("AUTH_KEY_PEPPER", ""),
        },
    },
    "redis": {
        "host": env("REDIS_HOST", "127.0.0.1"),
        "port": int(env("REDIS_PORT", "6379") or "6379"),
        "password": env("REDIS_PASSWORD") or None,
        "database": int(env("REDIS_AUTH_DB", env("REDIS_CACHE_DB", "0")) or "0"),
    },
}
"""

CONFIG_CACHE_CONTENT = """from __future__ import annotations

from swiftpy.helpers.helper import env

CONFIG = {
    "default": env("CACHE_DRIVER", "redis"),
    "prefix": env("CACHE_PREFIX", "swiftpy_cache:"),
    "default_ttl": int(env("CACHE_DEFAULT_TTL", "3600") or "3600"),
    "stores": {
        "memory": {
            "driver": "memory",
            "prefix": env("CACHE_PREFIX", "swiftpy_cache:"),
            "ttl": int(env("CACHE_DEFAULT_TTL", "3600") or "3600"),
        },
        "redis": {
            "driver": "redis",
            "host": env("REDIS_HOST", "127.0.0.1"),
            "port": int(env("REDIS_PORT", "6379") or "6379"),
            "password": env("REDIS_PASSWORD") or None,
            "database": int(env("REDIS_CACHE_DB", "0") or "0"),
            "prefix": env("CACHE_PREFIX", "swiftpy_cache:"),
            "ttl": int(env("CACHE_DEFAULT_TTL", "3600") or "3600"),
        },
    },
}
"""

CONFIG_CORS_CONTENT = """from __future__ import annotations

from swiftpy.helpers.helper import env, env_bool

CONFIG = {
    "allowed_origins": [
        origin.strip()
        for origin in str(env("CORS_ALLOWED_ORIGINS", "") or "").split(",")
        if origin.strip()
    ],
    "allowed_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    "allowed_headers": ["Content-Type", "Authorization", "X-Request-ID"],
    "expose_headers": ["X-Request-ID"],
    "allow_credentials": env_bool("CORS_ALLOW_CREDENTIALS", False),
    "max_age": int(env("CORS_MAX_AGE", "600") or "600"),
}
"""

CONFIG_DATABASE_CONTENT = """from __future__ import annotations

from swiftpy.helpers.helper import env

CONFIG = {
    "default": env("DB_CONNECTION", "postgresql"),
    "connections": {
        "postgresql": {
            "host": env("DB_HOST", "127.0.0.1"),
            "port": int(env("DB_PORT", "5432") or "5432"),
            "database": env("DB_DATABASE", "swiftpy"),
            "user": env("DB_USERNAME", "postgres"),
            "password": env("DB_PASSWORD", "secret"),
            "ssl": env("DB_SSL", "disable"),
            "ssl_ca_file": env("DB_SSL_CA_FILE") or None,
        },
    },
    "pool": {
        "min_size": int(env("DB_POOL_MIN_SIZE", "5") or "5"),
        "max_size": int(env("DB_POOL_MAX_SIZE", "20") or "20"),
        "max_queries": int(env("DB_POOL_MAX_QUERIES", "50000") or "50000"),
        "max_inactive_connection_lifetime": float(
            env("DB_POOL_MAX_INACTIVE_LIFETIME", "300.0") or "300.0"
        ),
        "command_timeout": float(env("DB_COMMAND_TIMEOUT", "60.0") or "60.0"),
    },
    "timeouts": {
        "connect": float(env("DB_CONNECT_TIMEOUT", "10.0") or "10.0"),
        "statement": float(env("DB_STATEMENT_TIMEOUT", "30.0") or "30.0"),
        "transaction": float(env("DB_TRANSACTION_TIMEOUT", "60.0") or "60.0"),
    },
}
"""

CONFIG_EVENTS_CONTENT = """from __future__ import annotations

from swiftpy.helpers.helper import env

CONFIG = {
    "default": env("EVENTS_DRIVER", "redis"),
    "redis": {
        "host": env("REDIS_HOST", "127.0.0.1"),
        "port": int(env("REDIS_PORT", "6379") or "6379"),
        "password": env("REDIS_PASSWORD") or None,
        "database": int(env("REDIS_EVENTS_DB", "2") or "2"),
        "prefix": env("EVENTS_PREFIX", "swiftpy_events:"),
    },
}
"""

CONFIG_FILESYSTEMS_CONTENT = """from __future__ import annotations

from swiftpy.helpers.helper import env

CONFIG = {
    "default": env("FILESYSTEM_DISK", "local"),
    "signing_key": env("FILESYSTEM_SIGNING_KEY") or None,
    "disks": {
        "local": {
            "driver": "local",
            "root": env("STORAGE_LOCAL_ROOT", "storage/app/private"),
            "visibility": "private",
        },
        "public": {
            "driver": "local",
            "root": env("STORAGE_PUBLIC_ROOT", "storage/app/public"),
            "url": env("STORAGE_PUBLIC_URL", "/storage"),
            "visibility": "public",
        },
        "s3": {
            "driver": "s3",
            "key": env("AWS_ACCESS_KEY_ID", ""),
            "secret": env("AWS_SECRET_ACCESS_KEY", ""),
            "region": env("AWS_DEFAULT_REGION", "us-east-1"),
            "bucket": env("AWS_BUCKET", ""),
            "endpoint": env("AWS_ENDPOINT") or None,
            "visibility": "private",
        },
    },
}
"""

CONFIG_LOGGING_CONTENT = """from __future__ import annotations

from swiftpy.helpers.helper import env

CONFIG = {
    "default": env("LOG_CHANNEL", "stack"),
    "level": env("LOG_LEVEL", "info"),
    "channels": {
        "stack": {
            "driver": "stack",
            "channels": ["daily"],
        },
        "single": {
            "driver": "single",
            "path": "storage/logs/swiftpy.log",
            "level": env("LOG_LEVEL", "info"),
        },
        "daily": {
            "driver": "daily",
            "path": "storage/logs/swiftpy.log",
            "level": env("LOG_LEVEL", "info"),
            "days": 14,
        },
        "stderr": {
            "driver": "stderr",
            "level": env("LOG_LEVEL", "info"),
        },
    },
}
"""

CONFIG_MAIL_CONTENT = """from __future__ import annotations

from swiftpy.helpers.helper import env, env_bool

CONFIG = {
    "default": env("MAIL_MAILER", "smtp"),
    "from": {
        "address": env("MAIL_FROM_ADDRESS", "noreply@example.com"),
        "name": env("MAIL_FROM_NAME", "SwiftPY"),
    },
    "mailers": {
        "smtp": {
            "transport": "smtp",
            "host": env("MAIL_HOST", "127.0.0.1"),
            "port": int(env("MAIL_PORT", "587") or "587"),
            "username": env("MAIL_USERNAME") or None,
            "password": env("MAIL_PASSWORD") or None,
            "encryption": env("MAIL_ENCRYPTION", "starttls"),
            "timeout": float(env("MAIL_TIMEOUT", "30.0") or "30.0"),
            "validate_certs": env_bool("MAIL_VALIDATE_CERTS", True),
        },
        "resend": {
            "transport": "resend",
            "api_key": env("RESEND_API_KEY", ""),
        },
        "sendgrid": {
            "transport": "sendgrid",
            "api_key": env("SENDGRID_API_KEY", ""),
        },
        "mailgun": {
            "transport": "mailgun",
            "api_key": env("MAILGUN_API_KEY", ""),
            "domain": env("MAILGUN_DOMAIN", ""),
        },
        "sendmail": {
            "transport": "sendmail",
            "path": "/usr/sbin/sendmail",
        },
        "console": {
            "transport": "console",
        },
        "fake": {
            "transport": "fake",
        },
    },
}
"""

CONFIG_QUEUE_CONTENT = """from __future__ import annotations

from swiftpy.helpers.helper import env

CONFIG = {
    "default": env("QUEUE_DRIVER", "redis"),
    "connections": {
        "redis": {
            "driver": "redis",
            "host": env("REDIS_HOST", "127.0.0.1"),
            "port": int(env("REDIS_PORT", "6379") or "6379"),
            "password": env("REDIS_PASSWORD") or None,
            "database": int(env("REDIS_QUEUE_DB", "1") or "1"),
            "queue": env("QUEUE_NAME", "default"),
            "prefix": env("QUEUE_PREFIX", "swiftpy_queue:"),
        },
    },
}
"""

VIEWS_MAIL_TEMPLATE_CONTENT = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }}</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background-color: #f4f4f7;
            color: #51545e;
            margin: 0;
            padding: 0;
            width: 100% !important;
            -webkit-text-size-adjust: none;
        }
        .email-wrapper {
            width: 100%;
            margin: 0;
            padding: 20px 0;
            background-color: #f4f4f7;
        }
        .email-content {
            max-width: 600px;
            margin: 0 auto;
            background-color: #ffffff;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.05);
        }
        .email-header {
            background-color: #1a1a2e;
            padding: 25px;
            text-align: center;
        }
        .email-header h1 {
            color: #ffffff;
            font-size: 20px;
            font-weight: 700;
            margin: 0;
        }
        .email-body {
            padding: 35px;
            line-height: 1.6;
        }
        .email-footer {
            padding: 20px;
            text-align: center;
            font-size: 12px;
            color: #a8aaaf;
        }
    </style>
</head>
<body>
    <div class="email-wrapper">
        <div class="email-content">
            <div class="email-header">
                <h1>{{ app_name }}</h1>
            </div>
            <div class="email-body">
                <h2>{{ title }}</h2>
                <p>{{ content }}</p>
            </div>
            <div class="email-footer">
                <p>&copy; {{ app_name }}. All rights reserved.</p>
            </div>
        </div>
    </div>
</body>
</html>
"""

TESTS_CONFTEST_CONTENT = """from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator

import pytest

# Ensure testing environment
os.environ["APP_ENV"] = "testing"
os.environ["MAIL_MAILER"] = "fake"

from swiftpy.mail import Mail
from swiftpy.testing.http_client import TestClient

from main import app


@pytest.fixture
async def client() -> AsyncGenerator[TestClient, None]:
    \"\"\"Test client fixture for making requests against the application.\"\"\"
    async with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def fake_mail() -> Generator[object, None, None]:
    \"\"\"Automatically fake mail subsystem during test runs.\"\"\"
    with Mail.fake() as fake:
        yield fake
"""

TESTS_TEST_API_CONTENT = """from __future__ import annotations

import pytest
from swiftpy.testing.http_client import TestClient


@pytest.mark.asyncio
async def test_health_check_endpoint(client: TestClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
"""


def _get_docker_compose(project_name: str) -> str:
    """Return generated docker-compose.yml content scoped to `project_name`."""
    return f"name: {project_name}\n\n{DOCKER_COMPOSE_CONTENT}"


def _get_env_example(project_name: str) -> str:
    """Return .env.example content with `DB_DATABASE` set to `project_name`."""
    return ENV_EXAMPLE_CONTENT.replace(
        "\nDB_DATABASE=swiftpy\n",
        f"\nDB_DATABASE={project_name}\n",
        1,
    )


def _get_config_database(project_name: str) -> str:
    """Return config/database.py content defaulting `database` to `project_name`."""
    return CONFIG_DATABASE_CONTENT.replace(
        '"database": env("DB_DATABASE", "swiftpy"),',
        f'"database": env("DB_DATABASE", "{project_name}"),',
        1,
    )


def _get_pyproject_toml(project_name: str) -> str:
    return f"""[build-system]
requires = ["setuptools>=75.0"]
build-backend = "setuptools.build_meta"

[project]
name = "{project_name}"
version = "0.1.0"
description = "A SwiftPY web application"
requires-python = ">=3.12"
dependencies = [
    "swiftpy-framework>=0.3.0",
    "uvicorn[standard]>=0.52.4",
    "asyncpg>=0.30.0",
    "redis>=8.1.0",
]

[dependency-groups]
dev = [
    "pytest>=9.1.1",
    "pytest-asyncio>=1.4.0",
    "mypy>=2.3.1",
    "ruff>=0.16.4",
]

[tool.setuptools]
py-modules = ["main"]

[tool.setuptools.packages.find]
include = ["app*", "config*", "database*", "routes*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.mypy]
strict = true
explicit_package_bases = true
follow_imports = "silent"
ignore_missing_imports = true

[tool.ruff.lint.per-file-ignores]
"app/models/**/*.py" = ["RUF012"]
"""


def _get_readme(project_name: str) -> str:
    return f"""# {project_name}

A high-performance Python ASGI backend powered by the [SwiftPY](https://github.com/swiftpy/swiftpy) framework.

## Prerequisites

- Python >= 3.12
- PostgreSQL 16 (with `pgvector` extension if using AI vector stores)
- Redis 7

## Getting Started

1. Install dependencies using `uv` (recommended) or `pip`:
   ```bash
   uv sync
   source .venv/bin/activate
   ```
   Or with standard `venv` + `pip`:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e .
   ```

2. Environment configuration:
   The `.env` file has been automatically initialized with a secure `APP_KEY`.
   Update database and Redis credentials in `.env` if needed.

3. Run outstanding database migrations:
   ```bash
   swiftpy migrate
   ```

4. Create symbolic link for public storage files:
   ```bash
   swiftpy storage:link
   ```

5. Start the development server with live reload:
   ```bash
   swiftpy start --reload
   ```

## Development & CLI Commands

- `swiftpy make:model <Name> -m`: Create model and migration
- `swiftpy make:controller <Name>`: Create an HTTP controller
- `swiftpy make:resource <Name>`: Create an API resource serializer
- `swiftpy make:service <Name>`: Create a domain service
- `swiftpy make:repository <Name>`: Create a data repository
- `swiftpy make:schema <Name>`: Create an input/output schema
- `swiftpy make:agent <Name>`: Create an AI agent under `app/ai/agents`
- `swiftpy make:tool <Name>`: Create an AI function-calling tool under `app/ai/tools`
- `swiftpy make:prompt <name>`: Create a versioned prompt template under `prompts/`
- `swiftpy make:seeder <Name>`: Create a database seeder
- `swiftpy db:seed`: Seed the database
- `swiftpy queue:work`: Start background queue worker

## Docker Deployment

To spin up the entire application stack (App, PostgreSQL, and Redis):
```bash
docker compose up -d
```
"""


class NewCommand:
    """Console command for scaffolding fresh SwiftPY projects."""

    @staticmethod
    def register(
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    ) -> None:
        parser = subparsers.add_parser(
            "new",
            help="Create a fresh SwiftPY project scaffolding",
        )
        parser.add_argument(
            "name",
            help="The name and directory of the new project",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            default=False,
            help="Overwrite existing directory if not empty",
        )
        parser.set_defaults(
            _handler=NewCommand.handle,
        )

    @classmethod
    def handle(cls, args: argparse.Namespace) -> None:
        raw_name: str = args.name.strip()
        if (
            not raw_name
            or "/" in raw_name
            or "\\" in raw_name
            or ".." in raw_name
            or not _NAME_PATTERN.fullmatch(raw_name)
        ):
            raise SystemExit(
                f"Invalid project name {raw_name!r}. Must contain only alphanumeric characters, underscores, or dashes."
            )

        target_dir = Path.cwd() / raw_name
        if target_dir.is_symlink():
            raise SystemExit(
                f"Refusing to scaffold into symlinked project directory at {target_dir}"
            )
        resolved_base = Path.cwd().resolve()
        resolved_target = target_dir.resolve()

        if (
            not resolved_target.is_relative_to(resolved_base)
            or resolved_target == resolved_base
        ):
            raise SystemExit(
                f"Invalid project path {raw_name!r}: cannot escape base directory or overwrite root."
            )

        if (
            target_dir.exists()
            and any(target_dir.iterdir())
            and not getattr(args, "force", False)
        ):
            raise SystemExit(
                f"Directory '{target_dir}' already exists and is not empty. Use --force to overwrite."
            )

        target_dir.mkdir(parents=True, exist_ok=True)
        if target_dir.is_symlink() or target_dir.resolve() != resolved_target:
            raise SystemExit(
                f"Invalid project path {raw_name!r}: project root resolves through a symlink or escapes base directory."
            )

        # Generate cryptographically secure 32-byte APP_KEY encoded in base64
        raw_key = secrets.token_bytes(32)
        app_key = f"base64:{base64.b64encode(raw_key).decode('ascii')}"

        # Create directory layout
        dirs_to_create = [
            "app/ai/agents",
            "app/ai/observers",
            "app/ai/pipelines",
            "app/ai/providers",
            "app/ai/tools",
            "app/ai/vectorstores",
            "app/events",
            "app/http/controllers",
            "app/http/handler",
            "app/http/middleware",
            "app/http/requests",
            "app/http/resources",
            "app/jobs",
            "app/listeners",
            "app/mail",
            "app/models",
            "app/providers",
            "app/repositories",
            "app/schemas",
            "app/services",
            "config",
            "database/migrations",
            "database/seeders",
            "prompts",
            "public",
            "routes",
            "storage/app/private",
            "storage/app/public",
            "storage/framework",
            "storage/logs",
            "views/mail",
            "tests",
        ]

        def _validate_safe_path(candidate: Path) -> None:
            current = candidate
            while True:
                if current.is_symlink():
                    raise SystemExit(f"Refusing to traverse symlink at {current}")
                if current == target_dir:
                    break
                if current.parent == current:
                    break
                current = current.parent
            if not candidate.resolve().is_relative_to(resolved_target):
                raise SystemExit(
                    f"Destination path escapes project directory: {candidate}"
                )

        for d in dirs_to_create:
            dir_path = target_dir / d
            _validate_safe_path(dir_path)
            dir_path.mkdir(parents=True, exist_ok=True)
            _validate_safe_path(dir_path)

        # Generate active .env with project DB_DATABASE, APP_KEY, and JWT_SECRET
        env_example = _get_env_example(raw_name)
        jwt_secret = secrets.token_urlsafe(48)
        env_content = env_example.replace("APP_KEY=", f"APP_KEY={app_key}")
        env_content = env_content.replace("JWT_SECRET=", f"JWT_SECRET={jwt_secret}")

        files: dict[str, str] = {
            ".dockerignore": DOCKERIGNORE_CONTENT,
            ".env.example": env_example,
            ".env": env_content,
            ".gitignore": GITIGNORE_CONTENT,
            "docker-compose.yml": _get_docker_compose(raw_name),
            "Dockerfile": DOCKERFILE_CONTENT,
            "main.py": MAIN_PY_CONTENT,
            "pyproject.toml": _get_pyproject_toml(raw_name),
            "README.md": _get_readme(raw_name),
            "routes/__init__.py": "from __future__ import annotations\n",
            "routes/api.py": ROUTES_API_PY_CONTENT,
            "app/ai/agents/assistant_agent.py": ASSISTANT_AGENT_CONTENT,
            "app/ai/tools/system_status_tool.py": SYSTEM_STATUS_TOOL_CONTENT,
            "app/ai/pipelines/.gitkeep": "",
            "app/ai/providers/.gitkeep": "",
            "app/ai/vectorstores/.gitkeep": "",
            "app/ai/observers/.gitkeep": "",
            "app/models/user.py": USER_MODEL_CONTENT,
            "app/schemas/user_schema.py": USER_SCHEMA_CONTENT,
            "app/services/user_service.py": USER_SERVICE_CONTENT,
            "app/repositories/user_repository.py": USER_REPOSITORY_CONTENT,
            "app/events/user_registered.py": USER_REGISTERED_EVENT_CONTENT,
            "app/listeners/send_welcome_notification.py": SEND_WELCOME_NOTIFICATION_CONTENT,
            "app/jobs/send_notification_job.py": SEND_NOTIFICATION_JOB_CONTENT,
            "app/mail/welcome_email.py": WELCOME_EMAIL_CONTENT,
            "app/http/controllers/user_controller.py": USER_CONTROLLER_CONTENT,
            "app/http/handler/exceptions.py": EXCEPTIONS_CONTENT,
            "app/http/middleware/auth_middleware.py": AUTH_MIDDLEWARE_CONTENT,
            "app/http/middleware/admin_middleware.py": ADMIN_MIDDLEWARE_CONTENT,
            "app/http/requests/create_user_request.py": CREATE_USER_REQUEST_CONTENT,
            "app/http/resources/user_resource.py": USER_RESOURCE_CONTENT,
            "prompts/summarise.py": PROMPT_SUMMARISE_CONTENT,
            "prompts/classify.py": PROMPT_CLASSIFY_CONTENT,
            "app/providers/app_service_provider.py": APP_SERVICE_PROVIDER_CONTENT,
            "app/providers/auth_service_provider.py": AUTH_SERVICE_PROVIDER_CONTENT,
            "app/providers/cache_service_provider.py": CACHE_SERVICE_PROVIDER_CONTENT,
            "app/providers/database_service_provider.py": DATABASE_SERVICE_PROVIDER_CONTENT,
            "app/providers/event_service_provider.py": EVENT_SERVICE_PROVIDER_CONTENT,
            "app/providers/filesystem_service_provider.py": FILESYSTEM_SERVICE_PROVIDER_CONTENT,
            "app/providers/mail_service_provider.py": MAIL_SERVICE_PROVIDER_CONTENT,
            "app/providers/queue_service_provider.py": QUEUE_SERVICE_PROVIDER_CONTENT,
            "config/ai.py": CONFIG_AI_CONTENT,
            "config/app.py": CONFIG_APP_CONTENT,
            "config/auth.py": CONFIG_AUTH_CONTENT,
            "config/cache.py": CONFIG_CACHE_CONTENT,
            "config/cors.py": CONFIG_CORS_CONTENT,
            "config/database.py": _get_config_database(raw_name),
            "config/events.py": CONFIG_EVENTS_CONTENT,
            "config/filesystems.py": CONFIG_FILESYSTEMS_CONTENT,
            "config/logging.py": CONFIG_LOGGING_CONTENT,
            "config/mail.py": CONFIG_MAIL_CONTENT,
            "config/queue.py": CONFIG_QUEUE_CONTENT,
            "database/migrations/2026_01_01_000000_create_users_table.py": CREATE_USERS_MIGRATION_CONTENT,
            "database/seeders/database_seeder.py": DATABASE_SEEDER_CONTENT,
            "views/mail/template.html": VIEWS_MAIL_TEMPLATE_CONTENT,
            "tests/conftest.py": TESTS_CONFTEST_CONTENT,
            "tests/test_api.py": TESTS_TEST_API_CONTENT,
            "storage/app/private/.gitkeep": "",
            "storage/app/public/.gitkeep": "",
            "storage/framework/.gitkeep": "",
            "storage/logs/.gitkeep": "",
        }

        existing_env = (target_dir / ".env").exists()

        for rel_path, content in files.items():
            dest = target_dir / rel_path
            # Preserve existing .env and secret keys when overwriting via --force
            if rel_path == ".env" and existing_env:
                continue

            _validate_safe_path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            _validate_safe_path(dest)

            dest.write_text(content, encoding="utf-8")

        if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
            print(
                f"\033[32mSuccessfully created SwiftPY application:\033[0m {raw_name}"
            )
        else:
            print(f"Successfully created SwiftPY application: {raw_name}")
