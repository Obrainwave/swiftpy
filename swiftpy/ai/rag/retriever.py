from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from typing import Any

from swiftpy.ai.embeddings import EmbeddingsClient
from swiftpy.ai.vectorstore.base import SearchResult, VectorRecord, VectorStore
from swiftpy.core.container import Container, get_container


def _estimate_text_tokens(text: str) -> int:
    """Approximate token count cleanly (~4 chars per token)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _extract_document_text(doc: SearchResult | str | dict[str, Any], index: int) -> str:
    """Extract plain text content from a document or raise TypeError."""
    if isinstance(doc, SearchResult):
        return doc.content
    if isinstance(doc, str):
        return doc
    if isinstance(doc, dict):
        raw = doc.get("content", "")
        return "" if raw is None else str(raw)
    raise TypeError(f"Unsupported document type at index {index}: {type(doc).__name__}")


def chunk_text(
    text: str,
    chunk_size: int = 1000,
    overlap: int = 200,
) -> list[str]:
    """
    Split text into overlapping chunks bounded by `chunk_size` characters.

    Prefers splitting on whitespace boundaries when available while strictly
    enforcing ``len(chunk) <= chunk_size`` and monotonic forward progress.
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be a str, got {type(text).__name__}")
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError(f"chunk_size must be a positive integer, got {chunk_size!r}")
    if isinstance(overlap, bool) or not isinstance(overlap, int) or overlap < 0:
        raise ValueError(f"overlap must be a non-negative integer, got {overlap!r}")
    if overlap >= chunk_size:
        raise ValueError(f"overlap ({overlap}) must be smaller than chunk_size ({chunk_size})")

    if not text.strip():
        return []

    chunks: list[str] = []
    start = 0
    length = len(text)

    while start < length:
        while start < length and text[start].isspace():
            start += 1
        if start >= length:
            break

        end = min(start + chunk_size, length)
        if end < length and not text[end].isspace():
            split_at = text.rfind(" ", start, end)
            if split_at == -1:
                split_at = text.rfind("\n", start, end)
            if split_at > start:
                end = split_at

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= length:
            break

        next_start = end - overlap
        if next_start <= start:
            next_start = start + 1
        start = next_start

    return chunks


def truncate_to_token_budget[DocT: SearchResult | str | dict[str, Any]](
    documents: Sequence[DocT],
    max_tokens: int,
    token_estimator: Callable[[str], int] | None = None,
) -> list[DocT]:
    """
    Retain an ordered prefix of documents whose cumulative token estimate stays
    within `max_tokens`.
    """
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError(f"max_tokens must be a positive integer, got {max_tokens!r}")
    if token_estimator is not None and not callable(token_estimator):
        raise TypeError("token_estimator must be callable when provided")

    kept: list[DocT] = []
    used_tokens = 0
    for idx, doc in enumerate(documents):
        content = _extract_document_text(doc, idx)
        est = token_estimator(content) if token_estimator is not None else _estimate_text_tokens(content)
        if est < 0:
            raise ValueError(f"Token estimate must be non-negative, got {est}")
        if used_tokens + est > max_tokens:
            break
        kept.append(doc)
        used_tokens += est
    return kept


