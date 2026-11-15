from __future__ import annotations

import hashlib
import hmac
import importlib
import inspect
import json
import logging
import math
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from swiftpy.core.config import ConfigurationError
from swiftpy.database.transaction import get_active_connection
from swiftpy.support.keys import (
    decode_signing_key,
    resolve_configured_app_key,
    resolve_configured_previous_keys,
    validate_signing_key,
)

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


_SIGNATURE_EXCLUDED_FIELDS: frozenset[str] = frozenset({"signature", "attempts"})


def _parse_optional_timeout(
    raw_timeout: Any, *, exc_cls: type[Exception] = JobSerializationError
) -> float | None:
    """Validate optional job execution timeout as a positive finite float."""
    if raw_timeout is None:
        return None
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
        raise exc_cls(
            f"Invalid 'timeout' in job envelope: expected positive number, got {type(raw_timeout).__name__}"
        )
    value = float(raw_timeout)
    if not math.isfinite(value) or value <= 0:
        raise exc_cls(
            f"Invalid 'timeout' in job envelope: must be a finite positive number, got {raw_timeout!r}"
        )
    return value


def _parse_int_field(
    raw_value: Any, *, field_name: str, minimum: int
) -> int:
    """Validate integer envelope metadata fields while rejecting booleans."""
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        raise JobSerializationError(
            f"Invalid '{field_name}' in job envelope: expected int, got {type(raw_value).__name__}"
        )
    if raw_value < minimum:
        raise JobSerializationError(
            f"Invalid '{field_name}' in job envelope: must be >= {minimum}"
        )
    return raw_value


def parse_class_path(
    class_path: str, *, exc_cls: type[Exception] = JobSerializationError
) -> tuple[str, str]:
    """Parse ``module:ClassName`` or ``module.ClassName`` into ``(module, class)``."""
    if ":" in class_path:
        module_name, class_name = class_path.split(":", 1)
    elif "." in class_path:
        module_name, class_name = class_path.rsplit(".", 1)
    else:
        raise exc_cls(f"Invalid job class identifier format: {class_path}")

    if not module_name or not class_name:
        raise exc_cls(f"Invalid job class identifier format: {class_path}")
    return module_name, class_name


def is_trusted_class_path(
    class_path: str,
    module_name: str,
    *,
    trusted_namespaces: Sequence[str] | None,
    trusted_classes: set[str] | frozenset[str] = frozenset(),
    class_name: str | None = None,
) -> bool:
    """Return True if a class path belongs to a trusted namespace or explicit allowlist."""
    if trusted_namespaces is None:
        return True
    resolved_cls = (
        class_name if class_name is not None else parse_class_path(class_path)[1]
    )
    candidates = {
        class_path,
        f"{module_name}:{resolved_cls}",
        f"{module_name}.{resolved_cls}",
    }
    return any(
        module_name == ns.rstrip(".")
        or module_name.startswith(f"{ns.rstrip('.')}.")
        for ns in trusted_namespaces
    ) or bool(candidates & set(trusted_classes))


