"""Client adapters SD uses to reach RAG and B&P.

Mirrors the BP-side adapters: a narrow Protocol + an in-process
implementation that forwards directly to a local service instance.
``BPClient`` here exposes only the slice of ``BP_MCP`` SD calls (the
relational ``find_products_for_service`` plus ``patch_page`` /
``get_page`` for SME re-integration) — no ``dispatch_query`` / refresh,
since SD never asks BP to drive its own retrieval.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# RAGClient
# ---------------------------------------------------------------------------

@runtime_checkable
class RAGClient(Protocol):
    def index(
        self,
        *,
        domain: str,
        source_uri: str,
        document: str,
        content_hash: str | None = None,
    ) -> dict[str, Any]: ...

    def retrieve(
        self,
        *,
        query: str,
        domain_filter: str = "sd",
        mode: str = "query",
    ) -> dict[str, Any]: ...

    def delete(self, *, domain: str, source_uri: str) -> dict[str, Any]: ...


class InProcessRAGClient:
    def __init__(self, service):
        self._service = service

    def index(self, *, domain, source_uri, document, content_hash=None):
        return self._service.index(
            domain=domain, source_uri=source_uri, document=document, content_hash=content_hash
        )

    def retrieve(self, *, query, domain_filter="sd", mode="query"):
        return self._service.retrieve(query=query, domain_filter=domain_filter, mode=mode)

    def delete(self, *, domain, source_uri):
        return self._service.delete(domain=domain, source_uri=source_uri)


# ---------------------------------------------------------------------------
# BPClient — narrow slice SD needs
# ---------------------------------------------------------------------------

@runtime_checkable
class BPClient(Protocol):
    """Slice of BP_MCP that SD actually calls."""

    def find_products_for_service(self, service_id: str) -> list[dict[str, Any]]: ...

    def get_page(self, page_uri: str) -> dict[str, Any]: ...


class InProcessBPClient:
    def __init__(self, bp_service):
        self._bp = bp_service

    def find_products_for_service(self, service_id: str) -> list[dict[str, Any]]:
        return self._bp.find_products_for_service(service_id)

    def get_page(self, page_uri: str) -> dict[str, Any]:
        return self._bp.get_page(page_uri)


class StubBPClient:
    """In-memory fallback when the real B&P specialist isn't wired in.

    Returns empty results — every BP cross-reference will surface as an
    open placeholder, which is the correct behaviour for a partially
    deployed POC."""

    def find_products_for_service(self, service_id: str) -> list[dict[str, Any]]:
        return []

    def get_page(self, page_uri: str) -> dict[str, Any]:
        return {"content": None, "doc_index_entry": None}
