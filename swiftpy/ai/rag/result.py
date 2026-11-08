from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from swiftpy.ai.pricing import (
    _CUSTOM_PRICING,
    _PRICING_LOCK,
    DEFAULT_MODEL_PRICING,
    calculate_token_cost,
    register_model_pricing,
)
from swiftpy.ai.providers.base import TokenUsage
from swiftpy.ai.vectorstore.base import SearchResult

__all__ = [
    "DEFAULT_MODEL_PRICING",
    "_CUSTOM_PRICING",
    "_PRICING_LOCK",
    "RAGResult",
    "calculate_token_cost",
    "register_model_pricing",
]


def _safe_serialize_val(val: Any) -> Any:
    """Safely convert metadata values to JSON-serializable types."""
    if val is None or isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, dict):
        return {str(k): _safe_serialize_val(v) for k, v in val.items()}
    if isinstance(val, (list, tuple, set)):
        return [_safe_serialize_val(item) for item in val]
    if hasattr(val, "to_dict") and callable(val.to_dict):
        return val.to_dict()
    if hasattr(val, "isoformat") and callable(val.isoformat):
        return val.isoformat()
    return str(val)


@dataclass(slots=True)
class RAGResult:
    """
    Result returned by a RAG pipeline execution.

    Exposes the generated text, retrieved source chunks, token telemetry,
    estimated dollar cost, and execution metadata.
    """

    text: str
    sources: list[SearchResult] = field(default_factory=list)
    tokens: TokenUsage = field(default_factory=TokenUsage)
    cost: float = 0.0
    model: str = ""
    latency_ms: float = 0.0
    context: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert RAGResult to a serializable dictionary representation."""
        return {
            "text": self.text,
            "sources": [
                {
                    "id": s.id,
                    "score": s.score,
                    "content": s.content,
                    "metadata": _safe_serialize_val(s.metadata),
                }
                for s in self.sources
            ],
            "tokens": {
                "prompt_tokens": self.tokens.prompt_tokens,
                "completion_tokens": self.tokens.completion_tokens,
                "total_tokens": self.tokens.total_tokens,
            },
            "cost": self.cost,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "context": self.context,
        }

