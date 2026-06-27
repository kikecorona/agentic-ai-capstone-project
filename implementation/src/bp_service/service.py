"""B&P Service — main façade for the BP specialist (§9.3).

Exposes the six §9.3.2 methods upstream callers (Orchestrator, peer SD,
validation script) depend on. Internally each method threads through:

  * **OTel spans** for the inbound MCP boundary (§9.6).
  * **Service log** for high-level entry/exit and unexpected paths.
  * **RAG client** for indexing / retrieval (§9.1.2).
  * **SD client** for cross-reference resolution (§9.2.2 ``find_services_for_product``).
  * **PageStore** for reading inputs and writing pages.
  * **DocIndex / SourcesInventory** for per-page metadata + change detection.

Method-by-method:

  * ``dispatch_query`` — query mode pipeline (§9.3.3 query mode):
    ``RAG_MCP.retrieve(domain_filter=bp/both, mode=query)`` →
    ``resolve_sd_links`` over services mentioned in the answer →
    composed answer with inline relative links. Never escalates.

  * ``dispatch_refresh`` — background-mode pipeline (§9.3.3 background
    mode): ingest → diff sources_inventory → ``RAG_MCP.index(domain=bp)``
    → service-candidate extraction → ``resolve_sd_links`` → compose page
    → write page. Returns affected pages + escalation envelopes.

  * ``find_products_for_service`` — pure relational lookup over the
    BP doc index (no LLM, no retrieval).

  * ``get_page`` — read the current page content + doc-index metadata.

  * ``patch_page`` — replace the fenced ``SME-PLACEHOLDER:question_id``
    block with the SME's text + relative link, write through the page
    store, update ``open_placeholders`` in the doc index, trigger a
    re-index against the patched content. Used by the orchestrator's
    ``ingest_sme_reply``.

  * ``ingest_sme_doc`` — turn an SME reply into a brand-new BP page,
    persist it through the page store, index it through RAG_MCP, and
    record a doc-index entry. Returns the new page URI for use in
    subsequent ``patch_page`` calls.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.shared.llm import get_chat_llm
from src.shared.otel_client import OTelClient
from src.shared.service_log import format_exception, get_logger

from .clients import RAGClient, SDClient
from .compose import (
    PageEscalation,
    compose_page,
    extract_service_candidates,
    merge_into_existing,
    replace_placeholder_block,
    resolve_sd_links,
)
from .enrich import (
    BP_REQUIRED_SECTIONS,
    detect_gaps,
    extract_answered_sme_blocks,
    fill_gap,
    side_info_revision,
    wrap_sme_answer,
)
from .ingest import normalize_input
from .pages import PageStore
from .store import BPDocIndex, BPSourcesInventory, DocIndexEntry, default_db_path

log = get_logger("bp.service")

SERVICE_NAME = "bp_service"

# How input source URIs map to BP page URIs. Production uses the full
# ``documentation/bp/...`` path inside the docs repo; for the POC we
# keep a flat ``bp/products/<slug>.md`` layout under ``pages_root``.
DEFAULT_PAGE_PREFIX = "bp/products/"


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

@dataclass
class RefreshOutcome:
    page_uri: str
    chunks_indexed: int
    chunking_strategy: str | None
    embedding_revision: str | None
    referenced_services: list[str]
    open_placeholders: list[str]
    escalations: list[dict[str, Any]]
    skipped: bool = False
    skip_reason: str | None = None
    # ``wrote=True`` when the enriched page was actually committed to
    # the page store (i.e. ``write_page`` ran). ``wrote=False`` covers
    # both the early skip-unchanged path AND the post-merge
    # "no enrichment changes" path where the merged content hashed to
    # the same value as the existing page. Lets ``dispatch_refresh``
    # surface a ``wrote/skipped_write`` split on the response.
    wrote: bool = False


# ---------------------------------------------------------------------------
# BPService
# ---------------------------------------------------------------------------

class BPService:
    """In-process B&P specialist."""

    def __init__(
        self,
        *,
        page_store: PageStore,
        rag: RAGClient,
        sd: SDClient,
        doc_index: BPDocIndex | None = None,
        sources_inventory: BPSourcesInventory | None = None,
        otel: OTelClient | None = None,
        page_prefix: str = DEFAULT_PAGE_PREFIX,
    ):
        self._pages = page_store
        self._rag = rag
        self._sd = sd
        db = default_db_path()
        self._doc_index = doc_index or BPDocIndex(db)
        self._inv = sources_inventory or BPSourcesInventory(db)
        self._otel = otel or OTelClient.from_env()
        self._page_prefix = page_prefix.rstrip("/") + "/"

    # ------------------------------------------------------------------ utils

    def _page_uri_for(self, source_uri: str) -> str:
        """Map an input source URI to the BP page URI we generate.

        Strips a leading ``inputs/`` if present and any extension, then
        slugifies the rest. ``inputs/business-cases/catalog-discovery.md``
        becomes ``bp/products/catalog-discovery.md``."""
        stem = source_uri
        if stem.startswith("inputs/"):
            stem = stem[len("inputs/"):]
        stem = Path(stem).stem
        slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "untitled"
        return f"{self._page_prefix}{slug}.md"

    # --------------------------------------------------------- dispatch_query

    def dispatch_query(
        self,
        *,
        query: str,
        domain_hint: str = "bp",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Query-mode entry point (§9.3.2). Never escalates to an SME."""
        if not query or not query.strip():
            log.error("dispatch_query: empty query")
            raise ValueError("dispatch_query: query is required")
        # Normalise and cap the domain_filter the spec allows.
        domain_filter = domain_hint if domain_hint in {"bp", "sd", "both"} else "bp"
        log.info(f"dispatch_query domain_hint={domain_hint} -> domain_filter={domain_filter}")

        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="dispatch_query",
            mcp_domain=domain_filter,
        ) as span:
            span.set_attribute("query_chars", len(query))
            rag_response = self._rag.retrieve(
                query=query,
                domain_filter=domain_filter,
                mode="query",
            )
            status = rag_response.get("status", "exhausted")
            answer = rag_response.get("answer")
            sources = rag_response.get("sources") or []

            # Inline cross-reference resolution at answer time (§9.3.3:
            # "the same resolve_sd_links node runs to resolve the reference
            # at answer time, so the user sees an up-to-date link even if
            # the persisted page is briefly stale").
            decorated_answer = answer
            cross_refs: list[dict[str, Any]] = []
            if answer:
                candidates = extract_service_candidates(answer, fallback_only=True)
                if candidates:
                    refs = resolve_sd_links(
                        product_id=(context or {}).get("product_id") if context else None,
                        candidates=candidates,
                        sd=self._sd,
                    )
                    decorated_answer = _inline_service_links(answer, refs)
                    cross_refs = [
                        {
                            "candidate": r.candidate,
                            "resolved": r.resolved,
                            "page_uri": r.page_uri,
                            "note": r.note,
                        }
                        for r in refs
                    ]

            span.set_status(status)
            span.set_payload_summary({
                "status": status,
                "sources_count": len(sources),
                "cross_refs": len(cross_refs),
            })
            log.info(
                f"dispatch_query done status={status} sources={len(sources)} "
                f"cross_refs={len(cross_refs)}"
            )
            return {
                "status": status,
                "answer": decorated_answer,
                "sources": sources,
                "retrieval_trail": rag_response.get("retrieval_trail") or [],
                "cross_references": cross_refs,
            }

    # ------------------------------------------------------- dispatch_refresh

    def dispatch_refresh(self, *, event: dict[str, Any]) -> dict[str, Any]:
        """Background-mode entry point (§9.3.2 — enrich-existing flow).

        ``event`` matches the orchestrator's REST envelope shape:
        ``{event_type, doc_id_or_commit_sha, change_kind, source, force}``.

        Iteration target is the set of *existing* BP pages found under
        the page store's pages prefix (``documentation/bp/`` by default).
        Each page is read, gap-detected via the LLM, filled from RAG
        (with SME-PLACEHOLDER fallback for low-confidence retrieval),
        merged back, and re-indexed.

        ``doc_id_or_commit_sha``:
          * empty → enrich every existing page **and** discover new ones
            from SD's doc-index (products referenced by SD pages but
            without a BP page yet).
          * non-empty → treat as a single page URI, enrich just that one.
        """
        change_kind = (event or {}).get("change_kind", "modified")
        single_page = (event or {}).get("doc_id_or_commit_sha") or ""
        force = bool((event or {}).get("force"))

        if single_page:
            pages = [single_page]
        else:
            pages = self._pages.list_pages()
            # Filter to BP-rooted pages only — the page-store now
            # surfaces the entire ``documentation/`` tree (so pages
            # from other domains and the shared
            # ``documentation/sme-responses/`` archive show up too).
            # We only enrich what we own.
            pages = [p for p in pages if p and p.startswith("bp/")]

        log.info(
            f"dispatch_refresh start change_kind={change_kind} "
            f"pages={len(pages)} single={single_page!r} force={force}"
        )

        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="dispatch_refresh",
        ) as span:
            span.set_attribute("change_kind", change_kind)
            span.set_attribute("page_count", len(pages))
            span.set_attribute("force", force)
            outcomes: list[RefreshOutcome] = []

            # 1) Enrich every existing page.
            for page_uri in pages:
                try:
                    outcomes.append(
                        self._refresh_one(
                            page_uri=page_uri,
                            change_kind=change_kind,
                            force=force,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 — per-page isolation
                    details = format_exception(exc)
                    log.error(f"dispatch_refresh: {page_uri} failed: {details}")
                    outcomes.append(RefreshOutcome(
                        page_uri=page_uri,
                        chunks_indexed=0,
                        chunking_strategy=None,
                        embedding_revision=None,
                        referenced_services=[],
                        open_placeholders=[],
                        escalations=[{"error": details}],
                        skipped=True,
                        skip_reason="refresh_error",
                    ))

            # 2) New-page discovery — only on a full refresh.
            if not single_page:
                discovered = self._discover_new_pages(known_page_uris=set(pages), force=force)
                for outcome in discovered:
                    outcomes.append(outcome)
                if discovered:
                    log.info(
                        f"dispatch_refresh: stubbed {len(discovered)} new BP page(s)"
                    )

            affected_pages = [o.page_uri for o in outcomes if not o.skipped]
            escalations = [e for o in outcomes for e in o.escalations]
            span.set_status("ok")
            span.set_payload_summary({
                "pages_seen": len(pages),
                "affected_pages": len(affected_pages),
                "escalations": len(escalations),
                "discovered": sum(1 for o in outcomes if (o.skip_reason or "") == "stub_created"),
            })
            log.info(
                f"dispatch_refresh done affected_pages={len(affected_pages)} "
                f"escalations={len(escalations)} "
                f"skipped={sum(1 for o in outcomes if o.skipped)}"
            )
            # Tag the head of the docs branch so each refresh has a
            # rollback marker. Best-effort — tag failure must NOT fail
            # the refresh (pages have already been written).
            self._tag_after_refresh(affected_pages_count=len(affected_pages))
            return {
                "affected_pages": affected_pages,
                "escalations": escalations,
                "details": [
                    {
                        "page_uri": o.page_uri,
                        "skipped": o.skipped,
                        "skip_reason": o.skip_reason,
                        "chunks_indexed": o.chunks_indexed,
                        "chunking_strategy": o.chunking_strategy,
                        "embedding_revision": o.embedding_revision,
                        "referenced_services": o.referenced_services,
                        "open_placeholders": o.open_placeholders,
                    }
                    for o in outcomes
                ],
            }

    # ------------------------------------------------------------------ enrich

    def _refresh_one(self, *, page_uri: str, change_kind: str, force: bool = False) -> RefreshOutcome:
        """Read existing BP page → detect gaps → fill via RAG / SME →
        merge back → re-index. The caller is responsible for catching
        per-page failures so one bad page doesn't kill the batch.

        Wraps the work in an OTel ``enrich_page`` span so the Dashboard
        can surface per-page outcomes (``enriched`` / ``unchanged`` /
        ``escalated_only`` / ``stub_created`` / ``error``).
        """
        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="enrich_page",
        ) as span:
            span.set_attribute("page_uri", page_uri)
            span.set_attribute("force", force)

            existing = self._pages.read_page(page_uri)
            if existing is None:
                log.warn(f"_refresh_one: {page_uri!r} not found on disk; treating as deletion")
                self._doc_index.delete(page_uri)
                try:
                    self._rag.delete(domain="bp", source_uri=page_uri)
                except Exception as exc:  # noqa: BLE001
                    log.error(f"_refresh_one: rag.delete({page_uri!r}) failed: {exc}")
                span.set_status("page_deleted")
                return RefreshOutcome(
                    page_uri=page_uri,
                    chunks_indexed=0,
                    chunking_strategy=None,
                    embedding_revision=None,
                    referenced_services=[],
                    open_placeholders=[],
                    escalations=[],
                    skipped=True,
                    skip_reason="page_not_found",
                )

            prior = self._doc_index.get(page_uri)
            page_hash = _hash_text(existing)

            # Skip-unchanged: page content hash matches AND side-info hash
            # matches. ``force=True`` bypasses both. The side-info hash is
            # computed fresh below so the caller can detect "page unchanged
            # but SD doc-index changed".
            sd_summary = self._collect_sd_summary()
            sd_revision = side_info_revision(sd_summary)
            if (
                not force
                and prior is not None
                and prior.content_hash == page_hash
                and (prior.side_info_revision or "") == sd_revision
            ):
                log.info(
                    f"_refresh_one: {page_uri} unchanged (page+side-info); skipping"
                )
                span.set_status("unchanged")
                span.set_attribute("gap_count", 0)
                return RefreshOutcome(
                    page_uri=page_uri,
                    chunks_indexed=0,
                    chunking_strategy=prior.chunking_strategy,
                    embedding_revision=prior.embedding_revision,
                    referenced_services=prior.referenced_services,
                    open_placeholders=prior.open_placeholders,
                    escalations=[],
                    skipped=True,
                    skip_reason="unchanged",
                )

            title = _title_from_content(existing) or _stem_title(page_uri)

            # Preserve any SME-answered prose carried by the previous page
            # body so re-enrichment doesn't overwrite human-authored answers.
            answered_blocks = extract_answered_sme_blocks(existing)
            if prior and prior.answered_sme_blocks:
                # Union of disk-scraped and doc-index-tracked answers.
                for qid, info in prior.answered_sme_blocks.items():
                    answered_blocks.setdefault(qid, info)

            # Detect gaps via the LLM judge — classifies the page kind
            # (product / business-case / flow / strategy / other) using
            # the URI as a hint plus the SD MCP cross-reference summary
            # as content, then proposes per-kind sections tailored to
            # whether the page is even supposed to talk about
            # integrations / use cases / etc.
            llm = self._get_chat_llm()
            gap_plan = detect_gaps(
                existing,
                page_uri=page_uri,
                page_title=title,
                sd_summary=sd_summary or None,
                llm=llm,
            )
            log.info(
                f"_refresh_one: {page_uri} kind={gap_plan.page_kind} "
                f"→ {len(gap_plan.gaps)} gap(s); "
                f"answered={len(answered_blocks)} force={force}"
            )
            span.set_attribute("gap_count", len(gap_plan.gaps))
            span.set_attribute("answered_block_count", len(answered_blocks))
            span.set_attribute("is_substantive", gap_plan.is_substantive)
            span.set_attribute("page_kind", gap_plan.page_kind)

            # Fill each gap. The SD summary doubles as side-info for the
            # ``sd-mcp``-strategy gaps (compose directly from the SD
            # cross-reference rather than RAG).
            filled = []
            escalations: list[PageEscalation] = []
            for gap in gap_plan.gaps:
                fg = fill_gap(
                    gap,
                    page_uri=page_uri,
                    page_title=title,
                    rag=self._rag,
                    sd_summary=sd_summary or None,
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
            # Per-strategy split — useful for triaging "are we hitting
            # the SD-MCP path enough?". ``sd_intended`` counts gaps the
            # detector tagged source-fillable; ``rag_intended`` is the
            # rest. The fall-through case (SD path failed → RAG)
            # increments both is fine for a POC dashboard.
            sd_intended = sum(1 for g in gap_plan.gaps if g.fill_strategy == "sd-mcp")
            rag_intended = sum(1 for g in gap_plan.gaps if g.fill_strategy != "sd-mcp")
            span.set_attribute("filled_count", filled_substantive)
            span.set_attribute("escalated_count", len(escalations))
            span.set_attribute("sd_intended_count", sd_intended)
            span.set_attribute("rag_intended_count", rag_intended)

            if filled:
                sections = [(fg.gap.section_title, fg.section_md) for fg in filled]
                new_content = merge_into_existing(
                    existing_content=existing,
                    sections=sections,
                )
            else:
                new_content = existing

            # Cross-reference resolution from the merged content.
            candidates = extract_service_candidates(new_content)
            product_id = _slug(title)
            refs = resolve_sd_links(product_id=product_id, candidates=candidates, sd=self._sd)

            # Only write if we actually changed something.
            new_hash = _hash_text(new_content)
            if new_hash == page_hash and not force:
                log.info(f"_refresh_one: {page_uri} no enrichment changes; skipping write")
                commit_sha = (prior.metadata.get("commit_sha") if prior else None) or ""
            else:
                commit_sha = self._pages.write_page(page_uri, new_content)
                log.info(
                    f"_refresh_one: wrote {page_uri} commit={commit_sha} "
                    f"refs={len(refs)} placeholders={len(escalations)}"
                )

            # Re-index in RAG against the (possibly enriched) page.
            rag_index_result: dict[str, Any] = {}
            try:
                rag_index_result = self._rag.index(
                    domain="bp",
                    source_uri=page_uri,
                    document=new_content,
                    content_hash=new_hash,
                )
            except Exception as exc:  # noqa: BLE001
                log.error(f"_refresh_one: rag.index({page_uri!r}) failed: {exc}")

            chunks_indexed = int(rag_index_result.get("chunks_indexed", 0))
            last_updated = time.time()

            # Persist sources inventory + doc index. Sources inventory now
            # tracks the BP page itself rather than a separate input doc.
            self._inv.upsert(page_uri, new_hash, metadata={"page_uri": page_uri})
            open_placeholder_ids = [esc.question_id for esc in escalations]
            self._doc_index.upsert(DocIndexEntry(
                page_uri=page_uri,
                title=title,
                last_updated=last_updated,
                source_documents=[page_uri],
                content_hash=new_hash,
                chunking_strategy=rag_index_result.get("chunking_strategy"),
                embedding_revision=rag_index_result.get("embedding_revision"),
                open_placeholders=open_placeholder_ids,
                referenced_services=_merged_referenced_services(
                    resolved_refs=refs, page_content=new_content, page_uri=page_uri,
                ),
                side_info_revision=sd_revision,
                answered_sme_blocks=answered_blocks,
                metadata={"commit_sha": commit_sha, "change_kind": change_kind},
            ))

            # Span status: "enriched" if we filled anything substantive,
            # "escalated_only" if every gap got pushed to SME,
            # "unchanged" if we found nothing to do.
            if filled_substantive > 0:
                span.set_status("enriched")
            elif escalations:
                span.set_status("escalated_only")
            else:
                span.set_status("unchanged")

            return RefreshOutcome(
                page_uri=page_uri,
                chunks_indexed=chunks_indexed,
                chunking_strategy=rag_index_result.get("chunking_strategy"),
                embedding_revision=rag_index_result.get("embedding_revision"),
                referenced_services=_merged_referenced_services(
                    resolved_refs=refs, page_content=new_content, page_uri=page_uri,
                ),
                open_placeholders=open_placeholder_ids,
                escalations=[_envelope(esc) for esc in escalations],
            )

    # --------------------------------------------------- new-page discovery

    def _discover_new_pages(
        self, *, known_page_uris: set[str], force: bool
    ) -> list[RefreshOutcome]:
        """Walk SD's doc-index for products referenced by SD pages that
        don't yet have a BP page. For each missing product, write a
        minimal stub page so the next refresh can enrich it normally.

        Returns one RefreshOutcome per stubbed page (with
        ``skip_reason='stub_created'`` so the caller can count it).
        """
        sd_summary = self._collect_sd_summary()
        if not sd_summary:
            return []

        # Build the set of product slugs SD already references.
        referenced: set[str] = set()
        for entry in sd_summary.get("pages", []):
            for prod in entry.get("referenced_products") or []:
                slug = _slug(prod)
                if slug:
                    referenced.add(slug)

        out: list[RefreshOutcome] = []
        for slug in sorted(referenced):
            stub_page_uri = f"{self._page_prefix}{slug}.md"
            if stub_page_uri in known_page_uris:
                continue
            if self._doc_index.get(stub_page_uri) is not None and not force:
                continue
            # Wrap the stub write in an ``enrich_page`` span with
            # ``status=stub_created`` so the Dashboard's
            # "new pages stubbed" KPI has a row to count. Without a
            # span emission here the status histogram never sees
            # ``stub_created`` no matter how many stubs we create.
            with self._otel.span(
                service=SERVICE_NAME,
                mcp_method="enrich_page",
                mcp_domain="bp",
                attributes={"page_uri": stub_page_uri, "page_kind": "stub"},
            ) as stub_span:
                try:
                    self._pages.write_page(stub_page_uri, _stub_page_md(slug))
                    log.info(f"_discover_new_pages: stubbed {stub_page_uri}")
                    stub_span.set_status("stub_created")
                    out.append(RefreshOutcome(
                        page_uri=stub_page_uri,
                        chunks_indexed=0,
                        chunking_strategy=None,
                        embedding_revision=None,
                        referenced_services=[],
                        open_placeholders=[],
                        escalations=[],
                        skipped=True,
                        skip_reason="stub_created",
                    ))
                except Exception as exc:  # noqa: BLE001
                    stub_span.set_status("error")
                    log.error(f"_discover_new_pages: stub {stub_page_uri} failed: {exc}")
        return out

    def _collect_sd_summary(self) -> dict[str, Any]:
        """Pull the SD doc-index summary used as side-info during
        enrichment + new-page discovery. Tolerant of older SD clients
        that don't expose ``list_pages``: returns ``{}`` on any error."""
        try:
            pages = self._sd.list_pages() if hasattr(self._sd, "list_pages") else []
        except Exception as exc:  # noqa: BLE001
            log.warn(f"_collect_sd_summary: sd.list_pages failed: {exc}")
            return {}
        return {"pages": pages or []}

    def _resolve_existing_page(self, page_uri: str) -> tuple[str | None, str]:
        """Read a page, trying alternate URI shapes if the primary
        lookup misses. Returns ``(content, resolved_uri)`` where
        ``content`` is None if no variant exists.

        Why try alternates: the queue's ``originating_pages`` may
        carry URIs from before the ``pages_prefix`` env-var fix.
        Pre-fix the page-store joined ``documentation/bp`` with
        ``bp/products/foo.md`` and produced
        ``documentation/bp/bp/products/foo.md`` — so older
        escalations have the URI that maps to that doubled path.
        Post-fix the page-store joins ``documentation`` with
        ``bp/products/foo.md`` for ``documentation/bp/products/foo.md``.
        Trying ``products/foo.md`` (strip the leading ``bp/``) and
        the leading ``documentation/`` strip both round-trip an old
        URI to whatever shape the page-store expects today.
        """
        candidates = [page_uri]
        # Strip leading ``documentation/`` if the caller passed a
        # repo-rooted path.
        if page_uri.startswith("documentation/"):
            candidates.append(page_uri[len("documentation/"):])
        # Try without the leading domain segment, in case the URI
        # was built when ``pages_prefix`` already supplied it.
        for prefix in ("bp/", "sd/"):
            if page_uri.startswith(prefix):
                candidates.append(page_uri[len(prefix):])
        # And the inverse: caller passed a path relative to the old
        # per-domain ``pages_prefix`` and we need to add ``bp/`` back.
        if not page_uri.startswith(("bp/", "sd/", "documentation/")):
            candidates.append(f"bp/{page_uri}")
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

    def _get_chat_llm(self):
        """Lazy LLM instance for enrichment. Cached on the service so
        the rubric prompt fits the same warm Ollama context across
        every page in a refresh batch."""
        if not hasattr(self, "_chat_llm") or self._chat_llm is None:
            self._chat_llm = get_chat_llm(
                module="bp.enrich",
                temperature=0.2,
                json_mode=True,
            )
        return self._chat_llm

    def _tag_after_refresh(self, *, affected_pages_count: int) -> None:
        """Mark a successful refresh in Git so each run has a rollback
        anchor. Tag shape: ``bp-refresh-<UTC timestamp>``. Skipped
        when no pages were written; silent on tag failure (the pages
        already landed, a tag failure must never fail the dispatch)."""
        if affected_pages_count <= 0:
            return
        ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        tag = f"bp-refresh-{ts}"
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

    # -------------------------------------------- find_products_for_service

    def find_products_for_service(self, service_id: str) -> list[dict[str, Any]]:
        """Pure relational lookup over the BP doc index (§9.3.2)."""
        if not service_id:
            log.error("find_products_for_service: missing service_id")
            raise ValueError("service_id is required")
        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="find_products_for_service",
        ) as span:
            entries = self._doc_index.find_pages_referencing(service_id)
            out = [
                {
                    "product": entry.title or entry.page_uri,
                    "page_uri": entry.page_uri,
                    "role": "owner-or-consumer",
                    "last_updated": entry.last_updated,
                }
                for entry in entries
            ]
            span.set_status("ok")
            span.set_payload_summary({"service_id": service_id, "matches": len(out)})
            log.info(f"find_products_for_service service={service_id} matches={len(out)}")
            return out

    # ----------------------------------------------------------- get_page

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
        """Replace the fenced placeholder block + re-index the page (§9.3.2)."""
        if not (page_uri and question_id and replacement):
            log.error("patch_page: page_uri, question_id, replacement are all required")
            raise ValueError("patch_page: page_uri, question_id, replacement are all required")
        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="patch_page",
        ) as span:
            # Some queue entries carry URIs from before the
            # ``pages_prefix`` fix (and from environments where the
            # value was ``documentation/bp`` rather than
            # ``documentation``). Try the queued URI first, and if it
            # 404s try a couple of alternate shapes so historical
            # escalations still patch cleanly:
            #   * ``documentation/bp/<rest>`` — caller passed a full
            #     repo-rooted path; strip the doc root.
            #   * ``products/foo.md`` (no ``bp/`` prefix) — caller
            #     passed a URI relative to the old per-domain
            #     ``pages_prefix=documentation/bp``.
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
            # (``_section_body_is_fillable`` returns False, the
            # merger's safety check refuses to overwrite). This is
            # what "trim the whole SME section" looks like in
            # practice: nothing in the page hints at the original
            # placeholder anymore.
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

            # Re-index the patched page so the fresh content shows up in
            # subsequent retrievals (§9.3.2 patch_page semantic).
            try:
                rag_res = self._rag.index(
                    domain="bp",
                    source_uri=page_uri,
                    document=new_content,
                )
                # Refresh embedding_revision in the doc index AND
                # persist the SME-answered prose so subsequent
                # refreshes can match it from `answered_sme_blocks`
                # even if the in-page fence detection misfires.
                entry = self._doc_index.get(page_uri)
                if entry is not None:
                    entry.embedding_revision = rag_res.get("embedding_revision")
                    entry.chunking_strategy = rag_res.get("chunking_strategy") or entry.chunking_strategy
                    entry.last_updated = time.time()
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
        """Persist an SME reply as a brand-new BP page (§9.3.2)."""
        if not (question_id and sme_text):
            log.error("ingest_sme_doc: question_id and sme_text are required")
            raise ValueError("ingest_sme_doc: question_id and sme_text are required")
        # SME replies live in a shared ``documentation/sme-responses/``
        # tree (no domain prefix) so neither BP's nor SD's
        # ``dispatch_refresh`` iterates them as enrichable pages
        # — see the per-service ``list_pages`` filter that drops
        # anything outside the domain root.
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
                    domain="bp",
                    source_uri=page_uri,
                    document=content,
                )
            except Exception as exc:  # noqa: BLE001
                log.error(f"ingest_sme_doc: rag.index({page_uri!r}) failed: {exc}")
                rag_res = {"chunking_strategy": None, "embedding_revision": None, "chunks_indexed": 0}

            self._doc_index.upsert(DocIndexEntry(
                page_uri=page_uri,
                title=f"SME reply: {question_id}",
                last_updated=now,
                source_documents=originating_pages or [],
                content_hash=normalize_input(source_uri=page_uri, raw_text=content).content_hash,
                chunking_strategy=rag_res.get("chunking_strategy"),
                embedding_revision=rag_res.get("embedding_revision"),
                open_placeholders=[],
                referenced_services=[],
                metadata={"sme_question_id": question_id, "commit_sha": commit_sha},
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
    def doc_index(self) -> BPDocIndex:
        return self._doc_index

    @property
    def sources_inventory(self) -> BPSourcesInventory:
        return self._inv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "untitled"


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


def _stem_title(page_uri: str) -> str:
    """Fallback title derived from the page filename when the page has
    no ``# Heading`` (or hasn't been written yet)."""
    stem = Path(page_uri).stem
    return stem.replace("-", " ").replace("_", " ").title() or "Untitled"


def _stub_page_md(slug: str) -> str:
    """Minimal stub for a newly-discovered BP page. The next refresh
    runs the full enrichment pass against this skeleton."""
    title = slug.replace("-", " ").replace("_", " ").title()
    return (
        f"# {title}\n\n"
        f"<!-- agent: stub page created from SD reference; "
        f"will be filled on the next refresh -->\n\n"
        f"## Overview\n\n"
        f"TBD — describe what this product is and the problem it solves.\n"
    )


def _envelope(esc: PageEscalation) -> dict[str, Any]:
    """Pack a :class:`PageEscalation` as the orchestrator's escalation envelope."""
    return {
        "question_id": esc.question_id,
        "placeholder_id": esc.placeholder_id,
        "topic": esc.topic,
        "question": esc.question,
        "best_guess": esc.best_guess,
        "originating_page": esc.page_uri,
    }


def _inline_service_links(answer: str, refs) -> str:
    """Replace bare ``service-name`` mentions in the answer with relative
    Markdown links to the resolved SD page. Keeps the rest of the answer
    untouched. Idempotent for already-linked references."""
    out = answer
    for r in refs:
        if not r.resolved or not r.page_uri:
            continue
        # Only replace bare occurrences (no existing link wrapping the token).
        # Backquoted form (`name`) and bare form both get linked.
        pattern = re.compile(rf"(?<!\]\(\.\./\.\./)\b{re.escape(r.candidate)}\b(?![^\[]*\]\()", re.IGNORECASE)
        replacement = f"[{r.candidate}](../../{r.page_uri})"
        out = pattern.sub(replacement, out, count=1)
    return out


def _merged_referenced_services(
    *,
    resolved_refs,
    page_content: str,
    page_uri: str,
) -> list[str]:
    """Union of services resolved through ``resolve_sd_links`` and any
    SD-page URIs scraped directly from the merged page body.

    The compose-time resolver finds candidates by name; the citation
    linker (``shared.citations.link_citations``) emits direct
    ``[S1](path)`` Markdown links, which compose's by-name extractor
    misses. Without this merge, the dashboard's
    ``cross_reference_health`` rollup undercounts citation-driven
    cross-domain links.
    """
    from src.shared.citations import extract_cross_domain_refs

    by_name = {r.candidate for r in (resolved_refs or []) if r.resolved}
    by_link = set(
        extract_cross_domain_refs(page_content, my_domain="bp", page_uri=page_uri)
    )
    return sorted(by_name | by_link)
