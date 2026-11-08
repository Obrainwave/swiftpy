from __future__ import annotations

from swiftpy.ai.rag.context_formatter import (
    ContextFormatter,
    NumberedListFormatter,
    PlainConcatenationFormatter,
    XMLBlocksFormatter,
)
from swiftpy.ai.rag.pipeline_builder import (
    DEFAULT_PROMPT_TEMPLATE,
    DEFAULT_SYSTEM_PROMPT,
    RAGPipelineBuilder,
)
from swiftpy.ai.rag.reranker import (
    BaseReranker,
    CohereReranker,
    CrossEncoderReranker,
)
from swiftpy.ai.rag.result import (
    DEFAULT_MODEL_PRICING,
    RAGResult,
    calculate_token_cost,
    register_model_pricing,
)
from swiftpy.ai.rag.retriever import Retriever


def rag() -> RAGPipelineBuilder:
    """Convenience factory returning a fluent RAGPipelineBuilder."""
    return RAGPipelineBuilder()


__all__ = [
    "DEFAULT_MODEL_PRICING",
    "DEFAULT_PROMPT_TEMPLATE",
    "DEFAULT_SYSTEM_PROMPT",
    "BaseReranker",
    "CohereReranker",
    "ContextFormatter",
    "CrossEncoderReranker",
    "NumberedListFormatter",
    "PlainConcatenationFormatter",
    "RAGPipelineBuilder",
    "RAGResult",
    "Retriever",
    "XMLBlocksFormatter",
    "calculate_token_cost",
    "rag",
    "register_model_pricing",
]

