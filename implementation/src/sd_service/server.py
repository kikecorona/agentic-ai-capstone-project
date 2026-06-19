"""``SD_MCP`` — MCP frontend for the SD Service (§8.2 / §9.2.2).

Same shape as ``BP_MCP``: in-process service inside the subprocess,
five tools exposed over stdio. Configurable via env vars:

  * ``SD_SOURCES_ROOT`` — root of the source-tree fixtures (default
    ``./data/sd/sources``). Each immediate child directory is a service.
  * ``SD_PAGES_ROOT``  — generated SD pages root (default
    ``./data/sd/pages``). Production target is ``documentation/sd/``
    inside the docs repo, written through the GitHub MCP.
  * ``SD_DB_PATH``     — SD doc index + sources inventory (default
    ``./data/sd/state.db``).
  * ``RAG_CHROMA_PATH`` / ``OTEL_DB_PATH`` / ``AUDIT_DB_PATH`` — shared.

Run with::

    python -m src.sd_service.server [--sources PATH] [--pages PATH]
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from src.bp_service.pages import LocalPageStore
from src.rag_service.service import RAGService
from src.rag_service.store import EmbeddingsStore, default_persist_path as default_chroma_path
from src.shared.llm import get_embedding_function
from src.shared.otel_client import OTelClient
from src.shared.peer_clients import BPHttpClient, RAGHttpClient, env_url
from src.shared.service_log import get_logger

from .clients import InProcessRAGClient, StubBPClient
from .service import SDService
from .sources import GitHubSourceStore, LocalSourceStore
from .store import SDDocIndex, SDSourcesInventory, default_db_path

log = get_logger("rag.sd.server")


def build_server(
    *,
    sources_root: str | os.PathLike[str] | None = None,
    pages_root: str | os.PathLike[str] | None = None,
    sd_db_path: str | os.PathLike[str] | None = None,
    chroma_path: str | os.PathLike[str] | None = None,
    host: str = "127.0.0.1",
    port: int = 8104,
) -> tuple[FastMCP, SDService]:
    sources_root = sources_root or os.environ.get("SD_SOURCES_ROOT", "./data/sd/sources")
    pages_root = pages_root or os.environ.get("SD_PAGES_ROOT", "./data/sd/pages")
    sd_db_path = sd_db_path or os.environ.get("SD_DB_PATH", str(default_db_path()))
    chroma_path = chroma_path or os.environ.get("RAG_CHROMA_PATH", str(default_chroma_path()))

    rag_url = env_url("RAG_MCP_URL")
    bp_url = env_url("BP_MCP_URL")
    gh_token = env_url("GITHUB_PERSONAL_ACCESS_TOKEN")
    gh_owner = env_url("GITHUB_OWNER")
    gh_repo = env_url("GITHUB_REPO")
    use_github = bool(gh_token and gh_owner and gh_repo)

    log.info(
        f"SD_MCP server starting page_store={'github' if use_github else 'local'} "
        f"source_store={'github' if use_github else 'local'} "
        f"sd_db={sd_db_path} chroma={chroma_path} "
        f"rag_peer={rag_url or '(in-process)'} "
        f"bp_peer={bp_url or '(stub)'}"
    )

    otel = OTelClient.from_env()

    if use_github:
        from src.bp_service.pages import GitHubPageStore
        from src.shared.github_mcp import GitHubMCPClient

        github = GitHubMCPClient(
            github_token=gh_token,
            owner=gh_owner,
            repo=gh_repo,
            branch=env_url("GITHUB_BRANCH") or "main",
        )
        # SD writes its pages under documentation/sd/. The "inputs"
        # parameter on PageStore is unused by SDService (it only ever
        # reads/writes pages), so we point it at an empty prefix.
        page_store = GitHubPageStore(
            github=github,
            inputs_prefix=env_url("SD_INPUTS_GH_PATH") or "documentation/sd",
            pages_prefix=env_url("SD_PAGES_GH_PATH") or "documentation/sd",
        )
        source_store = GitHubSourceStore(
            github=github,
            root_path=env_url("SD_SOURCES_GH_PATH") or "implementation",
        )
    else:
        page_store = LocalPageStore(inputs_root=sources_root, pages_root=pages_root)
        source_store = LocalSourceStore(sources_root)

    if rag_url:
        rag_client = RAGHttpClient(rag_url)
    else:
        rag_store = EmbeddingsStore(chroma_path, embedding_function=get_embedding_function())
        rag_service = RAGService(store=rag_store, otel=otel)
        rag_client = InProcessRAGClient(rag_service)

    bp_client = BPHttpClient(bp_url) if bp_url else StubBPClient()

    doc_index = SDDocIndex(sd_db_path)
    sources_inventory = SDSourcesInventory(sd_db_path)

    sd = SDService(
        page_store=page_store,
        source_store=source_store,
        rag=rag_client,
        bp=bp_client,
        doc_index=doc_index,
        sources_inventory=sources_inventory,
        otel=otel,
    )

    mcp = FastMCP(
        name="sd-mcp",
        host=host,
        port=port,
        instructions=(
            "System Design specialist (§9.2). dispatch_query for query mode "
            "(falls back to focused analyze_code when retrieval is weak; "
            "never escalates). dispatch_refresh runs analyze_code → ToT dep "
            "graph → page write → RAG_MCP.index. find_services_for_product "
            "for relational BP↔SD lookups. get_page / patch_page for SME "
            "re-integration."
        ),
    )

    @mcp.tool(
        description=(
            "Query-mode entry. Delegates to RAG_MCP.retrieve(domain_filter, "
            "mode=query); on low_confidence/exhausted, runs a focused "
            "analyze_code pass on the file backing the closest-matching "
            "endpoint and stitches the result into the answer. Returns "
            "{status, answer, sources, retrieval_trail, focused_analyze_code}."
        ),
    )
    def dispatch_query(
        query: str,
        domain_hint: str = "sd",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return sd.dispatch_query(query=query, domain_hint=domain_hint, context=context)

    @mcp.tool(
        description=(
            "Background-mode entry. For each affected service: pull source "
            "→ analyze_code → ToT dep graph → resolve_bp_links → compose "
            "page → write → RAG_MCP.index. Returns "
            "{affected_pages, escalations, details}."
        ),
    )
    def dispatch_refresh(event: dict[str, Any]) -> dict[str, Any]:
        return sd.dispatch_refresh(event=event)

    @mcp.tool(
        description=(
            "Relational lookup over the SD doc index — every SD page that "
            "names the given product. Pure metadata; no LLM, no RAG retrieve."
        ),
    )
    def find_services_for_product(product_id: str) -> list[dict[str, Any]]:
        return sd.find_services_for_product(product_id)

    @mcp.tool(description="Read an SD page's current content + doc index entry.")
    def get_page(page_uri: str) -> dict[str, Any]:
        return sd.get_page(page_uri)

    @mcp.tool(
        description=(
            "Replace a fenced SME-PLACEHOLDER:question_id block with the "
            "supplied replacement Markdown, write the page, and trigger "
            "RAG_MCP.index for the patched content."
        ),
    )
    def patch_page(page_uri: str, question_id: str, replacement: str) -> dict[str, Any]:
        return sd.patch_page(page_uri=page_uri, question_id=question_id, replacement=replacement)

    return mcp, sd


def main() -> None:
    parser = argparse.ArgumentParser(description="SD MCP server")
    parser.add_argument("--sources", default=os.environ.get("SD_SOURCES_ROOT"))
    parser.add_argument("--pages", default=os.environ.get("SD_PAGES_ROOT"))
    parser.add_argument("--sd-db", default=os.environ.get("SD_DB_PATH"))
    parser.add_argument("--chroma", default=os.environ.get("RAG_CHROMA_PATH"))
    parser.add_argument(
        "--transport",
        default=os.environ.get("SD_MCP_TRANSPORT", "stdio"),
        choices=("stdio", "sse", "streamable-http"),
    )
    parser.add_argument("--host", default=os.environ.get("SD_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SD_MCP_PORT", "8104")))
    args = parser.parse_args()

    server, _sd = build_server(
        sources_root=args.sources,
        pages_root=args.pages,
        sd_db_path=args.sd_db,
        chroma_path=args.chroma,
        host=args.host,
        port=args.port,
    )
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
