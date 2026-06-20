# Capstone Project — Research Agent for Org Knowledge (Implementation)

Enrique R. Corona Dominguez

---

This directory holds the running code for the architecture defined in
[`PROJECT_ARCHITECTURE.md`](../PROJECT_ARCHITECTURE.md) and
[`PROJECT_LOW_LEVEL_DESIGN.md`](../PROJECT_LOW_LEVEL_DESIGN.md). It is **self-contained**:
clone, create a virtualenv, install `requirements.txt`, set the env vars in `.env.example`,
and the validation scripts run end-to-end on a workstation.

The components are built in dependency order: utility services first
(OTel MCP, RAG Service), then the specialists ([B&P](../PROJECT_LOW_LEVEL_DESIGN.md#93-bp-service-design),
[SD](../PROJECT_LOW_LEVEL_DESIGN.md#92-sd-service-design)), then the
[Orchestrator](../PROJECT_LOW_LEVEL_DESIGN.md#94-orchestrator-service-design),
then the [Portal](../PROJECT_LOW_LEVEL_DESIGN.md#95-sme-interaction-module-6).

---

## Quick start

```bash
cd implementation
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edit if needed
```

Make sure Ollama is running locally with the chat and embed models pulled:

```bash
ollama serve  # if not already running
ollama pull llama3.1:8b
ollama pull chroma/all-minilm-l6-v2-f32
```

### Boot the whole stack

The full POC runs as five processes — OTel MCP, RAG MCP, BP MCP, SD MCP, and the
Orchestrator REST — wired to each other over MCP-HTTP:

```bash
./start_all.sh                          # boot everything
./start_all.sh --install-dependencies   # also pip install first
./stop_all.sh                           # tear it down
./reset_state.sh                        # nuke embeddings + audit logs + per-service state + force-reset pear-store to `starting-point`
./reset_state.sh --logs                 # only the audit + OTel SQLite stores
./reset_state.sh --state                # only the BP / SD / Orchestrator SQLite stores (doc indexes, queue, tasks)
./reset_state.sh --embeddings           # only the Chroma index
./reset_state.sh --repo                 # only the docs repo (force-pushes pear-store back to the tag)
```

`start_all.sh` exports `OTEL_MCP_URL` / `RAG_MCP_URL` / `BP_MCP_URL` / `SD_MCP_URL`
so each service points at the others. Logs land in `logs/<svc>.log`, PIDs in
`pids/<svc>.pid`. Default ports are `8101`–`8104` for the MCP servers and
`8000` for the Orchestrator REST — override with `OC_PORT` / `RAG_MCP_PORT` /
etc. in `.env`.

When `GITHUB_PERSONAL_ACCESS_TOKEN` + `GITHUB_OWNER` + `GITHUB_REPO` are set
in `.env`, BP and SD switch every read AND write off the local filesystem and
through the upstream `@modelcontextprotocol/server-github` MCP — that's the
production page-storage path the architecture spec calls out
([§8.5](../PROJECT_ARCHITECTURE.md#85-considerations-for-the-poc)). Each
specialist spawns its own GitHub MCP subprocess via `npx`, so Node.js must be
on `PATH`. The smoke test at
[`playground/github-mcp-pear-store/`](../playground/github-mcp-pear-store/)
is the cheapest way to confirm the PAT can actually reach the docs repo
before paying the cost of bringing up all five services.

### Validate each component as it lands

The validation scripts run each service in single-process mode (no peer URLs
exported) and exercise the contracts via stdio MCP / direct REST.

| Component    | Validation                                | Requires                                |
|--------------|-------------------------------------------|-----------------------------------------|
| OTel MCP     | `python scripts/validate_otel.py`         | nothing — pure Python + SQLite          |
| RAG Service  | `python scripts/validate_rag.py`          | Ollama running with chat + embed models |
| B&P Service  | `python scripts/validate_bp.py`           | Ollama running with chat + embed models |
| SD Service   | `python scripts/validate_sd.py`           | Ollama running with chat + embed models |
| Orchestrator | `python scripts/validate_orchestrator.py` | Ollama running with chat + embed models |

Both scripts spawn the MCP server as a stdio subprocess and exercise it
through `langchain-mcp-adapters` — the same client B&P, SD, and the
Orchestrator will use when they land.

---

## Layout

```
implementation/
├── README.md                # this file — progress tracker
├── requirements.txt         # all pip deps, pinned by lower-bound
├── .env.example             # config knobs (paths, model, RAG caps)
├── data/                    # generated at runtime (SQLite, Chroma, …)
├── scripts/                 # validation + demo entrypoints
└── src/
    ├── shared/              # cross-component utilities (OTel client, LoggedLLM, llm_log, service_log)
    ├── otel_mcp/            # fake OTel collector + MCP front (stdio)
    ├── rag_service/         # RAG Service: store, chunking ToT, Auto-RAG, MCP front
    ├── bp_service/          # B&P Service: page store, doc index, compose, MCP front
    ├── sd_service/          # SD Service: source store, analyze_code, ToT dep graph, compose, MCP front
    └── orchestrator/        # Orchestrator: state, routing, ingest_sme_reply, REST API

implementation/portal/      # Documentation Portal (§9.8) — Quasar v2 + Vue 3 SPA
```

`src/shared/` holds anything every service depends on. Today that's:

- [`shared/otel_client.py`](src/shared/otel_client.py) — context-manager span emitter ([§9.6](../PROJECT_LOW_LEVEL_DESIGN.md#96-audit-and-observability-module-6)). Spans go to the OTel MCP's SQLite store at `$OTEL_DB_PATH`.
- [`shared/llm.py`](src/shared/llm.py) — Ollama chat + embedder factories. `get_chat_llm(module=...)` returns a `LoggedLLM` that records every `invoke` / `ainvoke` to the audit DB. Forces a non-empty module tag so no call lands unlabeled.
- [`shared/llm_log.py`](src/shared/llm_log.py) — `llm_calls` table in the audit DB: `(module, request, response, started_at, latency_ms, model, temperature, json_mode, error)`. Inspect with `python -m src.shared.llm_log --summary`.
- [`shared/service_log.py`](src/shared/service_log.py) — Java-style service logger (`info` / `warn` / `error` / `debug`) backed by the `service_logs` table in the same audit DB: `(module, level, timestamp, message)`. Mirrors to stdlib `logging` so foreground processes still print to the console. Inspect with `python -m src.shared.service_log --prefix rag. --min-level info`.

Two complementary audit channels:

- **OTel spans** (`$OTEL_DB_PATH`) — *between-service* MCP traffic. One span per inbound/outbound MCP call, with status, latency, and a privacy-safe payload summary.
- **Audit DB** (`$AUDIT_DB_PATH`) — *inside-service* activity. Two tables in one file: `llm_calls` for every chat round-trip with the local Ollama, `service_logs` for high-level operational entries (start/done, gap warnings, errors).

Spec links for each component:

- `src/otel_mcp/` — [§8.5 "Considerations for the POC"](../PROJECT_ARCHITECTURE.md#85-considerations-for-the-poc),
  [§9.6 "Audit and observability"](../PROJECT_LOW_LEVEL_DESIGN.md#96-audit-and-observability-module-6).
- `src/rag_service/` — [§9.1 "RAG Service design"](../PROJECT_LOW_LEVEL_DESIGN.md#91-rag-service-design),
  including the [Autonomous RAG loop](../PROJECT_LOW_LEVEL_DESIGN.md#9131-autonomous-rag-loop) and the
  [ToT chunking sub-graph](../PROJECT_LOW_LEVEL_DESIGN.md#9132-tot-chunking-strategy).

Future components slot in as siblings under `src/`:

- `portal/` — [§9.5 SME interaction](../PROJECT_LOW_LEVEL_DESIGN.md#95-sme-interaction-module-6) — chatbot, SME UI, rendered docs.
- `update_trigger/` — [§8.5 Considerations for the POC](../PROJECT_ARCHITECTURE.md#85-considerations-for-the-poc) — cron + GitHub watcher.

---

## Progress tracker

Status legend: `[ ]` not started · `[~]` in progress · `[x]` shipped + validated.

### Module 5 — [High-level architecture](../PROJECT_ARCHITECTURE.md#8-high-level-architecture-module-5)

- [x] **OTel MCP** ([§8.5](../PROJECT_ARCHITECTURE.md#85-considerations-for-the-poc)) — fake collector behind an MCP, SQLite-persisted, exposes `record_span` / `query_spans` / `get_metrics` / `clear_spans`. _Validated by [`scripts/validate_otel.py`](scripts/validate_otel.py)._
- [x] **RAG Service** ([§9.1](../PROJECT_LOW_LEVEL_DESIGN.md#91-rag-service-design)) — in-process, ChromaDB-backed, `RAG_MCP` front, [Auto-RAG loop](../PROJECT_LOW_LEVEL_DESIGN.md#9131-autonomous-rag-loop), [ToT chunking](../PROJECT_LOW_LEVEL_DESIGN.md#9132-tot-chunking-strategy). _Code complete; run [`scripts/validate_rag.py`](scripts/validate_rag.py) on a host with Ollama to exercise the full path. Chunker + store contract smoke-tested without Ollama._
- [x] **B&P Service** ([§9.3](../PROJECT_LOW_LEVEL_DESIGN.md#93-bp-service-design)) — input-doc ingest, `RAG_MCP.index`, `resolve_sd_links` against a stub SD client (real SD wires in as a drop-in replacement), page write through `LocalPageStore`, query mode with inline cross-reference links, [§9.5.1 placeholder blocks](../PROJECT_LOW_LEVEL_DESIGN.md#951-placeholders-and-re-integration), `patch_page` + `ingest_sme_doc`. _Code complete; run [`scripts/validate_bp.py`](scripts/validate_bp.py) on a host with Ollama. In-process smoke test passes without Ollama (regex fallback covers service extraction)._
- [x] **Orchestrator** ([§9.4](../PROJECT_LOW_LEVEL_DESIGN.md#94-orchestrator-service-design)) — FastAPI REST app exposing all seven §9.4.2 endpoints under `/v1`, deterministic routing (`pick_dispatch_target_for_query`/`_for_refresh`/`owning_specialist_for_page`), `pending_sme_questions` + async-task state in SQLite at `$OC_DB_PATH`, `ingest_sme_reply` walking BP/SD `patch_page` fan-out (clears the queue, removes the placeholder fence). _Code complete; run [`scripts/validate_orchestrator.py`](scripts/validate_orchestrator.py) on a host with Ollama. OrchestratorService logic smoke-tested in-process with stubbed clients; FastAPI app builds and exposes the seven routes._
- [ ] **GitHub MCP** — reuse the upstream `@modelcontextprotocol/server-github` (already validated in [`playground/github-mcp-test/`](../playground/github-mcp-test)). Slot it in as the production `PageStore` / `SourceStore`.
- [x] **SD Service** ([§9.2](../PROJECT_LOW_LEVEL_DESIGN.md#92-sd-service-design)) — [`analyze_code`](../PROJECT_LOW_LEVEL_DESIGN.md#9231-analyze_code) AST pipeline (Flask routes, dataclasses, `requests.{verb}` HTTP, `sqlite3.execute` DB), [ToT dep-graph](../PROJECT_LOW_LEVEL_DESIGN.md#9233-tot-dep-graph) with K=3 candidates and beam B=2 D=2, code-only scoring (Monitoring MCP out of POC scope per [§8.5](../PROJECT_ARCHITECTURE.md#85-considerations-for-the-poc)), `resolve_bp_links` against a stub BP client (real BP wires in via the orchestrator), page write through `LocalPageStore`, query mode with focused `analyze_code` fallback on low_confidence/exhausted, [§9.5.1 placeholder blocks](../PROJECT_LOW_LEVEL_DESIGN.md#951-placeholders-and-re-integration) for dynamic routes / dynamic calls / low-confidence ToT winners, `patch_page`. _Code complete; run [`scripts/validate_sd.py`](scripts/validate_sd.py) on a host with Ollama. In-process smoke test passes without Ollama (analyze_code's LLM augment is the only Ollama dependency)._
- [x] **Documentation Portal** ([§9.8](../PROJECT_LOW_LEVEL_DESIGN.md#98-documentation-portal)) — Quasar + Vue 3 SPA at [`implementation/portal/`](portal/) with three tabs and a collapsible right-side X-Ray drawer: Documentation (live tree + Markdown render of `documentation/` from `kikecorona/pear-store` via the GitHub REST API; Mermaid diagrams render inline; covers `bp/`, `sd/`, and any sibling), SME Answers (queue list + reply form driving `POST /v1/sme-replies`), Dashboard (KPI tiles + per-(service, method) counts, p50/p95 latency, status histograms, RAG status mix, dispatch outcomes — polled from `GET /v1/metrics`), Multi-Agents X-Ray drawer (toggled from the header; subscribes to the merged `GET /v1/streams/events` SSE feed of service log + LLM call records, descending order, click-to-detail dialog). Floating chat bubble across all tabs (`POST /v1/queries`). Header branch selector (`main` / `starting-point` / free-text) cross-cuts the Documentation tab. Two orchestrator endpoints back the operator views: `GET /v1/streams/events`, `GET /v1/metrics`. CORS enabled. Plain Vite + `@quasar/vite-plugin` build (no `@quasar/cli`). _Code complete; run `cd portal && npm install && npm run dev` (Node 18+) — start_all.sh boots it automatically once `node_modules` is present._
- [ ] **Update Trigger** ([§8.5](../PROJECT_ARCHITECTURE.md#85-considerations-for-the-poc)) — cron + manual refresh (calls `POST /v1/refresh`).

### Cross-cutting (Module 6)

- [~] **Span instrumentation** — every service wraps inbound/outbound MCP calls in OTel spans ([§9.6](../PROJECT_LOW_LEVEL_DESIGN.md#96-audit-and-observability-module-6)). Implemented as a context-manager helper in [`src/shared/otel_client.py`](src/shared/otel_client.py). _RAG Service uses it; B&P/SD/OC/Portal will plug in with the same pattern._
- [x] **LLM call audit** — every chat invoke records `(module, request, response, started_at, latency_ms)` into the `llm_calls` table at `$AUDIT_DB_PATH` via [`src/shared/llm_log.py`](src/shared/llm_log.py). Wired through `src.shared.llm.LoggedLLM`; module tags follow `<service>.<area>.<step>` convention.
- [x] **Service log** — Java-style `info` / `warn` / `error` / `debug` entries persist to the `service_logs` table at `$AUDIT_DB_PATH` via [`src/shared/service_log.py`](src/shared/service_log.py). Wired across the RAG modules for entry/exit milestones, fallback paths, and validation errors. Mirrored to stdlib `logging` for live console output.
- [ ] **Online metrics dashboard** — derive escalation rate, grader-fail rate, RAG status distribution, latency p50/p95 from the OTel store ([§9.7](../PROJECT_LOW_LEVEL_DESIGN.md#97-evaluation-strategy-module-6)).
- [ ] **Golden Q&A set** — small (≈30) hand-validated set + LLM-as-judge scorer for offline correctness ([§9.7](../PROJECT_LOW_LEVEL_DESIGN.md#97-evaluation-strategy-module-6)).

---

## Architecture cross-reference

Each module file links back to its spec section so the contract stays the source of truth:

- [`src/otel_mcp/`](src/otel_mcp)
  → [§8.2 `OTEL_MCP` node](../PROJECT_ARCHITECTURE.md#82-high-level-architecture-diagram),
  [§8.5 POC OTel collector](../PROJECT_ARCHITECTURE.md#85-considerations-for-the-poc),
  [§9.6 audit & observability](../PROJECT_LOW_LEVEL_DESIGN.md#96-audit-and-observability-module-6).
- [`src/shared/otel_client.py`](src/shared/otel_client.py)
  → [§9.6 span boundary, span attributes, resilience](../PROJECT_LOW_LEVEL_DESIGN.md#96-audit-and-observability-module-6).
- [`src/shared/llm.py`](src/shared/llm.py)
  → [§8.6 LLM strategy — one Ollama, swappable per-node](../PROJECT_ARCHITECTURE.md#86-llm-strategy).
- [`src/shared/llm_log.py`](src/shared/llm_log.py)
  → [§9.6 audit channel](../PROJECT_LOW_LEVEL_DESIGN.md#96-audit-and-observability-module-6) for in-service LLM traffic (complements OTel spans, which cover inter-service MCP traffic).
- [`src/shared/service_log.py`](src/shared/service_log.py)
  → [§9.6 audit channel](../PROJECT_LOW_LEVEL_DESIGN.md#96-audit-and-observability-module-6) for in-service operational events (entry/exit, fallbacks, validation failures). Same SQLite file as `llm_log` (`$AUDIT_DB_PATH`), separate `service_logs` table.
- [`src/rag_service/store.py`](src/rag_service/store.py)
  → [§9.1.1 Embeddings DB ownership](../PROJECT_LOW_LEVEL_DESIGN.md#911-responsibilities),
  [§9.1.3.2 persisted chunk metadata](../PROJECT_LOW_LEVEL_DESIGN.md#9132-tot-chunking-strategy).
- [`src/rag_service/chunking.py`](src/rag_service/chunking.py)
  → [§6.2 chunking strategies](../PROJECT.md#62-chunking-strategies),
  [§9.1.3.2 ToT chunking strategy](../PROJECT_LOW_LEVEL_DESIGN.md#9132-tot-chunking-strategy).
- [`src/rag_service/auto_rag.py`](src/rag_service/auto_rag.py)
  → [§9.1.3.1 Autonomous RAG loop](../PROJECT_LOW_LEVEL_DESIGN.md#9131-autonomous-rag-loop).
- [`src/rag_service/service.py`](src/rag_service/service.py)
  → [§9.1.2 `RAG_MCP.retrieve / index / delete`](../PROJECT_LOW_LEVEL_DESIGN.md#912-apis-mcp).
- [`src/rag_service/server.py`](src/rag_service/server.py)
  → [§8.2 `RAG_MCP` node](../PROJECT_ARCHITECTURE.md#82-high-level-architecture-diagram),
  [§9.1.2 MCP frontend](../PROJECT_LOW_LEVEL_DESIGN.md#912-apis-mcp).
- [`src/bp_service/pages.py`](src/bp_service/pages.py)
  → [§8.5 page storage](../PROJECT_ARCHITECTURE.md#85-considerations-for-the-poc) —
  `PageStore` Protocol + `LocalPageStore` for the POC. `GitHubPageStore` slots in once GitHub MCP wiring lands.
- [`src/bp_service/store.py`](src/bp_service/store.py)
  → [§9.3.1 BP doc index + sources inventory](../PROJECT_LOW_LEVEL_DESIGN.md#931-responsibilities).
- [`src/bp_service/clients.py`](src/bp_service/clients.py)
  → RAG/SD client adapters — `InProcessRAGClient` for the POC, `StubSDClient` until SD lands.
- [`src/bp_service/ingest.py`](src/bp_service/ingest.py)
  → [§9.3.3 background mode ingest](../PROJECT_LOW_LEVEL_DESIGN.md#933-implementation-details) — normalize + content hash.
- [`src/bp_service/compose.py`](src/bp_service/compose.py)
  → [§9.3.3 page composition](../PROJECT_LOW_LEVEL_DESIGN.md#933-implementation-details),
  [§9.5.1 placeholder block format](../PROJECT_LOW_LEVEL_DESIGN.md#951-placeholders-and-re-integration).
- [`src/bp_service/service.py`](src/bp_service/service.py)
  → [§9.3.2 `BP_MCP` six methods](../PROJECT_LOW_LEVEL_DESIGN.md#932-apis-mcp) (`dispatch_query`, `dispatch_refresh`, `find_products_for_service`, `get_page`, `patch_page`, `ingest_sme_doc`).
- [`src/bp_service/server.py`](src/bp_service/server.py)
  → [§8.2 `BP_MCP` node](../PROJECT_ARCHITECTURE.md#82-high-level-architecture-diagram),
  [§9.3.2 MCP frontend](../PROJECT_LOW_LEVEL_DESIGN.md#932-apis-mcp).
- [`src/sd_service/sources.py`](src/sd_service/sources.py)
  → [§9.2.3.1 `pull_source`](../PROJECT_LOW_LEVEL_DESIGN.md#9231-analyze_code) — `SourceStore` Protocol + `LocalSourceStore` for the POC.
- [`src/sd_service/store.py`](src/sd_service/store.py)
  → [§9.2.1 SD doc index + sources inventory](../PROJECT_LOW_LEVEL_DESIGN.md#921-responsibilities) — last-known revision per service tree.
- [`src/sd_service/clients.py`](src/sd_service/clients.py)
  → RAG/BP client adapters — `InProcessRAGClient` / `InProcessBPClient` for the POC, `StubBPClient` until BP is wired in via the orchestrator.
- [`src/sd_service/analyze_code.py`](src/sd_service/analyze_code.py)
  → [§9.2.3.1 `analyze_code` five-step pipeline](../PROJECT_LOW_LEVEL_DESIGN.md#9231-analyze_code) — pull_source → parse_ast → extract_endpoints → extract_calls → llm_augment.
- [`src/sd_service/tot_dep_graph.py`](src/sd_service/tot_dep_graph.py)
  → [§9.2.3.3 ToT dep graph](../PROJECT_LOW_LEVEL_DESIGN.md#9233-tot-dep-graph) — K=3 candidates, beam B=2 D=2, code-only scoring (telemetry collapses to no-op when [§9.2.3.2 `verify_telemetry`](../PROJECT_LOW_LEVEL_DESIGN.md#9232-verify_telemetry) is unavailable).
- [`src/sd_service/compose.py`](src/sd_service/compose.py)
  → SD page composition — endpoints + dep graph + BP cross-references + [§9.5.1 placeholder blocks](../PROJECT_LOW_LEVEL_DESIGN.md#951-placeholders-and-re-integration) for dynamic routes / dynamic calls / low-confidence ToT winners.
- [`src/sd_service/service.py`](src/sd_service/service.py)
  → [§9.2.2 `SD_MCP` five methods](../PROJECT_LOW_LEVEL_DESIGN.md#922-apis-mcp) (`dispatch_query`, `dispatch_refresh`, `find_services_for_product`, `get_page`, `patch_page`); query-mode focused-`analyze_code` fallback on low_confidence/exhausted.
- [`src/sd_service/server.py`](src/sd_service/server.py)
  → [§8.2 `SD_MCP` node](../PROJECT_ARCHITECTURE.md#82-high-level-architecture-diagram),
  [§9.2.2 MCP frontend](../PROJECT_LOW_LEVEL_DESIGN.md#922-apis-mcp).
- [`src/orchestrator/state.py`](src/orchestrator/state.py)
  → [§9.4.3 `pending_sme_questions` + task tracker](../PROJECT_LOW_LEVEL_DESIGN.md#943-implementation-details).
- [`src/orchestrator/clients.py`](src/orchestrator/clients.py)
  → BP/SD client adapters (in-process today, MCP-backed later).
- [`src/orchestrator/routing.py`](src/orchestrator/routing.py)
  → deterministic dispatch routing — Portal query / refresh event / page-owning specialist.
- [`src/orchestrator/service.py`](src/orchestrator/service.py)
  → [§9.4.2 handlers](../PROJECT_LOW_LEVEL_DESIGN.md#942-apis-rest) plus [§9.5 `ingest_sme_reply`](../PROJECT_LOW_LEVEL_DESIGN.md#95-sme-interaction-module-6).
- [`src/orchestrator/server.py`](src/orchestrator/server.py)
  → [§9.4.2 REST API](../PROJECT_LOW_LEVEL_DESIGN.md#942-apis-rest) — FastAPI app exposing the seven `/v1` endpoints.

When you change a spec section, mark the corresponding implementation `[ ]` and re-validate.
