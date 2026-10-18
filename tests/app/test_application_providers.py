# tests/unit/test_application_providers.py
from __future__ import annotations

from pathlib import Path

from app.providers.app_service_provider import AppServiceProvider
from app.providers.database_service_provider import DatabaseServiceProvider
from swiftpy import Application, ServiceProvider
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
    assert type(provider_inst).booted is True
