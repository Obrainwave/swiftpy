from __future__ import annotations

import asyncio
import math
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import Any

try:
    import httpx
    _RERANK_HTTP_ERRORS: tuple[type[Exception], ...] = (
        httpx.TimeoutException,
        httpx.RequestError,
    )
except ImportError:
    httpx = None
    _RERANK_HTTP_ERRORS = (TimeoutError, OSError)

from swiftpy.ai.exceptions import (
    AIAuthenticationError,
    AIConnectionError,
    AIProviderError,
    AIRateLimitError,
)
from swiftpy.ai.pool import AIHttpPool
from swiftpy.ai.vectorstore.base import SearchResult, sanitize_error_message
from swiftpy.core.config import Config


def _normalize_documents(
    documents: Sequence[SearchResult | str | dict[str, Any]],
) -> tuple[list[str], list[SearchResult]]:
    """
    Normalize document inputs into a pair of (texts, SearchResult instances).
    """
    texts: list[str] = []
    results: list[SearchResult] = []

    for idx, doc in enumerate(documents):
        if isinstance(doc, SearchResult):
            texts.append(doc.content)
            # Create a shallow copy to safely update score without mutating original
            results.append(
                SearchResult(
                    id=doc.id,
                    score=doc.score,
                    metadata=dict(doc.metadata),
                    content=doc.content,
                    vector=doc.vector,
                )
            )
        elif isinstance(doc, str):
            texts.append(doc)
            results.append(
                SearchResult(
                    id=f"doc_{idx}",
                    score=0.0,
                    metadata={},
                    content=doc,
                )
            )
        elif isinstance(doc, dict):
            content = str(doc.get("content", ""))
            texts.append(content)
            results.append(
                SearchResult(
                    id=str(doc.get("id", f"doc_{idx}")),
                    score=float(doc.get("score", 0.0)),
                    metadata=dict(doc.get("metadata") or {}),
                    content=content,
                )
            )
        else:
            raise TypeError(
                f"Unsupported document type at index {idx}: {type(doc).__name__}"
            )

    return texts, results


class BaseReranker(ABC):
    """
    Abstract base class for RAG reranker models and services.
    """

    @abstractmethod
    async def rerank(
        self,
        query: str,
        documents: Sequence[SearchResult | str | dict[str, Any]],
        top_n: int | None = None,
    ) -> list[SearchResult]:
        """
        Rerank a sequence of documents against a query and return them sorted by relevance score.
        """


