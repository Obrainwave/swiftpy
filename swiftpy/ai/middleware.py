from __future__ import annotations

import asyncio
import logging
import re
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
    AIConnectionError,
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

_BUDGET_KEY_TTL_SECONDS: int = 172_800
_DEFAULT_MAX_TRACKED_KEYS: int = 10_000


def _extract_user_value(val: Any) -> str:
    if hasattr(val, "id"):
        return str(val.id)
    if isinstance(val, dict) and "id" in val:
        return str(val["id"])
    return str(val)


async def _build_ai_redis_client(
    config: Config | None,
    storage_mode: str,
) -> aioredis.Redis | None:
    """Resolve an async Redis client for AI middleware unless in memory or auto-testing mode."""
    if storage_mode == "memory":
        return None
    cfg = config or Config.get_instance()
    app_env = str(cfg.get("app.env", "local")).lower().strip()
    if app_env in ("testing", "test") and storage_mode == "auto":
        return None

    params = resolve_redis_params(cfg, section="ai")
    return aioredis.from_url(
        params["url"],
        decode_responses=True,
        socket_timeout=3.0,
        socket_connect_timeout=3.0,
    )


async def _scan_and_delete(client: aioredis.Redis, pattern: str) -> None:
    """Non-blocking key cleanup using Redis SCAN cursor iteration."""
    batch: list[str] = []
    async for key in client.scan_iter(match=pattern, count=200):
        batch.append(str(key))
        if len(batch) >= 200:
            await client.delete(*batch)
            batch.clear()
    if batch:
        await client.delete(*batch)


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
    Extract caller/user identifier from LLMRequest metadata, extra, or task Context.
    Defaults to 'anonymous' if no user context is present.
    """
    for mapping in (request.metadata, request.extra):
        if "user_id" in mapping:
            return str(mapping["user_id"])
        if "user" in mapping:
            return _extract_user_value(mapping["user"])

    for ctx_key in ("__swiftpy_current_user__", "auth.user", "user"):
        ctx_val = Context.get(ctx_key)
        if ctx_val is not None:
            return _extract_user_value(ctx_val)

    return "anonymous"


def resolve_tenant_id(request: LLMRequest) -> str:
    """Extract tenant identifier from LLMRequest metadata, extra, or task Context."""
    for mapping in (request.metadata, request.extra):
        if "tenant_id" in mapping:
            return str(mapping["tenant_id"])
        if "tenant" in mapping:
            return str(mapping["tenant"])

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
        max_tracked_keys: int = _DEFAULT_MAX_TRACKED_KEYS,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be greater than 0.")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be greater than 0.")
        if max_tracked_keys <= 0:
            raise ValueError("max_tracked_keys must be greater than 0.")

        self.requests_per_minute = requests_per_minute
        self.window_seconds = window_seconds
        self.storage_mode = storage.lower().strip()
        self.fail_closed = fail_closed
        self.key_prefix = key_prefix
        self._redis_client = redis_client
        self._config = config
        self._max_tracked_keys = max_tracked_keys

        self._user_timestamps: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def get_redis_client(self) -> aioredis.Redis | None:
        """Resolve Redis client instance if configured."""
        if self._redis_client is not None:
            return self._redis_client
        try:
            self._redis_client = await _build_ai_redis_client(self._config, self.storage_mode)
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
                await _scan_and_delete(client, f"{self.key_prefix}:*")
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
            existing = self._user_timestamps.get(scope_key, [])
            timestamps = [t for t in existing if t > cutoff]
            if len(timestamps) >= self.requests_per_minute:
                oldest = timestamps[0]
                self._user_timestamps[scope_key] = timestamps
                retry_after = round(self.window_seconds - (now - oldest), 2)
                raise AIRateLimitError(
                    f"AI rate limit exceeded for user '{user_id}'. "
                    f"Max {self.requests_per_minute} requests per {int(self.window_seconds)}s.",
                    retry_after=max(0.1, retry_after),
                )

            if scope_key not in self._user_timestamps and len(self._user_timestamps) >= self._max_tracked_keys:
                # Prune empty/expired buckets first before evicting oldest key
                expired_keys = [
                    k for k, vals in self._user_timestamps.items()
                    if not vals or vals[-1] <= cutoff
                ]
                for ek in expired_keys:
                    del self._user_timestamps[ek]
                if len(self._user_timestamps) >= self._max_tracked_keys:
                    oldest_key = next(iter(self._user_timestamps))
                    del self._user_timestamps[oldest_key]

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


_LUA_BUDGET_CHECK_AND_RESERVE = """
local limit1_t = tonumber(ARGV[1])
local limit1_c = tonumber(ARGV[2])
local limit2_t = tonumber(ARGV[3])
local limit2_c = tonumber(ARGV[4])
local res_t = tonumber(ARGV[5])
local res_c = tonumber(ARGV[6])
local is_hard = tonumber(ARGV[7])
local ttl_sec = tonumber(ARGV[8])

