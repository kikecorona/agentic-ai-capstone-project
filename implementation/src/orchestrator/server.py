"""Orchestrator REST API (§9.4.2).

Plain HTTP/REST is the right contract here — the upstream callers
(Documentation Portal, Update Trigger) are *not* LLM agents, so MCP would
add ceremony without value (§9.4.2 explicit). The Orchestrator continues
to *call* MCP downstream (BP_MCP, SD_MCP); the asymmetry is fine.

Seven §9.4.2 endpoints under ``/v1`` mirror the spec verbatim:

  * ``POST /v1/queries``                — Portal chatbot.
  * ``POST /v1/refresh``                — Update Trigger.
  * ``POST /v1/sme-replies``            — Portal SME answer UI.
  * ``GET  /v1/sme-questions``          — list pending questions (with optional sme_id filter).
  * ``GET  /v1/sme-questions/{id}``     — full detail of one question.
  * ``GET  /v1/tasks/{id}``             — poll an async refresh.
  * ``GET  /v1/health``                 — liveness for the trigger / external monitoring.

Plus two §9.8 portal-facing endpoints:

  * ``GET  /v1/streams/events``         — SSE; merged tail of both ``service_logs`` and
                                          ``llm_calls`` at $AUDIT_DB_PATH. Each event carries
                                          a ``kind`` discriminator so the client can format
                                          per-row.
  * ``GET  /v1/metrics``                — passthrough to the OTel ``get_metrics`` for the
                                          Telemetry tab.

For the POC the FastAPI app instantiates an in-process ``BPService`` and
a ``StubSDClient`` so the validation harness can drive the full pipeline
in one process. Production will swap the in-process clients for
MCP-backed adapters without touching the routes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.bp_service.clients import InProcessRAGClient, StubSDClient as BPStubSDClient
from src.bp_service.pages import LocalPageStore
from src.bp_service.service import BPService
from src.bp_service.store import (
    BPDocIndex,
    BPSourcesInventory,
    default_db_path as default_bp_db_path,
)
from src.rag_service.service import RAGService
from src.rag_service.store import EmbeddingsStore, default_persist_path as default_chroma_path
from src.shared.llm import get_embedding_function
from src.shared.otel_client import OTelClient
from src.shared.peer_clients import BPHttpClient, SDHttpClient, env_url
from src.shared.service_log import get_logger

from .clients import InProcessBPClient, StubSDClient
from .service import OrchestratorService
from .state import OrchestratorState, default_db_path as default_oc_db_path

log = get_logger("rag.oc.server")


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    query: str = Field(..., description="The user's question")
    user_id: str | None = Field(default=None, description="Authenticated user id (optional)")
    context: dict[str, Any] | None = Field(default=None, description="Free-form caller context")
    domain_hint: str | None = Field(
        default=None,
        description="Optional override: 'bp' | 'sd' | 'both'. Skips the Orchestrator's classifier.",
    )


class QueryResponse(BaseModel):
    status: str
    answer: str | None = None
    sources: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_trail: list[dict[str, Any]] = Field(default_factory=list)
    cross_references: list[dict[str, Any]] = Field(default_factory=list)
    dispatched_to: str


class RefreshRequest(BaseModel):
    event_type: str = Field(default="trigger_refresh")
    doc_id_or_commit_sha: str | None = Field(default=None)
    change_kind: str | None = Field(default="modified")
    source: str | None = Field(default=None)
    # When true, BP/SD bypass their `sources_inventory.is_unchanged()`
    # short-circuit and re-index every input doc regardless of
    # content-hash match. Useful for manual refreshes from the portal
    # where the operator wants to validate the pipeline end-to-end on
    # an already-indexed corpus.
    force: bool = Field(default=False)
    # Per-domain dispatch knob. ``None`` / ``"both"`` → fan out to both
    # specialists (legacy default). ``"sd"`` → SD only. ``"bp"`` → BP
    # only. The portal uses these to break the refresh into two
    # sequential per-domain calls so SME questions from SD become
    # visible BEFORE BP's leg even starts (§9.4.2 → see also the
    # orchestrator's ``_run_refresh_task`` ordering).
    domain: str | None = Field(default=None)


class RefreshResponse(BaseModel):
    task_id: str
    accepted_at: float
    status: str


class SMEReplyRequest(BaseModel):
    question_id: str
    sme_id: str | None = None
    sme_text: str


class SMEReplyResponse(BaseModel):
    status: str
    new_doc_uri: str | None = None
    patched_pages: list[dict[str, Any]] = Field(default_factory=list)
    cleared_question_id: str | None = None


class PendingQuestionView(BaseModel):
    question_id: str
    topic: str
    question: str
    placeholder_id: str | None = None
    best_guess: str | None = None
    retrieval_trail: list[Any] = Field(default_factory=list)
    originating_pages: list[str] = Field(default_factory=list)
    assigned_sme: str | None = None
    posted_at: float
    answered_at: float | None = None
    domain: str


class TaskView(BaseModel):
    task_id: str
    kind: str
    status: str
    payload: dict[str, Any]
    result: Any | None = None
    error: str | None = None
    started_at: float
    completed_at: float | None = None


class HealthResponse(BaseModel):
    status: str = "ok"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def build_app(
    *,
    bp_inputs_root: str | os.PathLike[str] | None = None,
    bp_pages_root: str | os.PathLike[str] | None = None,
    bp_db_path: str | os.PathLike[str] | None = None,
    chroma_path: str | os.PathLike[str] | None = None,
    oc_db_path: str | os.PathLike[str] | None = None,
    sd_stub_mapping: str | os.PathLike[str] | None = None,
) -> FastAPI:
    """Build a FastAPI app wired to a fresh OrchestratorService.

    All paths default to the corresponding ``$*_PATH`` env var so a single
    ``uvicorn src.orchestrator.server:app`` works against ``.env``.
    """
    bp_inputs_root = bp_inputs_root or os.environ.get("BP_INPUTS_ROOT", "./data/bp/inputs")
    bp_pages_root = bp_pages_root or os.environ.get("BP_PAGES_ROOT", "./data/bp/pages")
    bp_db_path = bp_db_path or os.environ.get("BP_DB_PATH", str(default_bp_db_path()))
    chroma_path = chroma_path or os.environ.get("RAG_CHROMA_PATH", str(default_chroma_path()))
    oc_db_path = oc_db_path or os.environ.get("OC_DB_PATH", str(default_oc_db_path()))
    sd_stub_mapping = sd_stub_mapping or os.environ.get("BP_SD_STUB_MAPPING_FILE")

    bp_url = env_url("BP_MCP_URL")
    sd_url = env_url("SD_MCP_URL")

    log.info(
        f"Orchestrator starting oc_db={oc_db_path} "
        f"bp_peer={bp_url or '(in-process)'} "
        f"sd_peer={sd_url or '(stub)'} "
        f"bp_inputs={bp_inputs_root if not bp_url else '(unused)'} "
        f"bp_pages={bp_pages_root if not bp_url else '(unused)'}"
    )

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Build the wiring once on startup. Two deployment modes:
        #   * Peer URLs set (multi-process / start_all): use MCP-HTTP
        #     clients only — the orchestrator owns no per-domain state
        #     beyond its queue + tasks.
        #   * Peer URLs absent (single-process validation harness):
        #     instantiate BP + RAG in-process so the FastAPI app works
        #     standalone.
        otel = OTelClient.from_env()
        bp_service = None
        rag_service = None

        if bp_url:
            bp_client = BPHttpClient(bp_url)
        else:
            rag_store = EmbeddingsStore(chroma_path, embedding_function=get_embedding_function())
            rag_service = RAGService(store=rag_store, otel=otel)
            page_store = LocalPageStore(inputs_root=bp_inputs_root, pages_root=bp_pages_root)
            bp_doc_index = BPDocIndex(bp_db_path)
            bp_sources = BPSourcesInventory(bp_db_path)
            bp_sd_stub_mapping = _load_json_mapping(sd_stub_mapping)
            bp_service = BPService(
                page_store=page_store,
                rag=InProcessRAGClient(rag_service),
                sd=BPStubSDClient(bp_sd_stub_mapping),
                doc_index=bp_doc_index,
                sources_inventory=bp_sources,
                otel=otel,
            )
            bp_client = InProcessBPClient(bp_service)

        sd_client = SDHttpClient(sd_url) if sd_url else StubSDClient()

        oc_state = OrchestratorState(oc_db_path)
        oc = OrchestratorService(
            bp=bp_client,
            sd=sd_client,
            state=oc_state,
            otel=otel,
        )

        app.state.otel = otel
        app.state.bp = bp_service
        app.state.oc = oc
        app.state.oc_state = oc_state
        log.info("Orchestrator wiring ready")
        try:
            yield
        finally:
            oc.close()
            # Tear down any HTTP-backed peer clients so their daemon
            # threads exit cleanly.
            for c in (bp_client, sd_client):
                close = getattr(c, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
            log.info("Orchestrator shut down cleanly")

    app = FastAPI(
        title="Capstone POC — Orchestrator",
        version="0.1.0",
        lifespan=_lifespan,
    )

    # CORS — the Quasar dev server runs on a different origin (default
    # http://127.0.0.1:9000) so the browser refuses to call our API
    # without explicit allow-origin headers. Production deployments
    # would lock this down to the portal's real origin or terminate
    # both behind one reverse proxy and disable this entirely.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.environ.get("OC_CORS_ORIGINS", "*").split(","),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ----------------------------------------------------------- /v1/health
    @app.get("/v1/health", response_model=HealthResponse, tags=["meta"])
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    # ----------------------------------------------------------- /v1/queries
    @app.post("/v1/queries", response_model=QueryResponse, tags=["query"])
    def post_query(req: QueryRequest) -> QueryResponse:
        oc: OrchestratorService = app.state.oc
        try:
            res = oc.handle_query(
                query=req.query,
                user_id=req.user_id,
                context=req.context,
                domain_hint=req.domain_hint,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return QueryResponse(**res.to_dict())

    # ----------------------------------------------------------- /v1/refresh
    @app.post("/v1/refresh", response_model=RefreshResponse, tags=["refresh"])
    def post_refresh(req: RefreshRequest) -> RefreshResponse:
        oc: OrchestratorService = app.state.oc
        event = req.model_dump(exclude_none=False)
        try:
            task = oc.handle_refresh(event=event)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return RefreshResponse(
            task_id=task.task_id,
            accepted_at=task.started_at,
            status=task.status,
        )

    # ------------------------------------------------------- /v1/sme-replies
    @app.post("/v1/sme-replies", response_model=SMEReplyResponse, tags=["sme"])
    def post_sme_reply(req: SMEReplyRequest) -> SMEReplyResponse:
        oc: OrchestratorService = app.state.oc
        try:
            res = oc.handle_sme_reply(
                question_id=req.question_id,
                sme_id=req.sme_id,
                sme_text=req.sme_text,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if res.status == "not_found":
            raise HTTPException(status_code=404, detail=f"unknown question_id {req.question_id!r}")
        return SMEReplyResponse(**res.to_dict())

    # ----------------------------------------------------- /v1/sme-questions
    @app.get(
        "/v1/sme-questions",
        response_model=list[PendingQuestionView],
        tags=["sme"],
    )
    def list_sme_questions(
        sme_id: str | None = Query(default=None),
        status: str = Query(default="pending", pattern="^(pending|answered|all)$"),
        limit: int = Query(default=200, ge=1, le=2000),
    ) -> list[PendingQuestionView]:
        oc: OrchestratorService = app.state.oc
        rows = oc.list_pending_questions(sme_id=sme_id, status=status, limit=limit)
        return [PendingQuestionView(**r.to_dict()) for r in rows]

    @app.get(
        "/v1/sme-questions/{question_id}",
        response_model=PendingQuestionView,
        tags=["sme"],
    )
    def get_sme_question(question_id: str) -> PendingQuestionView:
        oc: OrchestratorService = app.state.oc
        row = oc.get_pending_question(question_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown question_id {question_id!r}")
        return PendingQuestionView(**row.to_dict())

    # ----------------------------------------------------------- /v1/tasks
    @app.get("/v1/tasks/{task_id}", response_model=TaskView, tags=["refresh"])
    def get_task(task_id: str) -> TaskView:
        oc: OrchestratorService = app.state.oc
        task = oc.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"unknown task_id {task_id!r}")
        return TaskView(**task.to_dict())

    # ============================================================
    # §9.8 portal-facing endpoints — SSE event stream + metrics passthrough
    # ============================================================

    @app.get("/v1/streams/events", tags=["portal"])
    async def stream_events(
        since_service_id: int = Query(default=0, ge=0),
        since_llm_id: int = Query(default=0, ge=0),
        since_seconds_ago: float | None = Query(default=None, ge=0, le=86400),
        module_prefix: str | None = Query(default=None),
        poll_seconds: float = Query(default=1.0, ge=0.25, le=10.0),
    ) -> StreamingResponse:
        """Merged server-sent events tail of both ``service_logs`` and
        ``llm_calls`` at ``$AUDIT_DB_PATH``. Each emitted event carries a
        ``kind: "service" | "llm"`` discriminator so the client can format
        per-row without subscribing to two separate streams.

        Cursors are independent per table (``since_service_id`` /
        ``since_llm_id``) — the browser's ``EventSource`` reconnect uses
        the composite SSE id (``s:N`` or ``l:N``) to resume; we surface
        both as explicit query params so the contract is visible.

        ``since_seconds_ago`` is a convenience: when provided it computes
        the starting cursors by timestamp (the largest id whose timestamp
        is **older** than the cutoff), so the client can pass e.g. ``3600``
        to start the stream with the last hour of history.
        ``since_*_id`` overrides this when both are non-zero.
        """
        audit_db = os.environ.get("AUDIT_DB_PATH", "./data/audit/log.db")

        if since_seconds_ago is not None and since_seconds_ago > 0:
            cutoff = time.time() - float(since_seconds_ago)
            if since_service_id == 0:
                since_service_id = _max_id_before(audit_db, "service_logs", "timestamp", cutoff)
            if since_llm_id == 0:
                since_llm_id = _max_id_before(audit_db, "llm_calls", "started_at", cutoff)

        return StreamingResponse(
            _tail_combined_events(
                audit_db,
                since_service_id=since_service_id,
                since_llm_id=since_llm_id,
                module_prefix=module_prefix,
                poll_seconds=poll_seconds,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/metrics", tags=["portal"])
    def get_metrics(
        service: str | None = Query(default=None),
        since: float | None = Query(default=None),
        until: float | None = Query(default=None),
    ) -> dict[str, Any]:
        """Synchronous passthrough to the OTel ``get_metrics`` tool. The
        portal's Telemetry tab polls this on a 5s interval.

        Reads the OTel SQLite store directly (no MCP round-trip) since
        we're already inside the orchestrator process and the wire
        contract is identical.

        Augmented with an ``llm`` section sourced from the audit DB's
        ``llm_calls`` table — ``LLMCallLog.summarise_by_module`` already
        returns per-module counts/error/p50/p95/mean/max so the Agent
        Metrics dashboard can render LLM latency without a second round
        trip.
        """
        from src.otel_mcp.store import SpanStore  # local import to keep portal-free boots clean
        from src.shared.llm_log import LLMCallLog

        otel_db = os.environ.get("OTEL_DB_PATH", "./data/otel/spans.db")
        store = SpanStore(otel_db)
        out = store.get_metrics(service=service, since=since, until=until)

        # LLM aggregates (per-module + an overall roll-up). Failure here
        # must not poison the whole metrics payload — the OTel half is
        # still useful on its own.
        try:
            llm_db = os.environ.get("AUDIT_DB_PATH", "./data/audit/log.db")
            llm_log = LLMCallLog(llm_db)
            per_module = llm_log.summarise_by_module(since=since, until=until)
            out["llm"] = {
                "by_module": per_module,
                "overall": _llm_overall_rollup(per_module),
            }
        except Exception as exc:  # noqa: BLE001
            out["llm"] = {"by_module": {}, "overall": {}, "error": str(exc)}

        return out

    return app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _llm_overall_rollup(per_module: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Roll the per-module LLM summary into a single overall row.

    Counts and errors sum directly. Latency p50 / p95 are recomputed
    across every module's call list — the per-module dict only carries
    pre-computed quantiles, but a count-weighted re-quantile is close
    enough for a dashboard headline (true p95 needs raw samples; this
    over-weights modules that ran many slow calls without distorting
    the overall shape).
    """
    if not per_module:
        return {"count": 0, "errors": 0, "latency_ms": {"p50": 0, "p95": 0, "mean": 0, "max": 0}}
    total = sum(int(v.get("count", 0)) for v in per_module.values())
    errors = sum(int(v.get("errors", 0)) for v in per_module.values())
    if not total:
        return {"count": 0, "errors": errors, "latency_ms": {"p50": 0, "p95": 0, "mean": 0, "max": 0}}
    weighted_p50 = 0.0
    weighted_p95 = 0.0
    weighted_mean = 0.0
    overall_max = 0.0
    for v in per_module.values():
        n = int(v.get("count", 0))
        if not n:
            continue
        lat = v.get("latency_ms", {}) or {}
        weighted_p50 += float(lat.get("p50", 0) or 0) * n
        weighted_p95 += float(lat.get("p95", 0) or 0) * n
        weighted_mean += float(lat.get("mean", 0) or 0) * n
        overall_max = max(overall_max, float(lat.get("max", 0) or 0))
    return {
        "count": total,
        "errors": errors,
        "latency_ms": {
            "p50": weighted_p50 / total,
            "p95": weighted_p95 / total,
            "mean": weighted_mean / total,
            "max": overall_max,
        },
    }