class CohereReranker(BaseReranker):
    """
    Cohere Rerank API adapter.

    Uses SwiftPY's shared AIHttpPool for async non-blocking HTTP requests
    with zero third-party vendor SDK dependencies.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "rerank-v3.5",
        base_url: str = "https://api.cohere.com/v2/rerank",
        http_pool: AIHttpPool | None = None,
        config: Config | None = None,
        timeout: float = 30.0,
    ) -> None:
        cfg = config
        if cfg is None:
            try:
                cfg = Config.get_instance()
            except RuntimeError:
                cfg = Config()

        self._api_key = (
            api_key
            or str(cfg.get("ai.rerankers.cohere.api_key", ""))
            or (os.getenv("COHERE_API_KEY") or "")
        ).strip()

        self._model = model or str(
            cfg.get("ai.rerankers.cohere.model", "rerank-v3.5")
        )
        self._base_url = str(
            cfg.get("ai.rerankers.cohere.base_url", base_url)
        ).rstrip("/")
        self._timeout = float(cfg.get("ai.rerankers.cohere.timeout", timeout))
        self._http_pool = http_pool or AIHttpPool.get_instance()

    @property
    def model(self) -> str:
        return self._model

    async def rerank(
        self,
        query: str,
        documents: Sequence[SearchResult | str | dict[str, Any]],
        top_n: int | None = None,
    ) -> list[SearchResult]:
        if not documents:
            return []

        if not isinstance(query, str) or not query.strip():
            raise ValueError("Query must be a non-empty string.")

        if top_n is not None and top_n <= 0:
            raise ValueError(f"top_n must be greater than 0, got {top_n}")

        texts, results = _normalize_documents(documents)
        effective_top_n = min(top_n, len(texts)) if top_n is not None else len(texts)

        if not self._api_key:
            raise AIAuthenticationError(
                "Cohere API key is missing. Configure 'ai.rerankers.cohere.api_key' or set COHERE_API_KEY.",
                provider="cohere",
            )

        client = self._http_pool.get_client()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self._model,
            "query": query,
            "documents": texts,
            "top_n": effective_top_n,
        }

        try:
            response = await client.post(
                self._base_url,
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
        except _RERANK_HTTP_ERRORS as exc:
            raise AIConnectionError(
                f"Failed to connect to Cohere Rerank API: {exc}",
                provider="cohere",
            ) from exc

        body_text = response.text
        clean_text = sanitize_error_message(body_text)

        if response.status_code in (401, 403):
            raise AIAuthenticationError(
                f"Cohere authentication failed ({response.status_code}): {clean_text}",
                provider="cohere",
                status_code=response.status_code,
                response_body=clean_text,
            )
        if response.status_code == 400:
            raise AIProviderError(
                f"Cohere invalid request (400): {clean_text}",
                provider="cohere",
                status_code=400,
                response_body=clean_text,
            )
        if response.status_code == 404:
            raise AIProviderError(
                f"Cohere model or resource not found (404): {clean_text}",
                provider="cohere",
                status_code=404,
                response_body=clean_text,
            )
        if response.status_code == 413:
            raise AIProviderError(
                f"Cohere payload too large (413): {clean_text}",
                provider="cohere",
                status_code=413,
                response_body=clean_text,
            )
        if response.status_code == 422:
            raise AIProviderError(
                f"Cohere unprocessable entity (422): {clean_text}",
                provider="cohere",
                status_code=422,
                response_body=clean_text,
            )
        if response.status_code == 429:
            retry_after_hdr = response.headers.get("Retry-After")
            retry_after = float(retry_after_hdr) if retry_after_hdr else None
            raise AIRateLimitError(
                f"Cohere rate limit exceeded (429): {clean_text}",
                provider="cohere",
                status_code=response.status_code,
                response_body=clean_text,
                retry_after=retry_after,
            )
        if response.status_code == 503:
            raise AIProviderError(
                f"Cohere service unavailable (503): {clean_text}",
                provider="cohere",
                status_code=503,
                response_body=clean_text,
            )
        if response.status_code >= 400:
            raise AIProviderError(
                f"Cohere Rerank API error ({response.status_code}): {clean_text}",
                provider="cohere",
                status_code=response.status_code,
                response_body=clean_text,
            )

        try:
            data = response.json()
        except Exception as exc:
            raise AIProviderError(
                f"Cohere returned non-JSON response: {clean_text[:200]}",
                provider="cohere",
            ) from exc

        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            raise AIProviderError(
                "Cohere response missing 'results' list",
                provider="cohere",
            )

        reranked_results: list[SearchResult] = []
        seen_indices: set[int] = set()
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            idx_val = item.get("index")
            if not isinstance(idx_val, int) or idx_val < 0 or idx_val >= len(results):
                continue
            if idx_val in seen_indices:
                continue
            seen_indices.add(idx_val)

            score_val = item.get("relevance_score")
            score = float(score_val) if isinstance(score_val, (int, float)) else 0.0
            res = results[idx_val]
            res.score = score
            reranked_results.append(res)

        return reranked_results[:effective_top_n]


def _compute_lexical_cross_scores(
    query: str,
    texts: list[str],
) -> list[float]:
    """
    Deterministic cross-encoder relevance scoring function.

    Computes query-document cross-entropy scores using query token overlap,
    exact phrase matching bonuses, BM25-style term frequency saturation,
    and document length normalization.
    """
    clean_query = query.lower().strip()
    query_tokens = [w for w in re.split(r"\W+", clean_query) if w]
    if not query_tokens:
        return [0.0] * len(texts)

    query_set = set(query_tokens)
    scores: list[float] = []

    for text in texts:
        clean_text = text.lower().strip()
        doc_tokens = [w for w in re.split(r"\W+", clean_text) if w]
        doc_len = len(doc_tokens)
        if doc_len == 0:
            scores.append(0.0)
            continue

        # 1. Exact phrase match bonus
        phrase_bonus = 0.4 if clean_query in clean_text else 0.0

        # 2. Token overlap & frequency
        match_count = 0
        freq_sum = 0.0
        doc_token_counts: dict[str, int] = {}
        for t in doc_tokens:
            doc_token_counts[t] = doc_token_counts.get(t, 0) + 1

        for qt in query_tokens:
            if qt in doc_token_counts:
                match_count += 1
                tf = doc_token_counts[qt]
                # BM25-style term frequency saturation
                freq_sum += (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * (doc_len / 50.0)))

        coverage = match_count / len(query_tokens)
        normalized_freq = freq_sum / (len(query_tokens) * 2.0)

        # 3. Position bonus (earlier appearance gives slight bonus)
        pos_bonus = 0.0
        for qt in query_set:
            first_idx = clean_text.find(qt)
            if first_idx != -1:
                pos_bonus += max(0.0, 0.1 * (1.0 - min(first_idx, 500) / 500.0))
        pos_bonus = min(0.1, pos_bonus / max(1, len(query_set)))

        raw_score = (
            0.5 * coverage
            + 0.3 * min(1.0, normalized_freq)
            + phrase_bonus
            + pos_bonus
        )
        # Sigmoid clamp to [0.0, 1.0]
        score = 1.0 / (1.0 + math.exp(-3.0 * (raw_score - 0.5)))
        scores.append(round(score, 6))

    return scores


class CrossEncoderReranker(BaseReranker):
    """
    Reranker for in-process or model-based document scoring.

    Can be initialized with:
    1. A custom scoring callable `scoring_fn(query, texts) -> Sequence[float]`.
    2. A SentenceTransformers `CrossEncoder` model duck-typed via `.predict([[query, text], ...])`.
    3. Default built-in deterministic heuristic cross-scorer (BM25-style frequency saturation,
       token overlap, exact phrase match bonuses, and position weighting).

    All synchronous CPU computations are offloaded to an executor to adhere strictly
    to SwiftPY's event-loop non-blocking concurrency rule.
    """

    def __init__(
        self,
        model: Any | None = None,
        scoring_fn: Callable[[str, list[str]], Sequence[float]] | None = None,
    ) -> None:
        self._model = model
        self._scoring_fn = scoring_fn

    async def rerank(
        self,
        query: str,
        documents: Sequence[SearchResult | str | dict[str, Any]],
        top_n: int | None = None,
    ) -> list[SearchResult]:
        if not documents:
            return []

        if not isinstance(query, str) or not query.strip():
            raise ValueError("Query must be a non-empty string.")

        if top_n is not None and top_n <= 0:
            raise ValueError(f"top_n must be greater than 0, got {top_n}")

        texts, results = _normalize_documents(documents)
        effective_top_n = min(top_n, len(texts)) if top_n is not None else len(texts)

        # Offload CPU-heavy scoring to thread executor to prevent event loop blocking
        loop = asyncio.get_running_loop()
        scores = await loop.run_in_executor(
            None,
            self._score_sync,
            query,
            texts,
        )

        for i, score in enumerate(scores):
            results[i].score = float(score)

        # Stable sort descending by relevance score (preserves relative order for ties)
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:effective_top_n]

    def _score_sync(self, query: str, texts: list[str]) -> list[float]:
        """Synchronous scoring execution inside worker thread."""
        if not texts:
            return []

        raw_scores: Any
        if self._scoring_fn is not None:
            raw_scores = self._scoring_fn(query, texts)
        elif self._model is not None and hasattr(self._model, "predict"):
            # SentenceTransformers CrossEncoder model duck typing
            pairs = [[query, t] for t in texts]
            raw_scores = self._model.predict(pairs)
        else:
            # Built-in deterministic heuristic scoring
            raw_scores = _compute_lexical_cross_scores(query, texts)

        if not hasattr(raw_scores, "__len__") or not hasattr(raw_scores, "__iter__"):
            raise TypeError(
                f"Scoring engine must return an iterable sequence of floats, got {type(raw_scores).__name__}"
            )

        if len(raw_scores) != len(texts):
            raise ValueError(
                f"Scoring engine returned {len(raw_scores)} scores for {len(texts)} documents"
            )

        validated: list[float] = []
        for i, s in enumerate(raw_scores):
            if not isinstance(s, (int, float)) or isinstance(s, bool):
                raise TypeError(
                    f"Score at index {i} must be a numeric float, got {type(s).__name__}"
                )
            fs = float(s)
            if math.isnan(fs) or math.isinf(fs):
                raise ValueError(f"Score at index {i} is NaN or Infinite: {fs}")
            validated.append(fs)

        return validated
