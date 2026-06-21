"""RAG Service — the public face for B&P / SD specialists (§9.1.2).

A single ``RAGService`` class exposes the three methods the architecture
calls out:

  * ``retrieve`` — drives the Auto-RAG loop (§9.1.3.1) over the shared
    Embeddings Database. Returns ``status``, ``answer``, ``sources``,
    ``retrieval_trail``, ``grader_scores``, and ``index_quality_flags``.
  * ``index`` — runs the ToT chunking-strategy selector (§9.1.3.2) on a
    new or changed document, embeds the winning chunks, and persists them
    tagged with the caller's ``domain``. Existing chunks for the same
    ``(domain, source_uri)`` are replaced atomically.
  * ``delete`` — invalidates all chunks for a removed source.

Span emission is wired in here (§9.6) so every inbound MCP call ends up
in the OTel store with method, domain, status, latency, and a payload
summary that respects the privacy rule (counts and IDs only — never raw
queries or document content).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any

from .auto_rag import (
    AutoRAGConfig,
    AutoRAGResult,
    build_auto_rag_graph,
    run_auto_rag,
)
from .chunking import ToTResult, new_embedding_revision, select_chunking_strategy
from .store import EmbeddingsStore, default_persist_path
from src.shared.llm import get_embedding_function
from src.shared.otel_client import OTelClient
from src.shared.service_log import get_logger

log = get_logger("rag.service")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToTConfig:
    """ToT chunking-strategy caps from §7.4 / §9.1.3.2.

    Defaults match the POC values; production deployments can tighten or
    widen them via the ``RAG_TOT_*`` env vars."""
    k: int = int(os.environ.get("RAG_TOT_K", "4"))
    beam: int = int(os.environ.get("RAG_TOT_BEAM", "2"))
    depth: int = int(os.environ.get("RAG_TOT_DEPTH", "2"))
    threshold: float = 0.7
    probe_count: int = 4


def default_auto_rag_config() -> AutoRAGConfig:
    return AutoRAGConfig(
        rewrite_budget=int(os.environ.get("RAG_REWRITE_BUDGET", "2")),
        top_k=int(os.environ.get("RAG_TOP_K", "10")),
        grade_threshold=float(os.environ.get("RAG_GRADE_THRESHOLD", "2.0")),
    )


SERVICE_NAME = "rag_service"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class RAGService:
    """In-process RAG Service (POC §8.5).

    Exposes the three MCP-shaped methods callers depend on. The same class
    backs the stdio MCP server in :py:mod:`src.rag_service.server` so the
    contract is identical whether the caller is in-process or out-of-process.
    """

    def __init__(
        self,
        store: EmbeddingsStore | None = None,
        *,
        otel: OTelClient | None = None,
        auto_rag_config: AutoRAGConfig | None = None,
        tot_config: ToTConfig | None = None,
    ):
        self._store = store or EmbeddingsStore(
            default_persist_path(),
            embedding_function=get_embedding_function(),
        )
        self._otel = otel or OTelClient.from_env()
        self._auto_rag_cfg = auto_rag_config or default_auto_rag_config()
        self._tot_cfg = tot_config or ToTConfig()
        self._graph = build_auto_rag_graph(self._store, self._auto_rag_cfg)

    # -- index --------------------------------------------------------------

    def index(
        self,
        *,
        domain: str,
        source_uri: str,
        document: str,
        content_hash: str | None = None,
    ) -> dict[str, Any]:
        """Run the ToT chunking selector + persist chunks (§9.1.2 ``index``).

        Returns ``{chunks_indexed, chunking_strategy, embedding_revision,
        low_confidence, score, trail}``. ``low_confidence`` is true when
        no candidate cleared the ToT threshold; the caller can decide
        whether to surface a follow-up or accept the imperfect index
        (per §9.1.3.2).
        """
        if domain not in {"bp", "sd"}:
            log.error(f"index: unsupported domain {domain!r}; expected bp or sd")
            raise ValueError(f"index: unsupported domain {domain!r}; expected bp or sd")
        if not source_uri:
            log.error("index: source_uri required")
            raise ValueError("index: source_uri required")
        if not document or not document.strip():
            log.error(f"index: document is empty (domain={domain}, source_uri={source_uri})")
            raise ValueError("index: document is empty")

        log.info(f"index start domain={domain} source_uri={source_uri} doc_chars={len(document)}")
        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="index",
            mcp_domain=domain,
        ) as span:
            ch = content_hash or _sha1(document)
            # Wrap the ToT chunking selection in its own child span so
            # the dashboard's "ToT branch success rate" KPI has rows to
            # aggregate. Status mirrors the loop outcome — ``ok`` when a
            # candidate cleared the threshold, ``low_confidence`` when
            # the depth cap was reached without a winner. The ``index``
            # parent span keeps tracking the overall index outcome.
            with self._otel.span(
                service=SERVICE_NAME,
                mcp_method="tot.chunking",
                mcp_domain=domain,
            ) as tot_span:
                tot: ToTResult = select_chunking_strategy(
                    document,
                    k=self._tot_cfg.k,
                    beam=self._tot_cfg.beam,
                    depth=self._tot_cfg.depth,
                    threshold=self._tot_cfg.threshold,
                    probe_count=self._tot_cfg.probe_count,
                )
                tot_span.set_status("low_confidence" if tot.low_confidence else "ok")
                tot_span.set_attributes({
                    "chunking_strategy": tot.strategy.label(),
                    "tot_score": round(tot.score, 3),
                    "tot_branches": len(tot.trail),
                    "tot_depth": self._tot_cfg.depth,
                    "tot_beam": self._tot_cfg.beam,
                })
            embedding_revision = new_embedding_revision()
            chunks_indexed = self._store.upsert(
                domain=domain,
                source_uri=source_uri,
                content_hash=ch,
                chunks=tot.chunks,
                chunking_strategy=tot.strategy.label(),
                embedding_revision=embedding_revision,
            )
            span.set_status("ok" if not tot.low_confidence else "low_confidence")
            span.set_payload_summary({
                "chunks_indexed": chunks_indexed,
                "source_uri": source_uri,
                "content_hash": ch,
            })
            span.set_attributes({
                "chunking_strategy": tot.strategy.label(),
                "tot_score": round(tot.score, 3),
                "tot_branches": len(tot.trail),
                "embedding_revision": embedding_revision,
            })
            log.info(
                f"index done source_uri={source_uri} chunks={chunks_indexed} "
                f"strategy={tot.strategy.label()} score={tot.score:.3f}"
                + (" low_confidence" if tot.low_confidence else "")
            )
            return {
                "chunks_indexed": chunks_indexed,
                "chunking_strategy": tot.strategy.label(),
                "embedding_revision": embedding_revision,
                "score": round(tot.score, 3),
                "low_confidence": tot.low_confidence,
                "trail": tot.trail,
            }

    # -- retrieve -----------------------------------------------------------

    def retrieve(
        self,
        *,
        query: str,
        domain_filter: str = "both",
        mode: str = "query",
    ) -> dict[str, Any]:
        """Drive the Auto-RAG loop (§9.1.2 ``retrieve``).

        ``domain_filter ∈ {bp, sd, both}``; ``mode ∈ {query, background}``
        is advisory metadata that the caller branches on (the loop logic
        is identical, only the caller's reaction to ``exhausted`` differs).
        """
        if domain_filter not in {"bp", "sd", "both"}:
            log.error(f"retrieve: unsupported domain_filter {domain_filter!r}")
            raise ValueError(f"retrieve: unsupported domain_filter {domain_filter!r}")
        if mode not in {"query", "background"}:
            log.error(f"retrieve: unsupported mode {mode!r}")
            raise ValueError(f"retrieve: unsupported mode {mode!r}")
        if not query or not query.strip():
            log.error(f"retrieve: query is empty (domain_filter={domain_filter}, mode={mode})")
            raise ValueError("retrieve: query is empty")

        # Note: query text itself is NOT logged (§9.6 privacy — payload summaries
        # only) but domain_filter / mode are safe metadata.
        log.info(f"retrieve start domain_filter={domain_filter} mode={mode} query_chars={len(query)}")
        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="retrieve",
            mcp_domain=domain_filter,
        ) as span:
            span.set_attribute("mode", mode)
            result: AutoRAGResult = run_auto_rag(
                self._graph,
                query=query,
                domain_filter=None if domain_filter == "both" else domain_filter,
                mode=mode,
                cfg=self._auto_rag_cfg,
            )
            span.set_status(result.status)
            span.set_payload_summary({
                "sources_count": len(result.sources),
                "rewrites_used": result.rewrites_used,
                "index_quality_flags": len(result.index_quality_flags),
            })
            grader_max = max(
                (s.get("score", 0.0) for s in result.grader_scores if isinstance(s, dict) and "score" in s),
                default=0.0,
            )
            span.set_attributes({
                "rewrites_used": result.rewrites_used,
                "grader_max_score": grader_max,
            })
            log.info(
                f"retrieve done status={result.status} sources={len(result.sources)} "
                f"rewrites_used={result.rewrites_used} "
                f"index_quality_flags={len(result.index_quality_flags)}"
            )
            return {
                "status": result.status,
                "answer": result.answer,
                "sources": result.sources,
                "retrieval_trail": result.retrieval_trail,
                "grader_scores": result.grader_scores,
                "index_quality_flags": result.index_quality_flags,
                "rewrites_used": result.rewrites_used,
            }

    # -- delete -------------------------------------------------------------

    def delete(self, *, domain: str, source_uri: str) -> dict[str, Any]:
        """Invalidate all chunks for a removed source (§9.1.2 ``delete``)."""
        if domain not in {"bp", "sd"}:
            log.error(f"delete: unsupported domain {domain!r}")
            raise ValueError(f"delete: unsupported domain {domain!r}")
        log.info(f"delete start domain={domain} source_uri={source_uri}")
        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="delete",
            mcp_domain=domain,
        ) as span:
            removed = self._store.delete(domain=domain, source_uri=source_uri)
            span.set_status("ok")
            span.set_payload_summary({"chunks_deleted": removed, "source_uri": source_uri})
            log.info(f"delete done domain={domain} source_uri={source_uri} chunks_deleted={removed}")
            return {"chunks_deleted": removed}

    # -- introspection ------------------------------------------------------

    @property
    def store(self) -> EmbeddingsStore:
        return self._store


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()
