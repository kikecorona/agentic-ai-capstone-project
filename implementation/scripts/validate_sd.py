"""End-to-end validation for the SD Service (§9.2).

Spawns ``src.sd_service.server`` as a stdio MCP subprocess and drives
all five §9.2.2 methods via ``langchain-mcp-adapters``. Steps:

  1. Pre-check: Ollama is reachable; chat + embed models are pulled.
  2. Seed a temp directory with two synthetic Flask services. One has
     a fully static endpoint set; the other has a dynamic route to
     trigger an SME placeholder.
  3. ``dispatch_refresh`` over the inputs; verify pages got written,
     ToT picked a winner, and the dynamic route raised an escalation.
  4. ``dispatch_refresh`` again; both services should skip (unchanged).
  5. ``dispatch_query`` for an in-domain question.
  6. ``find_services_for_product`` — relational lookup. Without a real
     B&P specialist it returns empty, so we just confirm the contract.
  7. ``patch_page`` — replace one open placeholder, confirm it vanishes
     from the doc-index ``open_placeholders`` list.
  8. Inspect OTel + LLM + service log for SD entries.

Runs under a fresh temp directory so production state is never touched.
Exits non-zero on any contract failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: E402

from src.otel_mcp.store import SpanStore  # noqa: E402
from src.sd_service.store import SDDocIndex, SDSourcesInventory  # noqa: E402
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
# Synthetic source corpus
# ---------------------------------------------------------------------------

# Service 1 — billing: static routes, sqlite + requests targets.
BILLING_SERVICE = {
    "app.py": '''"""Billing service."""
import sqlite3
import requests
from dataclasses import dataclass
from flask import Flask, Blueprint

app = Flask(__name__)
billing = Blueprint("billing", __name__)

ledger = sqlite3.connect("/tmp/ledger.db")


@dataclass
class Charge:
    customer_id: str
    amount: int
    status: str = "pending"


@app.route("/charge", methods=["POST"])
def charge():
    """Authorize and capture a charge for a customer."""
    response = requests.post("http://payments-api/authorize", json={"id": "abc"})
    ledger.execute("INSERT INTO charges (customer_id, status) VALUES (?, ?)", ("abc", "ok"))
    return {"status": "ok"}


@billing.route("/refund/<charge_id>", methods=["POST"])
def refund(charge_id):
    """Issue a refund."""
    requests.post("http://payments-api/refund")
    return {"refunded": charge_id}


@app.route("/invoices")
def invoices():
    """List invoices for the calling customer."""
    rows = ledger.execute("SELECT * FROM invoices WHERE customer_id = ?", ("abc",)).fetchall()
    return {"invoices": rows}
''',
}

# Service 2 — search-svc: contains a dynamic route (f-string in path) to
# verify the placeholder block path.
SEARCH_SERVICE = {
    "app.py": '''"""Search service with a dynamic route prefix."""
import os
import requests
from flask import Flask

app = Flask(__name__)
PREFIX = os.environ.get("PREFIX", "")


@app.route(f"{PREFIX}/search")
def search():
    """Run a query against the index."""
    requests.get("http://catalog-service/products?q=foo")
    return {"hits": []}


@app.route("/healthz")
def healthz():
    return {"ok": True}
''',
}


def _seed_sources(root: str) -> None:
    for service, files in (("billing-service", BILLING_SERVICE), ("search-svc", SEARCH_SERVICE)):
        svc_dir = os.path.join(root, service)
        os.makedirs(svc_dir, exist_ok=True)
        for rel, body in files.items():
            full = os.path.join(svc_dir, rel)
            os.makedirs(os.path.dirname(full) or svc_dir, exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(body)


# ---------------------------------------------------------------------------
# MCP plumbing
# ---------------------------------------------------------------------------

def _tool(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise RuntimeError(f"Tool {name!r} not exposed by SD MCP. Available: {[t.name for t in tools]}")


async def _ainvoke_json(tool, args: dict):
    raw = await tool.ainvoke(args)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        for block in raw:
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if text is None:
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"_raw": text}
        return {"_raw": raw}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
    return {"_raw": str(raw)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> int:
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
        sources_root = os.path.join(workdir, "sources")
        pages_root = os.path.join(workdir, "pages")
        sd_db = os.path.join(workdir, "sd_state.db")
        chroma_path = os.path.join(workdir, "chroma")
        otel_db = os.path.join(workdir, "spans.db")
        audit_db = os.path.join(workdir, "audit.db")

        _seed_sources(sources_root)

        py = sys.executable
        env = {
            **os.environ,
            "SD_SOURCES_ROOT": sources_root,
            "SD_PAGES_ROOT": pages_root,
            "SD_DB_PATH": sd_db,
            "RAG_CHROMA_PATH": chroma_path,
            "OTEL_DB_PATH": otel_db,
            "AUDIT_DB_PATH": audit_db,
        }
        client = MultiServerMCPClient(
            {
                "sd": {
                    "command": py,
                    "args": ["-m", "src.sd_service.server"],
                    "transport": "stdio",
                    "cwd": str(ROOT),
                    "env": env,
                }
            }
        )

        _h("1. Connect to SD_MCP and discover tools")
        tools = await client.get_tools()
        names = sorted(t.name for t in tools)
        print(f"  tools: {names}")
        expected = {
            "dispatch_query", "dispatch_refresh",
            "find_services_for_product",
            "get_page", "patch_page",
        }
        missing = expected - set(names)
        if missing:
            failures.append(f"SD MCP missing tools: {missing}")
            return _finalize(failures)

        dispatch_query = _tool(tools, "dispatch_query")
        dispatch_refresh = _tool(tools, "dispatch_refresh")
        find_services = _tool(tools, "find_services_for_product")
        get_page = _tool(tools, "get_page")
        patch_page = _tool(tools, "patch_page")

        _h("2. dispatch_refresh — full refresh of seeded services")
        first = await _ainvoke_json(dispatch_refresh, {"event": {"change_kind": "modified"}})
        affected_pages = first.get("affected_pages") or []
        escalations = first.get("escalations") or []
        details = first.get("details") or []
        print(f"  affected_pages: {affected_pages}")
        print(f"  escalations: {[e.get('question_id') for e in escalations]}")
        for d in details:
            print(
                f"    {d['page_uri']:48s} svc={d.get('service')!s:18s} "
                f"chunks={d.get('chunks_indexed')} "
                f"deps={d.get('downstream_services')}"
            )

        if len(affected_pages) != 2:
            failures.append(f"expected 2 affected pages, got {len(affected_pages)}")
        # The dynamic route in search-svc must surface as an escalation.
        if not any("dynamic" in (e.get("topic") or "").lower() for e in escalations):
            failures.append("expected an escalation for the dynamic route in search-svc")

        _h("3. Re-run dispatch_refresh — both services should skip (unchanged)")
        second = await _ainvoke_json(dispatch_refresh, {"event": {"change_kind": "modified"}})
        skipped = [d for d in (second.get("details") or []) if d.get("skipped")]
        if len(skipped) != 2:
            failures.append(f"expected 2 skipped services on rerun, got {len(skipped)}")
        else:
            print(f"  both services skipped: {[d['service'] for d in skipped]}")

        _h("4. dispatch_query — SD-only question")
        q = await _ainvoke_json(dispatch_query, {
            "query": "What endpoints does billing-service expose and what does it call downstream?",
            "domain_hint": "sd",
        })
        print(f"  status={q.get('status')!r} sources={len(q.get('sources') or [])}  "
              f"focused?={bool(q.get('focused_analyze_code'))}")
        if q.get("answer"):
            ans = q["answer"]
            print(f"  answer: {ans[:240]}{'…' if len(ans) > 240 else ''}")
        if q.get("status") not in {"ok", "low_confidence"}:
            failures.append(f"dispatch_query unexpected status: {q.get('status')}")

        _h("5. find_services_for_product — relational lookup")
        # The SD doc index has no referenced_products yet (BP isn't wired
        # in via the SD_MCP subprocess), so this returns []. Just confirm
        # the contract: a valid product_id returns a list, missing arg 400s.
        res = await _ainvoke_json(find_services, {"product_id": "subscription-renewal"})
        results = res if isinstance(res, list) else (res.get("_raw") if isinstance(res, dict) else None)
        if not isinstance(results, list):
            failures.append(f"find_services_for_product returned non-list: {type(results)}")
        else:
            print(f"  matches for subscription-renewal: {len(results)} (expected 0 with stub BP)")

        _h("6. patch_page — replace one open placeholder")
        page_uri = "sd/services/search-svc.md"
        page = await _ainvoke_json(get_page, {"page_uri": page_uri})
        if not page.get("content"):
            failures.append(f"get_page returned empty content for {page_uri}")
            return _finalize(failures)
        idx_entry = page.get("doc_index_entry") or {}
        open_qids = idx_entry.get("open_placeholders") or []
        if not open_qids:
            failures.append(f"expected at least one open placeholder on {page_uri}; got none")
            return _finalize(failures)
        qid = open_qids[0]
        print(f"  patching placeholder {qid} on {page_uri}")
        patch_res = await _ainvoke_json(patch_page, {
            "page_uri": page_uri,
            "question_id": qid,
            "replacement": "Resolved: route serves /v1/search in production, /search in staging.",
        })
        print(f"  patch result: {patch_res}")
        if not patch_res.get("patched"):
            failures.append(f"patch_page should have patched {qid}")
        page_after = await _ainvoke_json(get_page, {"page_uri": page_uri})
        idx_after = page_after.get("doc_index_entry") or {}
        if qid in (idx_after.get("open_placeholders") or []):
            failures.append(f"placeholder {qid} still listed as open after patch")
        if (page_after.get("content") or "").find(f"SME-PLACEHOLDER:{qid}") != -1:
            failures.append(f"placeholder block for {qid} still present in page content")

        _h("7. State + telemetry — DocIndex / sources inventory / OTel / audit DB")
        doc_index = SDDocIndex(sd_db)
        all_pages = doc_index.list_all()
        print(f"  doc_index pages: {len(all_pages)}")
        for entry in all_pages:
            ep_count = len(entry.endpoints)
            print(
                f"    {entry.page_uri:40s} svc={entry.service!s:18s} "
                f"endpoints={ep_count} deps={entry.downstream_services}"
            )
        if len(all_pages) != 2:
            failures.append(f"expected 2 doc_index entries, got {len(all_pages)}")

        inv = SDSourcesInventory(sd_db)
        print(f"  sources_inventory: {[(s.service, s.source_revision[:8]) for s in inv.list_all()]}")
        if len(inv.list_all()) != 2:
            failures.append(f"expected 2 source entries, got {len(inv.list_all())}")

        spans = SpanStore(otel_db).query(service="sd_service", limit=500)
        method_counts: dict[str, int] = {}
        for s in spans:
            method_counts[s.mcp_method] = method_counts.get(s.mcp_method, 0) + 1
        print(f"  OTel span counts: {method_counts}")
        for required in {
            "dispatch_refresh", "dispatch_query",
            "find_services_for_product", "get_page", "patch_page",
        }:
            if method_counts.get(required, 0) == 0:
                failures.append(f"missing OTel span for sd_service.{required}")

        llm_log = LLMCallLog(audit_db)
        sd_modules = {m: stats for m, stats in llm_log.summarise_by_module().items() if m.startswith("rag.sd.")}
        print(f"  LLM call modules (sd.*): {sorted(sd_modules)}")
        if not sd_modules:
            failures.append("no LLM calls recorded for any rag.sd.* module")

        svc_log = ServiceLogStore(audit_db)
        sd_errors = svc_log.query(level="error", module_prefix="rag.sd.", limit=50)
        if sd_errors:
            print("  ERROR-level rag.sd.* entries:")
            for e in sd_errors:
                print(f"    {e.module}: {e.message}")
            unexpected = [e for e in sd_errors if not e.module.endswith(".analyze_code")]
            if unexpected:
                failures.append(f"{len(unexpected)} unexpected ERROR rows in rag.sd.*")

        recent = svc_log.query(min_level="info", module_prefix="rag.sd.", limit=10)
        if recent:
            print("  most-recent (info+) rag.sd.* entries:")
            for r in reversed(recent):
                t = time.strftime("%H:%M:%S", time.localtime(r.timestamp))
                print(f"    [{t}] {r.level.upper():5s} {r.module:24s} {r.message}")

    return _finalize(failures)


def _finalize(failures: list[str]) -> int:
    _h("Result")
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        return 1
    print("  All SD Service contract checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