def _load_json_mapping(path: str | os.PathLike[str] | None) -> dict[str, list[dict[str, Any]]]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        log.warn(f"BP_SD_STUB_MAPPING_FILE={p} not found; using empty SD stub")
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            log.error(f"BP_SD_STUB_MAPPING_FILE {p}: top-level must be a JSON object")
            return {}
        return data
    except Exception as exc:  # noqa: BLE001
        log.error(f"BP_SD_STUB_MAPPING_FILE {p}: parse failed: {exc}")
        return {}


# ---------------------------------------------------------------------------
# §9.8 SSE plumbing — tail both audit-DB tables and merge by timestamp
# ---------------------------------------------------------------------------

async def _tail_combined_events(
    db_path: str,
    *,
    since_service_id: int,
    since_llm_id: int,
    module_prefix: str | None,
    poll_seconds: float,
) -> AsyncIterator[bytes]:
    """Generic SSE generator: poll both ``service_logs`` and ``llm_calls``
    every ``poll_seconds`` and yield ``data: {json}`` frames merged by
    timestamp (oldest first within a batch).

    Each frame's ``data`` payload is the row dict plus a ``kind`` field
    set to ``"service"`` or ``"llm"`` so the client can format per-row.
    The SSE ``id:`` field is composite (``s:N`` / ``l:N``) — that's what
    the browser sends back as ``Last-Event-ID`` on reconnect; we parse
    the prefix to advance the matching cursor.
    """
    service_cursor = max(0, since_service_id)
    llm_cursor = max(0, since_llm_id)

    yield b": tail open\n\n"

    while True:
        try:
            service_rows = _fetch_audit_rows(
                db_path, "service_logs", service_cursor, module_prefix,
            )
        except sqlite3.OperationalError:
            service_rows = []
        try:
            llm_rows = _fetch_audit_rows(
                db_path, "llm_calls", llm_cursor, module_prefix,
            )
        except sqlite3.OperationalError:
            llm_rows = []

        # Merge by timestamp so the client sees a chronological stream
        # even when both tables had new rows in the same poll cycle.
        merged: list[tuple[float, str, sqlite3.Row]] = [
            (float(r["timestamp"]), "service", r) for r in service_rows
        ] + [
            (float(r["started_at"]), "llm", r) for r in llm_rows
        ]
        merged.sort(key=lambda triple: triple[0])

        for _, kind, row in merged:
            if kind == "service":
                service_cursor = max(service_cursor, int(row["id"]))
                event = _service_log_row_to_event(row)
                sse_id = f"s:{row['id']}"
            else:
                llm_cursor = max(llm_cursor, int(row["id"]))
                event = _llm_call_row_to_event(row)
                sse_id = f"l:{row['id']}"
            yield (
                f"id: {sse_id}\ndata: {json.dumps(event, default=str)}\n\n"
            ).encode("utf-8")

        try:
            await asyncio.sleep(poll_seconds)
        except asyncio.CancelledError:
            return


