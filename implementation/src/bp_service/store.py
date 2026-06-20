"""B&P state — doc index + sources inventory (§9.3.1).

Two SQLite tables that live in the same per-service DB at ``$BP_DB_PATH``
(default ``./data/bp/state.db``):

  * ``bp_doc_index`` — per-page metadata for every B&P page the agent
    has produced. The architecture lists ``last_updated``,
    ``source_documents``, ``content_hash``, ``open_placeholders``, and
    embedding revision; we add the chunking strategy returned by
    ``RAG_MCP.index`` and a free-form ``metadata`` JSON column so future
    fields land without a schema bump.
  * ``bp_sources_inventory`` — last-known content hash per *input* doc.
    Used to skip unchanged sources on a refresh fan-out: if the hash
    matches what we have, the doc is up to date and we don't re-index it.

Per the spec the doc index is owned by B&P alone — the Orchestrator
never caches a parallel view (§9.4.3). Reads from the index are the
backing store for the ``find_products_for_service`` relational lookup
and for the ``get_page`` MCP method.
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
CREATE TABLE IF NOT EXISTS bp_doc_index (
    page_uri           TEXT PRIMARY KEY,
    title              TEXT,
    last_updated       REAL NOT NULL,
    source_documents   TEXT NOT NULL,        -- JSON array of input source URIs
    content_hash       TEXT NOT NULL,
    chunking_strategy  TEXT,
    embedding_revision TEXT,
    open_placeholders  TEXT NOT NULL DEFAULT '[]',  -- JSON array of question_id
    referenced_services TEXT NOT NULL DEFAULT '[]', -- JSON array of service_id
    side_info_revision TEXT,                        -- hash of SD doc-index state at last enrich
    answered_sme_blocks TEXT NOT NULL DEFAULT '{}', -- JSON {question_id: {hash, prose}}
    metadata           TEXT NOT NULL DEFAULT '{}'   -- free-form JSON
);
CREATE INDEX IF NOT EXISTS idx_bp_doc_index_updated ON bp_doc_index(last_updated);

CREATE TABLE IF NOT EXISTS bp_sources_inventory (
    source_uri      TEXT PRIMARY KEY,
    content_hash    TEXT NOT NULL,
    last_seen       REAL NOT NULL,
    metadata        TEXT NOT NULL DEFAULT '{}'
);
"""

# Cheap forward migration for stores created before the side-info /
# answered-SME columns existed. Idempotent — ALTER TABLE … ADD COLUMN
# fails if the column is already there, which we swallow.
_MIGRATIONS = [
    "ALTER TABLE bp_doc_index ADD COLUMN side_info_revision TEXT",
    "ALTER TABLE bp_doc_index ADD COLUMN answered_sme_blocks TEXT NOT NULL DEFAULT '{}'",
]


# ---------------------------------------------------------------------------
# Doc index
# ---------------------------------------------------------------------------

@dataclass
class DocIndexEntry:
    page_uri: str
    title: str | None
    last_updated: float
    source_documents: list[str]
    content_hash: str
    chunking_strategy: str | None
    embedding_revision: str | None
    open_placeholders: list[str]
    referenced_services: list[str]
    # Hash of the SD doc-index state at the time this BP page was last
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
            "title": self.title,
            "last_updated": self.last_updated,
            "source_documents": list(self.source_documents),
            "content_hash": self.content_hash,
            "chunking_strategy": self.chunking_strategy,
            "embedding_revision": self.embedding_revision,
            "open_placeholders": list(self.open_placeholders),
            "referenced_services": list(self.referenced_services),
            "side_info_revision": self.side_info_revision,
            "answered_sme_blocks": dict(self.answered_sme_blocks),
            "metadata": dict(self.metadata),
        }


