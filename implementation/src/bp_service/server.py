"""``BP_MCP`` — MCP frontend for the B&P Service (§8.2 / §9.3.2).

For the POC the BP service runs in-process inside the validation harness
and inside the BP_MCP subprocess; this module is the *contract front* —
the same six methods exposed through MCP so the Orchestrator and the
peer SD specialist talk to BP through one stable surface.

Run with::

    python -m src.bp_service.server [--inputs PATH] [--pages PATH]

Defaults pull from environment variables:

  * ``BP_INPUTS_ROOT`` — read-only org input docs root (default ``./data/bp/inputs``).
  * ``BP_PAGES_ROOT``  — generated BP pages root (default ``./data/bp/pages``).
  * ``BP_DB_PATH``     — BP doc index + sources inventory SQLite (default ``./data/bp/state.db``).
  * ``RAG_CHROMA_PATH`` / ``OTEL_DB_PATH`` / ``AUDIT_DB_PATH`` — shared paths.

Cross-references currently resolve through a stub SD client because the
SD specialist hasn't landed yet. The stub honours
``BP_SD_STUB_MAPPING_FILE``, a JSON file with the
``{product_id: [{service, page_uri, role}, ...]}`` shape — useful for
the validation harness and for hand-tuning a demo.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from src.rag_service.service import RAGService
from src.rag_service.store import EmbeddingsStore, default_persist_path as default_chroma_path
from src.shared.llm import get_embedding_function
from src.shared.otel_client import OTelClient
from src.shared.peer_clients import RAGHttpClient, SDHttpClient, env_url
from src.shared.service_log import get_logger

from .clients import InProcessRAGClient, StubSDClient
from .pages import GitHubPageStore, LocalPageStore
from .service import BPService
from .store import BPDocIndex, BPSourcesInventory, default_db_path

log = get_logger("rag.bp.server")


def _load_sd_mapping(path: str | os.PathLike[str] | None) -> dict[str, list[dict[str, Any]]]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        log.warn(f"BP_SD_STUB_MAPPING_FILE={p} not found; using empty SD stub")
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            log.error(f"BP_SD_STUB_MAPPING_FILE {p}: top-level must be a JSON object")
            return {}
        return data
    except Exception as exc:  # noqa: BLE001
        log.error(f"BP_SD_STUB_MAPPING_FILE {p}: parse failed: {exc}")
        return {}


def build_server(
    *,
    inputs_root: str | os.PathLike[str] | None = None,
    pages_root: str | os.PathLike[str] | None = None,
    bp_db_path: str | os.PathLike[str] | None = None,
    chroma_path: str | os.PathLike[str] | None = None,
    sd_stub_mapping: str | os.PathLike[str] | None = None,
    host: str = "127.0.0.1",
    port: int = 8103,
) -> tuple[FastMCP, BPService]:
    """Build the FastMCP server bound to a freshly-constructed
    :class:`BPService`. Factored out for tests.

    When ``RAG_MCP_URL`` is set, the BP service reaches RAG over HTTP
    (the multi-process ``start_all`` deployment) and skips the in-process
    ``RAGService`` entirely. Same for ``SD_MCP_URL`` — present means we
    swap the stub for the real SD specialist."""

    inputs_root = inputs_root or os.environ.get("BP_INPUTS_ROOT", "./data/bp/inputs")
    pages_root = pages_root or os.environ.get("BP_PAGES_ROOT", "./data/bp/pages")
    bp_db_path = bp_db_path or os.environ.get("BP_DB_PATH", str(default_db_path()))
    chroma_path = chroma_path or os.environ.get("RAG_CHROMA_PATH", str(default_chroma_path()))
    sd_stub_mapping = sd_stub_mapping or os.environ.get("BP_SD_STUB_MAPPING_FILE")

    rag_url = env_url("RAG_MCP_URL")
    sd_url = env_url("SD_MCP_URL")
    gh_token = env_url("GITHUB_PERSONAL_ACCESS_TOKEN")
    gh_owner = env_url("GITHUB_OWNER")
    gh_repo = env_url("GITHUB_REPO")
    use_github = bool(gh_token and gh_owner and gh_repo)

    log.info(
        f"BP_MCP server starting page_store={'github' if use_github else 'local'} "
        f"inputs={'github://' + gh_repo if use_github else inputs_root} "
        f"pages={'github://' + gh_repo if use_github else pages_root} "
        f"bp_db={bp_db_path} chroma={chroma_path} "
        f"rag_peer={rag_url or '(in-process)'} "
        f"sd_peer={sd_url or '(stub)'}"
    )

    if use_github:
        from src.shared.github_mcp import GitHubMCPClient
        github = GitHubMCPClient(
            github_token=gh_token,
            owner=gh_owner,
            repo=gh_repo,
            branch=env_url("GITHUB_BRANCH") or "main",
        )
        # BP writes its pages under documentation/bp/. The page URIs
        # the service constructs already start with ``bp/`` (see
        # ``DEFAULT_PAGE_PREFIX = "bp/products/"``), so the
        # page-store's ``pages_prefix`` only contributes the
        # ``documentation`` root — anything more would double the
        # ``bp/`` segment and land pages at
        # ``documentation/bp/bp/products/...`` instead of the
        # intended ``documentation/bp/products/...``.
        page_store = GitHubPageStore(
            github=github,
            inputs_prefix=env_url("BP_INPUTS_GH_PATH") or "documentation",
            pages_prefix=env_url("BP_PAGES_GH_PATH") or "documentation",
        )
    else:
        page_store = LocalPageStore(inputs_root=inputs_root, pages_root=pages_root)

    otel = OTelClient.from_env()

    if rag_url:
        rag_client = RAGHttpClient(rag_url)
    else:
        # Single-process fallback: each BP_MCP subprocess gets its own
        # in-process RAGService sharing the Chroma persist directory.
        rag_store = EmbeddingsStore(chroma_path, embedding_function=get_embedding_function())
        rag_service = RAGService(store=rag_store, otel=otel)
        rag_client = InProcessRAGClient(rag_service)

    if sd_url:
        sd_client = SDHttpClient(sd_url)
    else:
        sd_client = StubSDClient(_load_sd_mapping(sd_stub_mapping))

    doc_index = BPDocIndex(bp_db_path)
    sources_inventory = BPSourcesInventory(bp_db_path)

    bp = BPService(
        page_store=page_store,
        rag=rag_client,
        sd=sd_client,
        doc_index=doc_index,
        sources_inventory=sources_inventory,
        otel=otel,
    )

    mcp = FastMCP(
        name="bp-mcp",
        host=host,
        port=port,
        instructions=(
            "Business & Product specialist (§9.3). dispatch_query for query mode "
            "(never escalates). dispatch_refresh for background mode (returns "
            "affected pages + escalation envelopes for unresolved cross-references). "
            "find_products_for_service for relational SD↔BP lookups. get_page / "
            "patch_page for SME re-integration. ingest_sme_doc to persist a fresh "
            "B&P page from an SME reply."
        ),
    )

    @mcp.tool(
        description=(
            "Query-mode entry. Delegates to RAG_MCP.retrieve(domain_filter, mode=query) "
            "and decorates the answer with relative Markdown links to SD pages "
            "for any service mentioned in the answer. Returns "
            "{status, answer, sources, retrieval_trail, cross_references}."
        ),
    )
    def dispatch_query(
        query: str,
        domain_hint: str = "bp",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return bp.dispatch_query(query=query, domain_hint=domain_hint, context=context)

    @mcp.tool(
        description=(
            "Background-mode entry. Diffs the input doc against the BP sources "
            "inventory, indexes via RAG_MCP.index(domain=bp), composes a BP page, "
            "writes it through the page store, and returns "
            "{affected_pages, escalations, details}."
        ),
    )
    def dispatch_refresh(event: dict[str, Any]) -> dict[str, Any]:
        return bp.dispatch_refresh(event=event)

    @mcp.tool(
        description=(
            "Relational lookup over the BP doc index — returns every product/page "
            "that references the given service. Pure metadata; no LLM, no RAG retrieve."
        ),
    )
    def find_products_for_service(service_id: str) -> list[dict[str, Any]]:
        return bp.find_products_for_service(service_id)

    @mcp.tool(
        description="Read a BP page's current content + doc index entry.",
    )
    def get_page(page_uri: str) -> dict[str, Any]:
        return bp.get_page(page_uri)

    @mcp.tool(
        description=(
            "Replace a fenced SME-PLACEHOLDER:question_id block with the "
            "supplied replacement Markdown, write the page, and trigger "
            "RAG_MCP.index for the patched content."
        ),
    )
    def patch_page(page_uri: str, question_id: str, replacement: str) -> dict[str, Any]:
        return bp.patch_page(page_uri=page_uri, question_id=question_id, replacement=replacement)

    @mcp.tool(
        description=(
            "Persist an SME reply as a new BP page; index it; record the doc-index "
            "entry. Returns {new_page_uri, embedding_revision, commit_sha}."
        ),
    )
    def ingest_sme_doc(
        question_id: str,
        sme_text: str,
        originating_pages: list[str] | None = None,
        topic: str | None = None,
        question: str | None = None,
    ) -> dict[str, Any]:
        return bp.ingest_sme_doc(
            question_id=question_id,
            sme_text=sme_text,
            originating_pages=originating_pages,
            topic=topic,
            question=question,
        )

    return mcp, bp


def main() -> None:
    parser = argparse.ArgumentParser(description="BP MCP server")
    parser.add_argument("--inputs", default=os.environ.get("BP_INPUTS_ROOT"))
    parser.add_argument("--pages", default=os.environ.get("BP_PAGES_ROOT"))
    parser.add_argument("--bp-db", default=os.environ.get("BP_DB_PATH"))
    parser.add_argument("--chroma", default=os.environ.get("RAG_CHROMA_PATH"))
    parser.add_argument("--sd-stub", default=os.environ.get("BP_SD_STUB_MAPPING_FILE"))
    parser.add_argument(
        "--transport",
        default=os.environ.get("BP_MCP_TRANSPORT", "stdio"),
        choices=("stdio", "sse", "streamable-http"),
    )
    parser.add_argument("--host", default=os.environ.get("BP_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BP_MCP_PORT", "8103")))
    args = parser.parse_args()

    server, _bp = build_server(
        inputs_root=args.inputs,
        pages_root=args.pages,
        bp_db_path=args.bp_db,
        chroma_path=args.chroma,
        sd_stub_mapping=args.sd_stub,
        host=args.host,
        port=args.port,
    )
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
