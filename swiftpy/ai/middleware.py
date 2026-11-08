from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from swiftpy.ai.exceptions import (
    AIBudgetExceededError,
    AIContentModerationError,
    AIRateLimitError,
)
from swiftpy.ai.pricing import calculate_token_cost
from swiftpy.ai.providers.base import LLMRequest, LLMResponse
from swiftpy.core.config import Config
from swiftpy.core.context import Context
from swiftpy.support.redis_config import resolve_redis_params

logger = logging.getLogger("swiftpy.ai.middleware")

NextHandler = Callable[[LLMRequest], Awaitable[LLMResponse]]


class AIMiddleware(Protocol):
    """Protocol for AI execution pipeline middleware."""

    async def __call__(
        self,
        request: LLMRequest,
        next_call: NextHandler,
    ) -> LLMResponse:
        """Process an LLM request or intercept/modify response."""
        ...


def resolve_user_id(request: LLMRequest) -> str:
    """
    Extract caller/user identifier from LLMRequest extra or task Context.
    Defaults to 'anonymous' if no user context is present.
    """
    if "user_id" in request.extra:
        return str(request.extra["user_id"])
    if "user" in request.extra:
        u = request.extra["user"]
        if hasattr(u, "id"):
            return str(u.id)
        if isinstance(u, dict) and "id" in u:
            return str(u["id"])
        return str(u)

    for ctx_key in ("__swiftpy_current_user__", "auth.user", "user"):
        ctx_val = Context.get(ctx_key)
        if ctx_val is not None:
            if hasattr(ctx_val, "id"):
                return str(ctx_val.id)
            if isinstance(ctx_val, dict) and "id" in ctx_val:
                return str(ctx_val["id"])
            return str(ctx_val)

    return "anonymous"


def resolve_tenant_id(request: LLMRequest) -> str:
    """Extract tenant identifier from LLMRequest extra or task Context."""
    if "tenant_id" in request.extra:
        return str(request.extra["tenant_id"])
    if "tenant" in request.extra:
        return str(request.extra["tenant"])

    for ctx_key in ("tenant_id", "tenant", "auth.tenant_id"):
        ctx_val = Context.get(ctx_key)
        if ctx_val is not None:
            return str(ctx_val)

    return "default"


class AIMiddlewarePipeline:
    """
    Asynchronous middleware execution pipeline for LLM requests.
    Wraps provider invocations with registered pre/post processing layers.
    """

    def __init__(
        self,
        middlewares: Sequence[AIMiddleware | Callable[..., Any]] | None = None,
    ) -> None:
        self._middlewares: list[AIMiddleware | Callable[..., Any]] = list(
            middlewares or []
        )

    def use(self, middleware: AIMiddleware | Callable[..., Any]) -> AIMiddlewarePipeline:
        """Append a middleware to this pipeline."""
        self._middlewares.append(middleware)
        return self

    @property
    def middlewares(self) -> list[AIMiddleware | Callable[..., Any]]:
        """Return registered middlewares."""
        return list(self._middlewares)

    async def execute(
        self,
        request: LLMRequest,
        terminal_handler: NextHandler,
    ) -> LLMResponse:
        """
        Execute request through all registered middlewares in sequence.
        """
        if not self._middlewares:
            return await terminal_handler(request)

        handler = terminal_handler
        for mw in reversed(self._middlewares):
            handler = self._wrap(mw, handler)

        return await handler(request)

    def _wrap(
        self,
        mw: AIMiddleware | Callable[..., Any],
        next_fn: NextHandler,
    ) -> NextHandler:
        async def wrapped(req: LLMRequest) -> LLMResponse:
            return await mw(req, next_fn)

        return wrapped


_LUA_SLIDING_WINDOW_RATE_LIMIT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local max_limit = tonumber(ARGV[3])
local member = ARGV[4]
local clear_before = now - window

redis.call('ZREMRANGEBYSCORE', key, '-inf', clear_before)
local current_count = redis.call('ZCARD', key)

if current_count < max_limit then
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, math.ceil(window) + 10)
    return {1, max_limit - current_count - 1, 0}
else
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = window
    if oldest and #oldest >= 2 then
        local oldest_ts = tonumber(oldest[2])
        if oldest_ts then
            retry_after = (oldest_ts + window) - now
        end
    end
    if retry_after < 0.1 then retry_after = 0.1 end
    return {0, 0, retry_after}
