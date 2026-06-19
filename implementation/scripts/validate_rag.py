"""End-to-end validation for the RAG Service (§9.1).

Spawns ``src.rag_service.server`` as a stdio MCP subprocess and drives
the three exposed tools through ``langchain-mcp-adapters`` — the same
client B&P and SD will use. Steps:

  1. Pre-check: Ollama is reachable; the chat + embed models are pulled.
  2. Index a small synthetic corpus (2 BP product pages + 2 SD service pages).
  3. Run a battery of grounded queries:
       a. BP-only query (domain_filter=bp) — expect ``ok`` with BP source.
       b. SD-only query (domain_filter=sd) — expect ``ok`` with SD source.
       c. Cross-domain query (domain_filter=both) — expect mixed sources.
       d. Off-topic query — expect ``low_confidence`` or ``exhausted``.
  4. Domain-isolation check: query domain_filter=bp and assert zero SD sources.
  5. Delete one doc and confirm its chunks are gone.
  6. Inspect the OTel store: every retrieve / index / delete left a span.

Runs under a fresh temp directory so the production Chroma store at
``$RAG_CHROMA_PATH`` is never touched. Exits non-zero on any contract
failure.
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
from src.shared.llm import embed_model, llm_model, ollama_host  # noqa: E402
from src.shared.llm_log import LLMCallLog  # noqa: E402
from src.shared.service_log import ServiceLogStore  # noqa: E402


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

def _h(title: str) -> None:
    bar = "=" * len(title)
    print(f"\n{bar}\n{title}\n{bar}")


def _step(msg: str) -> None:
    print(f"  → {msg}")


# ---------------------------------------------------------------------------
# Ollama pre-check
# ---------------------------------------------------------------------------

def precheck_ollama() -> list[str]:
    failures: list[str] = []
    base = ollama_host().rstrip("/")
    # Force a direct connection — corporate proxies sometimes intercept
    # localhost too, which would mask a perfectly-working Ollama install.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{base}/api/tags", timeout=4) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        failures.append(
            f"Cannot reach Ollama at {base}: {exc}. Is `ollama serve` running?"
        )
        return failures
    except Exception as exc:  # noqa: BLE001
        failures.append(f"Ollama tags check failed: {exc}")
        return failures

    names = {m.get("name", "") for m in data.get("models", [])}
    # Loose match — Ollama tags are like 'llama3.1:8b'; users may have 'llama3.1:latest'.
    chat = llm_model()
    embed = embed_model()
    if not any(chat.split(":", 1)[0] in n for n in names):
        failures.append(
            f"Ollama is up but no model matches LLM_MODEL={chat!r}. Pull it: `ollama pull {chat}`"
        )
    if not any(embed.split("/")[-1].split(":", 1)[0] in n for n in names):
        failures.append(
            f"Ollama is up but no model matches RAG_EMBED_MODEL={embed!r}. Pull it: `ollama pull {embed}`"
        )
    return failures


# ---------------------------------------------------------------------------
# Synthetic corpus
# ---------------------------------------------------------------------------

BP_DOCS = [
    {
        "uri": "bp/products/catalog-discovery.md",
        "domain": "bp",
        "doc": (
            "# Catalog Discovery\n\n"
            "Catalog Discovery is a flagship feature of our retail platform. It lets shoppers "
            "browse new arrivals, on-sale items, and recommendations on a single landing page.\n\n"
            "## Key behaviors\n\n"
            "Catalog Discovery uses the catalog-service to fetch product summaries and the "
            "recommendation-service to rank them. Promotional badges come from the promotions "
            "feed managed by the marketing team.\n\n"
            "## Owners\n\n"
            "Product owner: Maria Patel. Engineering owner: Catalog Platform team.\n\n"
            "## Open questions\n\n"
            "Whether to merge the New Arrivals and Featured rails on small screens is still "
            "under discussion with design.\n"
        ),
    },
    {
        "uri": "bp/products/subscription-renewal.md",
        "domain": "bp",
        "doc": (
            "# Subscription Renewal\n\n"
            "Subscription Renewal handles automatic billing for monthly memberships.\n\n"
            "When a renewal succeeds, customers receive an email confirmation. Failures route "
            "through the dunning flow with three retry attempts before downgrading the account.\n\n"
            "## Services involved\n\n"
            "billing-service charges the saved payment method. notification-service sends the "
            "confirmation email. account-service downgrades on terminal failure.\n\n"
            "## SLA\n\n"
            "Renewals run on the 1st of each month with a 24 hour processing window.\n"
        ),
    },
]

SD_DOCS = [
    {
        "uri": "sd/services/billing-service.md",
        "domain": "sd",
        "doc": (
            "# billing-service\n\n"
            "The billing-service exposes endpoints for charging saved payment methods, "
            "issuing refunds, and listing invoices.\n\n"
            "## Endpoints\n\n"
            "- POST /charge — creates a new charge given an amount and a customer id. "
            "  Calls the payments-api downstream for authorization.\n"
            "- POST /refund — issues a partial or full refund on an existing charge.\n"
            "- GET /invoices — lists invoices for the calling customer in the last 12 months.\n\n"
            "## Downstream dependencies\n\n"
            "- payments-api (HTTP) — authorizes and captures funds.\n"
            "- ledger-db (SQL) — durable record of every charge.\n"
        ),
    },
    {
        "uri": "sd/services/catalog-service.md",
        "domain": "sd",
        "doc": (
            "# catalog-service\n\n"
            "The catalog-service serves product metadata, categories, and inventory snapshots "
            "to the storefront.\n\n"
            "## Endpoints\n\n"
            "- GET /products/{id} — returns the canonical product record by id.\n"
            "- GET /products?category=... — returns up to 50 products in the requested category.\n"
            "- GET /inventory/{sku} — returns stock level for a sku across regions.\n\n"
            "## Downstream dependencies\n\n"
            "- product-db (SQL) — the source-of-truth catalog.\n"
            "- inventory-cache (Redis) — denormalized stock counts updated every 60 seconds.\n"
        ),
    },
]


# ---------------------------------------------------------------------------
# MCP plumbing
# ---------------------------------------------------------------------------

def _tool(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise RuntimeError(f"Tool {name!r} not exposed by RAG MCP. Available: {[t.name for t in tools]}")


async def _ainvoke_json(tool, args: dict) -> dict:
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
        chroma_path = os.path.join(workdir, "chroma")
        otel_db = os.path.join(workdir, "spans.db")
        audit_db = os.path.join(workdir, "audit.db")

        # Spawn RAG_MCP — a fresh subprocess with isolated paths.
        py = sys.executable
        env = {
            **os.environ,
            "RAG_CHROMA_PATH": chroma_path,
            "OTEL_DB_PATH": otel_db,
            "AUDIT_DB_PATH": audit_db,
        }
        client = MultiServerMCPClient(
            {
                "rag": {
                    "command": py,
                    "args": ["-m", "src.rag_service.server", "--chroma", chroma_path, "--otel-db", otel_db],
                    "transport": "stdio",
                    "cwd": str(ROOT),
                    "env": env,
                }
            }
        )

        _h("1. Connect to RAG_MCP and discover tools")
        tools = await client.get_tools()
        names = sorted(t.name for t in tools)
        print(f"  tools: {names}")
        expected = {"retrieve", "index", "delete"}
        missing = expected - set(names)
        if missing:
            failures.append(f"RAG MCP missing tools: {missing}")
            return _finalize(failures)

        retrieve = _tool(tools, "retrieve")
        index = _tool(tools, "index")
        delete = _tool(tools, "delete")

        _h("2. Index synthetic BP + SD corpus")
        index_results: dict[str, dict] = {}
        for doc in BP_DOCS + SD_DOCS:
            res = await _ainvoke_json(index, {
                "domain": doc["domain"],
                "source_uri": doc["uri"],
                "document": doc["doc"],
            })
            index_results[doc["uri"]] = res
            print(
                f"  {doc['uri']:48s} chunks={res.get('chunks_indexed')} "
                f"strategy={res.get('chunking_strategy')!s:30s} score={res.get('score')} "
                f"low_conf={res.get('low_confidence')}"
            )
            if res.get("chunks_indexed", 0) <= 0:
                failures.append(f"index returned zero chunks for {doc['uri']}")

        # Sanity: ToT should pick a chunking strategy and produce >=1 chunk per doc.
        total_chunks = sum(r.get("chunks_indexed", 0) for r in index_results.values())
        print(f"  total chunks indexed: {total_chunks}")
        if total_chunks < 4:
            failures.append(f"expected at least 4 chunks across 4 docs, got {total_chunks}")

        _h("3a. BP query — expect ok with a BP source")
        r = await _ainvoke_json(retrieve, {
            "query": "Who owns Catalog Discovery and which services back it?",
            "domain_filter": "bp",
            "mode": "query",
        })
        _print_result(r)
        if r.get("status") not in {"ok", "low_confidence"}:
            failures.append(f"BP query unexpected status: {r.get('status')}")
        if not any(s.get("domain") == "bp" for s in r.get("sources", [])):
            failures.append("BP query returned no BP sources")
        if any(s.get("domain") == "sd" for s in r.get("sources", [])):
            failures.append("BP query leaked SD sources — domain_filter not honored")

        _h("3b. SD query — expect ok with an SD source")
        r = await _ainvoke_json(retrieve, {
            "query": "What endpoints does the billing-service expose and what does it call downstream?",
            "domain_filter": "sd",
            "mode": "query",
        })
        _print_result(r)
        if r.get("status") not in {"ok", "low_confidence"}:
            failures.append(f"SD query unexpected status: {r.get('status')}")
        if not any(s.get("domain") == "sd" for s in r.get("sources", [])):
            failures.append("SD query returned no SD sources")
        if any(s.get("domain") == "bp" for s in r.get("sources", [])):
            failures.append("SD query leaked BP sources — domain_filter not honored")

        _h("3c. Cross-domain query — expect both domains visible")
        r = await _ainvoke_json(retrieve, {
            "query": "How does subscription renewal interact with the billing-service charge endpoint?",
            "domain_filter": "both",
            "mode": "query",
        })
        _print_result(r)
        domains = {s.get("domain") for s in r.get("sources", [])}
        if r.get("status") == "ok" and domains and not (domains & {"bp", "sd"}):
            failures.append(f"cross-domain query saw weird domains: {domains}")

        _h("3d. Off-topic query — expect low_confidence or exhausted")
        r = await _ainvoke_json(retrieve, {
            "query": "What is the migration plan for the legacy COBOL mainframe payroll runs?",
            "domain_filter": "both",
            "mode": "background",
        })
        _print_result(r)
        if r.get("status") == "ok":
            # Dump every grader score + the retrieval trail so we can see
            # exactly which chunk slipped through the threshold.
            _dump_grader_diagnostics(r)
            failures.append(
                f"off-topic query unexpectedly returned status=ok with {len(r.get('sources', []))} sources"
            )

        _h("4. Delete a BP doc — chunks should vanish")
        target = BP_DOCS[0]["uri"]
        d = await _ainvoke_json(delete, {"domain": "bp", "source_uri": target})
        print(f"  delete({target}) -> {d}")
        # Confirm by re-querying — we should now see zero chunks of that source.
        r = await _ainvoke_json(retrieve, {
            "query": "Who owns Catalog Discovery and which services back it?",
            "domain_filter": "bp",
            "mode": "query",
        })
        leaked = [s for s in r.get("sources", []) if s.get("source_uri") == target]
        if leaked:
            failures.append(f"chunks for {target} survived delete: {leaked}")
        else:
            print(f"  confirmed: {target} no longer appears in retrieval sources")

        _h("5. OTel side-channel — every MCP call left a span")
        # The RAG MCP subprocess wrote spans to the same SQLite file the
        # validation harness can read directly. (We're not going through
        # the OTel MCP here — that was Task #3's job.)
        store = SpanStore(otel_db)
        spans = store.query(service="rag_service", limit=200)
        index_spans = [s for s in spans if s.mcp_method == "index"]
        retrieve_spans = [s for s in spans if s.mcp_method == "retrieve"]
        delete_spans = [s for s in spans if s.mcp_method == "delete"]
        print(f"  index spans:    {len(index_spans)}")
        print(f"  retrieve spans: {len(retrieve_spans)}")
        print(f"  delete spans:   {len(delete_spans)}")
        if len(index_spans) != 4:
            failures.append(f"expected 4 index spans, got {len(index_spans)}")
        if len(retrieve_spans) < 5:
            failures.append(f"expected at least 5 retrieve spans, got {len(retrieve_spans)}")
        if len(delete_spans) != 1:
            failures.append(f"expected 1 delete span, got {len(delete_spans)}")

        # Sample the index span for the catalog discovery doc — should
        # carry chunking_strategy + tot_score in attributes.
        catalog_index = next(
            (s for s in index_spans if (s.payload_summary or {}).get("source_uri") == BP_DOCS[0]["uri"]),
            None,
        )
        if catalog_index is None:
            failures.append("index span for catalog-discovery missing")
        else:
            print(
                f"  catalog-discovery index span: strategy="
                f"{catalog_index.attributes.get('chunking_strategy')} "
                f"tot_score={catalog_index.attributes.get('tot_score')}"
            )

        _h("6. LLM call log — every chat invoke recorded by module")
        llm_log = LLMCallLog(audit_db)
        rows = llm_log.query(limit=1000)
        summary = llm_log.summarise_by_module()
        if not rows:
            failures.append("LLM call log is empty — LoggedLLM wrapper not wired in")
        else:
            print(f"  total LLM calls recorded: {len(rows)}")
            for module, stats in sorted(summary.items()):
                p50 = stats["latency_ms"]["p50"]
                p95 = stats["latency_ms"]["p95"]
                err = stats["errors"]
                print(
                    f"    {module:36s}  count={stats['count']:>3d}"
                    f"  p50={p50:>7.0f} ms  p95={p95:>7.0f} ms"
                    f"{'  errors=' + str(err) if err else ''}"
                )
            # All RAG-side modules must show up — confirms every call site is tagged.
            expected_prefixes = {
                "rag.chunking.probes",
                "rag.auto_rag.grader",
                "rag.auto_rag.generator",
                "rag.auto_rag.faithfulness",
            }
            missing = expected_prefixes - set(summary.keys())
            if missing:
                failures.append(f"LLM modules with no calls in this run: {missing}")
            errors = sum(s["errors"] for s in summary.values())
            if errors:
                failures.append(f"{errors} LLM call(s) errored — check llm_log error column")

        _h("7. Service log — Java-style log entries by module + level")
        svc_log = ServiceLogStore(audit_db)
        all_rows = svc_log.query(limit=2000)
        counts = svc_log.counts_by_level(module_prefix="rag.")
        print(f"  total service log entries: {len(all_rows)}")
        print(f"  level counts (rag.*): {counts}")
        # Every RAG module we instrumented should show at least one info entry.
        expected_modules = {"rag.server", "rag.service", "rag.chunking", "rag.auto_rag"}
        seen = {r.module for r in all_rows}
        missing_modules = expected_modules - seen
        if missing_modules:
            failures.append(f"service log missing entries from: {missing_modules}")
        # Print the latest 10 high-level entries so the report shows what the
        # service was doing during this run.
        recent = svc_log.query(min_level="info", limit=10)
        if recent:
            print("  most-recent (info+) entries:")
            for r in reversed(recent):
                t = time.strftime("%H:%M:%S", time.localtime(r.timestamp))
                print(f"    [{t}] {r.level.upper():5s} {r.module:18s} {r.message}")
        # Any error-level rows are bugs in this validation run.
        errs = svc_log.query(level="error", limit=20)
        if errs:
            print("  ERROR-level entries observed:")
            for r in errs:
                print(f"    {r.module}: {r.message}")
            failures.append(f"{len(errs)} ERROR-level service log entry/entries observed")

    return _finalize(failures)


def _print_result(r: dict) -> None:
    print(f"  status={r.get('status')!r}  rewrites_used={r.get('rewrites_used')}  "
          f"sources={len(r.get('sources') or [])}")
    if r.get("answer"):
        ans = r["answer"]
        print(f"  answer: {ans[:240]}{'…' if len(ans) > 240 else ''}")
    for src in (r.get("sources") or [])[:3]:
        print(f"    [{src.get('domain')}] {src.get('source_uri')}  d={src.get('distance')}")
    # Compact grader summary — the max score and how many chunks cleared
    # which level. Helps explain why the loop landed on a given status
    # without flooding the report with full grader objects.
    grader_scores = r.get("grader_scores") or []
    chunk_grades = [g for g in grader_scores if isinstance(g, dict) and "score" in g and "chunk_id" in g]
    if chunk_grades:
        max_s = max((g["score"] for g in chunk_grades), default=0.0)
        passing = sum(1 for g in chunk_grades if g["score"] >= 2.0)
        print(
            f"    grader: max={max_s} chunks_at_or_above_2={passing}/{len(chunk_grades)}"
        )
    # Retrieval trail step kinds + counts — confirms whether the rewrite
    # loop fired and how many retrieve passes happened.
    trail = r.get("retrieval_trail") or []
    if trail:
        steps = [t.get("step") for t in trail if isinstance(t, dict)]
        kind_counts: dict[str, int] = {}
        for s in steps:
            if s:
                kind_counts[s] = kind_counts.get(s, 0) + 1
        if kind_counts:
            print(f"    trail: {kind_counts}")


def _dump_grader_diagnostics(r: dict) -> None:
    """Verbose dump for failure paths — prints every chunk's grade with
    the grader's reason text and a trimmed snippet of the chunk so we can
    see exactly which chunk slipped through the threshold."""
    grader_scores = r.get("grader_scores") or []
    chunk_grades = [g for g in grader_scores if isinstance(g, dict) and "chunk_id" in g]
    sources_by_id = {s.get("chunk_id"): s for s in (r.get("sources") or [])}
    print("    grader scores (most-recent retrieve):")
    for g in chunk_grades:
        cid = g.get("chunk_id", "?")
        snippet = (sources_by_id.get(cid, {}).get("snippet") or "").replace("\n", " ")
        if len(snippet) > 140:
            snippet = snippet[:140] + "…"
        print(
            f"      chunk={cid[:12]:12s} score={g.get('score')} "
            f"domain={g.get('domain')} source={g.get('source_uri')}\n"
            f"        reason: {g.get('reason')!s}\n"
            f"        snippet: {snippet}"
        )
    # Faithfulness re-grade rows (if any).
    faithfulness = [g for g in grader_scores if isinstance(g, dict) and g.get("step") == "faithfulness"]
    for f in faithfulness:
        print(
            f"    faithfulness: supported={f.get('supported')} "
            f"answers_query={f.get('answers_query')} "
            f"unsupported_claims={f.get('unsupported_claims')} "
            f"non_answer_reason={f.get('non_answer_reason')!r}"
        )
    print("    retrieval trail:")
    for t in (r.get("retrieval_trail") or []):
        print(f"      {t}")


def _finalize(failures: list[str]) -> int:
    _h("Result")
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        return 1
    print("  All RAG Service contract checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
