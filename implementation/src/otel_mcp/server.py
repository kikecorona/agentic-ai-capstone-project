"""POC OpenTelemetry MCP server (§8.5).

A thin OpenTelemetry collector fronted by an MCP. Exposes four tools over
stdio so any service in the architecture can record / inspect spans through
a uniform contract:

  * ``record_span``  — append a span produced by an inbound or outbound MCP call.
  * ``query_spans``  — read recent spans with simple filters.
  * ``get_metrics``  — derived per-(service, method) counts, status histogram,
                       p50 / p95 latencies. Source for §9.6 / §9.7 dashboards.
  * ``clear_spans``  — wipe the store (useful for the validation harness).

Production deployments would swap this for a real OTel collector (Tempo,
Honeycomb, etc.) without changing the MCP-shaped contract on the service
side — that is the point of fronting it with MCP.

Run with::

    python -m src.otel_mcp.server [--db PATH]

stdio is the default transport (matches how every other MCP in this POC
will be wired up via ``langchain-mcp-adapters``).
"""

from __future__ import annotations

import argparse
import os
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .store import Span, SpanStore


def build_server(
    db_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8101,
) -> tuple[FastMCP, SpanStore]:
    """Create the FastMCP server bound to a SpanStore at ``db_path``.

    Factored out of ``main`` so tests can drive the same tool surface
    without spawning a subprocess. ``host``/``port`` only apply when the
    server runs over HTTP-based transports (``streamable-http`` / ``sse``);
    they're ignored for ``stdio``.
    """
    store = SpanStore(db_path)
    mcp = FastMCP(
        name="otel-mcp",
        host=host,
        port=port,
        instructions=(
            "POC OpenTelemetry collector. Services emit spans for inbound and "
            "outbound MCP calls; this server persists them and surfaces derived "
            "metrics. Do NOT send raw query text or document content — payload_summary "
            "is for counts, IDs, and statuses only (see §9.6 privacy)."
        ),
    )

    # ---- record_span ------------------------------------------------------
    @mcp.tool(
        description=(
            "Record one span. Returns the persisted span_id (the server "
            "generates one if the caller did not supply it). Latency is "
            "computed from started_at/ended_at when not provided."
        ),
    )
    def record_span(
        service: str,
        mcp_method: str,
        started_at: float | None = None,
        ended_at: float | None = None,
        mcp_latency_ms: float | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        mcp_domain: str | None = None,
        mcp_status: str | None = None,
        payload_summary: dict[str, Any] | None = None,
        attributes: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        s_at = started_at if started_at is not None else now
        e_at = ended_at if ended_at is not None else now
        latency = (
            mcp_latency_ms
            if mcp_latency_ms is not None
            else max(0.0, (e_at - s_at) * 1000.0)
        )
        span = Span(
            span_id=span_id or uuid.uuid4().hex,
            trace_id=trace_id or uuid.uuid4().hex,
            parent_span_id=parent_span_id,
            service=service,
            mcp_method=mcp_method,
            mcp_domain=mcp_domain,
            mcp_status=mcp_status,
            mcp_latency_ms=float(latency),
            started_at=float(s_at),
            ended_at=float(e_at),
            payload_summary=payload_summary or {},
            attributes=attributes or {},
            error=error,
        )
        store.record(span)
        return {"span_id": span.span_id, "trace_id": span.trace_id}

    # ---- query_spans ------------------------------------------------------
    @mcp.tool(
        description=(
            "Query stored spans with optional filters. Returns at most "
            "``limit`` spans (default 100), newest first. Use this to "
            "rebuild a trace (filter by trace_id) or inspect what one "
            "service has been doing (filter by service / method / status)."
        ),
    )
    def query_spans(
        service: str | None = None,
        mcp_method: str | None = None,
        mcp_domain: str | None = None,
        mcp_status: str | None = None,
        trace_id: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        spans = store.query(
            service=service,
            mcp_method=mcp_method,
            mcp_domain=mcp_domain,
            mcp_status=mcp_status,
            trace_id=trace_id,
            since=since,
            until=until,
            limit=limit,
        )
        return {"count": len(spans), "spans": [s.to_dict() for s in spans]}

    # ---- get_metrics ------------------------------------------------------
    @mcp.tool(
        description=(
            "Derived metrics over the span stream: total span count, "
            "per-(service, method) counts, status histogram, and "
            "p50/p95/max/mean latency. The §9.7 online metrics dashboard "
            "composes from this."
        ),
    )
    def get_metrics(
        service: str | None = None,
        since: float | None = None,
        until: float | None = None,
    ) -> dict[str, Any]:
        return store.get_metrics(service=service, since=since, until=until)

    # ---- clear_spans ------------------------------------------------------
    @mcp.tool(
        description=(
            "Delete every persisted span. Used by the validation harness to "
            "give each test run a clean slate. Returns the number of rows "
            "removed."
        ),
    )
    def clear_spans() -> dict[str, int]:
        return {"deleted": store.clear()}

    return mcp, store


def main() -> None:
    parser = argparse.ArgumentParser(description="POC OpenTelemetry MCP server")
    parser.add_argument(
        "--db",
        default=os.environ.get("OTEL_DB_PATH", "./data/otel/spans.db"),
        help="Path to the SQLite span store (default: $OTEL_DB_PATH or ./data/otel/spans.db)",
    )
    parser.add_argument(
        "--transport",
        default=os.environ.get("OTEL_MCP_TRANSPORT", "stdio"),
        choices=("stdio", "sse", "streamable-http"),
        help="MCP transport (default: $OTEL_MCP_TRANSPORT or stdio)",
    )
    parser.add_argument("--host", default=os.environ.get("OTEL_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("OTEL_MCP_PORT", "8101")))
    args = parser.parse_args()

    server, _store = build_server(args.db, host=args.host, port=args.port)
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