end
"""


class AIRateLimitMiddleware:
    """
    Per-user / per-tenant request rate limiting middleware.
    Supports in-memory sliding window and distributed Redis backend with
    atomic Lua script evaluation, preventing multi-worker race conditions.
    """

    def __init__(
        self,
        requests_per_minute: int = 60,
        window_seconds: float = 60.0,
        redis_client: aioredis.Redis | None = None,
        storage: str = "auto",
        fail_closed: bool = False,
        key_prefix: str = "ai:ratelimit",
        config: Config | None = None,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be greater than 0.")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be greater than 0.")

        self.requests_per_minute = requests_per_minute
        self.window_seconds = window_seconds
        self.storage_mode = storage.lower().strip()
        self.fail_closed = fail_closed
        self.key_prefix = key_prefix
        self._redis_client = redis_client
        self._config = config

        self._user_timestamps: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def get_redis_client(self) -> aioredis.Redis | None:
        """Resolve Redis client instance if configured."""
        if self._redis_client is not None:
            return self._redis_client
        if self.storage_mode == "memory":
            return None

        cfg = self._config or Config.get_instance()
        app_env = str(cfg.get("app.env", os.environ.get("APP_ENV", "local"))).lower().strip()
        if (app_env in ("testing", "test") or "pytest" in sys.modules) and self.storage_mode == "auto":
            return None

        try:
            params = resolve_redis_params(cfg, section="ai")
            host = os.environ.get("REDIS_HOST") or params["host"]
            password = os.environ.get("REDIS_PASSWORD") or params["password"]
            conn_kwargs: dict[str, Any] = {
                "decode_responses": True,
                "socket_timeout": 3.0,
                "socket_connect_timeout": 3.0,
            }
            if password:
                conn_kwargs["password"] = password
            if params["ssl"]:
                conn_kwargs["ssl"] = True

            self._redis_client = aioredis.Redis(
                host=host,
                port=params["port"],
                db=params["database"],
                **conn_kwargs,
            )
            return self._redis_client
        except Exception as exc:
            if self.storage_mode == "redis":
                logger.error("Failed to connect to configured Redis: %s", exc)
                raise
            return None

    def reset(self) -> None:
        """Clear local in-memory tracked rate limit buckets."""
        self._user_timestamps.clear()

    async def reset_backend(self) -> None:
        """Clear rate limit records from both memory and Redis backend."""
        self.reset()
        client = await self.get_redis_client()
        if client is not None:
            try:
                keys = await client.keys(f"{self.key_prefix}:*")
                if keys:
                    await client.delete(*keys)
            except RedisError as exc:
                logger.warning("Failed to reset Redis rate limit keys: %s", exc)

    async def _check_redis(
        self,
        client: aioredis.Redis,
        tenant_id: str,
        user_id: str,
    ) -> None:
        key = f"{self.key_prefix}:{tenant_id}:{user_id}"
        now = time.time()
        nonce = f"{now}:{uuid.uuid4().hex[:8]}"

        res = await client.eval(
            _LUA_SLIDING_WINDOW_RATE_LIMIT,
            1,
            key,
            now,
            self.window_seconds,
            self.requests_per_minute,
            nonce,
        )

        if isinstance(res, (list, tuple)) and len(res) >= 3:
            allowed = bool(res[0])
            retry_after = float(res[2])
            if not allowed:
                raise AIRateLimitError(
                    f"AI rate limit exceeded for user '{user_id}'. "
                    f"Max {self.requests_per_minute} requests per {int(self.window_seconds)}s.",
                    retry_after=max(0.1, round(retry_after, 2)),
                )

    async def _check_memory(self, tenant_id: str, user_id: str) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        scope_key = (tenant_id, user_id)

        async with self._lock:
            timestamps = [t for t in self._user_timestamps[scope_key] if t > cutoff]
            if len(timestamps) >= self.requests_per_minute:
                oldest = timestamps[0]
                retry_after = round(self.window_seconds - (now - oldest), 2)
                raise AIRateLimitError(
                    f"AI rate limit exceeded for user '{user_id}'. "
                    f"Max {self.requests_per_minute} requests per {int(self.window_seconds)}s.",
                    retry_after=max(0.1, retry_after),
                )

            timestamps.append(now)
            self._user_timestamps[scope_key] = timestamps

    async def __call__(
        self,
        request: LLMRequest,
        next_call: NextHandler,
    ) -> LLMResponse:
        user_id = resolve_user_id(request)
        tenant_id = resolve_tenant_id(request)

        if self.storage_mode != "memory":
            redis_client = await self.get_redis_client()
            if redis_client is not None:
                try:
                    await self._check_redis(redis_client, tenant_id, user_id)
                    return await next_call(request)
                except AIRateLimitError:
                    raise
                except Exception as exc:
                    if self.storage_mode == "redis":
                        if self.fail_closed:
                            raise AIRateLimitError(
                                f"Rate limit storage error (fail-closed): {exc}"
                            ) from exc
                        logger.warning("Redis rate limit unavailable, failing open: %s", exc)
                        return await next_call(request)
                    logger.debug("Redis unavailable, falling back to memory rate limiting: %s", exc)

        await self._check_memory(tenant_id, user_id)
        return await next_call(request)


class TokenBudgetMiddleware:
    """
    Token and spend budget enforcement middleware.
    Supports atomic preflight reservation (hard limit) and post-reconciliation,
    with distributed Redis support and simultaneous global/per-user budgets.
    """

    def __init__(
        self,
        daily_token_limit: int | None = None,
        daily_cost_limit: float | None = None,
        global_daily_token_limit: int | None = None,
        global_daily_cost_limit: float | None = None,
        per_user: bool = True,
        mode: str = "soft",
        default_reservation_tokens: int = 250,
        redis_client: aioredis.Redis | None = None,
        storage: str = "auto",
        key_prefix: str = "ai:budget",
        config: Config | None = None,
    ) -> None:
        if daily_token_limit is not None and daily_token_limit <= 0:
            raise ValueError("daily_token_limit must be a positive integer.")
        if daily_cost_limit is not None and daily_cost_limit <= 0:
            raise ValueError("daily_cost_limit must be greater than 0.")
        if global_daily_token_limit is not None and global_daily_token_limit <= 0:
            raise ValueError("global_daily_token_limit must be a positive integer.")
        if global_daily_cost_limit is not None and global_daily_cost_limit <= 0:
            raise ValueError("global_daily_cost_limit must be greater than 0.")

        self.daily_token_limit = daily_token_limit
        self.daily_cost_limit = daily_cost_limit
        self.global_daily_token_limit = global_daily_token_limit
        self.global_daily_cost_limit = global_daily_cost_limit
        self.per_user = per_user
        self.mode = mode.lower().strip()
        self.default_reservation_tokens = default_reservation_tokens
        self.storage_mode = storage.lower().strip()
        self.key_prefix = key_prefix
        self._redis_client = redis_client
        self._config = config

        self._daily_usage: dict[tuple[str, str], dict[str, Any]] = defaultdict(
            lambda: {"tokens": 0, "cost": 0.0}
        )
        self._lock = asyncio.Lock()

    async def get_redis_client(self) -> aioredis.Redis | None:
        """Resolve Redis client instance if configured."""
        if self._redis_client is not None:
            return self._redis_client
        if self.storage_mode == "memory":
            return None

        cfg = self._config or Config.get_instance()
        app_env = str(cfg.get("app.env", os.environ.get("APP_ENV", "local"))).lower().strip()
        if (app_env in ("testing", "test") or "pytest" in sys.modules) and self.storage_mode == "auto":
            return None

        try:
            params = resolve_redis_params(cfg, section="ai")
            host = os.environ.get("REDIS_HOST") or params["host"]
            password = os.environ.get("REDIS_PASSWORD") or params["password"]
            conn_kwargs: dict[str, Any] = {
                "decode_responses": True,
                "socket_timeout": 3.0,
                "socket_connect_timeout": 3.0,
            }
            if password:
                conn_kwargs["password"] = password
            if params["ssl"]:
                conn_kwargs["ssl"] = True

            self._redis_client = aioredis.Redis(
                host=host,
                port=params["port"],
                db=params["database"],
                **conn_kwargs,
            )
            return self._redis_client
        except Exception as exc:
            if self.storage_mode == "redis":
                logger.error("Failed to connect to configured Redis for budget: %s", exc)
                raise
            return None

    def reset(self) -> None:
        """Clear local in-memory daily budget tracking counters."""
        self._daily_usage.clear()

    async def reset_backend(self) -> None:
        """Clear budget counters in memory and Redis."""
        self.reset()
        client = await self.get_redis_client()
        if client is not None:
            try:
                keys = await client.keys(f"{self.key_prefix}:*")
                if keys:
                    await client.delete(*keys)
            except RedisError as exc:
                logger.warning("Failed to reset Redis budget keys: %s", exc)

    def get_current_usage(self, user_id: str = "anonymous") -> dict[str, Any]:
        """Return today's accumulated tokens and cost in memory."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        scope_id = user_id if self.per_user else "__global__"
        return dict(self._daily_usage[(today, scope_id)])

    def _estimate_request_usage(
        self, request: LLMRequest
    ) -> tuple[int, float]:
        prompt_chars = sum(len(m.content or "") for m in request.messages)
        prompt_tokens = max(1, prompt_chars // 4)
        reserved_completion = (
            min(request.max_tokens, 2048)
            if request.max_tokens is not None
            else self.default_reservation_tokens
        )
        total_reserved = prompt_tokens + reserved_completion
        model = request.model or "unknown"
        estimated_cost = calculate_token_cost(model, prompt_tokens, reserved_completion)
        return total_reserved, estimated_cost

    async def __call__(
        self,
        request: LLMRequest,
        next_call: NextHandler,
    ) -> LLMResponse:
        user_id = resolve_user_id(request)
        today = datetime.now(UTC).strftime("%Y-%m-%d")

        user_key = (today, user_id)
        global_key = (today, "__global__")

        est_tokens, est_cost = self._estimate_request_usage(request)
        is_hard = self.mode == "hard"

        # 1. Preflight budget check & reservation
        async with self._lock:
            # Check user / primary scope
            if self.per_user:
                u_usage = self._daily_usage[user_key]
                check_tokens = u_usage["tokens"] + (est_tokens if is_hard else 0)
                check_cost = u_usage["cost"] + (est_cost if is_hard else 0.0)

                if (
                    self.daily_token_limit is not None
                    and check_tokens > self.daily_token_limit
                ):
                    raise AIBudgetExceededError(
                        f"Daily token budget of {self.daily_token_limit} exceeded for '{user_id}'. "
                        f"Current: {u_usage['tokens']}, requested/reserved: {est_tokens} tokens."
                    )
                if (
                    self.daily_cost_limit is not None
                    and check_cost > self.daily_cost_limit
                ):
                    raise AIBudgetExceededError(
                        f"Daily cost budget of ${self.daily_cost_limit:.4f} exceeded for '{user_id}'. "
                        f"Current: ${u_usage['cost']:.4f}, estimated: ${est_cost:.4f}."
                    )
            else:
                g_usage = self._daily_usage[global_key]
                check_tokens = g_usage["tokens"] + (est_tokens if is_hard else 0)
                check_cost = g_usage["cost"] + (est_cost if is_hard else 0.0)

                if (
                    self.daily_token_limit is not None
                    and check_tokens > self.daily_token_limit
                ):
                    raise AIBudgetExceededError(
                        f"Daily token budget of {self.daily_token_limit} exceeded. "
                        f"Current: {g_usage['tokens']}, requested/reserved: {est_tokens} tokens."
                    )
                if (
                    self.daily_cost_limit is not None
                    and check_cost > self.daily_cost_limit
                ):
                    raise AIBudgetExceededError(
                        f"Daily cost budget of ${self.daily_cost_limit:.4f} exceeded. "
                        f"Current: ${g_usage['cost']:.4f}, estimated: ${est_cost:.4f}."
                    )

            # Check explicit global limits if set
            if (
                self.global_daily_token_limit is not None
                or self.global_daily_cost_limit is not None
            ):
                g_usage = self._daily_usage[global_key]
                g_check_tokens = g_usage["tokens"] + (est_tokens if is_hard else 0)
                g_check_cost = g_usage["cost"] + (est_cost if is_hard else 0.0)

                if (
                    self.global_daily_token_limit is not None
                    and g_check_tokens > self.global_daily_token_limit
                ):
                    raise AIBudgetExceededError(
                        f"Global daily token budget of {self.global_daily_token_limit} exceeded. "
                        f"Current: {g_usage['tokens']}, requested/reserved: {est_tokens} tokens."
                    )
                if (
                    self.global_daily_cost_limit is not None
                    and g_check_cost > self.global_daily_cost_limit
                ):
                    raise AIBudgetExceededError(
                        f"Global daily cost budget of ${self.global_daily_cost_limit:.4f} exceeded. "
                        f"Current: ${g_usage['cost']:.4f}, estimated: ${est_cost:.4f}."
                    )

            # In hard mode: apply reservation upfront
            if is_hard:
                if self.per_user:
                    self._daily_usage[user_key]["tokens"] += est_tokens
                    self._daily_usage[user_key]["cost"] = round(
                        self._daily_usage[user_key]["cost"] + est_cost, 6
                    )
                self._daily_usage[global_key]["tokens"] += est_tokens
                self._daily_usage[global_key]["cost"] = round(
                    self._daily_usage[global_key]["cost"] + est_cost, 6
                )

        # 2. Invoke provider terminal handler with rollback on failure
        try:
            response = await next_call(request)
        except Exception:
            # Rollback reservation if provider fails
            if is_hard:
                async with self._lock:
                    if self.per_user:
                        self._daily_usage[user_key]["tokens"] = max(
                            0, self._daily_usage[user_key]["tokens"] - est_tokens
                        )
                        self._daily_usage[user_key]["cost"] = max(
                            0.0,
                            round(self._daily_usage[user_key]["cost"] - est_cost, 6),
                        )
                    self._daily_usage[global_key]["tokens"] = max(
                        0, self._daily_usage[global_key]["tokens"] - est_tokens
                    )
                    self._daily_usage[global_key]["cost"] = max(
                        0.0,
                        round(self._daily_usage[global_key]["cost"] - est_cost, 6),
                    )
            raise

        # 3. Reconcile reserved vs actual usage
        if response.usage is not None:
            p_tokens = response.usage.prompt_tokens
            c_tokens = response.usage.completion_tokens
            act_tokens = response.usage.total_tokens
        else:
            p_chars = sum(len(m.content or "") for m in request.messages)
            p_tokens = max(1, p_chars // 4)
            c_chars = len(response.content or "")
            c_tokens = max(1, c_chars // 4)
            act_tokens = p_tokens + c_tokens

        act_cost = calculate_token_cost(
            response.model or request.model or "unknown",
            p_tokens,
            c_tokens,
        )

        async with self._lock:
            if is_hard:
                # Reconcile difference
                diff_tokens = act_tokens - est_tokens
                diff_cost = round(act_cost - est_cost, 6)

                if self.per_user:
                    self._daily_usage[user_key]["tokens"] = max(
                        0, self._daily_usage[user_key]["tokens"] + diff_tokens
                    )
                    self._daily_usage[user_key]["cost"] = max(
                        0.0, round(self._daily_usage[user_key]["cost"] + diff_cost, 6)
                    )
                self._daily_usage[global_key]["tokens"] = max(
                    0, self._daily_usage[global_key]["tokens"] + diff_tokens
                )
                self._daily_usage[global_key]["cost"] = max(
                    0.0, round(self._daily_usage[global_key]["cost"] + diff_cost, 6)
                )
            else:
                if self.per_user:
                    self._daily_usage[user_key]["tokens"] += act_tokens
                    self._daily_usage[user_key]["cost"] = round(
                        self._daily_usage[user_key]["cost"] + act_cost, 6
                    )
                self._daily_usage[global_key]["tokens"] += act_tokens
                self._daily_usage[global_key]["cost"] = round(
                    self._daily_usage[global_key]["cost"] + act_cost, 6
                )

        return response


ModerationChecker = Callable[[str], Awaitable[bool | tuple[bool, str]]]


class ContentModerationMiddleware:
    """
    Content moderation middleware executing policy checks BEFORE the LLM call.
    Inspects messages for banned patterns or delegates to a moderation checker.
    Raises AIContentModerationError without contacting the LLM if flagged.
    """

    def __init__(
        self,
        checker: ModerationChecker | None = None,
        banned_words: Sequence[str] | None = None,
        patterns: Sequence[re.Pattern[str]] | None = None,
        fail_closed: bool = True,
        check_all_roles: bool = True,
    ) -> None:
        self.checker = checker
        self.banned_words = [w.lower().strip() for w in (banned_words or []) if w.strip()]
        self.patterns = list(patterns or [])
        self.fail_closed = fail_closed
        self.check_all_roles = check_all_roles

    async def check_content(self, text: str) -> tuple[bool, str | None]:
        """
        Evaluate text against configured banned words, patterns, and custom checker.
        Returns (is_violating, reason).
        """
        lower = text.lower()
        for word in self.banned_words:
            if word in lower:
                return True, f"Contains banned keyword: '{word}'"

        for pat in self.patterns:
            if pat.search(text):
                return True, f"Matched prohibited pattern: '{pat.pattern}'"

        if self.checker is not None:
            try:
                res = await self.checker(text)
                if isinstance(res, tuple):
                    flagged, reason = res
                    if flagged:
                        return True, reason
                elif bool(res):
                    return True, "Flagged by content moderation policy"
            except Exception as exc:
                if self.fail_closed:
                    raise AIContentModerationError(
                        f"Content moderation service unavailable: {exc}"
                    ) from exc
                logger.warning(
                    "Content moderation checker failed, failing open: %s", exc
                )

        return False, None

    async def __call__(
        self,
        request: LLMRequest,
        next_call: NextHandler,
    ) -> LLMResponse:
        for msg in request.messages:
            if not self.check_all_roles and msg.role != "user":
                continue
            content = msg.content or ""
            is_violating, reason = await self.check_content(content)
            if is_violating:
                raise AIContentModerationError(
                    f"Content moderation violation on message role '{msg.role}': {reason}"
                )

        return await next_call(request)
