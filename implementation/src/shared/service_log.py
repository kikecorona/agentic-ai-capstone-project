"""Java-style service logger backed by the shared audit DB.

Every service emits structured log entries through ``get_logger(module)``;
each entry persists into the ``service_logs`` table of the SQLite file at
``$AUDIT_DB_PATH`` (the same file that holds the LLM call log — they share
storage so an operator inspects one DB, never two).

Schema::

    service_logs(
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        module      TEXT NOT NULL,         -- e.g. "rag.service", "bp.compose", "sd.analyze_code"
        level       TEXT NOT NULL,         -- "debug" | "info" | "warn" | "error"
        timestamp   REAL NOT NULL,         -- unix epoch seconds (UTC)
        message     TEXT NOT NULL
    )

Mirroring: every entry is *also* sent to the Python stdlib logger named
``module``. That way a developer running a service in the foreground sees
log lines on the console while the persisted DB serves as the audit trail
for evaluation (§9.7) and post-hoc debugging.

Resilience: persistence is fire-and-forget — a SQLite failure (disk full,
permission error) is swallowed so it can never mask the wrapped operation,
mirroring §9.6's note for OTel span emission.

Use::

    from src.shared.service_log import get_logger

    log = get_logger("rag.service")
    log.info("indexing source bp/products/discovery.md")
    log.warn("ToT score below threshold; using fallback")
    log.error("retrieve failed: domain_filter=invalid")
"""

from __future__ import annotations

import logging as stdlib_logging
import os
import sqlite3
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_LEVELS: tuple[str, ...] = ("debug", "info", "warn", "error")
_STDLIB_LEVEL = {
    "debug": stdlib_logging.DEBUG,
    "info": stdlib_logging.INFO,
    "warn": stdlib_logging.WARNING,
    "error": stdlib_logging.ERROR,
}


def format_exception(exc: BaseException) -> str:
    """Render an exception with **type + repr + traceback** in one
    string. Use anywhere an exception text is going to be persisted
    into a task-row / escalation envelope / API response — many
    exception types render to ``str(exc) == ""`` (bare ``Exception()``,
    some httpx/asyncio/MCP wire errors), so plain ``f"failed: {exc}"``
    messages tell the operator nothing.

    Format:
      ``ClassName: repr(exc)\\n<traceback>``

    The traceback is rendered if and only if the exception carries a
    ``__traceback__`` (i.e. we're inside ``except`` or chained from
    one). Outside of an exception context, just the type+repr line.
    """
    head = f"{type(exc).__name__}: {exc!r}"
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return f"{head}\n{tb}".rstrip() if tb.strip() else head


_SCHEMA = """
CREATE TABLE IF NOT EXISTS service_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    module    TEXT NOT NULL,
    level     TEXT NOT NULL,
    timestamp REAL NOT NULL,
    message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_service_logs_module    ON service_logs(module);
CREATE INDEX IF NOT EXISTS idx_service_logs_level     ON service_logs(level);
CREATE INDEX IF NOT EXISTS idx_service_logs_timestamp ON service_logs(timestamp);
"""


@dataclass
class ServiceLogRow:
    id: int
    module: str
    level: str
    timestamp: float
    message: str