local cur1_t = tonumber(redis.call('HGET', KEYS[1], 'tokens') or '0')
local cur1_c = tonumber(redis.call('HGET', KEYS[1], 'cost') or '0')
local chk1_t = cur1_t + (is_hard == 1 and res_t or 0)
local chk1_c = cur1_c + (is_hard == 1 and res_c or 0.0)

if limit1_t >= 0 and chk1_t > limit1_t then
    return {0, 1, 'tokens', tostring(cur1_t)}
end
if limit1_c >= 0 and chk1_c > (limit1_c + 1e-9) then
    return {0, 1, 'cost', tostring(cur1_c)}
end

if #KEYS >= 2 then
    local cur2_t = tonumber(redis.call('HGET', KEYS[2], 'tokens') or '0')
    local cur2_c = tonumber(redis.call('HGET', KEYS[2], 'cost') or '0')
    local chk2_t = cur2_t + (is_hard == 1 and res_t or 0)
    local chk2_c = cur2_c + (is_hard == 1 and res_c or 0.0)
    if limit2_t >= 0 and chk2_t > limit2_t then
        return {0, 2, 'tokens', tostring(cur2_t)}
    end
    if limit2_c >= 0 and chk2_c > (limit2_c + 1e-9) then
        return {0, 2, 'cost', tostring(cur2_c)}
    end
end

if is_hard == 1 then
    for i = 1, #KEYS do
        local nt = tonumber(redis.call('HGET', KEYS[i], 'tokens') or '0') + res_t
        local nc = tonumber(redis.call('HGET', KEYS[i], 'cost') or '0') + res_c
        redis.call('HSET', KEYS[i], 'tokens', nt, 'cost', string.format('%.6f', nc))
        redis.call('EXPIRE', KEYS[i], ttl_sec)
    end
end

return {1, 0, 'ok', '0'}
"""

_LUA_BUDGET_ADJUST = """
local delta_t = tonumber(ARGV[1])
local delta_c = tonumber(ARGV[2])
local ttl_sec = tonumber(ARGV[3])

for i = 1, #KEYS do
    local nt = tonumber(redis.call('HGET', KEYS[i], 'tokens') or '0') + delta_t
    if nt < 0 then nt = 0 end
    local nc = tonumber(redis.call('HGET', KEYS[i], 'cost') or '0') + delta_c
    if nc < 0 then nc = 0.0 end
    redis.call('HSET', KEYS[i], 'tokens', nt, 'cost', string.format('%.6f', nc))
    redis.call('EXPIRE', KEYS[i], ttl_sec)
