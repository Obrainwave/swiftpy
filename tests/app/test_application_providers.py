# tests/unit/test_application_providers.py
from __future__ import annotations

import logging as stdlib_logging
import queue as stdlib_queue
import sys
from pathlib import Path

import pytest

from app.providers.app_service_provider import AppServiceProvider
from app.providers.database_service_provider import DatabaseServiceProvider
from swiftpy import Application, ServiceProvider
from swiftpy.core.application import discover_providers
from swiftpy.core.config import ConfigurationError
from swiftpy.database.pool import DatabasePool


class RecordingProvider(ServiceProvider):
    register_calls = 0
    boot_calls = 0

    def register(self) -> None:
        RecordingProvider.register_calls += 1

    def boot(self) -> None:
        RecordingProvider.boot_calls += 1


class LateProvider(ServiceProvider):
    booted = False

    def register(self) -> None:
        pass

    def boot(self) -> None:
        LateProvider.booted = True


def test_boot_calls_register_and_boot_on_each_provider() -> None:
    RecordingProvider.register_calls = RecordingProvider.boot_calls = 0
    app = Application()
    app.register(RecordingProvider)

    assert RecordingProvider.register_calls == 1
    assert RecordingProvider.boot_calls == 0  # register() alone must not boot

    app.boot()
    assert RecordingProvider.boot_calls == 1


def test_boot_is_idempotent_on_second_call() -> None:
    RecordingProvider.register_calls = RecordingProvider.boot_calls = 0
    app = Application()
    app.register(RecordingProvider)

    app.boot()
    app.boot()  # second call must not re-boot

    assert RecordingProvider.boot_calls == 1


def test_late_registered_provider_is_not_locked_out() -> None:
    LateProvider.booted = False
    app = Application()
    app.boot()  # boot happens first, before this provider exists

    app.register(LateProvider)

    assert LateProvider.booted is True


def test_application_auto_discovers_default_providers() -> None:
    app = Application()

    # Providers should be automatically discovered and registered
    provider_classes = [type(p) for p in app._providers]
    assert AppServiceProvider in provider_classes
    assert DatabaseServiceProvider in provider_classes

    # DatabasePool should be resolved from the container thanks to DatabaseServiceProvider
    assert app.container.has(DatabasePool)


def test_application_fastapi_style_config_parameters() -> None:
    app = Application(
        title="Custom Store API",
        version="2.1.0",
        debug=True,
        custom_header="X-App",
    )

    assert app.title == "Custom Store API"
    assert app.version == "2.1.0"
    assert app.debug is True
    assert app.config.get("custom_header") == "X-App"


def test_application_auto_discovers_new_custom_provider_dynamically(
    tmp_path: Path,
) -> None:
    custom_provider_code = """from __future__ import annotations

from swiftpy.core.providers.base import ServiceProvider


class CustomMockService:
    def greet(self) -> str:
        return "hello from custom service"


class CustomServiceProvider(ServiceProvider):
    registered = False
    booted = False

    def register(self) -> None:
        CustomServiceProvider.registered = True
        self.container.singleton(CustomMockService)

    def boot(self) -> None:
        CustomServiceProvider.booted = True
"""
    (tmp_path / "custom_service_provider.py").write_text(
        custom_provider_code, encoding="utf-8"
    )

    app = Application(providers_path=tmp_path)
    provider_names = [type(p).__name__ for p in app._providers]
    assert "CustomServiceProvider" in provider_names

    app.boot()
    provider_inst = next(
        p for p in app._providers if type(p).__name__ == "CustomServiceProvider"
    )
    assert getattr(type(provider_inst), "booted", False) is True


def test_provider_named_queue_or_logging_does_not_shadow_stdlib(
    tmp_path: Path,
) -> None:
    """A provider file named queue.py or logging.py must never shadow Python stdlib modules."""
    (tmp_path / "queue.py").write_text(
        """from __future__ import annotations

import queue as _q

from swiftpy.core.providers.base import ServiceProvider


class QueueNamedProvider(ServiceProvider):
    def register(self) -> None:
        assert hasattr(_q, "Queue")
""",
        encoding="utf-8",
    )
    (tmp_path / "logging.py").write_text(
        """from __future__ import annotations

import logging as _log

from swiftpy.core.providers.base import ServiceProvider


class LoggingNamedProvider(ServiceProvider):
    def register(self) -> None:
        assert hasattr(_log, "getLogger")
""",
        encoding="utf-8",
    )

    app = Application(providers_path=tmp_path)
    provider_names = [type(p).__name__ for p in app._providers]
    assert "QueueNamedProvider" in provider_names
    assert "LoggingNamedProvider" in provider_names

    # target_dir must not be injected into sys.path and stdlib modules must remain intact
    assert str(tmp_path) not in sys.path
    assert str(tmp_path.resolve()) not in sys.path
    assert sys.modules["queue"] is stdlib_queue
    assert sys.modules["logging"] is stdlib_logging


def test_broken_provider_fails_startup_unless_strict_false(
    tmp_path: Path,
) -> None:
    """Import or syntax errors in a provider module must fail startup with the provider name."""
    (tmp_path / "broken_service_provider.py").write_text(
        """from __future__ import annotations

import nonexistent_dependency_for_provider_test_xyz  # noqa: F401
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="broken_service_provider"):
        Application(providers_path=tmp_path)

    # Opt-out flag for tooling skips broken provider
    app = Application(providers_path=tmp_path, strict_providers=False)
    assert app is not None
    assert discover_providers(tmp_path, strict=False) == []


def test_discover_providers_from_different_cwd_and_skip_underscore_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery works relative to explicit project_root from a different CWD and skips _ files."""
    repo_root = Path(__file__).resolve().parents[2]
    (tmp_path / "_private_helper.py").write_text(
        "raise RuntimeError('Private helper must be skipped during provider discovery')\n",
        encoding="utf-8",
    )

    # Verify _private_helper.py is skipped
    assert discover_providers(tmp_path) == []

    # Change CWD to tmp_path and discover from explicit project_root
    monkeypatch.chdir(tmp_path)
    app = Application(project_root=repo_root)
    provider_classes = [type(p) for p in app._providers]
    assert AppServiceProvider in provider_classes
    assert DatabaseServiceProvider in provider_classes
