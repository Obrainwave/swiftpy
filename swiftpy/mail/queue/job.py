from __future__ import annotations

import ast
import importlib
import inspect
import logging
import textwrap
import types
from collections.abc import Sequence
from typing import Any, ClassVar

from swiftpy.mail.exceptions import (
    MailAuthenticationError,
    MailConfigurationError,
    MailConnectionError,
    MailRateLimitError,
    MailTimeoutError,
    MailTransportError,
    MailValidationError,
)
from swiftpy.mail.mailable import Mailable
from swiftpy.mail.manager import MailManager
from swiftpy.queue.job import Job, is_trusted_class_path, parse_class_path

logger = logging.getLogger("swiftpy.mail.queue")

_MISSING = object()


def _collect_self_attr_targets(node: ast.AST, targets: set[str]) -> None:
    """Recursively collect ``self.<attr>`` names from AST assignment targets."""
    if isinstance(node, ast.Attribute):
        if (
            isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and not node.attr.startswith("_")
        ):
            targets.add(node.attr)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            _collect_self_attr_targets(elt, targets)


def _allowed_state_fields(target_cls: type[Mailable]) -> set[str]:
    """Return declared public instance/class attributes permitted in ``mailable_state``."""
    allowed: set[str] = set()
    for cls_in_mro in target_cls.__mro__:
        if cls_in_mro is object:
            continue

        for name in inspect.get_annotations(cls_in_mro):
            if isinstance(name, str) and not name.startswith("_"):
                allowed.add(name)

        for attr_name, attr_val in cls_in_mro.__dict__.items():
            if attr_name.startswith("_"):
                continue
            if not callable(attr_val) and (
                isinstance(attr_val, types.MemberDescriptorType)
                or (
                    not hasattr(attr_val, "__get__")
                    and not hasattr(attr_val, "__set__")
                )
            ):
                allowed.add(attr_name)

        init_fn = cls_in_mro.__dict__.get("__init__")
        if callable(init_fn):
            try:
                sig = inspect.signature(init_fn)
            except (ValueError, TypeError):
                sig = None
            if sig is not None:
                for param_name, param in sig.parameters.items():
                    if (
                        param_name != "self"
                        and not param_name.startswith("_")
                        and param.kind
                        not in (
                            inspect.Parameter.VAR_POSITIONAL,
                            inspect.Parameter.VAR_KEYWORD,
                        )
                    ):
                        allowed.add(param_name)

        for member in cls_in_mro.__dict__.values():
            if not inspect.isfunction(member):
                continue
            try:
                source = textwrap.dedent(inspect.getsource(member))
                tree = ast.parse(source)
            except (OSError, TypeError, SyntaxError):
                continue
            for ast_node in ast.walk(tree):
                if isinstance(ast_node, ast.Assign):
                    for target in ast_node.targets:
                        _collect_self_attr_targets(target, allowed)
                elif isinstance(ast_node, (ast.AnnAssign, ast.AugAssign)):
                    _collect_self_attr_targets(ast_node.target, allowed)

    return allowed


