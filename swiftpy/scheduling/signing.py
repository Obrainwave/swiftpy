from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from typing import Any

from swiftpy.queue.job import Job
from swiftpy.scheduling.exceptions import ScheduleSignatureError
from swiftpy.support.keys import decode_signing_key, validate_signing_key

_SIGNATURE_PURPOSE = "swiftpy.schedule.v1"


def _compute(
    *,
    task_kind: str,
    task_name: str,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    queue: str,
    key: str,
) -> str:
    canonical = json.dumps(
        {
            "purpose": _SIGNATURE_PURPOSE,
            "task_kind": task_kind,
            "task_name": task_name,
            "args": list(args),
            "kwargs": dict(kwargs),
            "queue": queue,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hmac.new(
        decode_signing_key(key), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def sign_schedule_fields(
    *,
    task_kind: str,
    task_name: str,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    queue: str,
) -> str | None:
    """
    HMAC-sign the fields that decide what a schedule executes, using the queue's ``APP_KEY``.

    Returns ``None`` when no key is configured; dispatching such a schedule later fails on
    queue job signing (or, with a key configured afterwards, on verification) rather than
    silently running unverified work.
    """
    key = Job.get_app_key()
    if key is None or not key.strip():
        return None
    validate_signing_key(key, purpose="APP_KEY")
    return _compute(
        task_kind=task_kind,
        task_name=task_name,
        args=args,
        kwargs=kwargs,
        queue=queue,
        key=key,
    )


def verify_schedule_signature(
    signature: str | None,
    *,
    task_kind: str,
    task_name: str,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    queue: str,
    label: str,
) -> None:
    """Raise ``ScheduleSignatureError`` unless ``signature`` matches the execution fields."""
    key = Job.get_app_key()
    if key is None or not key.strip():
        # Queue serialization enforces its own APP_KEY / allow_unsigned policy at push time.
        return
    if not signature:
        raise ScheduleSignatureError(f"Schedule '{label}' is unsigned; refusing to dispatch.")

    for candidate in (key, *Job.get_previous_keys()):
        expected = _compute(
            task_kind=task_kind,
            task_name=task_name,
            args=args,
            kwargs=kwargs,
            queue=queue,
            key=candidate,
        )
        if hmac.compare_digest(signature, expected):
            return
    raise ScheduleSignatureError(
        f"Schedule '{label}' failed signature verification: tampered or untrusted record."
    )


__all__ = ["sign_schedule_fields", "verify_schedule_signature"]
