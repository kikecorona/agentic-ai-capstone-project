"""SD state — doc index + sources inventory (§9.2.1).

Same shape as B&P's, two SQLite tables in ``$SD_DB_PATH`` (default
``./data/sd/state.db``):

  * ``sd_doc_index`` — per-page metadata for every SD page the agent has
    produced. Mirrors the BP shape but adds ``source_revision`` (the
    last-known commit sha for the service the page documents) and
    ``referenced_products`` (B&P pages this service backs).

  * ``sd_sources_inventory`` — last-known commit sha per service tree
    (§9.2.1). Used to skip unchanged services on a refresh and to drive
    the incremental ``analyze_code`` pull.

Per the spec the SD doc index is owned by SD alone — the orchestrator
never caches a parallel view. ``find_services_for_product`` (BP-side)
and ``find_products_for_service`` (SD-side) are pure relational reads.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sd_doc_index (
    page_uri              TEXT PRIMARY KEY,
    service               TEXT,
    title                 TEXT,
    last_updated          REAL NOT NULL,
    source_revision       TEXT,                       -- commit-sha-equivalent
    content_hash          TEXT NOT NULL,
    chunking_strategy     TEXT,
    embedding_revision    TEXT,
    open_placeholders     TEXT NOT NULL DEFAULT '[]',
    endpoints             TEXT NOT NULL DEFAULT '[]', -- JSON list of {method, path, handler}
    downstream_services   TEXT NOT NULL DEFAULT '[]', -- JSON list (resolved deps)
    referenced_products   TEXT NOT NULL DEFAULT '[]', -- JSON list of BP page URIs
    side_info_revision    TEXT,                       -- hash of source-code analysis at last enrich
    answered_sme_blocks   TEXT NOT NULL DEFAULT '{}', -- JSON {question_id: {hash, prose}}
    metadata              TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sd_doc_updated ON sd_doc_index(last_updated);
CREATE INDEX IF NOT EXISTS idx_sd_doc_service ON sd_doc_index(service);

CREATE TABLE IF NOT EXISTS sd_sources_inventory (
    service          TEXT PRIMARY KEY,
    source_revision  TEXT NOT NULL,                   -- aggregate hash of every file
    file_count       INTEGER NOT NULL DEFAULT 0,
    last_seen        REAL NOT NULL,
    metadata         TEXT NOT NULL DEFAULT '{}'
);
"""

# Idempotent forward-migrations for stores that pre-date the
# enrich-existing flow (added side-info + SME-answered tracking).
_MIGRATIONS = [
    "ALTER TABLE sd_doc_index ADD COLUMN side_info_revision TEXT",
    "ALTER TABLE sd_doc_index ADD COLUMN answered_sme_blocks TEXT NOT NULL DEFAULT '{}'",
]


# ---------------------------------------------------------------------------
# Doc index
# ---------------------------------------------------------------------------

@dataclass
class SDDocIndexEntry:
    page_uri: str
    service: str | None
    title: str | None
    last_updated: float
    source_revision: str | None
    content_hash: str
    chunking_strategy: str | None
    embedding_revision: str | None
    open_placeholders: list[str]
    endpoints: list[dict[str, Any]]
    downstream_services: list[str]
    referenced_products: list[str]
    # Hash of the source-code analysis at the time this SD page was last
    # enriched. The skip-unchanged check trips only when both the page
    # content and this revision are identical to the prior refresh.
    side_info_revision: str | None = None
    # Map of question_id → {"hash": <prose-hash>, "prose": <answered text>}
    # for SME-answered blocks we want to preserve verbatim across
    # subsequent refreshes (§9.5 SME flow).
    answered_sme_blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_uri": self.page_uri,
            "service": self.service,
            "title": self.title,
            "last_updated": self.last_updated,
            "source_revision": self.source_revision,
            "content_hash": self.content_hash,
            "chunking_strategy": self.chunking_strategy,
            "embedding_revision": self.embedding_revision,
            "open_placeholders": list(self.open_placeholders),
            "endpoints": list(self.endpoints),
            "downstream_services": list(self.downstream_services),
            "referenced_products": list(self.referenced_products),
            "side_info_revision": self.side_info_revision,
            "answered_sme_blocks": dict(self.answered_sme_blocks),
            "metadata": dict(self.metadata),
        }


