"""End-to-end validation for the Orchestrator REST API (§9.4).

Boots the FastAPI app via uvicorn on a free port (in a background
thread), then drives every endpoint via httpx. Steps:

  1. Pre-check: Ollama is reachable; chat + embed models are pulled.
  2. Bring the server up; ``GET /v1/health`` returns 200.
  3. ``POST /v1/refresh`` creates an async task; poll ``GET /v1/tasks/{id}``
     until it completes and assert affected pages + queued escalations.
  4. ``POST /v1/queries`` for an in-domain question; verify status + answer.
  5. ``GET /v1/sme-questions`` lists at least one queue entry; the
     ``GET /v1/sme-questions/{id}`` view matches.
  6. ``POST /v1/sme-replies`` ingests an SME reply, ``ingest_sme_reply``
     runs (new BP doc + patch_page + clear queue). Re-list the queue and
     confirm it's empty for that question_id.
  7. Inspect OTel + LLM call log + service log for orchestrator entries.

Runs under a fresh temp directory so production state is never touched.
Exits non-zero on any contract failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from src.orchestrator.state import OrchestratorState  # noqa: E402
from src.otel_mcp.store import SpanStore  # noqa: E402
from src.shared.llm import embed_model, llm_model, ollama_host  # noqa: E402
from src.shared.llm_log import LLMCallLog  # noqa: E402
from src.shared.service_log import ServiceLogStore  # noqa: E402


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

def _h(title: str) -> None:
    bar = "=" * len(title)
    print(f"\n{bar}\n{title}\n{bar}")


# ---------------------------------------------------------------------------
# Pre-check
# ---------------------------------------------------------------------------

def precheck_ollama() -> list[str]:
    failures: list[str] = []
    base = ollama_host().rstrip("/")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{base}/api/tags", timeout=4) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        failures.append(f"Cannot reach Ollama at {base}: {exc}. Is `ollama serve` running?")
        return failures
    except Exception as exc:  # noqa: BLE001
        failures.append(f"Ollama tags check failed: {exc}")
        return failures
    names = {m.get("name", "") for m in data.get("models", [])}
    chat = llm_model()
    embed = embed_model()
    if not any(chat.split(":", 1)[0] in n for n in names):
        failures.append(f"Ollama up but LLM_MODEL={chat!r} missing. `ollama pull {chat}`")
    if not any(embed.split("/")[-1].split(":", 1)[0] in n for n in names):
        failures.append(f"Ollama up but RAG_EMBED_MODEL={embed!r} missing. `ollama pull {embed}`")
    return failures


# ---------------------------------------------------------------------------
# Synthetic corpus
# ---------------------------------------------------------------------------

INPUT_DOCS = {
    "business-cases/catalog-discovery.md": (
        "# Catalog Discovery\n\n"
        "Catalog Discovery is a flagship feature exposing new arrivals and "
        "personalised picks. Backed by the catalog-service (product metadata) "
        "and the recommendation-service (ranking). Owner: Maria Patel.\n"
    ),
    "business-cases/subscription-renewal.md": (
        "# Subscription Renewal\n\n"
        "Renews monthly memberships. Uses billing-service to charge the saved "
        "card, notification-service for the email, and account-service to "
        "downgrade after terminal failure.\n"
    ),
}

SD_STUB_MAPPING = {
    "catalog-discovery": [
        {"service": "catalog-service", "page_uri": "sd/services/catalog-service.md", "role": "primary"},
    ],
    "subscription-renewal": [
        {"service": "billing-service", "page_uri": "sd/services/billing-service.md", "role": "primary"},
    ],
}


# ---------------------------------------------------------------------------
# uvicorn-in-a-thread helper
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _ServerThread:
    """Run uvicorn on a background thread so the validation script owns the loop."""

    def __init__(self, app, *, host: str = "127.0.0.1", port: int):
        self._config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, name="oc-uvicorn", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


def _wait_for(url: str, *, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=2.0) as c:
                r = c.get(url)
                if r.status_code == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    failures: list[str] = []

    _h("0. Pre-check Ollama")
    pre = precheck_ollama()
    for f in pre:
        print(f"  FAIL: {f}")
    if pre:
        print("\n  Resolve the issues above, then re-run this script.")
        return 1
    print(f"  Ollama OK at {ollama_host()}; chat={llm_model()}, embed={embed_model()}")

    with tempfile.TemporaryDirectory() as workdir:
        inputs_root = os.path.join(workdir, "inputs")
        pages_root = os.path.join(workdir, "pages")
        bp_db = os.path.join(workdir, "bp_state.db")
        chroma_path = os.path.join(workdir, "chroma")
        oc_db = os.path.join(workdir, "oc_state.db")
        otel_db = os.path.join(workdir, "spans.db")
        audit_db = os.path.join(workdir, "audit.db")
        sd_stub_file = os.path.join(workdir, "sd_stub.json")

        # Seed inputs + SD stub.
        for rel, body in INPUT_DOCS.items():
            full = os.path.join(inputs_root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(body)
        with open(sd_stub_file, "w", encoding="utf-8") as f:
            json.dump(SD_STUB_MAPPING, f)

        # Point every persistent path at the temp dir BEFORE we build the app.
        os.environ["BP_INPUTS_ROOT"] = inputs_root
        os.environ["BP_PAGES_ROOT"] = pages_root
        os.environ["BP_DB_PATH"] = bp_db
        os.environ["RAG_CHROMA_PATH"] = chroma_path
        os.environ["OC_DB_PATH"] = oc_db
        os.environ["OTEL_DB_PATH"] = otel_db
        os.environ["AUDIT_DB_PATH"] = audit_db
        os.environ["BP_SD_STUB_MAPPING_FILE"] = sd_stub_file

        # Reset shared singletons that may have cached the previous env.
        from src.shared.llm_log import LLMCallLog as _LLM
        from src.shared.service_log import ServiceLogStore as _SLS
        _LLM.reset_default()
        _SLS.reset_default()

        # Build a fresh app with the env-derived paths.
        from src.orchestrator.server import build_app
        app = build_app()

        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        server = _ServerThread(app, port=port)

        _h("1. Boot Orchestrator and ping /v1/health")
        server.start()
        try:
            if not _wait_for(f"{base}/v1/health", timeout=30.0):
                failures.append("Server did not become healthy within 30s")
                return _finalize(failures, server)
            # Generous timeout: synchronous endpoints (`/v1/queries`,
            # `/v1/sme-replies`) trigger Auto-RAG and ToT chunking, which
            # routinely take 30–60s on a workstation Ollama instance.
            with httpx.Client(timeout=180.0) as c:
                r = c.get(f"{base}/v1/health")
                print(f"  /v1/health -> {r.status_code} {r.json()}")
                if r.status_code != 200 or r.json().get("status") != "ok":
                    failures.append("/v1/health did not return ok")

                _h("2. POST /v1/refresh — full refresh, poll /v1/tasks/{id}")
                r = c.post(f"{base}/v1/refresh", json={
                    "event_type": "trigger_refresh",
                    "doc_id_or_commit_sha": None,  # full refresh
                    "change_kind": "modified",
                })
                if r.status_code != 200:
                    failures.append(f"/v1/refresh returned {r.status_code}: {r.text}")
                    return _finalize(failures, server)
                refresh = r.json()
                print(f"  accepted task: {refresh}")
                task_id = refresh["task_id"]
                # Poll /v1/tasks/{id} until completed.
                deadline = time.time() + 120.0
                final_task = None
                while time.time() < deadline:
                    rt = c.get(f"{base}/v1/tasks/{task_id}")
                    if rt.status_code != 200:
                        failures.append(f"/v1/tasks/{task_id} returned {rt.status_code}")
                        break
                    body = rt.json()
                    if body["status"] in ("completed", "failed"):
                        final_task = body
                        break
                    time.sleep(0.5)
                if final_task is None:
                    failures.append("refresh task did not complete within 120s")
                    return _finalize(failures, server)
                print(f"  final status: {final_task['status']}")
                if final_task["status"] != "completed":
                    failures.append(f"refresh task ended with status={final_task['status']!r}: {final_task.get('error')}")
                else:
                    affected = (final_task.get("result") or {}).get("affected_pages") or []
                    escalations = (final_task.get("result") or {}).get("escalations") or []
                    print(f"  affected pages: {affected}")
                    print(f"  escalations: {[e.get('question_id') for e in escalations]}")
                    if len(affected) < 2:
                        failures.append(f"expected ≥2 affected pages, got {len(affected)}")

                _h("3. POST /v1/queries — Portal-style query")
                r = c.post(f"{base}/v1/queries", json={
                    "query": "Who owns Catalog Discovery and which services back it?",
                    "user_id": "validation-script",
                    "domain_hint": "bp",
                })
                if r.status_code != 200:
                    failures.append(f"/v1/queries returned {r.status_code}: {r.text}")
                else:
                    q = r.json()
                    print(f"  status={q['status']} dispatched_to={q['dispatched_to']} sources={len(q['sources'])}")
                    if q.get("answer"):
                        print(f"  answer: {(q['answer'] or '')[:240]}")
                    if q["status"] not in {"ok", "low_confidence"}:
                        failures.append(f"unexpected query status: {q['status']}")
                    if q["dispatched_to"] != "bp":
                        failures.append(f"expected dispatched_to=bp, got {q['dispatched_to']!r}")

                _h("4. GET /v1/sme-questions — list pending escalations")
                r = c.get(f"{base}/v1/sme-questions", params={"status": "pending"})
                if r.status_code != 200:
                    failures.append(f"/v1/sme-questions returned {r.status_code}: {r.text}")
                    return _finalize(failures, server)
                queue = r.json()
                print(f"  pending entries: {len(queue)}")
                for q in queue:
                    print(f"    {q['question_id']:48s} domain={q['domain']} pages={q['originating_pages']}")
                if not queue:
                    failures.append("expected ≥1 pending question after refresh")
                    return _finalize(failures, server)

                # Detail view for the first question.
                first = queue[0]
                qid = first["question_id"]
                originating_page = (first.get("originating_pages") or [None])[0]
                rd = c.get(f"{base}/v1/sme-questions/{qid}")
                if rd.status_code != 200 or rd.json()["question_id"] != qid:
                    failures.append(f"/v1/sme-questions/{qid} returned wrong payload")
                else:
                    print(f"  detail view ok: question_id={qid}")

                _h("5. POST /v1/sme-replies — ingest_sme_reply round-trip")
                reply_text = (
                    "recommendation-service is owned by the Recos team. "
                    "It will live at sd/services/recommendation-service.md "
                    "once the SD specialist documents it."
                )
                r = c.post(f"{base}/v1/sme-replies", json={
                    "question_id": qid,
                    "sme_id": "alice",
                    "sme_text": reply_text,
                })
                if r.status_code != 200:
                    failures.append(f"/v1/sme-replies returned {r.status_code}: {r.text}")
                    return _finalize(failures, server)
                reply = r.json()
                print(f"  new_doc_uri: {reply['new_doc_uri']}")
                patched = reply.get("patched_pages") or []
                print(f"  patched: {patched}")
                if not (reply.get("new_doc_uri") or "").startswith("bp/products/sme-replies/"):
                    failures.append("ingest_sme_reply returned an unexpected new_doc_uri")
                if not patched:
                    failures.append("ingest_sme_reply patched no pages")
                if not all(p.get("patched") for p in patched):
                    failures.append(f"some patches did not apply: {patched}")

                # Confirm queue cleared.
                rq = c.get(f"{base}/v1/sme-questions", params={"status": "pending"})
                still_pending = [q for q in rq.json() if q["question_id"] == qid]
                if still_pending:
                    failures.append(f"queue still has {qid} after reply")
                else:
                    print(f"  queue cleared for {qid}")

                # Verify the originating page no longer carries the placeholder.
                if originating_page:
                    page_path = os.path.join(pages_root, originating_page)
                    page_text = Path(page_path).read_text(encoding="utf-8")
                    if f"SME-PLACEHOLDER:{qid}" in page_text:
                        failures.append(f"placeholder fence for {qid} still in page text")
                    else:
                        print(f"  placeholder fence removed from {originating_page}")

                _h("6. Negative path — unknown question_id and unknown task_id")
                r = c.post(f"{base}/v1/sme-replies", json={
                    "question_id": "Q-doesnotexist",
                    "sme_id": "alice",
                    "sme_text": "ignored",
                })
                if r.status_code != 404:
                    failures.append(f"unknown question_id should 404, got {r.status_code}")
                else:
                    print("  unknown question_id correctly returns 404")
                r = c.get(f"{base}/v1/tasks/doesnotexist")
                if r.status_code != 404:
                    failures.append(f"unknown task_id should 404, got {r.status_code}")
                else:
                    print("  unknown task_id correctly returns 404")

            _h("7. Audit — OTel spans + service log + LLM log")
            spans = SpanStore(otel_db).query(service="orchestrator", limit=500)
            method_counts: dict[str, int] = {}
            for s in spans:
                method_counts[s.mcp_method] = method_counts.get(s.mcp_method, 0) + 1
            print(f"  orchestrator span counts: {method_counts}")
            for required in {"route_query", "run_refresh", "ingest_sme_reply"}:
                if method_counts.get(required, 0) == 0:
                    failures.append(f"missing OTel span for orchestrator.{required}")

            svc_log = ServiceLogStore(audit_db)
            oc_recent = svc_log.query(min_level="info", module_prefix="rag.oc.", limit=10)
            print(f"  orchestrator service log entries: {len(oc_recent)}")
            for r in reversed(oc_recent):
                t = time.strftime("%H:%M:%S", time.localtime(r.timestamp))
                print(f"    [{t}] {r.level.upper():5s} {r.module:24s} {r.message}")
            errs = svc_log.query(level="error", module_prefix="rag.oc.", limit=20)
            if errs:
                print("  ERROR-level rag.oc.* entries:")
                for e in errs:
                    print(f"    {e.module}: {e.message}")
                failures.append(f"{len(errs)} ERROR-level orchestrator log rows")

            llm_summary = LLMCallLog(audit_db).summarise_by_module()
            print(f"  LLM call modules touched: {sorted(llm_summary.keys())}")

            # Cross-check the in-DB queue is fully clear of `qid`.
            oc_state = OrchestratorState(oc_db)
            still = oc_state.get_question(qid)
            if still is not None:
                failures.append(f"orchestrator state still has {qid}")
            else:
                print(f"  orchestrator state confirms {qid} cleared")

        finally:
            server.shutdown()

    return _finalize(failures, None)


def _finalize(failures: list[str], server: _ServerThread | None) -> int:
    if server is not None:
        with contextlib.suppress(Exception):
            server.shutdown()
    _h("Result")
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        return 1
    print("  All Orchestrator REST contract checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
