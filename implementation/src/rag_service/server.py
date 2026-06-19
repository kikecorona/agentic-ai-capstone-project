"""``RAG_MCP`` — MCP frontend for the RAG Service (§8.2 / §9.1.2).

For the POC the RAG Service runs in-process inside whatever process imports
``RAGService`` directly (B&P, SD, or the validation script). This module
also exposes the *same* three methods over an MCP server so the contract
is identical for any future caller that wants to reach the service across
a process boundary — splitting it behind ``RAG_MCP``-over-HTTP is the
later optimization §8.5 mentions; the contract here doesn't change.

The MCP only exposes ``retrieve`` / ``index`` / ``delete``. The ToT
chunking sub-graph's internal nodes (``embed``, ``score_strategy``) are
NOT exposed externally per §9.1.2 — specialists never run that loop.

Run with::

    python -m src.rag_service.server [--chroma PATH]
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .service import RAGService
from .store import EmbeddingsStore, default_persist_path
from src.shared.llm import get_embedding_function
from src.shared.otel_client import OTelClient
from src.shared.service_log import get_logger

log = get_logger("rag.server")


def build_server(
    chroma_path: str | os.PathLike[str] | None = None,
    otel_db_path: str | os.PathLike[str] | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8102,
) -> tuple[FastMCP, RAGService]:
    """Build the FastMCP server bound to a ``RAGService`` rooted at the
    given Chroma persist path. Factored out for tests. ``host``/``port``
    apply only to HTTP-based transports."""

    persist_path = chroma_path or default_persist_path()
    log.info(
        f"RAG_MCP server starting chroma={persist_path} "
        f"otel_db={otel_db_path or os.environ.get('OTEL_DB_PATH', './data/otel/spans.db')} "
        f"audit_db={os.environ.get('AUDIT_DB_PATH', './data/audit/log.db')}"
    )
    store = EmbeddingsStore(persist_path, embedding_function=get_embedding_function())
    otel = OTelClient.from_env(otel_db_path) if otel_db_path else OTelClient.from_env()
    service = RAGService(store=store, otel=otel)

    mcp = FastMCP(
        name="rag-mcp",
        host=host,
        port=port,
        instructions=(
            "Shared RAG service for the BP and SD specialists. Index documents "
            "with `index(domain, source_uri, document)`; retrieve with "
            "`retrieve(query, domain_filter, mode)`. Specialists pass their "
            "own `domain` tag — the service trusts the tag and never "
            "cross-writes."
        ),
    )

    @mcp.tool(
        description=(
            "Retrieve grounded evidence + a composed answer (Auto-RAG, §9.1.3.1). "
            "domain_filter ∈ {bp, sd, both}; mode ∈ {query, background} is "
            "advisory metadata the caller branches on. Returns "
            "{status, answer, sources, retrieval_trail, grader_scores, "
            "index_quality_flags, rewrites_used}."
        ),
    )
    def retrieve(
        query: str,
        domain_filter: str = "both",
        mode: str = "query",
    ) -> dict[str, Any]:
        return service.retrieve(query=query, domain_filter=domain_filter, mode=mode)

    @mcp.tool(
        description=(
            "Index a document into the shared embeddings DB. Runs the ToT "
            "chunking-strategy selector (§9.1.3.2) to pick a chunking recipe, "
            "embeds, and persists chunks tagged with the caller's `domain`. "
            "Existing chunks for the same (domain, source_uri) are replaced "
            "atomically. Returns {chunks_indexed, chunking_strategy, "
            "embedding_revision, score, low_confidence, trail}."
        ),
    )
    def index(
        domain: str,
        source_uri: str,
        document: str,
        content_hash: str | None = None,
    ) -> dict[str, Any]:
        return service.index(
            domain=domain,
            source_uri=source_uri,
            document=document,
            content_hash=content_hash,
        )

    @mcp.tool(
        description=(
            "Invalidate all chunks for (domain, source_uri). Used when an "
            "input doc disappears or a generated page is retired."
        ),
    )
    def delete(domain: str, source_uri: str) -> dict[str, Any]:
        return service.delete(domain=domain, source_uri=source_uri)

    return mcp, service


def main() -> None:
    parser = argparse.ArgumentParser(description="RAG MCP server")
    parser.add_argument(
        "--chroma",
        default=os.environ.get("RAG_CHROMA_PATH"),
        help="Path to Chroma persistent directory (default: $RAG_CHROMA_PATH or ./data/rag/chroma)",
    )
    parser.add_argument(
        "--otel-db",
        default=os.environ.get("OTEL_DB_PATH"),
        help="Path to OTel SQLite store (default: $OTEL_DB_PATH or ./data/otel/spans.db)",
    )
    parser.add_argument(
        "--transport",
        default=os.environ.get("RAG_MCP_TRANSPORT", "stdio"),
        choices=("stdio", "sse", "streamable-http"),
        help="MCP transport (default: $RAG_MCP_TRANSPORT or stdio)",
    )
    parser.add_argument("--host", default=os.environ.get("RAG_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RAG_MCP_PORT", "8102")))
    args = parser.parse_args()

    server, _service = build_server(
        chroma_path=args.chroma,
        otel_db_path=args.otel_db,
        host=args.host,
        port=args.port,
    )
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
