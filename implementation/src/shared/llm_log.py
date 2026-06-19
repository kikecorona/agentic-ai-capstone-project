"""SQLite-backed audit log for every LLM call (cross-cutting Module 6).

Complements the OTel span store: that one captures **MCP** calls between
services; this one captures the **LLM** calls inside each service. Every
row is one round-trip with the local Ollama instance:

  * ``module``     — caller-supplied tag, e.g. ``rag.auto_rag.grader`` or
                     ``bp.compose_answer``. By convention: ``<service>.<area>.<step>``.
  * ``request``    — serialised input (system + human messages, JSON-mode flag).
  * ``response``   — raw text the model produced (or an error string on failure).
  * ``started_at`` — unix epoch seconds when the invoke started.
  * ``latency_ms`` — wall-clock cost.
  * ``error``      — populated when the underlying invoke raised.

Persisted in the shared audit DB at ``$AUDIT_DB_PATH`` (default
``./data/audit/log.db``). The same file also holds the Java-style service
log (see :py:mod:`src.shared.service_log`) in a separate ``service_logs``
table — both streams audit *inside-service* activity, the OTel span store
audits *between-service* MCP traffic.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    module      TEXT NOT NULL,
    request     TEXT NOT NULL,
    response    TEXT NOT NULL,
    started_at  REAL NOT NULL,
    latency_ms  REAL NOT NULL,
    model       TEXT,
    temperature REAL,
    json_mode   INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_module  ON llm_calls(module);
CREATE INDEX IF NOT EXISTS idx_llm_started ON llm_calls(started_at);
"""


@dataclass
class LLMCallRow:
    id: int
    module: str
    request: str
    response: str
    started_at: float
    latency_ms: float
    model: str | None
    temperature: float | None
    json_mode: bool
    error: str | None


class LLMCallLog:
    """Thread-safe SQLite call log.

    One process-wide instance is plenty for the POC: writes are serialised
    by the connection lock, reads work concurrently. ``LoggedLLM`` (in
    :py:mod:`src.shared.llm`) is the only writer in the running services;
    debugging tools and the validation harness are the only readers.
    """

    _default: "LLMCallLog | None" = None

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @classmethod
    def default(cls) -> "LLMCallLog":
        """Singleton bound to ``$AUDIT_DB_PATH``. Constructed lazily so the
        directory only gets created on first use."""
        if cls._default is None:
            cls._default = cls(os.environ.get("AUDIT_DB_PATH", "./data/audit/log.db"))
        return cls._default

    @classmethod
    def reset_default(cls) -> None:
        """Used by tests + validation scripts to bind a fresh log path."""
        cls._default = None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- write --------------------------------------------------------------

    def record(
        self,
        *,
        module: str,
        request: str,
        response: str,
        started_at: float,
        latency_ms: float,
        model: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
        error: str | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO llm_calls (
                    module, request, response, started_at, latency_ms,
                    model, temperature, json_mode, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    module,
                    request,
                    response,
                    float(started_at),
                    float(latency_ms),
                    model,
                    None if temperature is None else float(temperature),
                    1 if json_mode else 0,
                    error,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def clear(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM llm_calls")
            n = int(cur.fetchone()[0])
            self._conn.execute("DELETE FROM llm_calls")
            self._conn.commit()
            return n

    # -- read ---------------------------------------------------------------

    def query(
        self,
        *,
        module: str | None = None,
        module_prefix: str | None = None,
        since: float | None = None,
        until: float | None = None,
        only_errors: bool = False,
        limit: int = 200,
    ) -> list[LLMCallRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if module is not None:
            clauses.append("module = ?"); params.append(module)
        if module_prefix is not None:
            clauses.append("module LIKE ?"); params.append(module_prefix.rstrip("%") + "%")
        if since is not None:
            clauses.append("started_at >= ?"); params.append(float(since))
        if until is not None:
            clauses.append("started_at <= ?"); params.append(float(until))
        if only_errors:
            clauses.append("error IS NOT NULL")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM llm_calls {where} ORDER BY started_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def summarise_by_module(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Returns ``{module: {count, errors, latency_ms: {p50,p95,mean}}}``.

        Cheap aggregate the validation script and any future dashboard
        can call to show "what is the LLM doing?" at a glance.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("started_at >= ?"); params.append(float(since))
        if until is not None:
            clauses.append("started_at <= ?"); params.append(float(until))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT module, latency_ms, error FROM llm_calls {where}"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        groups: dict[str, list[tuple[float, str | None]]] = {}
        for r in rows:
            groups.setdefault(r["module"], []).append((float(r["latency_ms"]), r["error"]))
        out: dict[str, dict[str, Any]] = {}
        for mod, vals in groups.items():
            latencies = sorted(v[0] for v in vals)
            errors = sum(1 for v in vals if v[1])
            out[mod] = {
                "count": len(vals),
                "errors": errors,
                "latency_ms": {
                    "p50": _percentile(latencies, 50),
                    "p95": _percentile(latencies, 95),
                    "mean": (sum(latencies) / len(latencies)) if latencies else 0.0,
                    "max": latencies[-1] if latencies else 0.0,
                },
            }
        return out

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _row(r: sqlite3.Row) -> LLMCallRow:
        return LLMCallRow(
            id=int(r["id"]),
            module=r["module"],
            request=r["request"],
            response=r["response"],
            started_at=float(r["started_at"]),
            latency_ms=float(r["latency_ms"]),
            model=r["model"],
            temperature=None if r["temperature"] is None else float(r["temperature"]),
            json_mode=bool(r["json_mode"]),
            error=r["error"],
        )


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


# ---------------------------------------------------------------------------
# CLI — dump a few rows for ad-hoc debugging
# ---------------------------------------------------------------------------

def _main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Inspect the LLM call log")
    p.add_argument("--db", default=os.environ.get("AUDIT_DB_PATH", "./data/audit/log.db"))
    p.add_argument("--module", default=None, help="exact module filter")
    p.add_argument("--prefix", default=None, help="module prefix filter (e.g. rag.)")
    p.add_argument("--errors-only", action="store_true")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--summary", action="store_true", help="print per-module summary instead of rows")
    args = p.parse_args()

    log = LLMCallLog(args.db)
    if args.summary:
        out = log.summarise_by_module()
        print(json.dumps(out, indent=2, default=str))
        return 0
    rows = log.query(
        module=args.module,
        module_prefix=args.prefix,
        only_errors=args.errors_only,
        limit=args.limit,
    )
    for r in rows:
        print(
            f"[{time.strftime('%H:%M:%S', time.localtime(r.started_at))}]"
            f" {r.module}  ({r.latency_ms:.0f} ms"
            f"{' ERROR' if r.error else ''})"
        )
        print(f"  request:  {r.request[:200]}{'…' if len(r.request) > 200 else ''}")
        print(f"  response: {r.response[:200]}{'…' if len(r.response) > 200 else ''}")
        if r.error:
            print(f"  error:    {r.error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
