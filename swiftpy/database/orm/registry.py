from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from swiftpy.database.orm.model import Model

# Process-global registry of Model classes by simple name -> {qualified_name: cls}.
# Maintained globally by design because Model definitions are class-level metadata;
# tests that dynamically define ephemeral Model subclasses can use
# `snapshot_model_registry()` / `restore_model_registry()` to keep the registry clean.
_MODEL_REGISTRY: dict[str, dict[str, type[Model]]] = {}


def snapshot_model_registry() -> dict[str, dict[str, type[Model]]]:
    """Return a deep snapshot of `_MODEL_REGISTRY` for test fixture restoration."""
    return {name: dict(candidates) for name, candidates in _MODEL_REGISTRY.items()}


def restore_model_registry(
    snapshot: dict[str, dict[str, type[Model]]],
) -> None:
    """Restore `_MODEL_REGISTRY` in-place to a previous snapshot."""
    _MODEL_REGISTRY.clear()
    for name, candidates in snapshot.items():
        _MODEL_REGISTRY[name] = dict(candidates)


__all__ = [
    "_MODEL_REGISTRY",
    "restore_model_registry",
    "snapshot_model_registry",
]
