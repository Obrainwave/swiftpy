# tests/unit/test_application_providers.py
from __future__ import annotations

from swiftpy import Application, ServiceProvider


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
