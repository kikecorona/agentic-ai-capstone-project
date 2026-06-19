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

from src.shared.otel_client import OTelClient
from src.shared.service_log import get_logger

from .clients import RAGClient, SDClient
from .compose import (
    PageEscalation,
    compose_page,
    extract_service_candidates,
    replace_placeholder_block,
    resolve_sd_links,
)
from .ingest import normalize_input
from .pages import PageStore
from .store import BPDocIndex, BPSourcesInventory, DocIndexEntry, default_db_path

log = get_logger("rag.bp.service")

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
        """Background-mode entry point (§9.3.2).

        ``event`` matches the orchestrator's REST envelope shape:
        ``{event_type, doc_id_or_commit_sha, change_kind, source}``.

        For BP, ``doc_id_or_commit_sha`` carries the input-doc URI for a
        single-doc refresh, or is empty for a full refresh that scans
        every input doc the page store knows about.
        """
        change_kind = (event or {}).get("change_kind", "modified")
        single_doc = (event or {}).get("doc_id_or_commit_sha") or ""
        # Operator override — bypass `sources_inventory.is_unchanged()`
        # so a manual refresh from the portal can re-run the pipeline
        # against a fully-indexed corpus. See orchestrator
        # ``RefreshRequest.force``.
        force = bool((event or {}).get("force"))

        if single_doc:
            sources = [single_doc]
        else:
            sources = self._pages.list_inputs()

        log.info(
            f"dispatch_refresh start change_kind={change_kind} "
            f"sources={len(sources)} single_doc={single_doc!r} force={force}"
        )

        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="dispatch_refresh",
        ) as span:
            span.set_attribute("change_kind", change_kind)
            span.set_attribute("source_count", len(sources))
            span.set_attribute("force", force)
            outcomes: list[RefreshOutcome] = []
            for src in sources:
                try:
                    outcomes.append(
                        self._refresh_one(source_uri=src, change_kind=change_kind, force=force)
                    )
                except Exception as exc:  # noqa: BLE001 — per-doc isolation
                    log.error(f"dispatch_refresh: {src} failed: {exc}")
                    outcomes.append(RefreshOutcome(
                        page_uri=self._page_uri_for(src),
                        chunks_indexed=0,
                        chunking_strategy=None,
                        embedding_revision=None,
                        referenced_services=[],
                        open_placeholders=[],
                        escalations=[{"error": f"{type(exc).__name__}: {exc}"}],
                        skipped=True,
                        skip_reason="refresh_error",
                    ))
            affected_pages = [o.page_uri for o in outcomes if not o.skipped]
            escalations = [e for o in outcomes for e in o.escalations]
            span.set_status("ok")
            span.set_payload_summary({
                "sources_seen": len(sources),
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

    def _refresh_one(self, *, source_uri: str, change_kind: str, force: bool = False) -> RefreshOutcome:
        """Run the background pipeline for a single input doc."""
        page_uri = self._page_uri_for(source_uri)
        try:
            raw = self._pages.read_input(source_uri)
        except FileNotFoundError:
            log.warn(f"_refresh_one: input {source_uri!r} not found; treating as deletion")
            self._inv.delete(source_uri)
            self._doc_index.delete(page_uri)
            try:
                self._rag.delete(domain="bp", source_uri=page_uri)
            except Exception as exc:  # noqa: BLE001
                log.error(f"_refresh_one: rag.delete({page_uri!r}) failed: {exc}")
            return RefreshOutcome(
                page_uri=page_uri,
                chunks_indexed=0,
                chunking_strategy=None,
                embedding_revision=None,
                referenced_services=[],
                open_placeholders=[],
                escalations=[],
                skipped=True,
                skip_reason="input_not_found",
            )

        norm = normalize_input(source_uri=source_uri, raw_text=raw)

        # Skip-unchanged: §9.3.3 explicitly says we use sources_inventory
        # to skip unchanged files on the next refresh. `force=True`
        # bypasses this so manual portal refreshes can re-run end-to-end
        # on an already-indexed corpus.
        if not force and self._inv.is_unchanged(source_uri, norm.content_hash):
            existing = self._doc_index.get(page_uri)
            if existing is not None:
                log.info(f"_refresh_one: {source_uri} unchanged; skipping")
                return RefreshOutcome(
                    page_uri=page_uri,
                    chunks_indexed=0,
                    chunking_strategy=existing.chunking_strategy,
                    embedding_revision=existing.embedding_revision,
                    referenced_services=existing.referenced_services,
                    open_placeholders=existing.open_placeholders,
                    escalations=[],
                    skipped=True,
                    skip_reason="unchanged",
                )

        log.info(
            f"_refresh_one: indexing {source_uri} -> {page_uri} "
            f"(hash={norm.content_hash[:12]})"
        )
        rag_index_result = self._rag.index(
            domain="bp",
            source_uri=page_uri,
            document=norm.text,
            content_hash=norm.content_hash,
        )
        chunks_indexed = int(rag_index_result.get("chunks_indexed", 0))

        # Cross-reference resolution.
        candidates = extract_service_candidates(norm.text)
        product_id = _slug(norm.title) if norm.title else _slug(Path(source_uri).stem)
        refs = resolve_sd_links(product_id=product_id, candidates=candidates, sd=self._sd)

        last_updated = time.time()
        composed = compose_page(
            page_uri=page_uri,
            title=norm.title,
            source_uri=source_uri,
            body=norm.text,
            references=refs,
            last_updated=last_updated,
            content_hash=norm.content_hash,
        )
        commit_sha = self._pages.write_page(page_uri, composed.content)
        log.info(
            f"_refresh_one: wrote {page_uri} commit={commit_sha} "
            f"refs={len(composed.referenced_services)} "
            f"placeholders={len(composed.open_placeholders)}"
        )

        # Persist sources inventory + doc index.
        self._inv.upsert(source_uri, norm.content_hash, metadata={"page_uri": page_uri})
        self._doc_index.upsert(DocIndexEntry(
            page_uri=page_uri,
            title=composed.title,
            last_updated=last_updated,
            source_documents=[source_uri],
            content_hash=norm.content_hash,
            chunking_strategy=rag_index_result.get("chunking_strategy"),
            embedding_revision=rag_index_result.get("embedding_revision"),
            open_placeholders=composed.open_placeholders,
            referenced_services=composed.referenced_services,
            metadata={"commit_sha": commit_sha, "change_kind": change_kind},
        ))

        return RefreshOutcome(
            page_uri=page_uri,
            chunks_indexed=chunks_indexed,
            chunking_strategy=rag_index_result.get("chunking_strategy"),
            embedding_revision=rag_index_result.get("embedding_revision"),
            referenced_services=composed.referenced_services,
            open_placeholders=composed.open_placeholders,
            escalations=[_envelope(esc) for esc in composed.escalations],
        )

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

            # Re-index the patched page so the fresh content shows up in
            # subsequent retrievals (§9.3.2 patch_page semantic).
            try:
                rag_res = self._rag.index(
                    domain="bp",
                    source_uri=page_uri,
                    document=new_content,
                )
                # Refresh embedding_revision in the doc index.
                entry = self._doc_index.get(page_uri)
                if entry is not None:
                    entry.embedding_revision = rag_res.get("embedding_revision")
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

    # -------------------------------------------------------- ingest_sme_doc

    def ingest_sme_doc(
        self,
        *,
        question_id: str,
        sme_text: str,
        originating_pages: list[str] | None = None,
    ) -> dict[str, Any]:
        """Persist an SME reply as a brand-new BP page (§9.3.2)."""
        if not (question_id and sme_text):
            log.error("ingest_sme_doc: question_id and sme_text are required")
            raise ValueError("ingest_sme_doc: question_id and sme_text are required")
        page_uri = f"{self._page_prefix}sme-replies/{question_id}.md"
        with self._otel.span(
            service=SERVICE_NAME,
            mcp_method="ingest_sme_doc",
        ) as span:
            now = time.time()
            content = (
                f"# SME reply: {question_id}\n\n"
                f"> Persisted on {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))}.\n"
                f"> Originating pages: {', '.join(originating_pages or []) or '(none)'}\n\n"
                f"{sme_text.strip()}\n"
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
