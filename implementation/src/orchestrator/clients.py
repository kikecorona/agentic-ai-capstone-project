"""Client adapters the Orchestrator uses to reach BP and SD specialists.

§9.4 routes work through ``BP_MCP`` / ``SD_MCP``; for the in-process POC
we keep the contract narrow and ship an in-process adapter that calls
straight into the BPService instance the Orchestrator owns. When SD
lands and we wire BP/SD/Orchestrator into separate processes, swapping
in an MCP-backed client is a constructor change only.

For the POC we also ship a :class:`StubSDClient` so the Orchestrator
exposes a coherent SD-aware contract from day one — the actual SD
specialist replaces the stub when it ships.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# BPClient — the slice of BP_MCP the Orchestrator calls
# ---------------------------------------------------------------------------

@runtime_checkable
class BPClient(Protocol):
    def dispatch_query(
        self,
        *,
        query: str,
        domain_hint: str = "bp",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def dispatch_refresh(self, *, event: dict[str, Any]) -> dict[str, Any]: ...

    def get_page(self, page_uri: str) -> dict[str, Any]: ...

    def patch_page(self, *, page_uri: str, question_id: str, replacement: str) -> dict[str, Any]: ...

    def ingest_sme_doc(
        self,
        *,
        question_id: str,
        sme_text: str,
        originating_pages: list[str] | None = None,
        topic: str | None = None,
        question: str | None = None,
    ) -> dict[str, Any]: ...


class InProcessBPClient:
    """Adapter over an in-memory :class:`src.bp_service.service.BPService`."""

    def __init__(self, bp_service):
        self._bp = bp_service

    def dispatch_query(
        self,
        *,
        query: str,
        domain_hint: str = "bp",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._bp.dispatch_query(query=query, domain_hint=domain_hint, context=context)

    def dispatch_refresh(self, *, event: dict[str, Any]) -> dict[str, Any]:
        return self._bp.dispatch_refresh(event=event)

    def get_page(self, page_uri: str) -> dict[str, Any]:
        return self._bp.get_page(page_uri)

    def patch_page(self, *, page_uri: str, question_id: str, replacement: str) -> dict[str, Any]:
        return self._bp.patch_page(
            page_uri=page_uri, question_id=question_id, replacement=replacement
        )

    def ingest_sme_doc(
        self,
        *,
        question_id: str,
        sme_text: str,
        originating_pages: list[str] | None = None,
        topic: str | None = None,
        question: str | None = None,
    ) -> dict[str, Any]:
        return self._bp.ingest_sme_doc(
            question_id=question_id,
            sme_text=sme_text,
            originating_pages=originating_pages,
            topic=topic,
            question=question,
        )


# ---------------------------------------------------------------------------
# SDClient — placeholder until the SD specialist ships
# ---------------------------------------------------------------------------

@runtime_checkable
class SDClient(Protocol):
    """The slice of SD_MCP the Orchestrator routes work to.

    Mirrors the §9.2.2 method shape so swapping in the real SD specialist
    is a wiring change only.
    """

    def dispatch_query(
        self,
        *,
        query: str,
        domain_hint: str = "sd",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def dispatch_refresh(self, *, event: dict[str, Any]) -> dict[str, Any]: ...

    def get_page(self, page_uri: str) -> dict[str, Any]: ...

    def patch_page(self, *, page_uri: str, question_id: str, replacement: str) -> dict[str, Any]: ...

    def ingest_sme_doc(
        self,
        *,
        question_id: str,
        sme_text: str,
        originating_pages: list[str] | None = None,
        topic: str | None = None,
        question: str | None = None,
    ) -> dict[str, Any]: ...


class StubSDClient:
    """Returns ``status='exhausted'`` for any SD work until the real SD
    specialist lands. Surface is deliberately complete so the Orchestrator
    plumbing can be exercised end-to-end without a fake-SD process."""

    def dispatch_query(
        self,
        *,
        query: str,
        domain_hint: str = "sd",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "exhausted",
            "answer": None,
            "sources": [],
            "retrieval_trail": [{"step": "stub", "note": "SD specialist not implemented yet"}],
            "cross_references": [],
        }

    def dispatch_refresh(self, *, event: dict[str, Any]) -> dict[str, Any]:
        return {"affected_pages": [], "escalations": [], "details": []}

    def get_page(self, page_uri: str) -> dict[str, Any]:
        return {"content": None, "doc_index_entry": None}

    def patch_page(self, *, page_uri: str, question_id: str, replacement: str) -> dict[str, Any]:
        return {"commit_sha": None, "patched": False}

    def ingest_sme_doc(
        self,
        *,
        question_id: str,
        sme_text: str,
        originating_pages: list[str] | None = None,
        topic: str | None = None,
        question: str | None = None,
    ) -> dict[str, Any]:
        return {"new_page_uri": None, "embedding_revision": None, "commit_sha": None}
