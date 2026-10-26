from __future__ import annotations

import importlib
import inspect
import json
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, ClassVar

logger = logging.getLogger("swiftpy.queue")

_dispatch_handler: Callable[[Job, str | None, float], Any] | None = None


def set_dispatch_handler(
    handler: Callable[[Job, str | None, float], Any] | None,
) -> None:
    """Set the global dispatch delegate for Job.dispatch()."""
    global _dispatch_handler
    _dispatch_handler = handler


class JobSerializationError(Exception):
    """Raised when a job payload cannot be serialized or deserialized."""


class Job(ABC):
    """
    Abstract base class for all SwiftPY queued jobs.

    Subclasses must implement the asynchronous `handle()` method.
    """

    queue: str = "default"
    tries: int = 3
    backoff: int | float | list[int | float] = 0
    timeout: float | None = None

    _trusted_namespaces: ClassVar[list[str] | None] = None
    _trusted_classes: ClassVar[set[str]] = set()

    @classmethod
    def set_trusted_namespaces(cls, namespaces: list[str] | None) -> None:
        """
        Configure allowed module namespace prefixes (e.g. ['app.', 'tests.']).
        If configured, deserialization of jobs from non-whitelisted modules is rejected
        before importlib.import_module() is called.
        Pass None to disable allowlist restriction.
        """
        cls._trusted_namespaces = namespaces

    @classmethod
    def allow_job_class(cls, target: type[Job] | str) -> None:
        """Allow a specific Job class or qualname string."""
        if isinstance(target, str):
            cls._trusted_classes.add(target)
        else:
            cls._trusted_classes.add(f"{target.__module__}:{target.__qualname__}")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.job_id: str = kwargs.pop("job_id", None) or uuid.uuid4().hex
        self.queue = kwargs.pop("queue", getattr(self, "queue", "default"))
        self.tries = kwargs.pop("tries", getattr(self, "tries", 3))
        self.attempts: int = kwargs.pop("attempts", 0)
        self.backoff = kwargs.pop("backoff", getattr(self, "backoff", 0))
        self.timeout = kwargs.pop("timeout", getattr(self, "timeout", None))
        self.pushed_at: float | None = kwargs.pop("pushed_at", None)
        self.args: tuple[Any, ...] = args
        self.kwargs: dict[str, Any] = kwargs
        self.data: dict[str, Any] = kwargs.pop("data", {})

    @abstractmethod
    async def handle(self) -> None:
        """
        Execute the job's business logic.

        Must be implemented by subclasses and will be awaited by a queue worker.
        """
        raise NotImplementedError

    async def failed(self, exception: BaseException) -> None:
        """
        Hook called when the job has permanently failed and exhausted all retry attempts.
        """

    def get_backoff_delay(self) -> float:
        """
        Calculate the backoff delay in seconds for the current attempt.
        """
        if isinstance(self.backoff, (int, float)):
            return max(0.0, float(self.backoff))
        if isinstance(self.backoff, (list, tuple)):
            if not self.backoff:
                return 0.0
            idx = max(0, min(self.attempts - 1, len(self.backoff) - 1))
            return max(0.0, float(self.backoff[idx]))
        return 0.0

    def serialize(self) -> dict[str, Any]:
        """
        Serialize job metadata and payload into a JSON-compatible dictionary.
        """
        qualname = f"{self.__class__.__module__}:{self.__class__.__qualname__}"
        return {
            "job_class": qualname,
            "job_id": self.job_id,
            "queue": self.queue,
            "tries": self.tries,
            "attempts": self.attempts,
            "backoff": self.backoff,
            "timeout": self.timeout,
            "pushed_at": self.pushed_at,
            "args": list(self.args),
            "kwargs": self.kwargs,
            "data": self.data,
        }

    @classmethod
    def deserialize(cls, payload: dict[str, Any] | str) -> Job:
        """
        Reconstruct a Job instance from a serialized dictionary or JSON string.
        """
        if isinstance(payload, str):
            try:
                data: dict[str, Any] = json.loads(payload)
            except Exception as exc:
                raise JobSerializationError(
                    f"Failed to parse JSON job envelope: {exc}"
                ) from exc
        else:
            data = payload

        if not isinstance(data, dict):
            raise JobSerializationError(
                f"Invalid job envelope: expected dict, got {type(data).__name__}"
            )

        job_class_str = data.get("job_class")
        if not job_class_str or not isinstance(job_class_str, str):
            raise JobSerializationError("Job envelope missing valid 'job_class'")

        # Resolve module and class name (support module:ClassName or module.ClassName)
        if ":" in job_class_str:
            module_name, class_name = job_class_str.split(":", 1)
        elif "." in job_class_str:
            module_name, class_name = job_class_str.rsplit(".", 1)
        else:
            raise JobSerializationError(
                f"Invalid job class identifier format: {job_class_str}"
            )

        # Check namespace allowlist before importing module
        if cls._trusted_namespaces is not None:
            is_trusted = (
                any(
                    module_name == ns.rstrip(".")
                    or module_name.startswith(f"{ns.rstrip('.')}.")
                    for ns in cls._trusted_namespaces
                )
                or job_class_str in cls._trusted_classes
            )
            if not is_trusted:
                raise JobSerializationError(
                    f"Job class '{job_class_str}' from module '{module_name}' is not in trusted namespaces"
                )

        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            raise JobSerializationError(
                f"Could not import module '{module_name}' for job '{job_class_str}': {exc}"
            ) from exc

        target_cls: Any = module
        for part in class_name.split("."):
            target_cls = getattr(target_cls, part, None)
            if target_cls is None:
                raise JobSerializationError(
                    f"Job class '{class_name}' not found in module '{module_name}'"
                )

        if not isinstance(target_cls, type) or not issubclass(target_cls, Job):
            raise JobSerializationError(
                f"Resolved target '{job_class_str}' is not a subclass of Job"
            )

        args = data.get("args") or []
        kwargs = dict(data.get("kwargs") or {})

        try:
            sig = inspect.signature(target_cls.__init__)
            params = sig.parameters

            init_kwargs = dict(kwargs)
            for meta_key in (
                "job_id",
                "queue",
                "tries",
                "attempts",
                "backoff",
                "timeout",
                "data",
            ):
                if (
                    meta_key in params
                    and meta_key not in init_kwargs
                    and meta_key in data
                ):
                    init_kwargs[meta_key] = data[meta_key]

            job_instance = target_cls(*args, **init_kwargs)
        except Exception as exc:
            raise JobSerializationError(
                f"Failed to instantiate job '{job_class_str}': {exc}"
            ) from exc

        # Ensure all persisted metadata is set on the instance
        if data.get("job_id"):
            job_instance.job_id = str(data["job_id"])
        if data.get("queue"):
            job_instance.queue = str(data["queue"])
        if "tries" in data and data["tries"] is not None:
            job_instance.tries = int(data["tries"])
        if "attempts" in data and data["attempts"] is not None:
            job_instance.attempts = int(data["attempts"])
        if "backoff" in data and data["backoff"] is not None:
            job_instance.backoff = data["backoff"]
        if "timeout" in data:
            job_instance.timeout = data["timeout"]
        if "pushed_at" in data:
            job_instance.pushed_at = data["pushed_at"]
        if "data" in data and isinstance(data["data"], dict):
            job_instance.data = data["data"]

        return job_instance

    async def dispatch(
        self,
        queue: str | None = None,
        delay: float = 0,
    ) -> str:
        """
        Convenience method to push this job instance onto the queue dispatcher.
        """
        if _dispatch_handler is None:
            raise RuntimeError(
                "No queue dispatcher has been initialized to dispatch jobs"
            )
        res = _dispatch_handler(self, queue, delay)
        if inspect.isawaitable(res):
            return str(await res)
        return str(res)