class Retriever:
    """
    Asynchronous document retriever for vector similarity search.

    Embeds incoming text queries asynchronously and queries the configured
    vector store to return ranked search result chunks.
    """

    def __init__(
        self,
        vectorstore: VectorStore,
        embeddings: EmbeddingsClient | None = None,
        top_k: int = 5,
        filter: dict[str, Any] | None = None,
        min_score: float | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> None:
        if top_k <= 0:
            raise ValueError(f"top_k must be greater than 0, got {top_k}")

        self._vectorstore = vectorstore
        if embeddings is None:
            try:
                container = get_container()
            except RuntimeError:
                container = Container()

            if container.has(EmbeddingsClient):
                self._embeddings = container.resolve(EmbeddingsClient)
            else:
                self._embeddings = EmbeddingsClient()
        else:
            self._embeddings = embeddings

        self._top_k = top_k
        self._filter = filter
        self._min_score = min_score
        self._model = model
        self._provider = provider

    @property
    def vectorstore(self) -> VectorStore:
        """The underlying vector store instance."""
        return self._vectorstore

    @property
    def embeddings(self) -> EmbeddingsClient:
        """The underlying embeddings client instance."""
        return self._embeddings

    @property
    def top_k(self) -> int:
        """Default top_k results to retrieve."""
        return self._top_k

    @property
    def filter(self) -> dict[str, Any] | None:
        """Default metadata filter."""
        return self._filter

    @property
    def min_score(self) -> float | None:
        """Optional minimum score threshold."""
        return self._min_score

    async def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        filter: dict[str, Any] | None = None,
        min_score: float | None = None,
    ) -> list[SearchResult]:
        """
        Embed the query asynchronously, search the vector store, and return ranked chunks.
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Query must be a non-empty string.")

        limit = top_k if top_k is not None else self._top_k
        if limit <= 0:
            raise ValueError(f"top_k must be greater than 0, got {limit}")

        active_filter = filter if filter is not None else self._filter
        cutoff = min_score if min_score is not None else self._min_score

        query_vector = await self._embeddings.embed(
            query,
            model=self._model,
            provider=self._provider,
        )

        results = await self._vectorstore.search(
            vector=query_vector,
            limit=limit,
            filter=active_filter,
        )

        if cutoff is not None:
            results = [r for r in results if r.score >= cutoff]

        return results

    async def add_documents(
        self,
        documents: Sequence[str | dict[str, Any] | VectorRecord],
        metadatas: Sequence[dict[str, Any]] | None = None,
        ids: Sequence[str] | None = None,
        chunk_size: int = 100,
        concurrency: int = 5,
    ) -> list[str]:
        """
        Convenience method to embed and ingest documents into the underlying vector store.

        Accepts a sequence of strings (texts), dictionaries, or VectorRecords.
        Returns the list of stored document identifiers.
        """
        if not documents:
            return []

        if ids is not None and len(ids) != len(documents):
            raise ValueError(
                f"Length of ids ({len(ids)}) does not match length of documents ({len(documents)})"
            )

        if metadatas is not None and len(metadatas) != len(documents):
            raise ValueError(
                f"Length of metadatas ({len(metadatas)}) does not match length of documents ({len(documents)})"
            )

        # 1. Validate & partition EVERY document up front (supporting mixed types)
        records_by_idx: dict[int, VectorRecord] = {}
        embed_indices: list[int] = []
        texts_to_embed: list[str] = []
        embed_ids: list[str] = []
        embed_metas: list[dict[str, Any]] = []

        for idx, doc in enumerate(documents):
            custom_id = str(ids[idx]) if ids is not None else f"doc_{idx}"
            custom_meta = copy.deepcopy(dict(metadatas[idx])) if metadatas is not None else {}

            if isinstance(doc, VectorRecord):
                merged_meta = (
                    {**custom_meta, **copy.deepcopy(doc.metadata)}
                    if custom_meta
                    else copy.deepcopy(doc.metadata)
                )
                records_by_idx[idx] = VectorRecord(
                    id=str(ids[idx]) if ids is not None else doc.id,
                    vector=list(doc.vector),
                    metadata=merged_meta,
                    content=doc.content,
                )
            elif isinstance(doc, str):
                embed_indices.append(idx)
                texts_to_embed.append(doc)
                embed_ids.append(custom_id)
                embed_metas.append(custom_meta)
            elif isinstance(doc, dict):
                raw_id = doc.get("id")
                item_id = str(raw_id) if raw_id is not None and str(raw_id) != "" else custom_id
                raw_content = doc.get("content", "")
                item_content = "" if raw_content is None else str(raw_content)
                doc_meta = doc.get("metadata")
                if doc_meta is not None and not isinstance(doc_meta, dict):
                    raise TypeError(
                        f"Document metadata at index {idx} must be a dict, got {type(doc_meta).__name__}"
                    )
                item_meta = (
                    {**custom_meta, **copy.deepcopy(doc_meta)} if doc_meta is not None else custom_meta
                )
                embed_indices.append(idx)
                texts_to_embed.append(item_content)
                embed_ids.append(item_id)
                embed_metas.append(item_meta)
            else:
                raise TypeError(
                    f"Unsupported document type at index {idx}: {type(doc).__name__}"
                )

        # 2. Batch-embed only text/dict entries asynchronously
        if texts_to_embed:
            vectors = await self._embeddings.embed_batch(
                texts=texts_to_embed,
                model=self._model,
                provider=self._provider,
                chunk_size=chunk_size,
                concurrency=concurrency,
            )
            for pos, orig_idx in enumerate(embed_indices):
                records_by_idx[orig_idx] = VectorRecord(
                    id=embed_ids[pos],
                    vector=vectors[pos],
                    metadata=embed_metas[pos],
                    content=texts_to_embed[pos],
                )

        # 3. Preserve original document ordering and upsert atomically
        records_to_upsert = [records_by_idx[i] for i in range(len(documents))]
        await self._vectorstore.upsert_batch(records_to_upsert)
        return [r.id for r in records_to_upsert]