class Job(ABC):
    """
    Abstract base class for all SwiftPY queued jobs.

    Subclasses must implement the asynchronous `handle()` method.
    """

    queue: str = "default"
    tries: int = 3
    backoff: int | float | Sequence[int | float] = 0
    timeout: float | None = None

    _app_key: ClassVar[str | None] = None
    _previous_keys: ClassVar[list[str]] = []
    _trusted_namespaces: ClassVar[list[str] | None] = None
    _trusted_classes: ClassVar[set[str]] = set()
    _allow_unsigned: ClassVar[bool] = False

    @classmethod
    def set_allow_unsigned(cls, enabled: bool) -> None:
        """Explicit opt-in to allow unsigned job serialization/deserialization in local tests."""
        cls._allow_unsigned = bool(enabled)

    @classmethod
    def get_allow_unsigned(cls) -> bool:
        """Return whether unsigned job payloads are explicitly opted in."""
        return cls._allow_unsigned

    @classmethod
    def set_app_key(cls, key: str | None) -> None:
        """Set the active application key used for HMAC envelope signing."""
        cls._app_key = key

    @classmethod
    def get_app_key(cls, strict: bool = False) -> str | None:
        """
        Retrieve the active application key.
        Checks explicit Job._app_key, then Config 'app.key' / 'key', then env('APP_KEY').
        """
        if cls._app_key is not None:
            return cls._app_key
        resolved = resolve_configured_app_key()
        if resolved is not None:
            return resolved
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
        return resolve_configured_previous_keys()

    @classmethod
    def set_trusted_namespaces(cls, namespaces: list[str] | None) -> None:
        """
        Configure allowed module namespace prefixes (e.g. ['app.', 'swiftpy.']).
        If configured, deserialization of jobs from non-whitelisted modules is rejected
        before importlib.import_module() is called.
        Pass None to use default trusted namespaces ('app', 'swiftpy').
        """
        cls._trusted_namespaces = namespaces

    @classmethod
    def get_trusted_namespaces(cls) -> list[str] | None:
        """Return configured or default trusted namespaces."""
        if cls._trusted_namespaces is not None:
            return cls._trusted_namespaces
        return ["app", "swiftpy"]

    @classmethod
    def _compute_signature(cls, envelope: dict[str, Any], key: str) -> str:
        """Compute an HMAC-SHA256 signature over immutable canonical fields."""
        canonical_fields = {
            k: v
            for k, v in envelope.items()
            if k not in _SIGNATURE_EXCLUDED_FIELDS
        }
        canonical_json = json.dumps(
            canonical_fields,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        secret_bytes = decode_signing_key(key)
        return hmac.new(
            secret_bytes,
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
        self.timeout: float | None = _parse_optional_timeout(
            kwargs.pop("timeout", getattr(self, "timeout", None)),
            exc_cls=ValueError,
        )
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

    def serialize(
        self,
        app_key: str | None = None,
        *,
        allow_unsigned: bool | None = None,
    ) -> dict[str, Any]:
        """
        Serialize job metadata and payload into a JSON-compatible dictionary.
        Signs the envelope with HMAC-SHA256 using APP_KEY unless unsigned mode is explicitly opted in.
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
        unsigned_ok = self.get_allow_unsigned() if allow_unsigned is None else allow_unsigned
        if key is None or not str(key).strip():
            if not unsigned_ok:
                raise ConfigurationError(
                    "APP_KEY (>=32 bytes) is required for queue job signing. "
                    "Configure APP_KEY or pass allow_unsigned=True for local tests."
                )
            return envelope

        validate_signing_key(str(key), purpose="APP_KEY")
        envelope["signature"] = self._compute_signature(envelope, str(key))
        return envelope

    @classmethod
    def deserialize(
        cls,
        payload: dict[str, Any] | str,
        app_key: str | None = None,
        *,
        allow_unsigned: bool | None = None,
    ) -> Job:
        """
        Reconstruct a Job instance from a serialized dictionary or JSON string.
        Verifies HMAC signature and trusted namespace before module import.
        """
        key = app_key if app_key is not None else cls.get_app_key()
        unsigned_ok = cls.get_allow_unsigned() if allow_unsigned is None else allow_unsigned

        if key is None or not str(key).strip():
            if not unsigned_ok:
                raise ConfigurationError(
                    "APP_KEY (>=32 bytes) is required for queue job deserialization. "
                    "Configure APP_KEY or pass allow_unsigned=True for local tests."
                )
        else:
            validate_signing_key(str(key), purpose="APP_KEY")

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
        if key is not None and str(key).strip():
            signature = data.get("signature")
            if signature is None or signature == "":
                if not unsigned_ok:
                    raise JobSerializationError(
                        "Job envelope is missing a signature or signature is invalid"
                    )
            elif not isinstance(signature, str):
                raise JobSerializationError(
                    "Job envelope is missing a signature or signature is invalid"
                )
            else:
                expected_sig = cls._compute_signature(data, str(key))
                if not hmac.compare_digest(signature, expected_sig):
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
        module_name, class_name = parse_class_path(job_class_str)

        # 2. Check namespace allowlist before importing module
        if not is_trusted_class_path(
            job_class_str,
            module_name,
            trusted_namespaces=cls.get_trusted_namespaces(),
            trusted_classes=cls._trusted_classes,
            class_name=class_name,
        ):
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
            data["tries"] = _parse_int_field(
                data["tries"], field_name="tries", minimum=1
            )

        if "attempts" in data and data["attempts"] is not None:
            data["attempts"] = _parse_int_field(
                data["attempts"], field_name="attempts", minimum=0
            )

        parsed_timeout: float | None = None
        if "timeout" in data and data["timeout"] is not None:
            parsed_timeout = _parse_optional_timeout(
                data["timeout"], exc_cls=JobSerializationError
            )
            data["timeout"] = parsed_timeout

        if "pushed_at" in data and data["pushed_at"] is not None:
            raw_pushed = data["pushed_at"]
            if (
                isinstance(raw_pushed, bool)
                or not isinstance(raw_pushed, (int, float))
                or not math.isfinite(float(raw_pushed))
            ):
                raise JobSerializationError(
                    "Invalid 'pushed_at' in job envelope: expected finite timestamp"
                )

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
                "pushed_at",
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