class ServiceLogStore:
    """Thread-safe persistence layer. ``ServiceLogger`` is the public
    entry point — most callers don't construct this directly."""

    _default: "ServiceLogStore | None" = None

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @classmethod
    def default(cls) -> "ServiceLogStore":
        if cls._default is None:
            cls._default = cls(os.environ.get("AUDIT_DB_PATH", "./data/audit/log.db"))
        return cls._default

    @classmethod
    def reset_default(cls) -> None:
        cls._default = None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- write --------------------------------------------------------------

    def record(self, *, module: str, level: str, timestamp: float, message: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO service_logs (module, level, timestamp, message) VALUES (?, ?, ?, ?)",
                (module, level, float(timestamp), message),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def clear(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM service_logs")
            n = int(cur.fetchone()[0])
            self._conn.execute("DELETE FROM service_logs")
            self._conn.commit()
            return n

    # -- read ---------------------------------------------------------------

    def query(
        self,
        *,
        module: str | None = None,
        module_prefix: str | None = None,
        level: str | None = None,
        min_level: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 200,
    ) -> list[ServiceLogRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if module is not None:
            clauses.append("module = ?"); params.append(module)
        if module_prefix is not None:
            clauses.append("module LIKE ?"); params.append(module_prefix.rstrip("%") + "%")
        if level is not None:
            clauses.append("level = ?"); params.append(level)
        if min_level is not None:
            allowed = _LEVELS[_LEVELS.index(min_level):]
            placeholders = ",".join(["?"] * len(allowed))
            clauses.append(f"level IN ({placeholders})")
            params.extend(allowed)
        if since is not None:
            clauses.append("timestamp >= ?"); params.append(float(since))
        if until is not None:
            clauses.append("timestamp <= ?"); params.append(float(until))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM service_logs {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [ServiceLogRow(int(r["id"]), r["module"], r["level"], float(r["timestamp"]), r["message"]) for r in rows]

    def counts_by_level(
        self,
        *,
        module_prefix: str | None = None,
        since: float | None = None,
    ) -> dict[str, int]:
        clauses: list[str] = []
        params: list[Any] = []
        if module_prefix is not None:
            clauses.append("module LIKE ?"); params.append(module_prefix.rstrip("%") + "%")
        if since is not None:
            clauses.append("timestamp >= ?"); params.append(float(since))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT level, COUNT(*) AS n FROM service_logs {where} GROUP BY level"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out: dict[str, int] = {lvl: 0 for lvl in _LEVELS}
        for r in rows:
            out[r["level"]] = int(r["n"])
        return out


class ServiceLogger:
    """Per-module logger handle. Cheap to create; held by callers as a
    module-level singleton (``log = get_logger(__name__-ish)``).

    Java-style API: :py:meth:`info`, :py:meth:`warn` (alias
    :py:meth:`warning`), :py:meth:`error`, :py:meth:`debug`. Each call
    persists *and* mirrors to the stdlib logger so foreground processes
    still print to the console.
    """

    def __init__(self, module: str, store: ServiceLogStore | None = None):
        if not module or not module.strip():
            raise ValueError("ServiceLogger: a non-empty module tag is required")
        self._module = module.strip()
        self._store = store or ServiceLogStore.default()
        self._stdlib = stdlib_logging.getLogger(self._module)

    @property
    def module(self) -> str:
        return self._module

    # -- level helpers ------------------------------------------------------

    def debug(self, message: str) -> None:
        self._emit("debug", message)

    def info(self, message: str) -> None:
        self._emit("info", message)

    def warn(self, message: str) -> None:
        self._emit("warn", message)

    # ``warning`` keeps the stdlib alias so existing call sites that used
    # ``logger.warning(...)`` swap with one rename, not two.
    warning = warn

    def error(self, message: str) -> None:
        self._emit("error", message)

    def exception(self, message: str, exc: BaseException) -> None:
        """Log ``message`` at error level with the full exception
        trace appended. Use at every top-level ``except`` that
        surfaces a failure into a task row / escalation envelope —
        many exception types render to ``str(exc) == ""``, so plain
        ``f"failed: {exc}"`` messages tell the operator nothing.
        """
        self._emit("error", f"{message}\n{format_exception(exc)}")

    # -- internals ----------------------------------------------------------

    def _emit(self, level: str, message: str) -> None:
        if level not in _STDLIB_LEVEL:
            level = "info"  # defensive — should never happen via public API
        # Mirror to stdlib so foreground processes show output on the console.
        try:
            self._stdlib.log(_STDLIB_LEVEL[level], message)
        except Exception:  # noqa: BLE001 — never let logging break the call site
            pass
        # Persist to the audit DB. Fire-and-forget per §9.6 resilience principle.
        try:
            self._store.record(
                module=self._module,
                level=level,
                timestamp=time.time(),
                message=message,
            )
        except Exception:  # noqa: BLE001
            pass


def get_logger(module: str) -> ServiceLogger:
    """Factory mirroring ``logging.getLogger(name)``. Returns a fresh
    handle; callers cache it at module scope by convention."""
    return ServiceLogger(module)


# ---------------------------------------------------------------------------
# CLI — tail recent log entries
# ---------------------------------------------------------------------------

def _main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Inspect the service log")
    p.add_argument("--db", default=os.environ.get("AUDIT_DB_PATH", "./data/audit/log.db"))
    p.add_argument("--module", default=None, help="exact module filter")
    p.add_argument("--prefix", default=None, help="module prefix filter (e.g. 'rag.')")
    p.add_argument("--level", default=None, choices=_LEVELS, help="exact level filter")
    p.add_argument("--min-level", default=None, choices=_LEVELS, help="min level (incl)")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--counts", action="store_true", help="print level counts instead of rows")
    args = p.parse_args()

    store = ServiceLogStore(args.db)
    if args.counts:
        counts = store.counts_by_level(module_prefix=args.prefix)
        for lvl in _LEVELS:
            print(f"  {lvl:5s}  {counts.get(lvl, 0)}")
        return 0
    rows = store.query(
        module=args.module,
        module_prefix=args.prefix,
        level=args.level,
        min_level=args.min_level,
        limit=args.limit,
    )
    # Print oldest-first so a tail-style read is intuitive.
    for r in reversed(rows):
        print(
            f"[{time.strftime('%H:%M:%S', time.localtime(r.timestamp))}] "
            f"{r.level.upper():5s} {r.module:32s} {r.message}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
