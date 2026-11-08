from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from swiftpy.ai.embeddings import EmbeddingsClient
from swiftpy.ai.vectorstore.base import SearchResult, VectorRecord, VectorStore
from swiftpy.core.container import Container, get_container


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

        # 1. If documents are already VectorRecords, upsert directly
        if isinstance(documents[0], VectorRecord):
            records = [doc for doc in documents if isinstance(doc, VectorRecord)]
            await self._vectorstore.upsert_batch(records)
            return [r.id for r in records]

        # 2. Extract contents, IDs, and metadatas
        texts_to_embed: list[str] = []
        doc_ids: list[str] = []
        doc_metas: list[dict[str, Any]] = []

        for idx, doc in enumerate(documents):
            custom_id = str(ids[idx]) if ids and idx < len(ids) else f"doc_{idx}"
            custom_meta = dict(metadatas[idx]) if metadatas and idx < len(metadatas) else {}

            if isinstance(doc, str):
                texts_to_embed.append(doc)
                doc_ids.append(custom_id)
                doc_metas.append(custom_meta)
            elif isinstance(doc, dict):
                item_id = str(doc.get("id") or custom_id)
                item_content = str(doc.get("content", ""))
                item_meta = dict(doc.get("metadata") or custom_meta)
                texts_to_embed.append(item_content)
                doc_ids.append(item_id)
                doc_metas.append(item_meta)
            else:
                raise TypeError(
                    f"Unsupported document type at index {idx}: {type(doc).__name__}"
                )

        # 3. Generate embeddings in batch asynchronously
        vectors = await self._embeddings.embed_batch(
            texts=texts_to_embed,
            model=self._model,
            provider=self._provider,
            chunk_size=chunk_size,
            concurrency=concurrency,
        )

        # 4. Construct VectorRecords
        records_to_upsert: list[VectorRecord] = [
            VectorRecord(
                id=doc_ids[i],
                vector=vectors[i],
                metadata=doc_metas[i],
                content=texts_to_embed[i],
            )
            for i in range(len(texts_to_embed))
        ]

        # 5. Upsert batch into vector store
        await self._vectorstore.upsert_batch(records_to_upsert)
        return doc_ids
