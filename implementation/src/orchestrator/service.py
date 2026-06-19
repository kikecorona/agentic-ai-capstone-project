"""Orchestrator service — the supervisor (§9.4).

Receives work from the Documentation Portal and the Update Trigger,
routes it to the right specialist, runs the §9.5 SME loop's
re-integration step on inbound replies, and persists task state. Owns
no content state of its own — only the queue + the task tracker (§9.4.3).

The Orchestrator's "ReAct loop" in the spec collapses to a deterministic
state machine in the POC because the action space is tiny — three event
types in, one of three actions out. Replacing the dispatcher with an
LLM-driven ``reason`` step is a future change that doesn't touch the
contract upstream callers depend on.

Span emission and service log entries are emitted at every entry/exit so
the trace stream covers the full Portal → Orchestrator → Specialist
chain (§9.6). Audit DB rows complement OTel spans with the in-service
view.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from src.shared.otel_client import OTelClient
from src.shared.service_log import get_logger

from .clients import BPClient, SDClient
from .routing import (
    DispatchTarget,
    owning_specialist_for_page,
    pick_dispatch_target_for_query,
    pick_dispatch_target_for_refresh,
)
from .state import OrchestratorState, PendingQuestion, Task, default_db_path

log = get_logger("rag.oc.service")
SERVICE_NAME = "orchestrator"


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

@dataclass
class QueryResult:
    status: str
    answer: str | None
    sources: list[dict[str, Any]]
    retrieval_trail: list[dict[str, Any]]
    cross_references: list[dict[str, Any]]
    dispatched_to: str  # 'bp' | 'sd' | 'both'

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "answer": self.answer,
            "sources": list(self.sources),
            "retrieval_trail": list(self.retrieval_trail),
            "cross_references": list(self.cross_references),
            "dispatched_to": self.dispatched_to,
        }


@dataclass
class SMEReplyResult:
    status: str
    new_doc_uri: str | None
    patched_pages: list[dict[str, Any]]
    cleared_question_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "new_doc_uri": self.new_doc_uri,
            "patched_pages": list(self.patched_pages),
            "cleared_question_id": self.cleared_question_id,
        }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class OrchestratorService:
    """In-process Orchestrator façade.

    Holds the durable :class:`OrchestratorState`, the wiring to BP/SD
    clients, and a small thread pool that backs the async refresh path.
    """

    def __init__(
        self,
        *,
        bp: BPClient,
        sd: SDClient,
        state: OrchestratorState | None = None,
        otel: OTelClient | None = None,
        worker_pool: ThreadPoolExecutor | None = None,
    ):
        self._bp = bp
        self._sd = sd
        self._state = state or OrchestratorState(default_db_path())
        self._otel = otel or OTelClient.from_env()
        # Bounded so a runaway refresh fan-out can't pin the host.
        self._workers = worker_pool or ThreadPoolExecutor(max_workers=4, thread_name_prefix="oc-worker")
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._workers.shutdown(wait=False, cancel_futures=False)

    @property
    def state(self) -> OrchestratorState:
        return self._state

    # ------------------------------------------------------------ /v1/queries

    def handle_query(
        self,
        *,
        query: str,
        user_id: str | None = None,
        context: dict[str, Any] | None = None,
        domain_hint: str | None = None,
    ) -> QueryResult:
        """Synchronous Portal-query handler (§9.4.2 ``POST /v1/queries``).

        Picks the right specialist, dispatches, and returns the merged
        response. Query-mode never escalates — the caller always gets a
        concrete status back.
        """
        if not query or not query.strip():
            log.error("handle_query: empty query")
            raise ValueError("query is required")
        target = pick_dispatch_target_for_query(query, domain_hint=domain_hint)
        log.info(
            f"handle_query user={user_id or '?'} hint={domain_hint!r} "
            f"-> dispatched_to={target.label}"
        )
        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="route_query",
            mcp_domain=target.label,
        ) as span:
            span.set_attribute("user_id", user_id or "")
            span.set_attribute("query_chars", len(query))

            results: list[dict[str, Any]] = []
            if target.bp:
                with self._otel.span(
                    service=SERVICE_NAME,
                    mcp_method="dispatch_to_bp",
                    mcp_domain="bp",
                ) as inner:
                    bp_resp = self._bp.dispatch_query(
                        query=query,
                        domain_hint=target.to_domain_filter(),
                        context=context,
                    )
                    inner.set_status(str(bp_resp.get("status", "exhausted")))
                    inner.set_payload_summary({
                        "sources_count": len(bp_resp.get("sources") or []),
                    })
                    results.append({"specialist": "bp", **bp_resp})
            if target.sd:
                with self._otel.span(
                    service=SERVICE_NAME,
                    mcp_method="dispatch_to_sd",
                    mcp_domain="sd",
                ) as inner:
                    sd_resp = self._sd.dispatch_query(
                        query=query,
                        domain_hint=target.to_domain_filter(),
                        context=context,
                    )
                    inner.set_status(str(sd_resp.get("status", "exhausted")))
                    inner.set_payload_summary({
                        "sources_count": len(sd_resp.get("sources") or []),
                    })
                    results.append({"specialist": "sd", **sd_resp})

            merged = _merge_query_results(results, target)
            span.set_status(merged.status)
            span.set_payload_summary({
                "specialists": [r["specialist"] for r in results],
                "sources_count": len(merged.sources),
            })
            log.info(
                f"handle_query done status={merged.status} "
                f"sources={len(merged.sources)} dispatched_to={merged.dispatched_to}"
            )
            return merged

    # ----------------------------------------------------------- /v1/refresh

    def handle_refresh(self, *, event: dict[str, Any]) -> Task:
        """Asynchronous refresh handler (§9.4.2 ``POST /v1/refresh``).

        Creates a task row, hands the dispatch off to the worker pool,
        and returns immediately so the caller can poll
        ``GET /v1/tasks/{task_id}``.
        """
        if not isinstance(event, dict):
            log.error("handle_refresh: event must be a dict")
            raise ValueError("event must be a JSON object")
        task = self._state.create_task(kind="trigger_refresh", payload=event)
        log.info(
            f"handle_refresh accepted task={task.task_id} "
            f"src={event.get('doc_id_or_commit_sha') or '(full refresh)'!r}"
        )
        self._workers.submit(self._run_refresh_task, task.task_id, dict(event))
        return task

    def _run_refresh_task(self, task_id: str, event: dict[str, Any]) -> None:
        """Worker entry-point — runs the dispatch + records the result.

        Errors are caught so a failure never escapes into the executor's
        unhandled-exception path; the task row records ``status='failed'``
        with the exception text for ``GET /v1/tasks/{id}`` to surface.
        """
        self._state.update_task(task_id, status="in_progress")
        try:
            with self._otel.span(
                service=SERVICE_NAME,
                mcp_method="run_refresh",
            ) as span:
                target = pick_dispatch_target_for_refresh(event)
                span.set_attribute("dispatched_to", target.label)

                affected_pages: list[str] = []
                escalations: list[dict[str, Any]] = []
                details: list[dict[str, Any]] = []

                if target.bp:
                    with self._otel.span(
                        service=SERVICE_NAME,
                        mcp_method="dispatch_to_bp",
                        mcp_domain="bp",
                    ) as inner:
                        bp_out = self._bp.dispatch_refresh(event=event)
                        affected_pages.extend(bp_out.get("affected_pages") or [])
                        escalations.extend(bp_out.get("escalations") or [])
                        details.extend(_tag_details(bp_out.get("details") or [], "bp"))
                        inner.set_status("ok")
                        inner.set_payload_summary({
                            "affected_pages": len(bp_out.get("affected_pages") or []),
                            "escalations": len(bp_out.get("escalations") or []),
                        })
                if target.sd:
                    with self._otel.span(
                        service=SERVICE_NAME,
                        mcp_method="dispatch_to_sd",
                        mcp_domain="sd",
                    ) as inner:
                        sd_out = self._sd.dispatch_refresh(event=event)
                        affected_pages.extend(sd_out.get("affected_pages") or [])
                        escalations.extend(sd_out.get("escalations") or [])
                        details.extend(_tag_details(sd_out.get("details") or [], "sd"))
                        inner.set_status("ok")
                        inner.set_payload_summary({
                            "affected_pages": len(sd_out.get("affected_pages") or []),
                            "escalations": len(sd_out.get("escalations") or []),
                        })

                # Open / merge a queue entry for every escalation so the
                # SME UI can pick them up. Dedup is by ``question_id``;
                # the state layer handles merging ``originating_pages``.
                queued = 0
                for env in escalations:
                    qid = env.get("question_id")
                    if not qid:
                        continue
                    domain = env.get("domain")
                    if not domain:
                        # Derive from the originating page URI when present.
                        page = env.get("originating_page")
                        domain = owning_specialist_for_page(page) or "bp"
                    pages = env.get("originating_pages") or (
                        [env["originating_page"]] if env.get("originating_page") else []
                    )
                    self._state.upsert_question(
                        question_id=qid,
                        topic=str(env.get("topic") or "(no topic)"),
                        question=str(env.get("question") or ""),
                        domain=str(domain),
                        placeholder_id=env.get("placeholder_id"),
                        best_guess=env.get("best_guess"),
                        retrieval_trail=env.get("retrieval_trail") or [],
                        originating_pages=pages,
                        assigned_sme=env.get("assigned_sme"),
                    )
                    queued += 1
                if queued:
                    log.info(f"handle_refresh queued {queued} escalation(s) into pending_sme_questions")

                span.set_status("ok")
                span.set_payload_summary({
                    "affected_pages": len(affected_pages),
                    "escalations": len(escalations),
                    "queued": queued,
                })

                self._state.update_task(
                    task_id,
                    status="completed",
                    result={
                        "affected_pages": affected_pages,
                        "escalations": escalations,
                        "details": details,
                        "dispatched_to": target.label,
                    },
                    completed=True,
                )
                log.info(
                    f"handle_refresh done task={task_id} "
                    f"affected_pages={len(affected_pages)} escalations={len(escalations)}"
                )
        except Exception as exc:  # noqa: BLE001 — record + continue
            log.error(f"handle_refresh task={task_id} failed: {exc}")
            self._state.update_task(
                task_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                completed=True,
            )

    # --------------------------------------------------------- /v1/sme-replies

    def handle_sme_reply(
        self,
        *,
        question_id: str,
        sme_id: str | None,
        sme_text: str,
    ) -> SMEReplyResult:
        """Run the §9.5 / §9.4.3 ``ingest_sme_reply`` step.

        Sequence:
          1. Look up the queue entry to find originating pages + domain.
          2. Persist the reply as a new BP doc via ``BP_MCP.ingest_sme_doc``.
          3. For each originating page, ask the owning specialist to
             ``patch_page`` (replace the fenced block with the SME text +
             relative link to the new doc).
          4. Mark the queue entry answered + remove from
             ``pending_sme_questions``.
        """
        if not (question_id and sme_text):
            log.error("handle_sme_reply: question_id and sme_text are required")
            raise ValueError("question_id and sme_text are required")

        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="ingest_sme_reply",
        ) as span:
            entry = self._state.get_question(question_id)
            if entry is None:
                log.warn(f"handle_sme_reply: no pending question {question_id!r}")
                span.set_status("not_found")
                return SMEReplyResult(
                    status="not_found",
                    new_doc_uri=None,
                    patched_pages=[],
                    cleared_question_id=None,
                )
            span.set_attribute("originating_pages", len(entry.originating_pages))
            log.info(
                f"handle_sme_reply question={question_id} sme={sme_id!r} "
                f"originating_pages={len(entry.originating_pages)}"
            )

            # 1. Persist as a fresh BP doc.
            with self._otel.span(
                service=SERVICE_NAME,
                mcp_method="ingest_sme_doc",
                mcp_domain="bp",
            ) as inner:
                ingest_out = self._bp.ingest_sme_doc(
                    question_id=question_id,
                    sme_text=sme_text,
                    originating_pages=list(entry.originating_pages),
                )
                inner.set_status("ok")
                inner.set_payload_summary({
                    "new_page_uri": ingest_out.get("new_page_uri"),
                })
            new_doc_uri = ingest_out.get("new_page_uri")

            # 2. Patch every originating page through its owning specialist.
            patched: list[dict[str, Any]] = []
            link_md = (
                f"> **SME answer (recorded {question_id})**\n>\n"
                f"> {sme_text.strip()}\n>\n"
                f"> See [`{new_doc_uri}`](../../{new_doc_uri}) for the canonical reply."
                if new_doc_uri
                else f"> **SME answer (recorded {question_id})**\n>\n> {sme_text.strip()}"
            )
            for page in entry.originating_pages:
                owner = owning_specialist_for_page(page) or entry.domain
                client = self._bp if owner == "bp" else self._sd
                try:
                    res = client.patch_page(
                        page_uri=page,
                        question_id=question_id,
                        replacement=link_md,
                    )
                    patched.append({
                        "page_uri": page,
                        "owner": owner,
                        "patched": bool(res.get("patched")),
                        "commit_sha": res.get("commit_sha"),
                    })
                except Exception as exc:  # noqa: BLE001 — keep going across pages
                    log.error(f"handle_sme_reply: patch_page({page!r}) failed: {exc}")
                    patched.append({
                        "page_uri": page,
                        "owner": owner,
                        "patched": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    })

            # 3. Clear the queue entry — mark answered for audit, then
            # remove so subsequent ``GET /v1/sme-questions?status=pending``
            # no longer surfaces it.
            self._state.mark_answered(question_id)
            self._state.delete_question(question_id)

            span.set_status("ok")
            span.set_payload_summary({
                "new_doc_uri": new_doc_uri,
                "patched_pages": sum(1 for p in patched if p.get("patched")),
                "originating_pages": len(entry.originating_pages),
            })
            log.info(
                f"handle_sme_reply done question={question_id} "
                f"new_doc={new_doc_uri} patched={sum(1 for p in patched if p.get('patched'))}/{len(patched)}"
            )
            return SMEReplyResult(
                status="ok",
                new_doc_uri=new_doc_uri,
                patched_pages=patched,
                cleared_question_id=question_id,
            )

    # ---------------------------------------------- queue + task introspection

    def list_pending_questions(
        self,
        *,
        sme_id: str | None = None,
        status: str = "pending",
        limit: int = 200,
    ) -> list[PendingQuestion]:
        return self._state.list_questions(sme_id=sme_id, status=status, limit=limit)

    def get_pending_question(self, question_id: str) -> PendingQuestion | None:
        return self._state.get_question(question_id)

    def get_task(self, task_id: str) -> Task | None:
        return self._state.get_task(task_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _merge_query_results(results: list[dict[str, Any]], target: DispatchTarget) -> QueryResult:
    """Merge specialist responses for a Portal query.

    With one specialist the merge is a passthrough. With two we
    concatenate sources/cross-refs and pick the strongest status
    (``ok`` > ``low_confidence`` > ``exhausted``)."""
    if not results:
        return QueryResult(
            status="exhausted",
            answer=None,
            sources=[],
            retrieval_trail=[],
            cross_references=[],
            dispatched_to=target.label,
        )
    if len(results) == 1:
        r = results[0]
        return QueryResult(
            status=str(r.get("status", "exhausted")),
            answer=r.get("answer"),
            sources=list(r.get("sources") or []),
            retrieval_trail=list(r.get("retrieval_trail") or []),
            cross_references=list(r.get("cross_references") or []),
            dispatched_to=target.label,
        )

    # Two specialists: pick the strongest status overall.
    rank = {"ok": 0, "low_confidence": 1, "exhausted": 2, "error": 3}
    results.sort(key=lambda r: rank.get(str(r.get("status", "exhausted")), 99))
    primary = results[0]
    answer_parts = [
        f"From {r['specialist'].upper()}: {r.get('answer')}"
        for r in results
        if r.get("answer")
    ]
    answer = "\n\n".join(answer_parts) if answer_parts else None
    return QueryResult(
        status=str(primary.get("status", "exhausted")),
        answer=answer,
        sources=[s for r in results for s in (r.get("sources") or [])],
        retrieval_trail=[t for r in results for t in (r.get("retrieval_trail") or [])],
        cross_references=[x for r in results for x in (r.get("cross_references") or [])],
        dispatched_to=target.label,
    )


def _tag_details(details: list[dict[str, Any]], specialist: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in details or []:
        item = dict(d)
        item.setdefault("specialist", specialist)
        out.append(item)
    return out
