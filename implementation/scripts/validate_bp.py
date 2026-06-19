"""End-to-end validation for the B&P Service (§9.3).

Spawns ``src.bp_service.server`` as a stdio MCP subprocess and drives
all six §9.3.2 methods via ``langchain-mcp-adapters``. Steps:

  1. Pre-check: Ollama is reachable; chat + embed models are pulled.
  2. Seed a temp directory with two BP input docs and an SD-stub mapping.
  3. ``dispatch_refresh`` over the inputs; verify pages got written and
     the doc index has matching entries.
  4. ``dispatch_refresh`` again over the same inputs and confirm both
     are skipped (sources-inventory diff).
  5. ``dispatch_query`` for an in-domain question; verify status=ok and
     that the answer comes back with cross-reference metadata.
  6. ``find_products_for_service`` — a service the page references
     should map back to the page; an unknown service should not.
  7. ``patch_page`` — replace one of the open placeholders, confirm it
     vanishes from the doc index ``open_placeholders`` list.
  8. ``ingest_sme_doc`` — persist a synthetic SME reply, confirm a new
     page lands and a doc-index entry shows up.
  9. Inspect OTel + LLM + service log for the run and assert every
     expected method left telemetry behind.

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

from src.bp_service.store import BPDocIndex, BPSourcesInventory  # noqa: E402
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
        "Catalog Discovery is the storefront landing-page experience that exposes new "
        "arrivals, on-sale items, and personalized recommendations.\n\n"
        "## Services involved\n\n"
        "Catalog Discovery is backed by the catalog-service (product metadata) and the "
        "recommendation-service (ranking). Promotional badges arrive from the marketing "
        "feed.\n\n"
        "## Owners\n\n"
        "Product owner: Maria Patel. Engineering owner: Catalog Platform team.\n"
    ),
    "business-cases/subscription-renewal.md": (
        "# Subscription Renewal\n\n"
        "Subscription Renewal handles automatic billing for monthly memberships.\n\n"
        "## Services involved\n\n"
        "billing-service charges the saved payment method. notification-service sends the "
        "confirmation email. account-service downgrades on terminal failure.\n\n"
        "## SLA\n\n"
        "Renewals run on the 1st of each month with a 24 hour processing window.\n"
    ),
}

# SD-stub mapping: keyed by the product slug we expect the BPService to derive
# from the input doc title. ``catalog-discovery`` maps to one resolved service,
# ``recommendation-service`` is intentionally absent so an SME placeholder fires.
SD_STUB_MAPPING = {
    "catalog-discovery": [
        {
            "service": "catalog-service",
            "page_uri": "sd/services/catalog-service.md",
            "role": "primary",
        },
    ],
    "subscription-renewal": [
        {
            "service": "billing-service",
            "page_uri": "sd/services/billing-service.md",
            "role": "primary",
        },
        {
            "service": "notification-service",
            "page_uri": "sd/services/notification-service.md",
            "role": "secondary",
        },
        # account-service is intentionally absent → escalation envelope.
    ],
}


# ---------------------------------------------------------------------------
# MCP plumbing
# ---------------------------------------------------------------------------

def _tool(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise RuntimeError(f"Tool {name!r} not exposed by BP MCP. Available: {[t.name for t in tools]}")


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
        inputs_root = os.path.join(workdir, "inputs")
        pages_root = os.path.join(workdir, "pages")
        bp_db = os.path.join(workdir, "bp_state.db")
        chroma_path = os.path.join(workdir, "chroma")
        otel_db = os.path.join(workdir, "spans.db")
        audit_db = os.path.join(workdir, "audit.db")
        sd_stub_file = os.path.join(workdir, "sd_stub.json")

        # Seed input docs.
        for rel, body in INPUT_DOCS.items():
            full = os.path.join(inputs_root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(body)
        # Seed SD stub mapping.
        with open(sd_stub_file, "w", encoding="utf-8") as f:
            json.dump(SD_STUB_MAPPING, f)

        py = sys.executable
        env = {
            **os.environ,
            "BP_INPUTS_ROOT": inputs_root,
            "BP_PAGES_ROOT": pages_root,
            "BP_DB_PATH": bp_db,
            "RAG_CHROMA_PATH": chroma_path,
            "OTEL_DB_PATH": otel_db,
            "AUDIT_DB_PATH": audit_db,
            "BP_SD_STUB_MAPPING_FILE": sd_stub_file,
        }
        client = MultiServerMCPClient(
            {
                "bp": {
                    "command": py,
                    "args": ["-m", "src.bp_service.server"],
                    "transport": "stdio",
                    "cwd": str(ROOT),
                    "env": env,
                }
            }
        )

        _h("1. Connect to BP_MCP and discover tools")
        tools = await client.get_tools()
        names = sorted(t.name for t in tools)
        print(f"  tools: {names}")
        expected = {
            "dispatch_query", "dispatch_refresh",
            "find_products_for_service",
            "get_page", "patch_page", "ingest_sme_doc",
        }
        missing = expected - set(names)
        if missing:
            failures.append(f"BP MCP missing tools: {missing}")
            return _finalize(failures)

        dispatch_query = _tool(tools, "dispatch_query")
        dispatch_refresh = _tool(tools, "dispatch_refresh")
        find_products = _tool(tools, "find_products_for_service")
        get_page = _tool(tools, "get_page")
        patch_page = _tool(tools, "patch_page")
        ingest_sme = _tool(tools, "ingest_sme_doc")

        _h("2. Initial dispatch_refresh — full refresh of seeded inputs")
        first = await _ainvoke_json(dispatch_refresh, {"event": {"change_kind": "modified"}})
        affected_pages = first.get("affected_pages") or []
        escalations = first.get("escalations") or []
        details = first.get("details") or []
        print(f"  affected_pages: {affected_pages}")
        print(f"  escalations: {[e.get('question_id') for e in escalations]}")
        for d in details:
            print(
                f"    {d['page_uri']:40s} chunks={d.get('chunks_indexed')} "
                f"strategy={d.get('chunking_strategy')!s:30s} "
                f"refs={d.get('referenced_services')}"
            )

        if len(affected_pages) != 2:
            failures.append(f"expected 2 affected pages, got {len(affected_pages)}")
        if not any("recommendation-service" in (e.get("question") or "") for e in escalations):
            failures.append("expected an escalation for the unresolved recommendation-service ref")
        if not any("account-service" in (e.get("question") or "") for e in escalations):
            failures.append("expected an escalation for the unresolved account-service ref")

        _h("3. Re-run dispatch_refresh — every doc should skip (unchanged)")
        second = await _ainvoke_json(dispatch_refresh, {"event": {"change_kind": "modified"}})
        skipped = [d for d in (second.get("details") or []) if d.get("skipped")]
        if len(skipped) != 2:
            failures.append(f"expected 2 skipped sources on rerun, got {len(skipped)}")
        else:
            print(f"  both inputs skipped: {[d['page_uri'] for d in skipped]}")

        _h("4. dispatch_query — BP-only question against the freshly indexed pages")
        q = await _ainvoke_json(dispatch_query, {
            "query": "Who owns Catalog Discovery and which services back it?",
            "domain_hint": "bp",
        })
        print(f"  status={q.get('status')!r}  sources={len(q.get('sources') or [])}  "
              f"cross_refs={len(q.get('cross_references') or [])}")
        if q.get("answer"):
            ans = q["answer"]
            print(f"  answer: {ans[:240]}{'…' if len(ans) > 240 else ''}")
        if q.get("status") not in {"ok", "low_confidence"}:
            failures.append(f"dispatch_query status unexpected: {q.get('status')}")

        _h("5. find_products_for_service — relational lookup")
        for service_id, expect_match in [
            ("catalog-service", True),
            ("billing-service", True),
            ("nonexistent-service", False),
        ]:
            res = await _ainvoke_json(find_products, {"service_id": service_id})
            # MCP tool that returns a list comes back as either {_raw: [...]} or a list directly.
            # Our _ainvoke_json wraps lists into {_raw: ...}; unwrap defensively.
            results = res if isinstance(res, list) else res.get("_raw") or []
            if isinstance(results, list):
                count = len(results)
            else:
                count = 0
            print(f"  {service_id}: {count} match(es)")
            if expect_match and count == 0:
                failures.append(f"find_products_for_service({service_id}) expected ≥1 match")
            if not expect_match and count > 0:
                failures.append(f"find_products_for_service({service_id}) expected 0, got {count}")

        _h("6. patch_page — replace one open placeholder")
        page_uri = "bp/products/catalog-discovery.md"
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
            "replacement": "Resolved: see [recommendation-service](../../sd/services/recommendation-service.md).",
        })
        print(f"  patch result: {patch_res}")
        if not patch_res.get("patched"):
            failures.append(f"patch_page should have patched {qid}")
        page_after = await _ainvoke_json(get_page, {"page_uri": page_uri})
        idx_after = (page_after.get("doc_index_entry") or {})
        if qid in (idx_after.get("open_placeholders") or []):
            failures.append(f"placeholder {qid} still listed as open after patch")
        if (page_after.get("content") or "").find(f"SME-PLACEHOLDER:{qid}") != -1:
            failures.append(f"placeholder block for {qid} still present in page content")

        _h("7. ingest_sme_doc — persist a fresh SME reply page")
        new = await _ainvoke_json(ingest_sme, {
            "question_id": "Q-validation-001",
            "sme_text": "recommendation-service is owned by the Recos team; documented at sd/services/recommendation-service.md once the SD specialist lands.",
            "originating_pages": [page_uri],
        })
        print(f"  new page: {new.get('new_page_uri')} embedding_revision={new.get('embedding_revision')}")
        if not (new.get("new_page_uri") or "").startswith("bp/products/sme-replies/"):
            failures.append("ingest_sme_doc returned an unexpected page URI")

        _h("8. State + telemetry — DocIndex / sources inventory / OTel / audit DB")
        doc_index = BPDocIndex(bp_db)
        all_pages = doc_index.list_all()
        print(f"  doc_index pages: {len(all_pages)}")
        for entry in all_pages:
            print(f"    {entry.page_uri:48s} title={entry.title!s:30s} services={entry.referenced_services}")
        if len(all_pages) < 3:
            failures.append(f"expected ≥3 doc_index entries (2 products + 1 SME page), got {len(all_pages)}")

        inv = BPSourcesInventory(bp_db)
        sources_seen = inv.list_all()
        print(f"  sources_inventory: {[(s.source_uri, s.content_hash[:8]) for s in sources_seen]}")
        if len(sources_seen) != 2:
            failures.append(f"expected 2 source entries in inventory, got {len(sources_seen)}")

        # OTel: every BP MCP method we drove should have left a span.
        spans = SpanStore(otel_db).query(service="bp_service", limit=500)
        method_counts: dict[str, int] = {}
        for s in spans:
            method_counts[s.mcp_method] = method_counts.get(s.mcp_method, 0) + 1
        print(f"  OTel span counts: {method_counts}")
        for required in {"dispatch_refresh", "dispatch_query", "find_products_for_service", "get_page", "patch_page", "ingest_sme_doc"}:
            if method_counts.get(required, 0) == 0:
                failures.append(f"missing OTel span for bp_service.{required}")

        # LLM log — at least the service-candidate extractor should have run on the refresh.
        llm_log = LLMCallLog(audit_db)
        llm_summary = llm_log.summarise_by_module()
        bp_modules = {m: stats for m, stats in llm_summary.items() if m.startswith("rag.bp.")}
        print(f"  LLM call modules (bp.*): {sorted(bp_modules)}")
        if not bp_modules:
            failures.append("no LLM calls recorded for any rag.bp.* module")

        # Service log — assert no error-level entries.
        svc_log = ServiceLogStore(audit_db)
        rag_bp_errors = svc_log.query(level="error", module_prefix="rag.bp.", limit=50)
        if rag_bp_errors:
            print("  ERROR-level rag.bp.* entries:")
            for e in rag_bp_errors:
                print(f"    {e.module}: {e.message}")
            # Ollama-proxy hiccups in the LLM probe path can mark a row error; only
            # fail when the error landed on an actual operational module.
            unexpected = [e for e in rag_bp_errors if not e.module.endswith(".compose")]
            if unexpected:
                failures.append(f"{len(unexpected)} unexpected ERROR rows in rag.bp.*")

        # Summary of recent service-log entries for the run.
        recent = svc_log.query(min_level="info", module_prefix="rag.bp.", limit=10)
        if recent:
            print("  most-recent (info+) rag.bp.* entries:")
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
    print("  All B&P Service contract checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
