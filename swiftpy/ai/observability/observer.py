from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from swiftpy.ai.observability.drivers import (
    AIObservation,
    BaseObservabilityDriver,
    DatabaseObservabilityDriver,
    FileObservabilityDriver,
    InMemoryObservabilityDriver,
)
from swiftpy.ai.pricing import calculate_token_cost
from swiftpy.ai.providers.base import LLMResponse, Message
from swiftpy.core.config import Config

logger = logging.getLogger("swiftpy.ai.observability")


def compute_prompt_hash(messages: Sequence[Message | dict[str, Any]] | str) -> str:
    """
    Compute a deterministic SHA-256 hash for a prompt or list of messages.
    """
    if isinstance(messages, str):
        content_to_hash = messages
    else:
        parts: list[str] = []
        for m in messages:
            if isinstance(m, Message):
                parts.append(f"{m.role}:{m.content or ''}")
            elif isinstance(m, dict):
                role = str(m.get("role", ""))
                content = str(m.get("content", ""))
                parts.append(f"{role}:{content}")
            else:
                parts.append(str(m))
        content_to_hash = "\n".join(parts)

    return hashlib.sha256(content_to_hash.encode("utf-8")).hexdigest()


class ObservationContext:
    """Helper mutable context for tracking LLM invocation telemetry."""

    def __init__(
        self,
        model: str,
        provider: str,
        prompt_hash: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.provider = provider
        self.prompt_hash = prompt_hash
        self.metadata = dict(metadata or {})
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.cost: float | None = None
        self.status = "success"
        self.error_message: str | None = None
        self.ttft_ms: float | None = None
        self.duration_ms: float | None = None

    def record_first_token(self, start_time: float) -> None:
        """Record time-to-first-token in milliseconds."""
        if self.ttft_ms is None:
            self.ttft_ms = (time.perf_counter() - start_time) * 1000.0

    def set_response(self, response: LLMResponse) -> None:
        """Extract usage and model details from completed LLM response."""
        if response.usage is not None:
            self.input_tokens = response.usage.prompt_tokens
            self.output_tokens = response.usage.completion_tokens
            self.total_tokens = response.usage.total_tokens
        if response.model:
            self.model = response.model

    def set_tokens(self, input_tokens: int, output_tokens: int) -> None:
        """Directly set token metrics."""
        self.input_tokens = max(0, input_tokens)
        self.output_tokens = max(0, output_tokens)
        self.total_tokens = self.input_tokens + self.output_tokens


class AIObservabilityManager:
    """
    Observability orchestrator for the SwiftPY AI module.
    Hooks into all LLM calls, records model, provider, tokens, latency,
    estimated cost, TTFT, and prompt hash across active storage drivers.
    """

    _instance: AIObservabilityManager | None = None

    def __init__(
        self,
        drivers: Sequence[BaseObservabilityDriver] | None = None,
        config: Config | None = None,
        fail_closed: bool = False,
    ) -> None:
        self.fail_closed = fail_closed
        if config is None:
            try:
                self._config = Config.get_instance()
            except RuntimeError:
                self._config = Config()
        else:
            self._config = config

        self._drivers: list[BaseObservabilityDriver] = (
            list(drivers) if drivers is not None else []
        )
        if not self._drivers:
            self._init_default_drivers()

    def _init_default_drivers(self) -> None:
        """Initialize driver(s) based on configuration."""
        driver_cfg = str(self._config.get("ai.observability.driver", "")).lower().strip()
        if not driver_cfg:
            app_env = str(
                self._config.get("app.env", os.environ.get("APP_ENV", "local"))
            ).lower().strip()
            if app_env in ("testing", "test") or "pytest" in sys.modules:
                driver_cfg = "memory"
            else:
                driver_cfg = "database"

        if driver_cfg == "file":
            log_path = str(self._config.get("ai.observability.file_path", "storage/logs/ai.log"))
            self._drivers.append(FileObservabilityDriver(log_path=log_path))
        elif driver_cfg in ("memory", "in_memory"):
            self._drivers.append(InMemoryObservabilityDriver())
        elif driver_cfg == "all":
            self._drivers.append(InMemoryObservabilityDriver())
            self._drivers.append(FileObservabilityDriver())
            self._drivers.append(DatabaseObservabilityDriver())
        else:
            # Default to database driver
            self._drivers.append(DatabaseObservabilityDriver())

    @classmethod
    def get_instance(cls) -> AIObservabilityManager:
        """Return the application-wide singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def set_instance(cls, instance: AIObservabilityManager | None) -> None:
        """Override application singleton instance (useful in tests)."""
        cls._instance = instance

    @property
    def drivers(self) -> list[BaseObservabilityDriver]:
        """Return a copy of currently registered drivers."""
        return list(self._drivers)

    def add_driver(self, driver: BaseObservabilityDriver) -> None:
        """Register an additional observability driver."""
        self._drivers.append(driver)

    def set_driver(self, driver: BaseObservabilityDriver) -> None:
        """Replace all drivers with a single driver."""
        self._drivers = [driver]

    def clear_drivers(self) -> None:
        """Remove all registered drivers."""
        self._drivers.clear()

    async def record(
        self,
        model: str,
        provider: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
        prompt_hash: str,
        cost: float | None = None,
        status: str = "success",
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
        ttft_ms: float | None = None,
        duration_ms: float | None = None,
    ) -> AIObservation:
        """
        Record a completed or failed LLM operation to all active drivers.
        """
        tot_tokens = input_tokens + output_tokens
        if cost is None:
            calculated_cost = calculate_token_cost(model, input_tokens, output_tokens)
        else:
            calculated_cost = cost

        observation = AIObservation(
            id=uuid.uuid4().hex,
            timestamp=timestamp or datetime.now(UTC),
            model=model,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=tot_tokens,
            latency_ms=round(latency_ms, 2),
            cost=calculated_cost,
            prompt_hash=prompt_hash,
            status=status,
            error_message=error_message,
            ttft_ms=ttft_ms,
            duration_ms=duration_ms,
            metadata=dict(metadata or {}),
        )

        if self._drivers:
            results = await asyncio.gather(
                *(d.record(observation) for d in self._drivers),
                return_exceptions=True,
            )
            for res in results:
                if isinstance(res, Exception):
                    if self.fail_closed:
                        raise res
                    logger.warning("Observability driver failed to record observation: %s", res)

        return observation

    async def get_observations(
        self,
        limit: int = 100,
        offset: int = 0,
        *,
        start_date: datetime | str | None = None,
        end_date: datetime | str | None = None,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
    ) -> list[AIObservation]:
        """Query recent observations from the primary driver."""
        if not self._drivers:
            return []
        return await self._drivers[0].get_observations(
            limit=limit,
            offset=offset,
            start_date=start_date,
            end_date=end_date,
            provider=provider,
            model=model,
            status=status,
        )

    async def get_stats(self) -> dict[str, Any]:
        """Query aggregate statistics from the primary driver."""
        if not self._drivers:
            return {
                "total_calls": 0,
                "successful_calls": 0,
                "failed_calls": 0,
                "total_tokens": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cost": 0.0,
                "avg_latency_ms": 0.0,
                "by_provider": {},
                "by_model": {},
            }
        return await self._drivers[0].get_stats()

    async def clear(self) -> None:
        """Clear observations across all active drivers."""
        if self._drivers:
            await asyncio.gather(
                *(d.clear() for d in self._drivers),
                return_exceptions=True,
            )

    @asynccontextmanager
    async def observe_call(
        self,
        model: str,
        provider: str,
        messages: Sequence[Message | dict[str, Any]] | str,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[ObservationContext]:
        """
        Async context manager that measures latency, captures tokens/model,
        and automatically records observation upon exit (success, cancellation, or error).
        """
        prompt_hash = compute_prompt_hash(messages)
        ctx = ObservationContext(
            model=model,
            provider=provider,
            prompt_hash=prompt_hash,
            metadata=metadata,
        )
        start_time = time.perf_counter()

        try:
            yield ctx
        except (asyncio.CancelledError, GeneratorExit) as exc:
            ctx.status = "cancelled"
            ctx.error_message = str(exc) or "Stream consumer cancelled or disconnected."
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            ctx.duration_ms = latency_ms
            await self.record(
                model=ctx.model,
                provider=ctx.provider,
                input_tokens=ctx.input_tokens,
                output_tokens=ctx.output_tokens,
                latency_ms=latency_ms,
                prompt_hash=ctx.prompt_hash,
                cost=ctx.cost,
                status=ctx.status,
                error_message=ctx.error_message,
                metadata=ctx.metadata,
                ttft_ms=ctx.ttft_ms,
                duration_ms=ctx.duration_ms,
            )
            raise
        except Exception as exc:
            ctx.status = "error"
            ctx.error_message = str(exc)
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            ctx.duration_ms = latency_ms
            await self.record(
                model=ctx.model,
                provider=ctx.provider,
                input_tokens=ctx.input_tokens,
                output_tokens=ctx.output_tokens,
                latency_ms=latency_ms,
                prompt_hash=ctx.prompt_hash,
                cost=ctx.cost,
                status=ctx.status,
                error_message=ctx.error_message,
                metadata=ctx.metadata,
                ttft_ms=ctx.ttft_ms,
                duration_ms=ctx.duration_ms,
            )
            raise
        else:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            ctx.duration_ms = latency_ms
            await self.record(
                model=ctx.model,
                provider=ctx.provider,
                input_tokens=ctx.input_tokens,
                output_tokens=ctx.output_tokens,
                latency_ms=latency_ms,
                prompt_hash=ctx.prompt_hash,
                cost=ctx.cost,
                status=ctx.status,
                error_message=ctx.error_message,
                metadata=ctx.metadata,
                ttft_ms=ctx.ttft_ms,
                duration_ms=ctx.duration_ms,
            )


def get_observability() -> AIObservabilityManager:
    """Convenience accessor for the global AIObservabilityManager."""
    return AIObservabilityManager.get_instance()