class _SQLiteBacked:
    """Tiny mixin: shared connection + lock for the two tables in one file."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        # Forward-migration: ALTER TABLE ADD COLUMN raises OperationalError
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


class BPDocIndex(_SQLiteBacked):
    """Per-page metadata for every B&P page."""

    # -- writes -------------------------------------------------------------

    def upsert(self, entry: DocIndexEntry) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO bp_doc_index (
                    page_uri, title, last_updated, source_documents, content_hash,
                    chunking_strategy, embedding_revision, open_placeholders,
                    referenced_services, side_info_revision, answered_sme_blocks,
                    metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(page_uri) DO UPDATE SET
                    title = excluded.title,
                    last_updated = excluded.last_updated,
                    source_documents = excluded.source_documents,
                    content_hash = excluded.content_hash,
                    chunking_strategy = excluded.chunking_strategy,
                    embedding_revision = excluded.embedding_revision,
                    open_placeholders = excluded.open_placeholders,
                    referenced_services = excluded.referenced_services,
                    side_info_revision = excluded.side_info_revision,
                    answered_sme_blocks = excluded.answered_sme_blocks,
                    metadata = excluded.metadata
                """,
                (
                    entry.page_uri,
                    entry.title,
                    float(entry.last_updated),
                    json.dumps(entry.source_documents),
                    entry.content_hash,
                    entry.chunking_strategy,
                    entry.embedding_revision,
                    json.dumps(entry.open_placeholders),
                    json.dumps(entry.referenced_services),
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
            cur = self._conn.execute("DELETE FROM bp_doc_index WHERE page_uri = ?", (page_uri,))
            self._conn.commit()
            return cur.rowcount > 0

    # -- reads --------------------------------------------------------------

    def get(self, page_uri: str) -> DocIndexEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bp_doc_index WHERE page_uri = ?", (page_uri,)
            ).fetchone()
        return self._row(row) if row else None

    def list_all(self) -> list[DocIndexEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bp_doc_index ORDER BY last_updated DESC"
            ).fetchall()
        return [self._row(r) for r in rows]

    def find_pages_referencing(self, service_id: str) -> list[DocIndexEntry]:
        """Backing for ``find_products_for_service`` (§9.3.2): scan the
        ``referenced_services`` JSON arrays for pages that name the given
        service. SQLite's JSON filtering would be cleaner with the JSON1
        extension, but ``LIKE`` keeps us portable to vanilla builds."""
        like = f"%{json.dumps(service_id)[1:-1]}%"  # bare name without quotes
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bp_doc_index WHERE referenced_services LIKE ?",
                (like,),
            ).fetchall()
        out: list[DocIndexEntry] = []
        for r in rows:
            entry = self._row(r)
            if service_id in entry.referenced_services:
                out.append(entry)
        return out

    def pages_with_open_placeholders(self) -> list[DocIndexEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bp_doc_index WHERE open_placeholders != '[]'"
            ).fetchall()
        return [self._row(r) for r in rows]

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _row(r: sqlite3.Row) -> DocIndexEntry:
        # `side_info_revision` and `answered_sme_blocks` were added in a
        # forward migration; older rows may not have them yet, so guard
        # the column reads via Row's keys() rather than direct access.
        keys = set(r.keys())
        side_info_revision = r["side_info_revision"] if "side_info_revision" in keys else None
        answered_blocks_raw = r["answered_sme_blocks"] if "answered_sme_blocks" in keys else None
        return DocIndexEntry(
            page_uri=r["page_uri"],
            title=r["title"],
            last_updated=float(r["last_updated"]),
            source_documents=json.loads(r["source_documents"] or "[]"),
            content_hash=r["content_hash"],
            chunking_strategy=r["chunking_strategy"],
            embedding_revision=r["embedding_revision"],
            open_placeholders=json.loads(r["open_placeholders"] or "[]"),
            referenced_services=json.loads(r["referenced_services"] or "[]"),
            side_info_revision=side_info_revision,
            answered_sme_blocks=json.loads(answered_blocks_raw or "{}"),
            metadata=json.loads(r["metadata"] or "{}"),
        )


# ---------------------------------------------------------------------------
# Sources inventory
# ---------------------------------------------------------------------------

@dataclass
class SourceEntry:
    source_uri: str
    content_hash: str
    last_seen: float
    metadata: dict[str, Any] = field(default_factory=dict)


class BPSourcesInventory(_SQLiteBacked):
    """Last-known hash per input doc. Used to skip unchanged sources
    on a refresh fan-out (§9.3.3 background mode)."""

    def upsert(self, source_uri: str, content_hash: str, *, metadata: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO bp_sources_inventory (source_uri, content_hash, last_seen, metadata)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_uri) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    last_seen = excluded.last_seen,
                    metadata = excluded.metadata
                """,
                (
                    source_uri,
                    content_hash,
                    float(time.time()),
                    json.dumps(metadata or {}, default=str),
                ),
            )
            self._conn.commit()

    def get(self, source_uri: str) -> SourceEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bp_sources_inventory WHERE source_uri = ?", (source_uri,)
            ).fetchone()
        if not row:
            return None
        return SourceEntry(
            source_uri=row["source_uri"],
            content_hash=row["content_hash"],
            last_seen=float(row["last_seen"]),
            metadata=json.loads(row["metadata"] or "{}"),
        )

    def is_unchanged(self, source_uri: str, content_hash: str) -> bool:
        existing = self.get(source_uri)
        return existing is not None and existing.content_hash == content_hash

    def delete(self, source_uri: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM bp_sources_inventory WHERE source_uri = ?", (source_uri,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_all(self) -> list[SourceEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bp_sources_inventory ORDER BY last_seen DESC"
            ).fetchall()
        return [
            SourceEntry(
                source_uri=r["source_uri"],
                content_hash=r["content_hash"],
                last_seen=float(r["last_seen"]),
                metadata=json.loads(r["metadata"] or "{}"),
            )
            for r in rows
        ]


def default_db_path() -> Path:
    return Path(os.environ.get("BP_DB_PATH", "./data/bp/state.db")).resolve()
