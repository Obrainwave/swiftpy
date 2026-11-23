from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from swiftpy.queue.job import Job
from swiftpy.scheduling.exceptions import InvalidScheduleError

TaskCallable = Callable[..., Any | Awaitable[Any]]
CallableResolver = Callable[[str], TaskCallable | None]

_callable_resolver: CallableResolver | None = None


def set_callable_resolver(resolver: CallableResolver | None) -> None:
    """Register the active callable resolution hook used by ``ScheduledCallableJob``."""
    global _callable_resolver
    _callable_resolver = resolver


def get_callable_resolver() -> CallableResolver | None:
    """Return the active callable resolution hook, if registered."""
    return _callable_resolver


class ScheduledCallableJob(Job):
    """
    Trusted Job wrapper dispatching registered named callables onto ``QueueWorker``.

    Only callables explicitly registered with the active ``Scheduler`` are permitted
    to execute; arbitrary dynamic code or unverified import strings are never accepted.
    """

    def __init__(
        self,
        task_name: str,
        task_args: list[Any] | None = None,
        task_kwargs: dict[str, Any] | None = None,
        *,
        job_id: str | None = None,
        queue: str | None = None,
        tries: int | None = None,
        attempts: int | None = None,
        backoff: float | Sequence[float] | None = None,
        timeout: float | None = None,
        pushed_at: float | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        args_list = list(task_args or [])
        kwargs_dict = dict(task_kwargs or {})
        meta: dict[str, Any] = {}
        if job_id is not None:
            meta["job_id"] = job_id
        if queue is not None:
            meta["queue"] = queue
        if tries is not None:
            meta["tries"] = tries
        if attempts is not None:
            meta["attempts"] = attempts
        if backoff is not None:
            meta["backoff"] = backoff
        if timeout is not None:
            meta["timeout"] = timeout
        if pushed_at is not None:
            meta["pushed_at"] = pushed_at
        if data is not None:
            meta["data"] = dict(data)
        super().__init__(
            task_name=task_name,
            task_args=args_list,
            task_kwargs=kwargs_dict,
            **meta,
        )
        self.task_name = task_name
        self.task_args = args_list
        self.task_kwargs = kwargs_dict

    async def handle(self) -> None:
        """Resolve and invoke the registered callable on the active container Scheduler."""
        handler = _callable_resolver(self.task_name) if _callable_resolver is not None else None
        if handler is None:
            raise InvalidScheduleError(
                f"Scheduled callable '{self.task_name}' is not registered in the active Scheduler."
            )
        result = handler(*self.task_args, **self.task_kwargs)
        if inspect.isawaitable(result):
            await result


__all__ = [
    "CallableResolver",
    "ScheduledCallableJob",
    "TaskCallable",
    "get_callable_resolver",
    "set_callable_resolver",
]
