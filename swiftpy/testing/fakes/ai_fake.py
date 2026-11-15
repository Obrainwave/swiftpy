from __future__ import annotations

from swiftpy.ai._deterministic import (
    generate_deterministic_embedding as _deterministic_fake_vector,
)
from swiftpy.ai._fake import (
    AIFake,
    FakeCall,
    FakeEmbeddingCall,
    FixtureType,
    _task_fake_stack,
)

__all__ = [
    "AIFake",
    "FakeCall",
    "FakeEmbeddingCall",
    "FixtureType",
    "_deterministic_fake_vector",
    "_task_fake_stack",
]
