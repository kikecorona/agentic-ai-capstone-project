"""HTTP-backed MCP client adapters for cross-process service-to-service calls.

When the POC runs as a single in-process stack (the validation scripts'
default), services use ``InProcessXClient`` adapters that call peer
service classes directly. When ``start_all`` brings each service up as
its own process, the same Protocol surfaces are satisfied by these
HTTP-backed wrappers — each one points at a peer's MCP HTTP endpoint
and shells calls through :class:`MCPHttpClient`.

The contract every wrapper honours is the narrow Protocol the *consuming*
service expects (e.g. :class:`src.bp_service.clients.RAGClient`); the
wrappers here are intentionally fat (they expose every tool the upstream
MCP advertises) so the same instance can satisfy multiple consumers
without duplication.
"""

from __future__ import annotations

import os
from typing import Any

from .mcp_http_client import MCPHttpClient


class RAGHttpClient:
    """HTTP wrapper around ``RAG_MCP`` (§9.1.2).

    Satisfies both ``src.bp_service.clients.RAGClient`` and
    ``src.sd_service.clients.RAGClient`` (their Protocols are identical
    aside from the default ``domain_filter`` value, which the caller
    passes explicitly anyway).
    """

    def __init__(self, url: str, *, timeout_s: float = 7200.0):
        self._mcp = MCPHttpClient(url, name="rag", timeout_s=timeout_s)

    def index(
        self,
        *,
        domain: str,
        source_uri: str,
        document: str,
        content_hash: str | None = None,
    ) -> dict[str, Any]:
        return self._mcp.call("index", {
            "domain": domain,
            "source_uri": source_uri,
            "document": document,
            "content_hash": content_hash,
        }) or {}

    def retrieve(
        self,
        *,
        query: str,
        domain_filter: str = "both",
        mode: str = "query",
    ) -> dict[str, Any]:
        return self._mcp.call("retrieve", {
            "query": query,
            "domain_filter": domain_filter,
            "mode": mode,
        }) or {}

    def delete(self, *, domain: str, source_uri: str) -> dict[str, Any]:
        return self._mcp.call("delete", {
            "domain": domain,
            "source_uri": source_uri,
        }) or {}

    def close(self) -> None:
        self._mcp.close()


class BPHttpClient:
    """HTTP wrapper around ``BP_MCP`` (§9.3.2).

    Exposes every BP tool so the same instance can satisfy the
    orchestrator's ``BPClient`` (all six methods) and SD's ``BPClient``
    (just ``find_products_for_service`` + ``get_page``).
    """

    def __init__(self, url: str, *, timeout_s: float = 7200.0):
        self._mcp = MCPHttpClient(url, name="bp", timeout_s=timeout_s)

    def dispatch_query(
        self,
        *,
        query: str,
        domain_hint: str = "bp",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._mcp.call("dispatch_query", {
            "query": query,
            "domain_hint": domain_hint,
            "context": context,
        }) or {}

    def dispatch_refresh(self, *, event: dict[str, Any]) -> dict[str, Any]:
        return self._mcp.call("dispatch_refresh", {"event": event}) or {}

    def find_products_for_service(self, service_id: str) -> list[dict[str, Any]]:
        out = self._mcp.call("find_products_for_service", {"service_id": service_id})
        return out if isinstance(out, list) else []

    def get_page(self, page_uri: str) -> dict[str, Any]:
        return self._mcp.call("get_page", {"page_uri": page_uri}) or {}

    def patch_page(self, *, page_uri: str, question_id: str, replacement: str) -> dict[str, Any]:
        return self._mcp.call("patch_page", {
            "page_uri": page_uri,
            "question_id": question_id,
            "replacement": replacement,
        }) or {}

    def ingest_sme_doc(
        self,
        *,
        question_id: str,
        sme_text: str,
        originating_pages: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._mcp.call("ingest_sme_doc", {
            "question_id": question_id,
            "sme_text": sme_text,
            "originating_pages": originating_pages,
        }) or {}

    def close(self) -> None:
        self._mcp.close()


class SDHttpClient:
    """HTTP wrapper around ``SD_MCP`` (§9.2.2).

    Same shape as :class:`BPHttpClient` minus ``ingest_sme_doc`` (SD
    doesn't own any input docs).
    """

    def __init__(self, url: str, *, timeout_s: float = 7200.0):
        self._mcp = MCPHttpClient(url, name="sd", timeout_s=timeout_s)

    def dispatch_query(
        self,
        *,
        query: str,
        domain_hint: str = "sd",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._mcp.call("dispatch_query", {
            "query": query,
            "domain_hint": domain_hint,
            "context": context,
        }) or {}

    def dispatch_refresh(self, *, event: dict[str, Any]) -> dict[str, Any]:
        return self._mcp.call("dispatch_refresh", {"event": event}) or {}

    def find_services_for_product(self, product_id: str) -> list[dict[str, Any]]:
        out = self._mcp.call("find_services_for_product", {"product_id": product_id})
        return out if isinstance(out, list) else []

    def get_page(self, page_uri: str) -> dict[str, Any]:
        return self._mcp.call("get_page", {"page_uri": page_uri}) or {}

    def list_pages(self) -> list[dict[str, Any]]:
        out = self._mcp.call("list_pages", {})
        return out if isinstance(out, list) else []

    def patch_page(self, *, page_uri: str, question_id: str, replacement: str) -> dict[str, Any]:
        return self._mcp.call("patch_page", {
            "page_uri": page_uri,
            "question_id": question_id,
            "replacement": replacement,
        }) or {}

    def close(self) -> None:
        self._mcp.close()


# ---------------------------------------------------------------------------
# Env-var helpers — every server boot uses the same pair of helpers
# ---------------------------------------------------------------------------

def env_url(*names: str) -> str | None:
    """Return the first non-empty env var among ``names`` (case-sensitive)."""
    for name in names:
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip()
    return None
