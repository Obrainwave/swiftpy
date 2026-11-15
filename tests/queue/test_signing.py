from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest

from swiftpy.core.config import Config
from swiftpy.core.container import Container, get_container, set_container
from swiftpy.queue.job import Job, JobSerializationError


class DummySignedJob(Job):
    def __init__(self, message: str = "hello", **kwargs: object) -> None:
        super().__init__(message, **kwargs)
        self.message = message

    async def handle(self) -> None:
        pass


@pytest.fixture(autouse=True)
def reset_job_keys_and_namespaces() -> Generator[None, None, None]:
    original_key = Job.get_app_key()
    original_prev = Job.get_previous_keys()
    original_namespaces = Job._trusted_namespaces
    original_trusted = Job._trusted_classes.copy()
    original_allow_unsigned = Job.get_allow_unsigned()

    Job.set_app_key(None)
    Job.set_previous_keys([])
    Job.set_trusted_namespaces(None)
    Job.set_allow_unsigned(False)
    Job.allow_job_class(DummySignedJob)

    yield

    Job.set_app_key(original_key)
    Job.set_previous_keys(original_prev)
    Job.set_trusted_namespaces(original_namespaces)
    Job._trusted_classes = original_trusted
    Job.set_allow_unsigned(original_allow_unsigned)


def test_unsigned_payload_is_rejected_and_never_imports_module() -> None:
    """When an app key is configured, an unsigned payload is rejected before import_module."""
    secret_key = "a" * 32
    Job.set_app_key(secret_key)

    job = DummySignedJob(message="secret")
    payload = job.serialize(app_key=None, allow_unsigned=True)  # Unsigned payload
    payload.pop("signature", None)

    with patch("importlib.import_module") as mock_import:
        with pytest.raises(JobSerializationError, match="signature"):
            Job.deserialize(payload)
        mock_import.assert_not_called()


def test_tampered_payload_signature_is_rejected_and_never_imports_module() -> None:
    """Modifying any field in a signed envelope invalidates signature and prevents import."""
    secret_key = "b" * 32
    Job.set_app_key(secret_key)

    job = DummySignedJob(message="initial")
    payload = job.serialize()
    assert "signature" in payload

    # Tamper with the payload args
    payload["args"] = ["tampered_payload"]

    with patch("importlib.import_module") as mock_import:
        with pytest.raises(JobSerializationError, match="signature verification failed"):
            Job.deserialize(payload)
        mock_import.assert_not_called()


def test_validly_signed_payload_succeeds() -> None:
    """A payload correctly signed with the active app key deserializes cleanly."""
    secret_key = "c" * 32
    Job.set_app_key(secret_key)

    job = DummySignedJob(message="valid")
    payload = job.serialize()

    deserialized = Job.deserialize(payload)
    assert isinstance(deserialized, DummySignedJob)
    assert deserialized.message == "valid"


def test_key_rotation_accepts_previous_key() -> None:
    """A payload signed with a retired previous key is accepted during key rotation."""
    old_key = "old_secret_key_" + "1" * 17
    new_key = "new_secret_key_" + "2" * 17

    # Sign with old key
    job = DummySignedJob(message="key-rotation")
    payload = job.serialize(app_key=old_key)

    # Server now has new key as primary, and old key in previous_keys
    Job.set_app_key(new_key)
    Job.set_previous_keys([old_key])

    deserialized = Job.deserialize(payload)
    assert isinstance(deserialized, DummySignedJob)
    assert deserialized.message == "key-rotation"


def test_untrusted_namespace_is_rejected_before_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When signing is off, default trusted namespaces block arbitrary modules from importing."""
    prev_cfg = Config._instance
    prev_container = get_container()
    try:
        monkeypatch.delenv("APP_KEY", raising=False)
        empty_cfg = Config({"app": {"key": ""}})
        c = Container()
        c.instance(Config, empty_cfg)
        set_container(c)
        Config._instance = empty_cfg
        Job.set_app_key(None)

        untrusted_payload = {
            "job_class": "os.system",
            "args": ["echo evil"],
        }

        with patch("importlib.import_module") as mock_import:
            with pytest.raises(JobSerializationError, match="trusted namespaces"):
                Job.deserialize(untrusted_payload, allow_unsigned=True)
            mock_import.assert_not_called()
    finally:
        Config._instance = prev_cfg
        set_container(prev_container)


def test_target_class_not_job_subclass_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if module is within trusted namespaces, non-Job classes are rejected."""
    prev_cfg = Config._instance
    prev_container = get_container()
    try:
        monkeypatch.delenv("APP_KEY", raising=False)
        empty_cfg = Config({"app": {"key": ""}})
        c = Container()
        c.instance(Config, empty_cfg)
        set_container(c)
        Config._instance = empty_cfg
        Job.set_app_key(None)
        Job.set_trusted_namespaces(["tests.", "test_signing"])

        # tests.queue.test_signing has non-Job functions/classes
        payload = {
            "job_class": f"{reset_job_keys_and_namespaces.__module__}:{reset_job_keys_and_namespaces.__name__}",
            "args": [],
        }

        with pytest.raises(JobSerializationError, match="not a subclass of Job"):
            Job.deserialize(payload, allow_unsigned=True)
    finally:
        Config._instance = prev_cfg
        set_container(prev_container)
