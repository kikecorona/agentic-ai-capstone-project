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
from src.shared.otel_client import OTelClient
from src.shared.service_log import get_logger

from .analyze_code import ServiceAnalysis, analyze_service
from .clients import BPClient, RAGClient
from .compose import (
    PageEscalation,
    compose_page,
    replace_placeholder_block,
)
from .sources import SourceStore
from .store import (
    SDDocIndex,
    SDDocIndexEntry,
    SDSourcesInventory,
    default_db_path,
)
from .tot_dep_graph import select_dep_graph

log = get_logger("rag.sd.service")

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
        """Background-mode entry (§9.2.2)."""
        change_kind = (event or {}).get("change_kind", "modified")
        single = (event or {}).get("doc_id_or_commit_sha") or ""
        # Operator override — bypass `sources_inventory.is_unchanged()`
        # so a manual refresh from the portal can re-run the pipeline
        # against a fully-indexed corpus. See orchestrator
        # ``RefreshRequest.force``.
        force = bool((event or {}).get("force"))

        if single:
            services = [_service_from_event(single)]
            services = [s for s in services if s]
        else:
            services = self._sources.list_services()

        log.info(
            f"dispatch_refresh start change_kind={change_kind} "
            f"services={len(services)} single={single!r} force={force}"
        )

        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="dispatch_refresh",
        ) as span:
            span.set_attribute("change_kind", change_kind)
            span.set_attribute("service_count", len(services))
            span.set_attribute("force", force)
            outcomes: list[RefreshOutcome] = []
            for svc in services:
                try:
                    outcomes.append(
                        self._refresh_one(service=svc, change_kind=change_kind, force=force)
                    )
                except Exception as exc:  # noqa: BLE001 — per-service isolation
                    log.error(f"dispatch_refresh: {svc} failed: {exc}")
                    outcomes.append(RefreshOutcome(
                        page_uri=self._page_uri_for(svc),
                        service=svc,
                        chunks_indexed=0,
                        chunking_strategy=None,
                        embedding_revision=None,
                        downstream_services=[],
                        referenced_products=[],
                        open_placeholders=[],
                        escalations=[{"error": f"{type(exc).__name__}: {exc}"}],
                        skipped=True,
                        skip_reason="refresh_error",
                    ))

            affected_pages = [o.page_uri for o in outcomes if not o.skipped]
            escalations = [e for o in outcomes for e in o.escalations]
            span.set_status("ok")
            span.set_payload_summary({
                "services_seen": len(services),
                "affected_pages": len(affected_pages),
                "escalations": len(escalations),
            })
            log.info(
                f"dispatch_refresh done affected_pages={len(affected_pages)} "
                f"escalations={len(escalations)} "
                f"skipped={sum(1 for o in outcomes if o.skipped)}"
            )
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

    def _refresh_one(self, *, service: str, change_kind: str, force: bool = False) -> RefreshOutcome:
        page_uri = self._page_uri_for(service)
        analysis: ServiceAnalysis = analyze_service(
            service=service, store=self._sources, augment=True
        )

        # Skip unchanged services per §9.2.1 sources-inventory diff.
        # `force=True` bypasses this so manual portal refreshes can
        # re-run end-to-end on an already-indexed corpus.
        if not force and self._inv.is_unchanged(service, analysis.source_revision):
            existing = self._doc_index.get(page_uri)
            if existing is not None:
                log.info(f"_refresh_one: {service} unchanged; skipping")
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
            f"_refresh_one: indexing {service} -> {page_uri} "
            f"revision={analysis.source_revision} "
            f"endpoints={len(analysis.endpoints)} calls={len(analysis.downstream_calls)}"
        )

        # ToT dep graph + BP cross-references.
        tot = select_dep_graph(analysis)
        try:
            related = self._bp.find_products_for_service(service)
        except Exception as exc:  # noqa: BLE001
            log.error(f"_refresh_one: bp.find_products_for_service({service!r}) failed: {exc}")
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
            f"_refresh_one: wrote {page_uri} commit={commit_sha} "
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
            log.error(f"_refresh_one: rag.index({page_uri!r}) failed: {exc}")

        chunks_indexed = int(rag_index_result.get("chunks_indexed", 0))

        self._inv.upsert(service, analysis.source_revision, file_count=len(analysis.files_seen))
        self._doc_index.upsert(SDDocIndexEntry(
            page_uri=page_uri,
            service=service,
            title=service,
            last_updated=last_updated,
            source_revision=analysis.source_revision,
            content_hash=analysis.source_revision,
            chunking_strategy=rag_index_result.get("chunking_strategy"),
            embedding_revision=rag_index_result.get("embedding_revision"),
            open_placeholders=composed.open_placeholders,
            endpoints=composed.endpoints,
            downstream_services=composed.downstream_services,
            referenced_products=composed.referenced_products,
            metadata={"commit_sha": commit_sha, "change_kind": change_kind},
        ))

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
            content = self._pages.read_page(page_uri)
            if content is None:
                span.set_status("not_found")
                log.error(f"patch_page: {page_uri!r} does not exist")
                raise FileNotFoundError(f"page not found: {page_uri}")
            new_content, replaced = replace_placeholder_block(
                content,
                question_id=question_id,
                replacement=replacement,
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
