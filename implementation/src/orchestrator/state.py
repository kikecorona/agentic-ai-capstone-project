"""Orchestrator state — pending SME questions + async task tracker (§9.4.3).

Two SQLite tables that live in the same per-service DB at ``$OC_DB_PATH``
(default ``./data/orchestrator/state.db``):

  * ``pending_sme_questions`` — the **only** durable content state the
    Orchestrator owns (§9.4.3). Each row is one open SME-escalation,
    keyed by ``question_id``. The schema mirrors the spec's envelope:
    ``topic, question, placeholder_id, originating_pages (JSON list),
    best_guess, retrieval_trail (JSON), assigned_sme, posted_at,
    answered_at, domain (bp|sd — which specialist owns the page that
    fenced the placeholder)``.

  * ``tasks`` — async-task tracker for the ``POST /v1/refresh`` flow.
    The REST endpoint accepts the event, returns ``{task_id,
    accepted_at}``, then a background worker drives the dispatch and
    writes the result back so ``GET /v1/tasks/{task_id}`` stays
    polling-friendly.

Resumability (§9.4.3 *Resumability*) is bought cheaply by writing every
state transition through SQLite — if the process dies mid-dispatch the
next start can recover the queue from disk.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_sme_questions (
    question_id        TEXT PRIMARY KEY,
    topic              TEXT NOT NULL,
    question           TEXT NOT NULL,
    placeholder_id     TEXT,
    best_guess         TEXT,
    retrieval_trail    TEXT NOT NULL DEFAULT '[]',  -- JSON list
    originating_pages  TEXT NOT NULL DEFAULT '[]',  -- JSON list of page URIs
    assigned_sme       TEXT,
    posted_at          REAL NOT NULL,
    answered_at        REAL,
    domain             TEXT NOT NULL                -- 'bp' | 'sd'
);
CREATE INDEX IF NOT EXISTS idx_pending_posted ON pending_sme_questions(posted_at);
CREATE INDEX IF NOT EXISTS idx_pending_sme    ON pending_sme_questions(assigned_sme);

CREATE TABLE IF NOT EXISTS tasks (
    task_id       TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,    -- 'portal_query' | 'trigger_refresh' | 'sme_reply'
    status        TEXT NOT NULL,    -- 'accepted' | 'in_progress' | 'completed' | 'failed'
    payload       TEXT NOT NULL,    -- JSON of the inbound event
    result        TEXT,             -- JSON of result on completion
    error         TEXT,             -- populated on failure
    started_at    REAL NOT NULL,
    completed_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_kind   ON tasks(kind);
"""


# ---------------------------------------------------------------------------
# Pending SME questions
# ---------------------------------------------------------------------------