def _max_id_before(
    db_path: str,
    table: str,
    time_col: str,
    cutoff: float,
) -> int:
    """Return the largest row id in ``table`` whose ``time_col`` is
    strictly less than ``cutoff`` — used to seed the SSE cursor for the
    ``since_seconds_ago`` convenience param.

    Returns ``0`` if the table is empty, missing, or every row sits
    inside the requested window (cursor=0 → emit everything).
    """
    if not Path(db_path).exists():
        return 0
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            f"SELECT MAX(id) FROM {table} WHERE {time_col} < ?",
            (float(cutoff),),
        ).fetchone()
    except sqlite3.OperationalError:
        # Table doesn't exist yet on first boot.
        return 0
    finally:
        conn.close()
    return int(row[0] or 0)


def _fetch_audit_rows(
    db_path: str,
    table: str,
    since_id: int,
    module_prefix: str | None,
) -> list[sqlite3.Row]:
    """One blocking SQLite read — fast, ID-indexed."""
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        clauses = ["id > ?"]
        params: list[Any] = [int(since_id)]
        if module_prefix:
            clauses.append("module LIKE ?")
            params.append(module_prefix.rstrip("%") + "%")
        where = " AND ".join(clauses)
        # Cap per-poll so a long-disconnect client doesn't ship 100k rows
        # in one frame — they'll catch up on the next poll.
        sql = f"SELECT * FROM {table} WHERE {where} ORDER BY id ASC LIMIT 500"
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _service_log_row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "kind": "service",
        "id": int(row["id"]),
        "module": row["module"],
        "level": row["level"],
        "timestamp": float(row["timestamp"]),
        "message": row["message"],
    }


def _llm_call_row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "kind": "llm",
        "id": int(row["id"]),
        "module": row["module"],
        # Mirror started_at as ``timestamp`` too so the client can
        # treat both kinds uniformly when sorting / formatting.
        "timestamp": float(row["started_at"]),
        "started_at": float(row["started_at"]),
        "latency_ms": float(row["latency_ms"]),
        "model": row["model"],
        "temperature": (float(row["temperature"]) if row["temperature"] is not None else None),
        "json_mode": bool(row["json_mode"]),
        "request": row["request"],
        "response": row["response"],
        "error": row["error"],
    }



# Module-level app for `uvicorn src.orchestrator.server:app`.
app = build_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Orchestrator REST server")
    parser.add_argument("--host", default=os.environ.get("OC_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("OC_PORT", "8000")))
    args = parser.parse_args()
    import uvicorn  # imported here so `uvicorn` doesn't load on every import

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
