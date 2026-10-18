from __future__ import annotations

import pytest

from swiftpy import CircularDependencyError, Container


class ServiceA:
    def __init__(self, b: ServiceB) -> None:
        self.b = b


class ServiceB:
    def __init__(self, a: ServiceA) -> None:
        self.a = a


def test_circular_dependency_raises_with_chain() -> None:
    container = Container()

    with pytest.raises(CircularDependencyError) as exc_info:
        container.resolve(ServiceA)

    message = str(exc_info.value)
    assert "ServiceA" in message
    assert "ServiceB" in message


def test_circular_dependency_not_masked_by_default_fallback() -> None:
    """The specific regression this guards against: a broad except
    swallowing CircularDependencyError and replacing it with a vague
    'cannot resolve parameter' message."""
    container = Container()

    with pytest.raises(CircularDependencyError):
        container.resolve(ServiceB)