@dataclass
class SDSourceEntry:
    service: str
    source_revision: str
    file_count: int
    last_seen: float
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class _SQLiteBacked:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        # Forward-migrate older stores. ALTER TABLE … ADD COLUMN errors
        # if the column already exists; swallow that so re-runs are no-ops.
        for stmt in _MIGRATIONS:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class SDDocIndex(_SQLiteBacked):
    """Per-page metadata for every SD page."""

    def upsert(self, entry: SDDocIndexEntry) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sd_doc_index (
                    page_uri, service, title, last_updated, source_revision,
                    content_hash, chunking_strategy, embedding_revision,
                    open_placeholders, endpoints, downstream_services,
                    referenced_products, side_info_revision,
                    answered_sme_blocks, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(page_uri) DO UPDATE SET
                    service = excluded.service,
                    title = excluded.title,
                    last_updated = excluded.last_updated,
                    source_revision = excluded.source_revision,
                    content_hash = excluded.content_hash,
                    chunking_strategy = excluded.chunking_strategy,
                    embedding_revision = excluded.embedding_revision,
                    open_placeholders = excluded.open_placeholders,
                    endpoints = excluded.endpoints,
                    downstream_services = excluded.downstream_services,
                    referenced_products = excluded.referenced_products,
                    side_info_revision = excluded.side_info_revision,
                    answered_sme_blocks = excluded.answered_sme_blocks,
                    metadata = excluded.metadata
                """,
                (
                    entry.page_uri,
                    entry.service,
                    entry.title,
                    float(entry.last_updated),
                    entry.source_revision,
                    entry.content_hash,
                    entry.chunking_strategy,
                    entry.embedding_revision,
                    json.dumps(entry.open_placeholders),
                    json.dumps(entry.endpoints, default=str),
                    json.dumps(entry.downstream_services),
                    json.dumps(entry.referenced_products),
                    entry.side_info_revision,
                    json.dumps(entry.answered_sme_blocks, default=str),
                    json.dumps(entry.metadata, default=str),
                ),
            )
            self._conn.commit()

    def add_open_placeholder(self, page_uri: str, question_id: str) -> None:
        entry = self.get(page_uri)
        if entry is None:
            return
        if question_id not in entry.open_placeholders:
            entry.open_placeholders.append(question_id)
            self.upsert(entry)

    def remove_open_placeholder(self, page_uri: str, question_id: str) -> None:
        entry = self.get(page_uri)
        if entry is None:
            return
        if question_id in entry.open_placeholders:
            entry.open_placeholders.remove(question_id)
            self.upsert(entry)

    def delete(self, page_uri: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM sd_doc_index WHERE page_uri = ?", (page_uri,))
            self._conn.commit()
            return cur.rowcount > 0

    def get(self, page_uri: str) -> SDDocIndexEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sd_doc_index WHERE page_uri = ?", (page_uri,)
            ).fetchone()
        return self._row(row) if row else None

    def get_by_service(self, service: str) -> SDDocIndexEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sd_doc_index WHERE service = ? ORDER BY last_updated DESC LIMIT 1",
                (service,),
            ).fetchone()
        return self._row(row) if row else None

    def list_all(self) -> list[SDDocIndexEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sd_doc_index ORDER BY last_updated DESC"
            ).fetchall()
        return [self._row(r) for r in rows]

    def find_pages_for_service(self, service_id: str) -> list[SDDocIndexEntry]:
        """Backing for ``find_services_for_product``-adjacent SD lookups —
        returns every SD page that documents the named service. The same
        method also helps the focused-analyze-code path locate the SD
        page (and its source files) for an endpoint mentioned in a query."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sd_doc_index WHERE service = ?",
                (service_id,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def pages_with_open_placeholders(self) -> list[SDDocIndexEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sd_doc_index WHERE open_placeholders != '[]'"
            ).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(r: sqlite3.Row) -> SDDocIndexEntry:
        # `side_info_revision` and `answered_sme_blocks` were added in a
        # forward migration; older rows may not have them yet, so guard
        # the column reads via Row's keys() rather than direct access.
        keys = set(r.keys())
        side_info_revision = r["side_info_revision"] if "side_info_revision" in keys else None
        answered_blocks_raw = r["answered_sme_blocks"] if "answered_sme_blocks" in keys else None
        return SDDocIndexEntry(
            page_uri=r["page_uri"],
            service=r["service"],
            title=r["title"],
            last_updated=float(r["last_updated"]),
            source_revision=r["source_revision"],
            content_hash=r["content_hash"],
            chunking_strategy=r["chunking_strategy"],
            embedding_revision=r["embedding_revision"],
            open_placeholders=json.loads(r["open_placeholders"] or "[]"),
            endpoints=json.loads(r["endpoints"] or "[]"),
            downstream_services=json.loads(r["downstream_services"] or "[]"),
            referenced_products=json.loads(r["referenced_products"] or "[]"),
            side_info_revision=side_info_revision,
            answered_sme_blocks=json.loads(answered_blocks_raw or "{}"),
            metadata=json.loads(r["metadata"] or "{}"),
        )


class SDSourcesInventory(_SQLiteBacked):
    """Last-known revision per service tree (§9.2.1)."""

    def upsert(
        self,
        service: str,
        source_revision: str,
        *,
        file_count: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sd_sources_inventory (service, source_revision, file_count, last_seen, metadata)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(service) DO UPDATE SET
                    source_revision = excluded.source_revision,
                    file_count = excluded.file_count,
                    last_seen = excluded.last_seen,
                    metadata = excluded.metadata
                """,
                (
                    service,
                    source_revision,
                    int(file_count),
                    float(time.time()),
                    json.dumps(metadata or {}, default=str),
                ),
            )
            self._conn.commit()

    def get(self, service: str) -> SDSourceEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sd_sources_inventory WHERE service = ?",
                (service,),
            ).fetchone()
        if not row:
            return None
        return SDSourceEntry(
            service=row["service"],
            source_revision=row["source_revision"],
            file_count=int(row["file_count"]),
            last_seen=float(row["last_seen"]),
            metadata=json.loads(row["metadata"] or "{}"),
        )

    def is_unchanged(self, service: str, source_revision: str) -> bool:
        existing = self.get(service)
        return existing is not None and existing.source_revision == source_revision

    def delete(self, service: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sd_sources_inventory WHERE service = ?",
                (service,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_all(self) -> list[SDSourceEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sd_sources_inventory ORDER BY last_seen DESC"
            ).fetchall()
        return [
            SDSourceEntry(
                service=r["service"],
                source_revision=r["source_revision"],
                file_count=int(r["file_count"]),
                last_seen=float(r["last_seen"]),
                metadata=json.loads(r["metadata"] or "{}"),
            )
            for r in rows
        ]


def default_db_path() -> Path:
    return Path(os.environ.get("SD_DB_PATH", "./data/sd/state.db")).resolve()
