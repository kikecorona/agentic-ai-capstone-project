"""Client adapters B&P uses to reach the RAG Service and the SD specialist.

§8.5 says the POC runs the RAG Service in-process — which is true for the
single-process validation script — but the contract is still
``RAG_MCP``-shaped. Putting a thin Protocol in front of the dependency
means:

  * The validation harness can inject an in-process ``RAGService``
    directly (cheap, no subprocess overhead).
  * Production B&P_MCP servers can swap in a real MCP-backed client
    without B&P logic changing a line.
  * SD doesn't exist yet, so :class:`StubSDClient` is enough to exercise
    the cross-reference resolution path until the real SD lands.

The Protocols below are deliberately narrow — only the methods B&P
actually calls on each peer.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# RAG client — talks to RAG_MCP (§9.1.2)
# ---------------------------------------------------------------------------

@runtime_checkable
class RAGClient(Protocol):
    """The slice of RAG_MCP that B&P uses."""

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
        domain_filter: str = "bp",
        mode: str = "query",
    ) -> dict[str, Any]: ...

    def delete(self, *, domain: str, source_uri: str) -> dict[str, Any]: ...


class InProcessRAGClient:
    """Adapter that forwards to a local :class:`RAGService` instance."""

    def __init__(self, service):
        self._service = service

    def index(
        self,
        *,
        domain: str,
        source_uri: str,
        document: str,
        content_hash: str | None = None,
    ) -> dict[str, Any]:
        return self._service.index(
            domain=domain,
            source_uri=source_uri,
            document=document,
            content_hash=content_hash,
        )

    def retrieve(
        self,
        *,
        query: str,
        domain_filter: str = "bp",
        mode: str = "query",
    ) -> dict[str, Any]:
        return self._service.retrieve(query=query, domain_filter=domain_filter, mode=mode)

    def delete(self, *, domain: str, source_uri: str) -> dict[str, Any]:
        return self._service.delete(domain=domain, source_uri=source_uri)


# ---------------------------------------------------------------------------
# SD client — talks to SD_MCP (§9.2.2)
# ---------------------------------------------------------------------------

@runtime_checkable
class SDClient(Protocol):
    """The slice of SD_MCP B&P uses for cross-references (§9.2.2)."""

    def find_services_for_product(self, product_id: str) -> list[dict[str, Any]]: ...

    def get_page(self, page_uri: str) -> dict[str, Any] | None: ...

    def list_pages(self) -> list[dict[str, Any]]: ...


class StubSDClient:
    """In-memory stand-in until the SD specialist lands.

    Construct with a hand-coded ``mapping`` from product_id (or any
    referent the BP page might mention) to a list of ``{service, page_uri,
    role}`` dicts. ``find_services_for_product`` returns the mapping
    verbatim; ``get_page`` looks up by page_uri across all mappings;
    ``list_pages`` synthesises a doc-index dump out of the same mapping
    so BP's new-page discovery can run end-to-end against the stub.

    Validation scripts use this to exercise B&P's cross-reference path
    without standing up the real SD service.
    """

    def __init__(self, mapping: dict[str, list[dict[str, Any]]] | None = None):
        self._mapping = dict(mapping or {})

    def find_services_for_product(self, product_id: str) -> list[dict[str, Any]]:
        return list(self._mapping.get(product_id, []))

    def get_page(self, page_uri: str) -> dict[str, Any] | None:
        for entries in self._mapping.values():
            for e in entries:
                if e.get("page_uri") == page_uri:
                    return {
                        "page_uri": page_uri,
                        "service": e.get("service"),
                        "role": e.get("role"),
                    }
        return None

    def list_pages(self) -> list[dict[str, Any]]:
        """Synthesise SD doc-index entries from the mapping. Each unique
        ``page_uri`` becomes one entry whose ``referenced_products`` is
        the union of ``product_id`` keys that mention it — mirrors the
        real SD doc-index shape closely enough for BP's
        ``_collect_sd_summary`` + ``_discover_new_pages`` to work."""
        by_page: dict[str, dict[str, Any]] = {}
        for product_id, entries in self._mapping.items():
            for e in entries:
                page_uri = e.get("page_uri")
                if not page_uri:
                    continue
                row = by_page.setdefault(page_uri, {
                    "page_uri": page_uri,
                    "service": e.get("service"),
                    "referenced_products": [],
                    "downstream_services": [],
                    "open_placeholders": [],
                })
                if product_id not in row["referenced_products"]:
                    row["referenced_products"].append(product_id)
        return list(by_page.values())

    # Test/dev helper.
    def add(self, product_id: str, *services: dict[str, Any]) -> None:
        self._mapping.setdefault(product_id, []).extend(services)