@dataclass
class PendingQuestion:
    question_id: str
    topic: str
    question: str
    placeholder_id: str | None
    best_guess: str | None
    retrieval_trail: list[Any]
    originating_pages: list[str]
    assigned_sme: str | None
    posted_at: float
    answered_at: float | None
    domain: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "topic": self.topic,
            "question": self.question,
            "placeholder_id": self.placeholder_id,
            "best_guess": self.best_guess,
            "retrieval_trail": list(self.retrieval_trail),
            "originating_pages": list(self.originating_pages),
            "assigned_sme": self.assigned_sme,
            "posted_at": self.posted_at,
            "answered_at": self.answered_at,
            "domain": self.domain,
        }


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@dataclass
class Task:
    task_id: str
    kind: str
    status: str
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    started_at: float
    completed_at: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "status": self.status,
            "payload": dict(self.payload),
            "result": (dict(self.result) if isinstance(self.result, dict) else self.result),
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class OrchestratorState:
    """Thread-safe persistence for the queue + tasks. Single-file SQLite
    keeps recovery trivial and the schema obvious; the orchestrator does
    not need anything beefier for the POC."""

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

    # -- pending_sme_questions ---------------------------------------------

    def upsert_question(
        self,
        *,
        question_id: str,
        topic: str,
        question: str,
        domain: str,
        placeholder_id: str | None = None,
        best_guess: str | None = None,
        retrieval_trail: list[Any] | None = None,
        originating_pages: list[str] | None = None,
        assigned_sme: str | None = None,
        posted_at: float | None = None,
    ) -> PendingQuestion:
        """Open (or merge into) a pending question.

        When the same ``question_id`` is escalated by a second page, we
        merge ``originating_pages`` instead of overwriting (§9.4.3 dedup
        by topic so two pages hitting the same gap don't both page the
        SME)."""
        existing = self.get_question(question_id)
        merged_pages = list(existing.originating_pages) if existing else []
        for p in (originating_pages or []):
            if p not in merged_pages:
                merged_pages.append(p)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO pending_sme_questions (
                    question_id, topic, question, placeholder_id, best_guess,
                    retrieval_trail, originating_pages, assigned_sme, posted_at,
                    answered_at, domain
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(question_id) DO UPDATE SET
                    topic = excluded.topic,
                    question = excluded.question,
                    placeholder_id = COALESCE(excluded.placeholder_id, placeholder_id),
                    best_guess = COALESCE(excluded.best_guess, best_guess),
                    retrieval_trail = excluded.retrieval_trail,
                    originating_pages = excluded.originating_pages,
                    assigned_sme = COALESCE(excluded.assigned_sme, assigned_sme),
                    domain = excluded.domain
                """,
                (
                    question_id,
                    topic,
                    question,
                    placeholder_id,
                    best_guess,
                    json.dumps(list(retrieval_trail or [])),
                    json.dumps(merged_pages),
                    assigned_sme,
                    float(posted_at if posted_at is not None else time.time()),
                    domain,
                ),
            )
            self._conn.commit()
        out = self.get_question(question_id)
        assert out is not None
        return out

    def get_question(self, question_id: str) -> PendingQuestion | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_sme_questions WHERE question_id = ?",
                (question_id,),
            ).fetchone()
        return self._row_question(row) if row else None

    def list_questions(
        self,
        *,
        sme_id: str | None = None,
        status: str = "pending",
        limit: int = 200,
    ) -> list[PendingQuestion]:
        clauses: list[str] = []
        params: list[Any] = []
        if status == "pending":
            clauses.append("answered_at IS NULL")
        elif status == "answered":
            clauses.append("answered_at IS NOT NULL")
        # status == 'all' → no filter
        if sme_id is not None:
            clauses.append("assigned_sme = ?")
            params.append(sme_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM pending_sme_questions {where} ORDER BY posted_at ASC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_question(r) for r in rows]

    def mark_answered(self, question_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE pending_sme_questions SET answered_at = ? WHERE question_id = ?",
                (float(time.time()), question_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def delete_question(self, question_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM pending_sme_questions WHERE question_id = ?",
                (question_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # -- tasks --------------------------------------------------------------

    def create_task(self, *, kind: str, payload: dict[str, Any]) -> Task:
        task_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tasks (task_id, kind, status, payload, started_at)
                VALUES (?, ?, 'accepted', ?, ?)
                """,
                (task_id, kind, json.dumps(payload, default=str), now),
            )
            self._conn.commit()
        return Task(
            task_id=task_id,
            kind=kind,
            status="accepted",
            payload=dict(payload),
            result=None,
            error=None,
            started_at=now,
            completed_at=None,
        )

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        result: Any | None = None,
        error: str | None = None,
        completed: bool = False,
    ) -> Task | None:
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?"); params.append(status)
        if result is not None:
            sets.append("result = ?"); params.append(json.dumps(result, default=str))
        if error is not None:
            sets.append("error = ?"); params.append(error)
        if completed:
            sets.append("completed_at = ?"); params.append(float(time.time()))
        if not sets:
            return self.get_task(task_id)
        params.append(task_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE task_id = ?",
                params,
            )
            self._conn.commit()
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._row_task(row) if row else None

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _row_question(r: sqlite3.Row) -> PendingQuestion:
        return PendingQuestion(
            question_id=r["question_id"],
            topic=r["topic"],
            question=r["question"],
            placeholder_id=r["placeholder_id"],
            best_guess=r["best_guess"],
            retrieval_trail=json.loads(r["retrieval_trail"] or "[]"),
            originating_pages=json.loads(r["originating_pages"] or "[]"),
            assigned_sme=r["assigned_sme"],
            posted_at=float(r["posted_at"]),
            answered_at=(float(r["answered_at"]) if r["answered_at"] is not None else None),
            domain=r["domain"],
        )

    @staticmethod
    def _row_task(r: sqlite3.Row) -> Task:
        return Task(
            task_id=r["task_id"],
            kind=r["kind"],
            status=r["status"],
            payload=json.loads(r["payload"] or "{}"),
            result=(json.loads(r["result"]) if r["result"] else None),
            error=r["error"],
            started_at=float(r["started_at"]),
            completed_at=(float(r["completed_at"]) if r["completed_at"] is not None else None),
        )


def default_db_path() -> Path:
    return Path(os.environ.get("OC_DB_PATH", "./data/orchestrator/state.db")).resolve()
