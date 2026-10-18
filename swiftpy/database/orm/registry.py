from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from swiftpy.database.orm.model import Model

_MODEL_REGISTRY: dict[str, dict[str, type[Model]]] = {}
