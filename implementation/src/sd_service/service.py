"""SD Service — main façade for the System Design specialist (§9.2).

Exposes the five §9.2.2 methods through the in-process surface; the
``SD_MCP`` frontend in :py:mod:`src.sd_service.server` wraps the same
methods over stdio. Implementation flows:

  * **dispatch_query** — delegates to ``RAG_MCP.retrieve(domain_filter=
    sd, mode=query)``. On ``low_confidence`` / ``exhausted`` runs a
    *focused* ``analyze_code`` pass on the file backing the
    closest-matching endpoint (per §9.2.3 query mode) so the user always
    gets *something* back. Never escalates to an SME.

  * **dispatch_refresh** — for each affected service runs the §9.2.3
    background pipeline: pull source → analyze_code → ToT dep graph →
    resolve_bp_links via BP_MCP → compose page → write through the
    PageStore → ``RAG_MCP.index(domain=sd, ...)``. Skip-unchanged check
    against the SD sources inventory. Returns affected pages +
    escalation envelopes for the orchestrator to queue.

  * **find_services_for_product** — pure relational lookup over the SD
    doc index. Symmetric counterpart to BP's
    ``find_products_for_service``.

  * **get_page** / **patch_page** — same shape as BP's. ``patch_page``
    replaces the fenced placeholder block, removes the ``question_id``
    from ``open_placeholders``, and re-indexes the patched page.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from src.bp_service.pages import PageStore
from src.shared.llm import get_chat_llm
from src.shared.otel_client import OTelClient
from src.shared.service_log import format_exception, get_logger

from .analyze_code import ServiceAnalysis, analyze_service
from .clients import BPClient, RAGClient
from .compose import (
    PageEscalation,
    compose_page,
    merge_into_existing,
    replace_placeholder_block,
)
from .enrich import (
    SD_REQUIRED_SECTIONS,
    detect_gaps,
    extract_answered_sme_blocks,
    fill_gap,
    side_info_revision,
    wrap_sme_answer,
)
from .sources import SourceStore
from .store import (
    SDDocIndex,
    SDDocIndexEntry,
    SDSourcesInventory,
    default_db_path,
)
from .tot_dep_graph import select_dep_graph

log = get_logger("sd.service")

SERVICE_NAME = "sd_service"
DEFAULT_PAGE_PREFIX = "sd/services/"


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

@dataclass
class RefreshOutcome:
    page_uri: str
    service: str
    chunks_indexed: int
    chunking_strategy: str | None
    embedding_revision: str | None
    downstream_services: list[str]
    referenced_products: list[str]
    open_placeholders: list[str]
    escalations: list[dict[str, Any]]
    skipped: bool = False
    skip_reason: str | None = None


# ---------------------------------------------------------------------------
# SDService
# ---------------------------------------------------------------------------

class SDService:
    """In-process SD specialist."""

    def __init__(
        self,
        *,
        page_store: PageStore,
        source_store: SourceStore,
        rag: RAGClient,
        bp: BPClient,
        doc_index: SDDocIndex | None = None,
        sources_inventory: SDSourcesInventory | None = None,
        otel: OTelClient | None = None,
        page_prefix: str = DEFAULT_PAGE_PREFIX,
    ):
        self._pages = page_store
        self._sources = source_store
        self._rag = rag
        self._bp = bp
        db = default_db_path()
        self._doc_index = doc_index or SDDocIndex(db)
        self._inv = sources_inventory or SDSourcesInventory(db)
        self._otel = otel or OTelClient.from_env()
        self._page_prefix = page_prefix.rstrip("/") + "/"

    # ------------------------------------------------------------ utilities

    def _page_uri_for(self, service: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", service.lower()).strip("-") or "untitled"
        return f"{self._page_prefix}{slug}.md"

    # --------------------------------------------------------- dispatch_query

    def dispatch_query(
        self,
        *,
        query: str,
        domain_hint: str = "sd",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Query-mode entry (§9.2.2)."""
        if not query or not query.strip():
            log.error("dispatch_query: empty query")
            raise ValueError("dispatch_query: query is required")
        domain_filter = domain_hint if domain_hint in {"bp", "sd", "both"} else "sd"
        log.info(f"dispatch_query domain_hint={domain_hint} -> domain_filter={domain_filter}")

        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="dispatch_query",
            mcp_domain=domain_filter,
        ) as span:
            span.set_attribute("query_chars", len(query))
            rag_response = self._rag.retrieve(
                query=query, domain_filter=domain_filter, mode="query"
            )
            status = str(rag_response.get("status", "exhausted"))
            answer = rag_response.get("answer")
            sources = rag_response.get("sources") or []

            focused: dict[str, Any] | None = None
            if status in {"low_confidence", "exhausted"}:
                # §9.2.3 query mode fallback: focused analyze_code on the
                # file backing the closest-matching endpoint.
                focused = self._focused_analyze_code(sources)
                if focused is not None:
                    answer = self._compose_focused_answer(
                        query=query,
                        rag_answer=answer,
                        focused=focused,
                    )

            span.set_status(status)
            span.set_payload_summary({
                "status": status,
                "sources_count": len(sources),
                "focused_analyze_code": focused is not None,
            })
            log.info(
                f"dispatch_query done status={status} sources={len(sources)} "
                f"focused={'yes' if focused else 'no'}"
            )
            return {
                "status": status,
                "answer": answer,
                "sources": sources,
                "retrieval_trail": rag_response.get("retrieval_trail") or [],
                "focused_analyze_code": focused,
            }

    def _focused_analyze_code(self, sources: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Pick the closest-matching SD source and run a small
        ``analyze_code`` pass scoped to its file. Returns a compact dict
        or ``None`` when nothing usable is in scope (e.g. no SD source)."""
        sd_sources = [
            s for s in (sources or [])
            if (s.get("domain") == "sd" and s.get("source_uri"))
        ]
        if not sd_sources:
            return None
        # The source URI in the RAG response is the SD page URI; map it
        # back to the underlying service via the doc index.
        page_uri = sd_sources[0]["source_uri"]
        entry = self._doc_index.get(page_uri)
        if entry is None or not entry.service:
            return None
        log.info(
            f"focused analyze_code: page={page_uri} service={entry.service} "
            f"(triggered by RAG low/exhausted)"
        )
        try:
            files_filter: list[str] | None = None
            # Pull only files that already host endpoints we know about
            # so the prompt stays narrow (§9.2.3 query-mode note).
            if entry.endpoints:
                files_filter = sorted({
                    e.get("source_path") for e in entry.endpoints if e.get("source_path")
                })
            analysis = analyze_service(
                service=entry.service,
                store=self._sources,
                augment=False,           # focused path: skip per-endpoint LLM prose
                files_filter=files_filter,
            )
        except Exception as exc:  # noqa: BLE001
            log.error(f"focused analyze_code failed for {entry.service}: {exc}")
            return None
        return {
            "service": entry.service,
            "page_uri": page_uri,
            "endpoints": [e.to_dict() for e in analysis.endpoints],
            "downstream_calls": [c.to_dict() for c in analysis.downstream_calls],
            "files_seen": list(analysis.files_seen),
        }

    def _compose_focused_answer(
        self,
        *,
        query: str,
        rag_answer: str | None,
        focused: dict[str, Any],
    ) -> str:
        """Stitch the RAG response together with the focused-analyze-code
        snapshot. Pure string composition; no extra LLM call so the
        fallback path stays cheap."""
        parts: list[str] = []
        if rag_answer:
            parts.append(rag_answer.strip())
        parts.append(
            f"\n\n*Focused code analysis on `{focused['service']}`* "
            f"(triggered because retrieval was low_confidence/exhausted):"
        )
        for ep in (focused.get("endpoints") or []):
            parts.append(
                f"- `{ep.get('method')} {ep.get('path')}` (handler `{ep.get('handler')}`)"
            )
        if not (focused.get("endpoints")):
            parts.append("- no endpoints recovered from the source files")
        return "\n".join(parts)

    # ------------------------------------------------------- dispatch_refresh

    def dispatch_refresh(self, *, event: dict[str, Any]) -> dict[str, Any]:
        """Background-mode entry (§9.2.2 — enrich-existing flow).

        Iterates the **union** of:
          1. Existing SD pages under the page store (``documentation/sd/``)
             — each is read, gap-detected, filled from RAG with the
             service's freshly-pulled source-code analysis as side-info,
             and merged back in place.
          2. Services in ``SD_SOURCES_GH_PATH`` whose subdirectory has
             *no* SD page yet — composed from scratch via the original
             "analyze → compose" path so newly-added services pick up a
             starter page on the next refresh.

        ``doc_id_or_commit_sha``:
          * empty → enrich every existing page + discover new services.
          * non-empty → either a page URI (``sd/services/foo.md``) or a
            service name (``foo``) — only that one is processed.
          * ``force=True`` bypasses skip-unchanged checks.
        """
        change_kind = (event or {}).get("change_kind", "modified")
        single = (event or {}).get("doc_id_or_commit_sha") or ""
        force = bool((event or {}).get("force"))

        if single:
            single_clean = single.strip()
            if single_clean.startswith(self._page_prefix) or single_clean.endswith(".md"):
                pages = [single_clean]
                services = []
            else:
                # Treat as a service name → resolve to its page_uri.
                pages = [self._page_uri_for(_service_from_event(single_clean) or single_clean)]
                services = []
        else:
            pages = self._pages.list_pages()
            # Filter to SD-rooted pages only — the page-store now
            # surfaces the entire ``documentation/`` tree (so BP
            # pages and the shared ``documentation/sme-responses/``
            # archive show up too). We only enrich what we own.
            pages = [p for p in pages if p and p.startswith("sd/")]
            services = self._sources.list_services()

        log.info(
            f"dispatch_refresh start change_kind={change_kind} "
            f"pages={len(pages)} services={len(services)} "
            f"single={single!r} force={force}"
        )

        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="dispatch_refresh",
        ) as span:
            span.set_attribute("change_kind", change_kind)
            span.set_attribute("page_count", len(pages))
            span.set_attribute("service_count", len(services))
            span.set_attribute("force", force)
            outcomes: list[RefreshOutcome] = []
            covered_services: set[str] = set()

            # 1) Enrich every existing SD page.
            for page_uri in pages:
                try:
                    outcome = self._refresh_one_page(
                        page_uri=page_uri,
                        change_kind=change_kind,
                        force=force,
                    )
                    if outcome.service:
                        covered_services.add(outcome.service)
                    outcomes.append(outcome)
                except Exception as exc:  # noqa: BLE001 — per-page isolation
                    details = format_exception(exc)
                    log.error(f"dispatch_refresh: page {page_uri} failed: {details}")
                    outcomes.append(RefreshOutcome(
                        page_uri=page_uri,
                        service=None,
                        chunks_indexed=0,
                        chunking_strategy=None,
                        embedding_revision=None,
                        downstream_services=[],
                        referenced_products=[],
                        open_placeholders=[],
                        escalations=[{"error": details}],
                        skipped=True,
                        skip_reason="refresh_error",
                    ))

            # 2) New-service discovery — services with no SD page yet.
            for svc in services:
                if svc in covered_services:
                    continue
                if self._doc_index.get_by_service(svc) is not None and not force:
                    continue
                try:
                    outcomes.append(
                        self._refresh_one_service(
                            service=svc, change_kind=change_kind, force=force,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 — per-service isolation
                    details = format_exception(exc)
                    log.error(f"dispatch_refresh: service {svc} failed: {details}")
                    outcomes.append(RefreshOutcome(
                        page_uri=self._page_uri_for(svc),
                        service=svc,
                        chunks_indexed=0,
                        chunking_strategy=None,
                        embedding_revision=None,
                        downstream_services=[],
                        referenced_products=[],
                        open_placeholders=[],
                        escalations=[{"error": details}],
                        skipped=True,
                        skip_reason="refresh_error",
                    ))

            affected_pages = [o.page_uri for o in outcomes if not o.skipped]
            escalations = [e for o in outcomes for e in o.escalations]
            span.set_status("ok")
            span.set_payload_summary({
                "pages_seen": len(pages),
                "services_seen": len(services),
                "affected_pages": len(affected_pages),
                "escalations": len(escalations),
            })
            log.info(
                f"dispatch_refresh done affected_pages={len(affected_pages)} "
                f"escalations={len(escalations)} "
                f"skipped={sum(1 for o in outcomes if o.skipped)}"
            )
            # Tag the head of the docs branch so each refresh has a
            # rollback marker. Best-effort — tag failure must NOT fail
            # the refresh, since the pages have already been written.
            self._tag_after_refresh(affected_pages_count=len(affected_pages))
            return {
                "affected_pages": affected_pages,
                "escalations": escalations,
                "details": [
                    {
                        "page_uri": o.page_uri,
                        "service": o.service,
                        "skipped": o.skipped,
                        "skip_reason": o.skip_reason,
                        "chunks_indexed": o.chunks_indexed,
                        "chunking_strategy": o.chunking_strategy,
                        "embedding_revision": o.embedding_revision,
                        "downstream_services": o.downstream_services,
                        "referenced_products": o.referenced_products,
                        "open_placeholders": o.open_placeholders,
                    }
                    for o in outcomes
                ],
            }

    # ----------------------------------- _refresh_one_page (enrich existing)

    def _refresh_one_page(
        self, *, page_uri: str, change_kind: str, force: bool = False
    ) -> RefreshOutcome:
        """Read existing SD page → detect gaps via LLM → fill via RAG
        with source-code analysis as side-info → merge back → re-index.

        Wrapped in an OTel ``enrich_page`` span tagged with
        ``gap_count`` / ``filled_count`` / ``escalated_count`` and
        status ``enriched`` / ``escalated_only`` / ``unchanged`` /
        ``page_deleted`` so the Dashboard can surface per-page outcomes.
        """
        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="enrich_page",
        ) as span:
            span.set_attribute("page_uri", page_uri)
            span.set_attribute("force", force)

            existing = self._pages.read_page(page_uri)
            if existing is None:
                log.warn(f"_refresh_one_page: {page_uri!r} not found; treating as deletion")
                self._doc_index.delete(page_uri)
                try:
                    self._rag.delete(domain="sd", source_uri=page_uri)
                except Exception as exc:  # noqa: BLE001
                    log.error(f"_refresh_one_page: rag.delete({page_uri!r}) failed: {exc}")
                span.set_status("page_deleted")
                return RefreshOutcome(
                    page_uri=page_uri,
                    service=None,
                    chunks_indexed=0,
                    chunking_strategy=None,
                    embedding_revision=None,
                    downstream_services=[],
                    referenced_products=[],
                    open_placeholders=[],
                    escalations=[],
                    skipped=True,
                    skip_reason="page_not_found",
                )

            prior = self._doc_index.get(page_uri)
            page_hash = _hash_text(existing)
            service_name = (
                (prior.service if prior else None)
                or _service_from_page_uri(page_uri, self._page_prefix)
            )
            # Data-store / category pages have a URI like
            # ``database/<svc>-db.md`` that doesn't directly name a
            # service. Try common DB-page naming conventions to resolve
            # the owning service so we can pull its analysis as
            # side-info (otherwise schema/data-store pages get no
            # source-code context and escalate every gap).
            try:
                known_services = set(self._sources.list_services())
            except Exception as exc:  # noqa: BLE001
                log.warn(f"_refresh_one_page: list_services failed: {exc}")
                known_services = set()
            if service_name and known_services and service_name not in known_services:
                candidates = []
                last = service_name.split("/")[-1]
                for suffix in ("-db", "_db", "-database", "_database"):
                    if last.endswith(suffix):
                        candidates.append(last[: -len(suffix)])
                for prefix in ("db-", "db_"):
                    if last.startswith(prefix):
                        candidates.append(last[len(prefix):])
                for cand in candidates:
                    if cand in known_services:
                        log.info(
                            f"_refresh_one_page: {page_uri} resolved owning service "
                            f"{service_name!r} → {cand!r} via DB-naming convention"
                        )
                        service_name = cand
                        break

            # Pull fresh source-code analysis as side-info (best-effort —
            # missing source code is non-fatal; the gap-fill loop will
            # just lean on RAG without the contextual hints).
            analysis_summary: dict[str, Any] = {}
            analysis: ServiceAnalysis | None = None
            if service_name:
                try:
                    analysis = analyze_service(
                        service=service_name, store=self._sources, augment=True,
                    )
                    analysis_summary = {
                        "service": service_name,
                        "endpoints": [
                            {"method": e.method, "path": e.path, "handler": getattr(e, "handler", None)}
                            for e in (analysis.endpoints or [])[:24]
                        ],
                        "downstream_services": list((analysis.downstream_calls or [])[:24]),
                        "data_stores": [
                            d.to_dict() for d in (analysis.data_stores or [])
                        ],
                        "data_structures": [
                            d.to_dict() for d in (analysis.data_structures or [])[:12]
                        ],
                        "source_revision": analysis.source_revision,
                    }
                except Exception as exc:  # noqa: BLE001
                    log.warn(
                        f"_refresh_one_page: analyze_service({service_name!r}) failed: {exc}"
                    )

            sd_revision = side_info_revision(analysis_summary)

            # Skip-unchanged: page-content + side-info both unchanged.
            if (
                not force
                and prior is not None
                and prior.content_hash == page_hash
                and (prior.side_info_revision or "") == sd_revision
            ):
                log.info(
                    f"_refresh_one_page: {page_uri} unchanged (page+side-info); skipping"
                )
                span.set_status("unchanged")
                span.set_attribute("gap_count", 0)
                return RefreshOutcome(
                    page_uri=page_uri,
                    service=service_name,
                    chunks_indexed=0,
                    chunking_strategy=prior.chunking_strategy,
                    embedding_revision=prior.embedding_revision,
                    downstream_services=prior.downstream_services,
                    referenced_products=prior.referenced_products,
                    open_placeholders=prior.open_placeholders,
                    escalations=[],
                    skipped=True,
                    skip_reason="unchanged",
                )

            title = _title_from_content(existing) or service_name or page_uri

            # Preserve any SME-answered prose carried by the previous page
            # body so re-enrichment doesn't overwrite human-authored answers.
            answered_blocks = extract_answered_sme_blocks(existing)
            if prior and prior.answered_sme_blocks:
                for qid, info in prior.answered_sme_blocks.items():
                    answered_blocks.setdefault(qid, info)

            # Detect gaps via the LLM judge — classifies the page kind
            # (service / architecture-overview / data-flow / data-store /
            # other) using the URI as a hint plus the source-code
            # analysis as content, then proposes per-kind sections
            # tailored to whether the page is even supposed to talk
            # about endpoints / schemas / etc.
            llm = self._get_chat_llm()
            gap_plan = detect_gaps(
                existing,
                page_uri=page_uri,
                page_title=title,
                analysis_summary=analysis_summary or None,
                llm=llm,
            )
            log.info(
                f"_refresh_one_page: {page_uri} kind={gap_plan.page_kind} "
                f"→ {len(gap_plan.gaps)} gap(s); "
                f"answered={len(answered_blocks)} force={force}"
            )
            span.set_attribute("gap_count", len(gap_plan.gaps))
            span.set_attribute("answered_block_count", len(answered_blocks))
            span.set_attribute("is_substantive", gap_plan.is_substantive)
            span.set_attribute("page_kind", gap_plan.page_kind)

            # Fill each gap with the analysis summary as side-info.
            filled = []
            escalations: list[PageEscalation] = []
            for gap in gap_plan.gaps:
                fg = fill_gap(
                    gap,
                    page_uri=page_uri,
                    page_title=title,
                    service=service_name,
                    analysis_summary=analysis_summary,
                    rag=self._rag,
                    llm=llm,
                    answered_sme_blocks=answered_blocks,
                )
                filled.append(fg)
                if fg.is_sme_placeholder and fg.question_id:
                    escalations.append(PageEscalation(
                        question_id=fg.question_id,
                        placeholder_id=fg.question_id,
                        topic=gap.section_title,
                        question=gap.fill_prompt,
                        best_guess=None,
                        page_uri=page_uri,
                    ))

            filled_substantive = sum(
                1 for fg in filled if not fg.is_sme_placeholder
            )
            # How many gaps went source-first vs RAG vs SME — useful
            # signal on the dashboard when triaging "why are pages
            # still escalating?". Only counts the gaps the detector
            # *intended* to source-fill; not the fall-through case
            # where source failed and we re-tried via RAG.
            source_intended = sum(1 for g in gap_plan.gaps if g.fill_strategy == "source")
            rag_intended = sum(1 for g in gap_plan.gaps if g.fill_strategy != "source")
            span.set_attribute("filled_count", filled_substantive)
            span.set_attribute("escalated_count", len(escalations))
            span.set_attribute("source_intended_count", source_intended)
            span.set_attribute("rag_intended_count", rag_intended)

            if filled:
                sections = [(fg.gap.section_title, fg.section_md) for fg in filled]
                new_content = merge_into_existing(
                    existing_content=existing,
                    sections=sections,
                )
            else:
                new_content = existing

            new_hash = _hash_text(new_content)
            if new_hash == page_hash and not force:
                log.info(f"_refresh_one_page: {page_uri} no enrichment changes; skipping write")
                commit_sha = (prior.metadata.get("commit_sha") if prior else None) or ""
            else:
                commit_sha = self._pages.write_page(page_uri, new_content)
                log.info(
                    f"_refresh_one_page: wrote {page_uri} commit={commit_sha} "
                    f"placeholders={len(escalations)}"
                )

            # Re-index in RAG.
            rag_index_result: dict[str, Any] = {}
            try:
                rag_index_result = self._rag.index(
                    domain="sd",
                    source_uri=page_uri,
                    document=new_content,
                    content_hash=new_hash,
                )
            except Exception as exc:  # noqa: BLE001
                log.error(f"_refresh_one_page: rag.index({page_uri!r}) failed: {exc}")

            chunks_indexed = int(rag_index_result.get("chunks_indexed", 0))
            last_updated = time.time()

            # Inventory now tracks the SD page hash itself.
            if service_name:
                self._inv.upsert(
                    service_name,
                    analysis.source_revision if analysis else (prior.source_revision if prior else "unknown"),
                    file_count=len(analysis.files_seen) if analysis else 0,
                )
            open_placeholder_ids = [esc.question_id for esc in escalations]

            # Compose-derived structural fields (endpoints, downstream
            # services) come from the freshly-pulled analysis when we
            # have one; otherwise carry over from the prior entry.
            endpoints_payload = (
                [
                    {"method": e.method, "path": e.path, "handler": getattr(e, "handler", None)}
                    for e in (analysis.endpoints or [])
                ] if analysis else (prior.endpoints if prior else [])
            )
            downstream_payload = (
                list(analysis.downstream_calls or []) if analysis
                else (prior.downstream_services if prior else [])
            )

            self._doc_index.upsert(SDDocIndexEntry(
                page_uri=page_uri,
                service=service_name,
                title=title,
                last_updated=last_updated,
                source_revision=analysis.source_revision if analysis else (prior.source_revision if prior else None),
                content_hash=new_hash,
                chunking_strategy=rag_index_result.get("chunking_strategy"),
                embedding_revision=rag_index_result.get("embedding_revision"),
                open_placeholders=open_placeholder_ids,
                endpoints=endpoints_payload,
                downstream_services=downstream_payload,
                referenced_products=_merged_cross_refs(
                    prior=(prior.referenced_products if prior else []),
                    page_content=new_content,
                    my_domain="sd",
                    page_uri=page_uri,
                ),
                side_info_revision=sd_revision,
                answered_sme_blocks=answered_blocks,
                metadata={"commit_sha": commit_sha, "change_kind": change_kind},
            ))

            if filled_substantive > 0:
                span.set_status("enriched")
            elif escalations:
                span.set_status("escalated_only")
            else:
                span.set_status("unchanged")

            return RefreshOutcome(
                page_uri=page_uri,
                service=service_name,
                chunks_indexed=chunks_indexed,
                chunking_strategy=rag_index_result.get("chunking_strategy"),
                embedding_revision=rag_index_result.get("embedding_revision"),
                downstream_services=downstream_payload,
                referenced_products=_merged_cross_refs(
                    prior=(prior.referenced_products if prior else []),
                    page_content=new_content,
                    my_domain="sd",
                    page_uri=page_uri,
                ),
                open_placeholders=open_placeholder_ids,
                escalations=[esc.envelope() for esc in escalations],
            )

    # ------------------------ _refresh_one_service (new-page compose flow)

    def _refresh_one_service(
        self, *, service: str, change_kind: str, force: bool = False
    ) -> RefreshOutcome:
        """Compose a fresh SD page from the source-code analysis. Used
        for services discovered in source code that don't yet have an
        SD page; on subsequent refreshes the page goes through
        :meth:`_refresh_one_page` like any other.

        This is the original "analyze → ToT dep graph → compose"
        pipeline preserved verbatim — we just no longer enter it for
        services that *already* have a page.

        Wrapped in an ``enrich_page`` span tagged ``stub_created`` /
        ``unchanged`` / ``error`` so the Dashboard's "new pages
        stubbed" KPI counts SD's first-run service pages alongside
        BP's product stubs (otherwise the histogram bucket stays
        empty).
        """
        page_uri = self._page_uri_for(service)
        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="enrich_page",
            mcp_domain="sd",
            attributes={
                "page_uri": page_uri,
                "page_kind": "stub",
                "service": service,
            },
        ) as span:
            try:
                analysis: ServiceAnalysis = analyze_service(
                    service=service, store=self._sources, augment=True
                )

                # Skip unchanged services per §9.2.1 sources-inventory diff.
                if not force and self._inv.is_unchanged(service, analysis.source_revision):
                    existing = self._doc_index.get(page_uri)
                    if existing is not None:
                        log.info(f"_refresh_one_service: {service} unchanged; skipping")
                        span.set_status("unchanged")
                        return RefreshOutcome(
                            page_uri=page_uri,
                            service=service,
                            chunks_indexed=0,
                            chunking_strategy=existing.chunking_strategy,
                            embedding_revision=existing.embedding_revision,
                            downstream_services=existing.downstream_services,
                            referenced_products=existing.referenced_products,
                            open_placeholders=existing.open_placeholders,
                            escalations=[],
                            skipped=True,
                            skip_reason="unchanged",
                        )

                log.info(
                    f"_refresh_one_service: composing fresh page for {service} -> {page_uri} "
                    f"revision={analysis.source_revision} "
                    f"endpoints={len(analysis.endpoints)} calls={len(analysis.downstream_calls)}"
                )

                # ToT dep graph + BP cross-references.
                tot = select_dep_graph(analysis)
                try:
                    related = self._bp.find_products_for_service(service)
                except Exception as exc:  # noqa: BLE001
                    log.error(f"_refresh_one_service: bp.find_products_for_service({service!r}) failed: {exc}")
                    related = []

                last_updated = time.time()
                composed = compose_page(
                    page_uri=page_uri,
                    analysis=analysis,
                    tot=tot,
                    related_products=related,
                    last_updated=last_updated,
                    content_hash=analysis.source_revision,
                )

                commit_sha = self._pages.write_page(page_uri, composed.content)
                log.info(
                    f"_refresh_one_service: wrote {page_uri} commit={commit_sha} "
                    f"deps={len(composed.downstream_services)} "
                    f"placeholders={len(composed.open_placeholders)}"
                )

                # Index through RAG_MCP.
                rag_index_result: dict[str, Any] = {}
                try:
                    rag_index_result = self._rag.index(
                        domain="sd",
                        source_uri=page_uri,
                        document=composed.content,
                        content_hash=analysis.source_revision,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.error(f"_refresh_one_service: rag.index({page_uri!r}) failed: {exc}")

                chunks_indexed = int(rag_index_result.get("chunks_indexed", 0))

                self._inv.upsert(service, analysis.source_revision, file_count=len(analysis.files_seen))
                # Compute side_info_revision from the same analysis summary
                # _refresh_one_page would have used, so a subsequent enrich pass
                # treats the page as fresh.
                analysis_summary = {
                    "service": service,
                    "endpoints": [
                        {"method": e.method, "path": e.path, "handler": getattr(e, "handler", None)}
                        for e in (analysis.endpoints or [])[:24]
                    ],
                    "downstream_services": list((analysis.downstream_calls or [])[:24]),
                    "data_stores": [d.to_dict() for d in (analysis.data_stores or [])],
                    "data_structures": [
                        d.to_dict() for d in (analysis.data_structures or [])[:12]
                    ],
                    "source_revision": analysis.source_revision,
                }
                sd_revision = side_info_revision(analysis_summary)

                self._doc_index.upsert(SDDocIndexEntry(
                    page_uri=page_uri,
                    service=service,
                    title=service,
                    last_updated=last_updated,
                    source_revision=analysis.source_revision,
                    content_hash=_hash_text(composed.content),
                    chunking_strategy=rag_index_result.get("chunking_strategy"),
                    embedding_revision=rag_index_result.get("embedding_revision"),
                    open_placeholders=composed.open_placeholders,
                    endpoints=composed.endpoints,
                    downstream_services=composed.downstream_services,
                    referenced_products=composed.referenced_products,
                    side_info_revision=sd_revision,
                    answered_sme_blocks={},
                    metadata={"commit_sha": commit_sha, "change_kind": change_kind, "stub": True},
                ))

                span.set_status("stub_created")
                span.set_payload_summary({
                    "service": service,
                    "page_uri": page_uri,
                    "chunks_indexed": chunks_indexed,
                    "endpoints": len(composed.endpoints),
                    "open_placeholders": len(composed.open_placeholders),
                })
                return RefreshOutcome(
                    page_uri=page_uri,
                    service=service,
                    chunks_indexed=chunks_indexed,
                    chunking_strategy=rag_index_result.get("chunking_strategy"),
                    embedding_revision=rag_index_result.get("embedding_revision"),
                    downstream_services=composed.downstream_services,
                    referenced_products=composed.referenced_products,
                    open_placeholders=composed.open_placeholders,
                    escalations=[esc.envelope() for esc in composed.escalations],
                )
            except Exception:
                span.set_status("error")
                raise

    def _get_chat_llm(self):
        """Lazy LLM instance for enrichment. Cached on the service so
        the rubric prompt fits the same warm Ollama context across
        every page in a refresh batch."""
        if not hasattr(self, "_chat_llm") or self._chat_llm is None:
            self._chat_llm = get_chat_llm(
                module="sd.enrich",
                temperature=0.2,
                json_mode=True,
            )
        return self._chat_llm

    def _resolve_existing_page(self, page_uri: str) -> tuple[str | None, str]:
        """Read a page, trying alternate URI shapes if the primary
        lookup misses. Same shape as ``BPService._resolve_existing_page``
        — see that method for the rationale (queue entries from
        before the ``pages_prefix`` env-var fix may carry URIs that
        no longer match the current page-store layout).
        """
        candidates = [page_uri]
        if page_uri.startswith("documentation/"):
            candidates.append(page_uri[len("documentation/"):])
        for prefix in ("sd/", "bp/"):
            if page_uri.startswith(prefix):
                candidates.append(page_uri[len(prefix):])
        if not page_uri.startswith(("sd/", "bp/", "documentation/")):
            candidates.append(f"sd/{page_uri}")
        seen: set[str] = set()
        for cand in candidates:
            if not cand or cand in seen:
                continue
            seen.add(cand)
            content = self._pages.read_page(cand)
            if content is not None:
                if cand != page_uri:
                    log.warn(
                        f"patch_page: resolved {page_uri!r} via alternate "
                        f"{cand!r} (likely from a pre-prefix-fix queue entry)"
                    )
                return content, cand
        return None, page_uri

    def _tag_after_refresh(self, *, affected_pages_count: int) -> None:
        """Mark a successful refresh in Git so each run has a rollback
        anchor. Tag shape: ``sd-refresh-<UTC timestamp>``. Skipped
        when no pages were written (nothing to mark) and silenced on
        any error (a tag failure must never fail the dispatch — the
        pages already landed)."""
        if affected_pages_count <= 0:
            return
        ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        tag = f"sd-refresh-{ts}"
        try:
            res = self._pages.create_tag(tag)
            if res.get("created"):
                log.info(
                    f"tagged refresh {tag} → {res.get('commit_sha') or '(no sha)'}"
                )
            else:
                log.info(f"tag {tag} already existed; skipped")
        except Exception as exc:  # noqa: BLE001
            log.warn(f"create_tag {tag} failed (non-fatal): {format_exception(exc)}")

    # -------------------------------------------- find_services_for_product

    def find_services_for_product(self, product_id: str) -> list[dict[str, Any]]:
        """SD-side relational lookup (§9.2.2). Returns every SD page that
        names the product (via ``referenced_products``)."""
        if not product_id:
            log.error("find_services_for_product: missing product_id")
            raise ValueError("product_id is required")
        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="find_services_for_product",
        ) as span:
            entries: list[SDDocIndexEntry] = []
            for entry in self._doc_index.list_all():
                if any(product_id in p for p in entry.referenced_products):
                    entries.append(entry)
            out: list[dict[str, Any]] = []
            for entry in entries:
                for ep in entry.endpoints[:3]:  # cap so the response stays compact
                    out.append({
                        "service": entry.service,
                        "page_uri": entry.page_uri,
                        "endpoint": f"{ep.get('method')} {ep.get('path')}",
                        "role": "owner",
                    })
                if not entry.endpoints:
                    out.append({
                        "service": entry.service,
                        "page_uri": entry.page_uri,
                        "endpoint": None,
                        "role": "owner",
                    })
            span.set_status("ok")
            span.set_payload_summary({"product_id": product_id, "matches": len(out)})
            log.info(f"find_services_for_product product={product_id} matches={len(out)}")
            return out

    # ----------------------------------------------------------- get_page

    def list_pages(self) -> list[dict[str, Any]]:
        """Pure relational dump of the SD doc-index — used by BP's
        enrich pipeline as side-info ("which products do SD pages
        reference?") and by BP's new-page discovery to find products
        without a BP page yet. No LLM, no RAG."""
        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="list_pages",
        ) as span:
            entries = self._doc_index.list_all()
            span.set_status("ok")
            span.set_payload_summary({"count": len(entries)})
            return [e.to_dict() for e in entries]

    def get_page(self, page_uri: str) -> dict[str, Any]:
        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="get_page",
        ) as span:
            content = self._pages.read_page(page_uri)
            entry = self._doc_index.get(page_uri)
            if content is None:
                span.set_status("not_found")
                log.warn(f"get_page: {page_uri!r} not present on disk")
                return {"content": None, "doc_index_entry": entry.to_dict() if entry else None}
            span.set_status("ok")
            span.set_payload_summary({
                "page_uri": page_uri,
                "has_index_entry": entry is not None,
            })
            return {"content": content, "doc_index_entry": entry.to_dict() if entry else None}

    # ------------------------------------------------------------ patch_page

    def patch_page(self, *, page_uri: str, question_id: str, replacement: str) -> dict[str, Any]:
        if not (page_uri and question_id and replacement):
            log.error("patch_page: page_uri, question_id, replacement are all required")
            raise ValueError("patch_page: page_uri, question_id, replacement are all required")
        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="patch_page",
        ) as span:
            # Try the URI as queued, then a couple of alternate
            # shapes so historical escalations from before the
            # ``pages_prefix`` env-var fix still patch cleanly.
            # Mirror of the BP-side helper.
            content, resolved_uri = self._resolve_existing_page(page_uri)
            if content is None:
                span.set_status("not_found")
                log.error(f"patch_page: {page_uri!r} does not exist (tried alternates)")
                raise FileNotFoundError(f"page not found: {page_uri}")
            page_uri = resolved_uri
            # The SME's reply is inserted as plain prose — no wrapping
            # fences. The whole ``SME-PLACEHOLDER`` block is replaced
            # with the answer, so the page reads naturally and the
            # next refresh treats the prose as substantive content
            # (the merger's ``_section_body_is_fillable`` guard
            # refuses to overwrite).
            replacement_prose = (replacement or "").strip()
            new_content, replaced = replace_placeholder_block(
                content,
                question_id=question_id,
                replacement=replacement_prose,
            )
            if not replaced:
                span.set_status("not_found")
                log.warn(
                    f"patch_page: placeholder {question_id!r} not found in {page_uri!r}; "
                    "doc index entry left untouched"
                )
                return {"commit_sha": None, "patched": False}

            commit_sha = self._pages.write_page(page_uri, new_content)
            self._doc_index.remove_open_placeholder(page_uri, question_id)

            try:
                rag_res = self._rag.index(
                    domain="sd",
                    source_uri=page_uri,
                    document=new_content,
                )
                entry = self._doc_index.get(page_uri)
                if entry is not None:
                    entry.embedding_revision = rag_res.get("embedding_revision") or entry.embedding_revision
                    entry.chunking_strategy = rag_res.get("chunking_strategy") or entry.chunking_strategy
                    entry.last_updated = time.time()
                    # Persist the SME-answered prose so subsequent
                    # refreshes can match it from `answered_sme_blocks`
                    # even if the in-page fence detection misfires.
                    entry.answered_sme_blocks = dict(entry.answered_sme_blocks or {})
                    entry.answered_sme_blocks[question_id] = {
                        "hash": _hash_text(replacement)[:12],
                        "prose": replacement.strip(),
                    }
                    self._doc_index.upsert(entry)
            except Exception as exc:  # noqa: BLE001
                log.error(f"patch_page: rag.index({page_uri!r}) failed: {exc}")

            span.set_status("ok")
            span.set_payload_summary({
                "page_uri": page_uri,
                "question_id": question_id,
                "commit_sha": commit_sha,
            })
            log.info(f"patch_page: {page_uri} question_id={question_id} replaced=True")
            return {"commit_sha": commit_sha, "patched": True}

    # -------------------------------------------------------- ingest_sme_doc

    def ingest_sme_doc(
        self,
        *,
        question_id: str,
        sme_text: str,
        originating_pages: list[str] | None = None,
        topic: str | None = None,
        question: str | None = None,
    ) -> dict[str, Any]:
        """Persist an SME reply as a brand-new SD page so the answer
        is searchable on its own. Mirrors ``BPService.ingest_sme_doc``
        but lands the file under ``sd/sme-replies/`` so SD-domain
        questions stay in the SD tree (the page-store contributes the
        ``documentation/`` root, so the final path is
        ``documentation/sd/sme-replies/<id>.md``)."""
        if not (question_id and sme_text):
            log.error("ingest_sme_doc: question_id and sme_text are required")
            raise ValueError("ingest_sme_doc: question_id and sme_text are required")
        page_uri = f"sme-responses/{question_id}.md"
        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="ingest_sme_doc",
        ) as span:
            now = time.time()
            origin_lines = ", ".join(originating_pages or []) or "(none)"
            content = (
                f"# SME reply: {topic or question_id}\n\n"
                f"> Persisted on {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))}.\n"
                f"> Question id: `{question_id}` · Originating pages: {origin_lines}\n\n"
                f"## Question\n\n{(question or '(not recorded)').strip()}\n\n"
                f"## Answer\n\n{sme_text.strip()}\n"
            )
            commit_sha = self._pages.write_page(page_uri, content)
            try:
                rag_res = self._rag.index(
                    domain="sd",
                    source_uri=page_uri,
                    document=content,
                )
            except Exception as exc:  # noqa: BLE001
                log.error(f"ingest_sme_doc: rag.index({page_uri!r}) failed: {exc}")
                rag_res = {"chunking_strategy": None, "embedding_revision": None, "chunks_indexed": 0}

            self._doc_index.upsert(SDDocIndexEntry(
                page_uri=page_uri,
                service="sme-replies",
                title=f"SME reply: {topic or question_id}",
                last_updated=now,
                source_revision="sme-reply",
                content_hash=_hash_text(content),
                chunking_strategy=rag_res.get("chunking_strategy"),
                embedding_revision=rag_res.get("embedding_revision"),
                open_placeholders=[],
                endpoints=[],
                downstream_services=[],
                referenced_products=[],
                side_info_revision=None,
                answered_sme_blocks={},
                metadata={
                    "sme_question_id": question_id,
                    "originating_pages": list(originating_pages or []),
                    "commit_sha": commit_sha,
                },
            ))

            span.set_status("ok")
            span.set_payload_summary({
                "question_id": question_id,
                "page_uri": page_uri,
                "originating_pages": len(originating_pages or []),
            })
            log.info(
                f"ingest_sme_doc: wrote {page_uri} chunks={rag_res.get('chunks_indexed', 0)}"
            )
            return {
                "new_page_uri": page_uri,
                "embedding_revision": rag_res.get("embedding_revision"),
                "commit_sha": commit_sha,
            }

    # ----------------------------------------------------- introspection

    @property
    def doc_index(self) -> SDDocIndex:
        return self._doc_index

    @property
    def sources_inventory(self) -> SDSourcesInventory:
        return self._inv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_text(text: str) -> str:
    import hashlib
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


_TITLE_RE = re.compile(r"^\s*#\s+(?P<title>.+?)\s*$", re.MULTILINE)


def _title_from_content(content: str) -> str | None:
    """Pull the first ``# Heading`` from a Markdown page if present."""
    if not content:
        return None
    m = _TITLE_RE.search(content)
    return m.group("title").strip() if m else None


def _service_from_page_uri(page_uri: str, page_prefix: str) -> str | None:
    """Reverse of ``SDService._page_uri_for``. Strips the ``page_prefix``
    plus the ``.md`` extension to recover the service name. Returns
    ``None`` if the URI doesn't match the expected layout (e.g. the page
    was hand-authored without going through the agent's naming scheme)."""
    pp = page_prefix.rstrip("/") + "/"
    uri = page_uri.lstrip("/")
    if not uri.startswith(pp) or not uri.endswith(".md"):
        return None
    stem = uri[len(pp):-len(".md")]
    return stem.strip("/") or None


def _service_from_event(token: str) -> str | None:
    """Map a refresh token to the service it belongs to.

    Accepts either a bare service name (e.g. ``billing-service``) or a
    path-shaped token (``services/billing-service/handler.py``); the
    second form is the GitHub MCP idiom we'll follow once the trigger
    feeds real commit-sha events into the orchestrator.
    """
    token = (token or "").strip().strip("/")
    if not token:
        return None
    parts = token.split("/")
    if len(parts) == 1:
        return parts[0]
    if parts[0] in {"services", "src", "code"}:
        return parts[1] if len(parts) > 1 else None
    return parts[0]


def _merged_cross_refs(
    *,
    prior: list[str] | None,
    page_content: str,
    my_domain: str,
    page_uri: str,
) -> list[str]:
    """Union of prior cross-domain references and any new ones scraped
    from the current page body. Keeps the dashboard's
    ``cross_reference_health`` rollup honest as the LLM (and SMEs)
    drop links into pages over time."""
    from src.shared.citations import extract_cross_domain_refs

    fresh = extract_cross_domain_refs(
        page_content, my_domain=my_domain, page_uri=page_uri,
    )
    return sorted(set(prior or []) | set(fresh))
