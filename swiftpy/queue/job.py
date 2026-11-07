from __future__ import annotations

import hashlib
import hmac
import importlib
import inspect
import json
import logging
import os
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, ClassVar

from swiftpy.core.config import Config, ConfigurationError
from swiftpy.database.transaction import get_active_connection

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

    _app_key: ClassVar[str | None] = None
    _previous_keys: ClassVar[list[str]] = []
    _trusted_namespaces: ClassVar[list[str] | None] = None
    _trusted_classes: ClassVar[set[str]] = set()

    @classmethod
    def set_app_key(cls, key: str | None) -> None:
        """Set the active application key used for HMAC envelope signing."""
        cls._app_key = key

    @classmethod
    def get_app_key(cls, strict: bool = False) -> str | None:
        """
        Retrieve the active application key.
        Checks explicit Job._app_key, then Config 'app.key' / 'key', then os.getenv('APP_KEY').
        """
        if cls._app_key is not None:
            return cls._app_key
        try:
            cfg = Config.get_instance()
            key = cfg.get("app.key") or cfg.get("key")
            if key and isinstance(key, str):
                return key
        except (ConfigurationError, RuntimeError):
            logger.debug("Config not yet available for queue APP_KEY", exc_info=True)
        env_key = os.getenv("APP_KEY")
        if env_key:
            return env_key
        if strict:
            raise ConfigurationError(
                "APP_KEY is required and must be configured for queue signing."
            )
        return None

    @classmethod
    def set_previous_keys(cls, keys: list[str]) -> None:
        """Set previous retired application keys to accept during key rotation."""
        cls._previous_keys = list(keys)

    @classmethod
    def get_previous_keys(cls) -> list[str]:
        """Retrieve previous keys configured for key rotation."""
        if cls._previous_keys:
            return list(cls._previous_keys)
        try:
            cfg = Config.get_instance()
            prev = cfg.get("app.previous_keys")
            if isinstance(prev, (list, tuple)):
                return [str(k) for k in prev]
        except (ConfigurationError, RuntimeError):
            logger.debug("Config not yet available for previous_keys", exc_info=True)
        return []

    @classmethod
    def set_trusted_namespaces(cls, namespaces: list[str] | None) -> None:
        """
        Configure allowed module namespace prefixes (e.g. ['app.', 'tests.']).
        If configured, deserialization of jobs from non-whitelisted modules is rejected
        before importlib.import_module() is called.
        Pass None to use default trusted namespaces ('app', 'swiftpy', 'tests').
        """
        cls._trusted_namespaces = namespaces

    @classmethod
    def get_trusted_namespaces(cls) -> list[str] | None:
        """Return configured or default trusted namespaces."""
        if cls._trusted_namespaces is not None:
            return cls._trusted_namespaces
        return ["app", "swiftpy", "tests", "test"]

    @classmethod
    def _compute_signature(cls, envelope: dict[str, Any], key: str) -> str:
        """Compute an HMAC-SHA256 signature over the canonical representation of the envelope."""
        canonical_fields = {k: v for k, v in envelope.items() if k != "signature"}
        canonical_json = json.dumps(
            canonical_fields,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hmac.new(
            key.encode("utf-8"),
            canonical_json.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

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

    def serialize(self, app_key: str | None = None) -> dict[str, Any]:
        """
        Serialize job metadata and payload into a JSON-compatible dictionary.
        Signs the envelope with HMAC-SHA256 if an app key is configured.
        """
        qualname = f"{self.__class__.__module__}:{self.__class__.__qualname__}"
        envelope: dict[str, Any] = {
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
        key = app_key if app_key is not None else self.get_app_key()
        if key:
            envelope["signature"] = self._compute_signature(envelope, key)
        return envelope

    @classmethod
    def deserialize(
        cls,
        payload: dict[str, Any] | str,
        app_key: str | None = None,
    ) -> Job:
        """
        Reconstruct a Job instance from a serialized dictionary or JSON string.
        Verifies HMAC signature and trusted namespace before module import.
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

        # 1. Verify signature BEFORE importing module
        key = app_key if app_key is not None else cls.get_app_key()
        signature = data.get("signature")
        if key is not None:
            if not signature or not isinstance(signature, str):
                raise JobSerializationError(
                    "Job envelope is missing a signature or signature is invalid"
                )
            expected_sig = cls._compute_signature(data, key)
            if not hmac.compare_digest(signature, expected_sig):
                # Try previous keys for key rotation
                matched_prev = False
                for prev_key in cls.get_previous_keys():
                    if hmac.compare_digest(
                        signature, cls._compute_signature(data, prev_key)
                    ):
                        matched_prev = True
                        break
                if not matched_prev:
                    raise JobSerializationError(
                        "Job envelope signature verification failed: tampered or untrusted payload"
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

        # 2. Check namespace allowlist before importing module
        trusted_namespaces = cls.get_trusted_namespaces()
        if trusted_namespaces is not None:
            is_trusted = (
                any(
                    module_name == ns.rstrip(".")
                    or module_name.startswith(f"{ns.rstrip('.')}.")
                    or (
                        ns.rstrip(".") in ("tests", "test")
                        and module_name.startswith("test")
                    )
                    for ns in trusted_namespaces
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

        raw_args = data.get("args")
        if raw_args is not None:
            if not isinstance(raw_args, (list, tuple)):
                raise JobSerializationError("Envelope field 'args' must be a list or tuple")
            args = list(raw_args)
        else:
            args = []

        raw_kwargs = data.get("kwargs")
        if raw_kwargs is not None:
            if not isinstance(raw_kwargs, dict):
                raise JobSerializationError("Envelope field 'kwargs' must be a dict")
            kwargs = dict(raw_kwargs)
        else:
            kwargs = {}

        if "tries" in data and data["tries"] is not None:
            try:
                parsed_tries = int(data["tries"])
                if parsed_tries < 1:
                    raise ValueError("tries must be >= 1")
                data["tries"] = parsed_tries
            except (ValueError, TypeError) as exc:
                raise JobSerializationError(
                    f"Invalid 'tries' in job envelope: {exc}"
                ) from exc

        if "attempts" in data and data["attempts"] is not None:
            try:
                parsed_attempts = int(data["attempts"])
                if parsed_attempts < 0:
                    raise ValueError("attempts must be >= 0")
                data["attempts"] = parsed_attempts
            except (ValueError, TypeError) as exc:
                raise JobSerializationError(
                    f"Invalid 'attempts' in job envelope: {exc}"
                ) from exc

        if "data" in data and data["data"] is not None and not isinstance(data["data"], dict):
            raise JobSerializationError("Envelope field 'data' must be a dict")

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
        after_commit: bool = False,
    ) -> str:
        """
        Convenience method to push this job instance onto the queue dispatcher.

        If after_commit is True and a database transaction is active, the dispatch
        is deferred until the transaction successfully commits. If the transaction
        rolls back, the job is dropped. If no transaction is active, it dispatches immediately.
        """
        if _dispatch_handler is None:
            raise RuntimeError(
                "No queue dispatcher has been initialized to dispatch jobs"
            )

        if after_commit:
            conn = get_active_connection()
            if conn is not None and hasattr(conn, "on_commit"):

                async def _commit_dispatch() -> None:
                    if _dispatch_handler is not None:
                        res = _dispatch_handler(self, queue, delay)
                        if inspect.isawaitable(res):
                            await res

                conn.on_commit(_commit_dispatch)
                return self.job_id

        res = _dispatch_handler(self, queue, delay)
        if inspect.isawaitable(res):
            return str(await res)
        return str(res)
