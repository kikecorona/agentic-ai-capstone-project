"""Shared Embeddings Database — the single store owned by the RAG Service.

§9.1.1 / §9.1.3.2: one Chroma collection holds every chunk in the system.
Each row carries a ``domain`` tag (``bp`` or ``sd``) plus the source URI,
content hash, and the chunking strategy that produced it. Cross-domain
isolation is enforced by the metadata filter on every read; the collection
itself is shared so a future specialist (e.g., Security) plugs in by
reserving a new ``domain`` value.

This module is the *only* place in the codebase that calls Chroma. Every
other RAG component talks to ``EmbeddingsStore`` instead of Chroma APIs
directly so the vector store is swappable later.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import chromadb
from chromadb.api.types import EmbeddingFunction


COLLECTION_NAME = "rag_chunks"


@dataclass(frozen=True)
class StoredChunk:
    """A retrieval result returned to the Auto-RAG loop."""

    chunk_id: str
    domain: str
    source_uri: str
    text: str
    distance: float
    metadata: dict[str, Any]


def chunk_id(domain: str, source_uri: str, ordinal: int, content_hash: str) -> str:
    """Deterministic chunk id so re-indexing the same source replaces in place."""
    base = f"{domain}::{source_uri}::{ordinal}::{content_hash}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:24]


class EmbeddingsStore:
    """Chroma-backed shared embeddings store.

    The schema is intentionally minimal — every field beyond the chunk
    text is metadata, so adding new tags (e.g., ``content_hash`` revisions
    or per-page placeholders) is just a new key in the metadata dict.
    """

    def __init__(self, persist_path: str | Path, embedding_function: EmbeddingFunction):
        self.persist_path = Path(persist_path)
        self.persist_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self.persist_path))
        self._collection = self._client.get_or_create_collection(
            COLLECTION_NAME,
            embedding_function=embedding_function,
            metadata={"hnsw:space": "cosine"},
        )

    # -- Writes -------------------------------------------------------------

    def upsert(
        self,
        *,
        domain: str,
        source_uri: str,
        content_hash: str,
        chunks: list[str],
        chunking_strategy: str,
        embedding_revision: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> int:
        """Replace every chunk for ``(domain, source_uri)`` atomically.

        Returns the number of chunks written. Existing chunks for the same
        ``(domain, source_uri)`` are deleted first per §9.1.2 ("Existing
        chunks for the same (domain, source_uri) are replaced atomically").
        """
        self.delete(domain=domain, source_uri=source_uri)
        if not chunks:
            return 0
        ids = [chunk_id(domain, source_uri, i, content_hash) for i in range(len(chunks))]
        metadatas: list[dict[str, Any]] = []
        for i, _ in enumerate(chunks):
            md: dict[str, Any] = {
                "domain": domain,
                "source_uri": source_uri,
                "content_hash": content_hash,
                "chunking_strategy": chunking_strategy,
                "embedding_revision": embedding_revision,
                "ordinal": i,
            }
            if extra_metadata:
                md.update(extra_metadata)
            metadatas.append(md)
        self._collection.add(documents=chunks, metadatas=metadatas, ids=ids)
        return len(chunks)

    def delete(self, *, domain: str, source_uri: str) -> int:
        """Remove every chunk tagged ``(domain, source_uri)``."""
        before = self._count(domain=domain, source_uri=source_uri)
        if before:
            self._collection.delete(where={"$and": [{"domain": domain}, {"source_uri": source_uri}]})
        return before

    def clear_domain(self, domain: str) -> int:
        """Test/dev helper — drop everything in a domain. Not exposed via MCP."""
        before = self._count(domain=domain)
        if before:
            self._collection.delete(where={"domain": domain})
        return before

    # -- Reads --------------------------------------------------------------

    def query(
        self,
        *,
        query_text: str,
        domain_filter: str | None,
        top_k: int,
    ) -> list[StoredChunk]:
        """Similarity query over the shared collection.

        ``domain_filter ∈ {'bp', 'sd', 'both', None}`` matches §9.1.2 —
        ``both`` and ``None`` skip the filter so cross-domain queries see
        the whole index without any merge step.
        """
        where: dict[str, Any] | None = None
        if domain_filter and domain_filter != "both":
            where = {"domain": domain_filter}
        res = self._collection.query(
            query_texts=[query_text],
            n_results=max(1, int(top_k)),
            where=where,
        )
        return list(self._unpack(res))

    def fetch(self, chunk_ids: Iterable[str]) -> list[StoredChunk]:
        """Look up chunks by id — used by the Auto-RAG loop to re-fetch
        the closest matches when the rewrite budget is exhausted."""
        ids = list(chunk_ids)
        if not ids:
            return []
        got = self._collection.get(ids=ids, include=["documents", "metadatas"])
        out: list[StoredChunk] = []
        for cid, doc, md in zip(got.get("ids", []), got.get("documents", []), got.get("metadatas", [])):
            md = md or {}
            out.append(
                StoredChunk(
                    chunk_id=cid,
                    domain=md.get("domain", ""),
                    source_uri=md.get("source_uri", ""),
                    text=doc or "",
                    distance=0.0,
                    metadata=md,
                )
            )
        return out

    def count(self, *, domain: str | None = None) -> int:
        return self._count(domain=domain)

    # -- Internal -----------------------------------------------------------

    def _count(self, *, domain: str | None = None, source_uri: str | None = None) -> int:
        """Counts using ``get`` because Chroma 1.x removed ``count`` with where."""
        where: dict[str, Any] | None = None
        if domain and source_uri:
            where = {"$and": [{"domain": domain}, {"source_uri": source_uri}]}
        elif domain:
            where = {"domain": domain}
        elif source_uri:
            where = {"source_uri": source_uri}
        if where is None:
            return self._collection.count()
        got = self._collection.get(where=where, include=[])
        return len(got.get("ids", []) or [])

    @staticmethod
    def _unpack(res: dict[str, Any]) -> Iterable[StoredChunk]:
        ids_batches = res.get("ids") or []
        docs_batches = res.get("documents") or []
        meta_batches = res.get("metadatas") or []
        dist_batches = res.get("distances") or []
        if not ids_batches:
            return
        # Single query → batch index 0.
        ids = ids_batches[0]
        docs = docs_batches[0] if docs_batches else [""] * len(ids)
        metas = meta_batches[0] if meta_batches else [{}] * len(ids)
        dists = dist_batches[0] if dist_batches else [0.0] * len(ids)
        for cid, doc, md, dist in zip(ids, docs, metas, dists):
            md = md or {}
            yield StoredChunk(
                chunk_id=cid,
                domain=md.get("domain", ""),
                source_uri=md.get("source_uri", ""),
                text=doc or "",
                distance=float(dist),
                metadata=md,
            )


def default_persist_path() -> Path:
    return Path(os.environ.get("RAG_CHROMA_PATH", "./data/rag/chroma")).resolve()