class SendQueuedMailableJob(Job):
    """
    Asynchronous background job for delivering a Mailable.
    Handles serialization, execution, and retry semantics.
    """

    queue = "emails"
    tries = 3
    backoff: int | float | Sequence[int | float] = (5, 15, 60)
    _trusted_mailable_classes: ClassVar[set[str]] = set()

    @classmethod
    def allow_mailable_class(cls, mailable_cls_or_path: type[Mailable] | str) -> str:
        """Explicitly allow deserialization of a Mailable class or module path."""
        if isinstance(mailable_cls_or_path, str):
            mod_name, cls_name = parse_class_path(
                mailable_cls_or_path, exc_cls=MailValidationError
            )
            class_path = f"{mod_name}:{cls_name}"
        elif isinstance(mailable_cls_or_path, type) and issubclass(
            mailable_cls_or_path, Mailable
        ):
            class_path = (
                f"{mailable_cls_or_path.__module__}:{mailable_cls_or_path.__qualname__}"
            )
        else:
            raise MailValidationError(
                "allow_mailable_class requires a Mailable subclass or valid class path"
            )
        cls._trusted_mailable_classes.add(class_path)
        return class_path

    def __init__(self, mailable: Mailable | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.mailable = mailable

        if mailable is not None:
            self.queue = getattr(mailable, "queue", self.queue)
            self.tries = getattr(mailable, "tries", self.tries)
            self.backoff = getattr(mailable, "backoff", self.backoff)
            self.timeout = getattr(mailable, "timeout", self.timeout)

            # Store mailable class and state in data
            self.data["mailable_class"] = (
                f"{mailable.__class__.__module__}:{mailable.__class__.__qualname__}"
            )
            state: dict[str, Any] = {}
            if hasattr(mailable, "__dict__"):
                for k, v in vars(mailable).items():
                    if not k.startswith("_"):
                        state[k] = v
            self.data["mailable_state"] = state
            self.data["mailer_name"] = mailable.mailer_name

    def _resolve_mailable(self) -> Mailable:
        if self.mailable is not None:
            return self.mailable

        mailable_class_str = self.data.get("mailable_class")
        if not mailable_class_str or not isinstance(mailable_class_str, str):
            raise MailValidationError(
                "SendQueuedMailableJob missing 'mailable_class' in payload"
            )

        mod_name, cls_name = parse_class_path(
            mailable_class_str, exc_cls=MailValidationError
        )
        if not is_trusted_class_path(
            mailable_class_str,
            mod_name,
            trusted_namespaces=Job.get_trusted_namespaces(),
            trusted_classes=self._trusted_mailable_classes,
            class_name=cls_name,
        ):
            raise MailValidationError(
                f"Mailable class '{mailable_class_str}' from module '{mod_name}' is not in trusted namespaces"
            )

        module = importlib.import_module(mod_name)
        target_cls = getattr(module, cls_name, None)

        if (
            target_cls is None
            or not isinstance(target_cls, type)
            or not issubclass(target_cls, Mailable)
        ):
            raise MailValidationError(
                f"Class '{mailable_class_str}' is not a valid subclass of Mailable"
            )

        instance = target_cls.__new__(target_cls)
        state = self.data.get("mailable_state", {})
        if state is not None:
            if not isinstance(state, dict):
                raise MailValidationError(
                    "SendQueuedMailableJob 'mailable_state' must be a dictionary"
                )
            allowed_fields = _allowed_state_fields(target_cls)
            for key, value in state.items():
                if not isinstance(key, str) or key.startswith("_"):
                    raise MailValidationError(
                        f"Disallowed attribute '{key}' in mailable_state"
                    )
                existing = inspect.getattr_static(target_cls, key, _MISSING)
                if existing is not _MISSING and (
                    callable(existing)
                    or (
                        (hasattr(existing, "__get__") or hasattr(existing, "__set__"))
                        and not isinstance(existing, types.MemberDescriptorType)
                    )
                ):
                    raise MailValidationError(
                        f"Disallowed descriptor or callable attribute '{key}' in mailable_state"
                    )
                if key not in allowed_fields:
                    raise MailValidationError(
                        f"Undeclared attribute '{key}' in mailable_state for {target_cls.__name__}"
                    )
                setattr(instance, key, value)

        mailer_name = self.data.get("mailer_name")
        if mailer_name is not None:
            instance.mailer_name = str(mailer_name)

        self.mailable = instance
        return instance

    async def handle(self) -> None:
        mailable_label = (
            self.mailable.__class__.__name__
            if self.mailable is not None
            else str(self.data.get("mailable_class", "unknown"))
        )

        try:
            mailable = self._resolve_mailable()
            mailable_label = mailable.__class__.__name__
            message = await mailable.build_message()
            manager = MailManager.get_instance()
            result = await manager.send(message, mailer=mailable.mailer_name)
            if not result.success:
                raise MailTransportError(
                    f"Email delivery via {result.provider} was not successful: rejected={result.rejected}"
                )
        except (MailConnectionError, MailRateLimitError, MailTimeoutError) as exc:
            # Transient error: re-raise to trigger queue retry
            logger.warning(
                "Transient delivery failure for mailable %s: %s (attempt %d/%d)",
                mailable_label,
                exc,
                self.attempts,
                self.tries,
            )
            raise
        except (
            MailValidationError,
            MailAuthenticationError,
            MailConfigurationError,
        ):
            # Permanent error: exhaust retries so QueueWorker sends the job to DLQ instead of looping
            logger.exception(
                "Permanent delivery failure for mailable %s",
                mailable_label,
            )
            self.attempts = max(self.attempts, self.tries)
            raise

    async def failed(self, exception: BaseException) -> None:
        logger.error(
            "Permanent failure delivering mailable %s: %s",
            self.mailable.__class__.__name__ if self.mailable else "unknown",
            exception,
        )


__all__ = ["SendQueuedMailableJob"]
