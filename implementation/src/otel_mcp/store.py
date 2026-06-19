"""SQLite-backed span store for the POC OpenTelemetry MCP.

Implements the audit storage described in §9.6 of the architecture spec
(`PROJECT_LOW_LEVEL_DESIGN.md`). The schema captures the span attributes the
spec lists explicitly:

  * ``mcp_method``        — inbound or outbound MCP method (e.g. ``retrieve``).
  * ``mcp_domain``        — for RAG calls: ``bp`` / ``sd`` / ``both``.
  * ``mcp_status``        — e.g. ``ok`` / ``low_confidence`` / ``exhausted``.
  * ``mcp_latency_ms``    — wall-clock of the wrapped call.
  * ``trace_id``          — propagated through MCP envelopes for end-to-end
                            traces (Portal → Orchestrator → B&P → RAG).
  * ``payload_summary``   — counts, IDs, status. **Never** raw queries or
                            document content (§9.6 privacy).
  * ``attributes``        — free-form JSON: rewrite count, grader scores,
                            faithfulness pass/fail, ToT branch count, etc.

The store is a plain Python class — the MCP server (``server.py``) and the
in-process OTel client (``shared.otel_client``) both layer on top of it.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
    span_id        TEXT PRIMARY KEY,
    trace_id       TEXT NOT NULL,
    parent_span_id TEXT,
    service        TEXT NOT NULL,
    mcp_method     TEXT NOT NULL,
    mcp_domain     TEXT,
    mcp_status     TEXT,
    mcp_latency_ms REAL NOT NULL,
    started_at     REAL NOT NULL,
    ended_at       REAL NOT NULL,
    payload_summary TEXT,
    attributes     TEXT,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_spans_trace   ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_service ON spans(service, mcp_method);
CREATE INDEX IF NOT EXISTS idx_spans_started ON spans(started_at);
"""


@dataclass
class Span:
    """One OTel span as recorded by a service.

    All fields except ``span_id`` / ``trace_id`` / ``service`` / ``mcp_method``
    are optional so the validation script and ad-hoc emitters can produce a
    minimal span without tripping the schema.
    """

    span_id: str
    trace_id: str
    service: str
    mcp_method: str
    started_at: float
    ended_at: float
    mcp_latency_ms: float
    parent_span_id: str | None = None
    mcp_domain: str | None = None
    mcp_status: str | None = None
    payload_summary: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_row(self) -> tuple:
        return (
            self.span_id,
            self.trace_id,
            self.parent_span_id,
            self.service,
            self.mcp_method,
            self.mcp_domain,
            self.mcp_status,
            self.mcp_latency_ms,
            self.started_at,
            self.ended_at,
            json.dumps(self.payload_summary, default=str) if self.payload_summary else None,
            json.dumps(self.attributes, default=str) if self.attributes else None,
            self.error,
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Span":
        return cls(
            span_id=row["span_id"],
            trace_id=row["trace_id"],
            parent_span_id=row["parent_span_id"],
            service=row["service"],
            mcp_method=row["mcp_method"],
            mcp_domain=row["mcp_domain"],
            mcp_status=row["mcp_status"],
            mcp_latency_ms=row["mcp_latency_ms"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            payload_summary=json.loads(row["payload_summary"]) if row["payload_summary"] else {},
            attributes=json.loads(row["attributes"]) if row["attributes"] else {},
            error=row["error"],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SpanStore:
    """Thread-safe SQLite span store.

    SQLite is plenty for the POC — single-host workstation, low write volume
    (one span per inbound/outbound MCP call). Concurrent writers are
    serialised by the connection lock; readers are unaffected.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- Writes -------------------------------------------------------------

    def record(self, span: Span) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO spans (
                    span_id, trace_id, parent_span_id, service, mcp_method,
                    mcp_domain, mcp_status, mcp_latency_ms, started_at,
                    ended_at, payload_summary, attributes, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                span.to_row(),
            )
            self._conn.commit()

    def clear(self) -> int:
        """Remove all spans. Returns the number of rows deleted."""
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM spans")
            n = cur.fetchone()[0]
            self._conn.execute("DELETE FROM spans")
            self._conn.commit()
            return int(n)

    # -- Reads --------------------------------------------------------------

    def query(
        self,
        *,
        service: str | None = None,
        mcp_method: str | None = None,
        mcp_domain: str | None = None,
        mcp_status: str | None = None,
        trace_id: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 500,
    ) -> list[Span]:
        clauses: list[str] = []
        params: list[Any] = []
        if service is not None:
            clauses.append("service = ?"); params.append(service)
        if mcp_method is not None:
            clauses.append("mcp_method = ?"); params.append(mcp_method)
        if mcp_domain is not None:
            clauses.append("mcp_domain = ?"); params.append(mcp_domain)
        if mcp_status is not None:
            clauses.append("mcp_status = ?"); params.append(mcp_status)
        if trace_id is not None:
            clauses.append("trace_id = ?"); params.append(trace_id)
        if since is not None:
            clauses.append("started_at >= ?"); params.append(since)
        if until is not None:
            clauses.append("started_at <= ?"); params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM spans {where} ORDER BY started_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Span.from_row(r) for r in rows]

    def get_metrics(
        self,
        *,
        service: str | None = None,
        since: float | None = None,
        until: float | None = None,
    ) -> dict[str, Any]:
        """Derived metrics over the span stream (§9.6 derived metrics).

        Returns a dict with three sections:
          * ``counts``        — total span count overall and per (service, method).
          * ``status_counts`` — status histogram per (service, method).
          * ``latency_ms``    — p50 / p95 / max per (service, method).

        The B&P/SD/RAG-specific metrics (escalation rate, RAG status
        distribution, etc.) compose from ``status_counts`` — keeping the
        store side generic means new specialists land without re-shaping it.
        """
        spans = self.query(service=service, since=since, until=until, limit=10_000)
        out: dict[str, Any] = {
            "total_spans": len(spans),
            "counts": {},
            "status_counts": {},
            "latency_ms": {},
        }
        # Group by (service, method).
        groups: dict[tuple[str, str], list[Span]] = {}
        for s in spans:
            groups.setdefault((s.service, s.mcp_method), []).append(s)
        for (svc, method), grp in groups.items():
            key = f"{svc}.{method}"
            out["counts"][key] = len(grp)
            status_hist: dict[str, int] = {}
            latencies: list[float] = []
            for s in grp:
                status_hist[s.mcp_status or "unset"] = status_hist.get(s.mcp_status or "unset", 0) + 1
                latencies.append(s.mcp_latency_ms)
            out["status_counts"][key] = status_hist
            if latencies:
                latencies.sort()
                out["latency_ms"][key] = {
                    "p50": _percentile(latencies, 50),
                    "p95": _percentile(latencies, 95),
                    "max": latencies[-1],
                    "mean": statistics.fmean(latencies),
                }
        return out


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)