end
return 1
"""


class TokenBudgetMiddleware:
    """
    Token and spend budget enforcement middleware.
    Supports atomic preflight reservation (hard limit) and post-reconciliation,
    with distributed Redis support and simultaneous global/per-user/per-tenant budgets.
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
        max_tracked_keys: int = _DEFAULT_MAX_TRACKED_KEYS,
    ) -> None:
        if daily_token_limit is not None and daily_token_limit <= 0:
            raise ValueError("daily_token_limit must be a positive integer.")
        if daily_cost_limit is not None and daily_cost_limit <= 0:
            raise ValueError("daily_cost_limit must be greater than 0.")
        if global_daily_token_limit is not None and global_daily_token_limit <= 0:
            raise ValueError("global_daily_token_limit must be a positive integer.")
        if global_daily_cost_limit is not None and global_daily_cost_limit <= 0:
            raise ValueError("global_daily_cost_limit must be greater than 0.")
        if max_tracked_keys <= 0:
            raise ValueError("max_tracked_keys must be greater than 0.")

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
        self._max_tracked_keys = max_tracked_keys

        # Keys: (date_str, tenant_id, user_id) or ("__global__", "__global__", date_str)
        self._daily_usage: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
            lambda: {"tokens": 0, "cost": 0.0}
        )
        self._lock = asyncio.Lock()

    async def get_redis_client(self) -> aioredis.Redis | None:
        """Resolve Redis client instance if configured."""
        if self._redis_client is not None:
            return self._redis_client
        try:
            self._redis_client = await _build_ai_redis_client(self._config, self.storage_mode)
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
                await _scan_and_delete(client, f"{self.key_prefix}:*")
            except RedisError as exc:
                logger.warning("Failed to reset Redis budget keys: %s", exc)

    def get_current_usage(self, user_id: str = "anonymous", tenant_id: str | None = None) -> dict[str, Any]:
        """Return today's accumulated tokens and cost in memory."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if self.per_user:
            eff_tenant = tenant_id if tenant_id is not None else resolve_tenant_id(LLMRequest(messages=[]))
            return dict(self._daily_usage[(today, eff_tenant, user_id)])
        return dict(self._daily_usage[("__global__", "__global__", today)])

    def _estimate_request_usage(self, request: LLMRequest) -> tuple[int, float]:
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

    def _compute_actual_usage(self, request: LLMRequest, response: LLMResponse) -> tuple[int, float]:
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
        return act_tokens, act_cost

    def _prune_memory_buckets(self, today: str) -> None:
        stale_keys = [
            k for k in self._daily_usage
            if (k[0] == "__global__" and k[2] != today) or (k[0] != "__global__" and k[0] != today)
        ]
        for k in stale_keys:
            del self._daily_usage[k]
        while len(self._daily_usage) >= self._max_tracked_keys:
            oldest_key = next(iter(self._daily_usage))
            del self._daily_usage[oldest_key]

    def _memory_keys_to_update(
        self,
        today: str,
        tenant_id: str,
        user_id: str,
    ) -> list[tuple[str, str, str]]:
        global_key = ("__global__", "__global__", today)
        has_global = (
            self.global_daily_token_limit is not None
            or self.global_daily_cost_limit is not None
        )
        if not self.per_user:
            return [global_key]
        keys: list[tuple[str, str, str]] = [(today, tenant_id, user_id)]
        if has_global:
            keys.append(global_key)
        return keys

    def _redis_keys_to_update(
        self,
        today: str,
        tenant_id: str,
        user_id: str,
    ) -> list[str]:
        global_key = f"{self.key_prefix}:{today}:global"
        has_global = (
            self.global_daily_token_limit is not None
            or self.global_daily_cost_limit is not None
        )
        if not self.per_user:
            return [global_key]
        keys = [f"{self.key_prefix}:{today}:u:{tenant_id}:{user_id}"]
        if has_global:
            keys.append(global_key)
        return keys

    def _apply_memory_delta(
        self,
        keys: Sequence[tuple[str, str, str]],
        delta_tokens: int,
        delta_cost: float,
    ) -> None:
        for key in keys:
            bucket = self._daily_usage[key]
            bucket["tokens"] = max(0, int(bucket["tokens"]) + delta_tokens)
            bucket["cost"] = max(0.0, round(float(bucket["cost"]) + delta_cost, 6))

    async def _run_memory_budget(
        self,
        request: LLMRequest,
        next_call: NextHandler,
        tenant_id: str,
        user_id: str,
        today: str,
        est_tokens: int,
        est_cost: float,
    ) -> LLMResponse:
        is_hard = self.mode == "hard"
        user_key = (today, tenant_id, user_id)
        global_key = ("__global__", "__global__", today)
        update_keys = self._memory_keys_to_update(today, tenant_id, user_id)

        async with self._lock:
            self._prune_memory_buckets(today)
            if self.per_user:
                u_usage = self._daily_usage[user_key]
                check_tokens = u_usage["tokens"] + (est_tokens if is_hard else 0)
                check_cost = u_usage["cost"] + (est_cost if is_hard else 0.0)
                if self.daily_token_limit is not None and check_tokens > self.daily_token_limit:
                    raise AIBudgetExceededError(
                        f"Daily token budget of {self.daily_token_limit} exceeded for '{user_id}'. "
                        f"Current: {u_usage['tokens']}, requested/reserved: {est_tokens} tokens."
                    )
                if self.daily_cost_limit is not None and check_cost > self.daily_cost_limit:
                    raise AIBudgetExceededError(
                        f"Daily cost budget of ${self.daily_cost_limit:.4f} exceeded for '{user_id}'. "
                        f"Current: ${u_usage['cost']:.4f}, estimated: ${est_cost:.4f}."
                    )
            else:
                g_usage = self._daily_usage[global_key]
                check_tokens = g_usage["tokens"] + (est_tokens if is_hard else 0)
                check_cost = g_usage["cost"] + (est_cost if is_hard else 0.0)
                if self.daily_token_limit is not None and check_tokens > self.daily_token_limit:
                    raise AIBudgetExceededError(
                        f"Daily token budget of {self.daily_token_limit} exceeded. "
                        f"Current: {g_usage['tokens']}, requested/reserved: {est_tokens} tokens."
                    )
                if self.daily_cost_limit is not None and check_cost > self.daily_cost_limit:
                    raise AIBudgetExceededError(
                        f"Daily cost budget of ${self.daily_cost_limit:.4f} exceeded. "
                        f"Current: ${g_usage['cost']:.4f}, estimated: ${est_cost:.4f}."
                    )

            if self.global_daily_token_limit is not None or self.global_daily_cost_limit is not None:
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

            if is_hard:
                self._apply_memory_delta(update_keys, est_tokens, est_cost)

        try:
            response = await next_call(request)
        except Exception:
            if is_hard:
                async with self._lock:
                    self._apply_memory_delta(update_keys, -est_tokens, -est_cost)
            raise

        act_tokens, act_cost = self._compute_actual_usage(request, response)
        async with self._lock:
            if is_hard:
                self._apply_memory_delta(
                    update_keys,
                    act_tokens - est_tokens,
                    round(act_cost - est_cost, 6),
                )
            else:
                self._apply_memory_delta(update_keys, act_tokens, act_cost)
        return response

    async def _run_redis_budget(
        self,
        client: aioredis.Redis,
        request: LLMRequest,
        next_call: NextHandler,
        tenant_id: str,
        user_id: str,
        today: str,
        est_tokens: int,
        est_cost: float,
    ) -> LLMResponse:
        is_hard = self.mode == "hard"
        keys = self._redis_keys_to_update(today, tenant_id, user_id)

        if self.per_user:
            l1_t = self.daily_token_limit if self.daily_token_limit is not None else -1
            l1_c = self.daily_cost_limit if self.daily_cost_limit is not None else -1.0
            l2_t = self.global_daily_token_limit if self.global_daily_token_limit is not None else -1
            l2_c = self.global_daily_cost_limit if self.global_daily_cost_limit is not None else -1.0
        else:
            tok_bounds = [v for v in (self.daily_token_limit, self.global_daily_token_limit) if v is not None]
            cost_bounds = [v for v in (self.daily_cost_limit, self.global_daily_cost_limit) if v is not None]
            l1_t = min(tok_bounds) if tok_bounds else -1
            l1_c = min(cost_bounds) if cost_bounds else -1.0
            l2_t = -1
            l2_c = -1.0

        res = await client.eval(
            _LUA_BUDGET_CHECK_AND_RESERVE,
            len(keys),
            *keys,
            l1_t,
            l1_c,
            l2_t,
            l2_c,
            est_tokens,
            est_cost,
            1 if is_hard else 0,
            _BUDGET_KEY_TTL_SECONDS,
        )
        if isinstance(res, (list, tuple)) and len(res) >= 4 and int(res[0]) == 0:
            scope_idx = int(res[1])
            metric = str(res[2])
            cur_val = str(res[3])
            is_global_scope = (scope_idx == 2) or (not self.per_user and self.global_daily_token_limit is not None and metric == "tokens" and (self.daily_token_limit is None or self.global_daily_token_limit <= self.daily_token_limit))
            if metric == "tokens":
                if is_global_scope:
                    raise AIBudgetExceededError(
                        f"Global daily token budget of {self.global_daily_token_limit} exceeded. "
                        f"Current: {cur_val}, requested/reserved: {est_tokens} tokens."
                    )
                target_limit = self.daily_token_limit
                suffix = f" for '{user_id}'" if self.per_user else ""
                raise AIBudgetExceededError(
                    f"Daily token budget of {target_limit} exceeded{suffix}. "
                    f"Current: {cur_val}, requested/reserved: {est_tokens} tokens."
                )
            if is_global_scope:
                raise AIBudgetExceededError(
                    f"Global daily cost budget of ${self.global_daily_cost_limit or 0:.4f} exceeded. "
                    f"Current: ${float(cur_val):.4f}, estimated: ${est_cost:.4f}."
                )
            suffix = f" for '{user_id}'" if self.per_user else ""
            raise AIBudgetExceededError(
                f"Daily cost budget of ${self.daily_cost_limit or 0:.4f} exceeded{suffix}. "
                f"Current: ${float(cur_val):.4f}, estimated: ${est_cost:.4f}."
            )

        try:
            response = await next_call(request)
        except Exception:
            if is_hard:
                try:
                    await client.eval(
                        _LUA_BUDGET_ADJUST,
                        len(keys),
                        *keys,
                        -est_tokens,
                        -est_cost,
                        _BUDGET_KEY_TTL_SECONDS,
                    )
                except RedisError as rb_exc:
                    logger.warning("Failed Redis budget rollback: %s", rb_exc)
            raise

        act_tokens, act_cost = self._compute_actual_usage(request, response)
        delta_t = (act_tokens - est_tokens) if is_hard else act_tokens
        delta_c = round(act_cost - est_cost, 6) if is_hard else act_cost
        try:
            await client.eval(
                _LUA_BUDGET_ADJUST,
                len(keys),
                *keys,
                delta_t,
                delta_c,
                _BUDGET_KEY_TTL_SECONDS,
            )
        except RedisError as adj_exc:
            if self.storage_mode == "redis":
                raise AIConnectionError(
                    f"Failed to record budget usage in Redis: {adj_exc}",
                    provider="redis",
                ) from adj_exc
            logger.warning("Failed Redis budget reconciliation: %s", adj_exc)
        return response

    async def __call__(
        self,
        request: LLMRequest,
        next_call: NextHandler,
    ) -> LLMResponse:
        user_id = resolve_user_id(request)
        tenant_id = resolve_tenant_id(request)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        est_tokens, est_cost = self._estimate_request_usage(request)

        if self.storage_mode != "memory":
            redis_client = await self.get_redis_client()
            if redis_client is not None:
                try:
                    return await self._run_redis_budget(
                        redis_client,
                        request,
                        next_call,
                        tenant_id,
                        user_id,
                        today,
                        est_tokens,
                        est_cost,
                    )
                except (AIBudgetExceededError, AIConnectionError):
                    raise
                except Exception as exc:
                    if self.storage_mode == "redis":
                        raise AIConnectionError(
                            f"Budget Redis error: {exc}",
                            provider="redis",
                        ) from exc
                    logger.debug("Redis unavailable, falling back to memory budget: %s", exc)

        return await self._run_memory_budget(
            request,
            next_call,
            tenant_id,
            user_id,
            today,
            est_tokens,
            est_cost,
        )


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
