"""LangGraph agent that uses the GitHub MCP to inspect ``kikecorona/pear-store``.

Same shape as ``../github-mcp-test/github-mcp-with-langgraph.py``: spawn the
reference ``@modelcontextprotocol/server-github`` server over stdio via
``langchain-mcp-adapters``, bind its tools to a local Ollama model, and let
LangGraph drive the agent loop. The only difference is the **target repo**
— this script is the smoke test that confirms our PAT can reach
``kikecorona/pear-store`` (the docs repo §8.5 names) before we wire the
GitHub MCP into the BP / SD services as the production ``PageStore`` /
``SourceStore``.

By default the agent lists ``documentation/`` and summarizes whatever
B&P pages are there; pass ``--path`` to point it at a different file or
folder, and ``--branch`` to switch branches.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Annotated, Literal

from typing_extensions import TypedDict
from dotenv import load_dotenv

from langchain_ollama import ChatOllama
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import StateGraph, START
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv()


# ==========================================
# 1. Spin up the GitHub MCP server and load its tools
# ==========================================
GITHUB_TOKEN = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
if not GITHUB_TOKEN:
    raise RuntimeError(
        "GITHUB_PERSONAL_ACCESS_TOKEN is required. Set it in your shell or in .env"
    )

# Default target — can be overridden by env vars or by editing here. The
# repo + branch combo matches §8.5's "BP and SD share a single GitHub repo"
# layout (documentation/bp/ + documentation/sd/).
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "kikecorona")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "pear-store")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

mcp_client = MultiServerMCPClient(
    {
        "github": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": GITHUB_TOKEN},
            "transport": "stdio",
        }
    }
)

# Tools resolve once at import time — same pattern as github-mcp-test so
# `langgraph dev` can pick `graph` up without spinning up a fresh client.
tools = asyncio.run(mcp_client.get_tools())
tool_node = ToolNode(tools)


# ==========================================
# 2. Initialize Ollama and bind the MCP tools
# ==========================================
llm = ChatOllama(model=os.environ.get("LLM_MODEL", "llama3.1"), temperature=0)
llm_with_tools = llm.bind_tools(tools)

SYSTEM_PROMPT = (
    "You are an assistant that uses the GitHub MCP tools to inspect the "
    f"`{GITHUB_OWNER}/{GITHUB_REPO}` documentation repo (the docs repo for "
    "the Capstone POC).\n\n"
    f"Default arguments for every `get_file_contents` call: owner=`{GITHUB_OWNER}`, "
    f"repo=`{GITHUB_REPO}`, branch=`{GITHUB_BRANCH}` (unless the user explicitly "
    "names a different branch).\n\n"
    "Each entry returned by `get_file_contents` on a directory has a `type` "
    "field — either `file` or `dir`. You MUST handle them differently:\n\n"
    "  * For every entry with `type=='dir'`, call `get_file_contents` again "
    "    using that entry's `path` and recurse the SAME way. Do NOT summarise "
    "    a directory as if it were a file — you have to open it. Keep going "
    "    until you reach actual files.\n"
    "  * For every entry with `type=='file'`, call `get_file_contents` to "
    "    fetch its content and produce a one-to-two-sentence Markdown summary.\n"
    "  * Skip binary / unreadable files (images, PDFs, archives).\n\n"
    "Cap recursion at 4 levels of nested directories to avoid runaway traversal.\n\n"
    "Output format: a hierarchical Markdown bullet list rooted at the path "
    "the user asked about. Indent sub-directories under their parent; put each "
    "file's one-to-two-sentence summary on its own bullet. Confirm you actually "
    "called `get_file_contents` for every leaf file — don't invent paths or "
    "content."
)


# ==========================================
# 3. Define the graph state and nodes
# ==========================================
class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def call_model(state: AgentState):
    """Invoke the model, prepending the system prompt on the first turn."""
    messages = state["messages"]
    if not any(isinstance(m, SystemMessage) for m in messages):
        messages = [SystemMessage(content=SYSTEM_PROMPT), *messages]
    response = llm_with_tools.invoke(messages)
    return {"messages": [response]}


def route_after_model(state: AgentState) -> Literal["tools", "__end__"]:
    last_message = state["messages"][-1]
    if last_message.tool_calls:
        return "tools"
    return "__end__"


# ==========================================
# 4. Build the workflow graph
# ==========================================
workflow = StateGraph(AgentState)
workflow.add_node("agent", call_model)
workflow.add_node("tools", tool_node)
workflow.add_edge(START, "agent")
workflow.add_conditional_edges("agent", route_after_model)
workflow.add_edge("tools", "agent")

# `graph` is the symbol referenced by langgraph.json so `langgraph dev` can
# serve it to the Agent Chat UI.
graph = workflow.compile()


# ==========================================
# 5. CLI smoke test
# ==========================================
# LangGraph defaults to a recursion_limit of 25 — that's per-step, so each
# `agent → tool → agent` round-trip costs 2 steps. A directory tree with a
# handful of subdirs and ~5 files each blows past 25 fast. 100 gives the
# agent enough room to walk a documentation/* tree without truncation.
RECURSION_LIMIT = int(os.environ.get("PEARSTORE_RECURSION_LIMIT", "100"))


async def _run_cli(query: str):
    inputs = {"messages": [HumanMessage(content=query)]}
    latest_msg = None
    async for output in graph.astream(
        inputs,
        stream_mode="values",
        config={"recursion_limit": RECURSION_LIMIT},
    ):
        latest_msg = output["messages"][-1]
    return latest_msg


def _build_default_query(path: str, branch: str) -> str:
    return (
        f"Inspect the path `{path}` in repository `{GITHUB_OWNER}/{GITHUB_REPO}` "
        f"on branch `{branch}`. If it's a directory, list its files and produce a "
        "Markdown bullet summary of each one (one to two sentences per bullet). "
        "If it's a single file, summarize what it covers. Confirm you actually "
        "read the contents — don't invent paths or content."
    )


# ==========================================
# 6. Python-driven walker (default CLI mode)
# ==========================================
# Small local LLMs (e.g. llama3.1:8b) reliably *describe* a recursion plan
# but skip making the actual `get_file_contents` calls — the agent emits
# "No files found in this directory" for every sub-dir without ever
# checking. To stop fighting that bias, drive the walk in Python and let
# the LLM do only what it's good at: summarizing one file at a time.
#
# The `graph` symbol above stays intact for the langgraph dev UI in case
# anyone wants to compare; this CLI path is the reliable one.
import base64
import json
from typing import Any

# File-extension allow-list. Only these extensions get fetched + summarised;
# everything else (images, PDFs, mystery files) is silently skipped. Simpler
# and more reliable than a "is this binary?" heuristic — the docs repo is
# >99% Markdown anyway.
_TEXT_EXTS = {".md", ".py", ".json", ".txt", ".sh"}
_TOOLS_BY_NAME = {t.name: t for t in tools}


def _is_text_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return any(name.endswith(ext) for ext in _TEXT_EXTS)


def _unpack(raw: Any) -> Any:
    """Normalise a langchain-mcp-adapters tool result into a Python value."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        if raw and isinstance(raw[0], dict) and raw[0].get("type") in {"text", "json"}:
            for block in raw:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if text is not None:
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return text
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


async def _gh(tool_name: str, args: dict) -> Any:
    tool = _TOOLS_BY_NAME.get(tool_name)
    if tool is None:
        raise RuntimeError(
            f"GitHub MCP didn't expose {tool_name!r}; have: {sorted(_TOOLS_BY_NAME)}"
        )
    return _unpack(await tool.ainvoke(args))


def _join(base: str, leaf: str) -> str:
    base = (base or "").strip("/")
    leaf = (leaf or "").strip("/")
    if not base:
        return leaf
    if not leaf:
        return base
    return f"{base}/{leaf}"


async def _walk(prefix: str, branch: str, *, max_depth: int = 6) -> list[dict]:
    """Stack-based DFS via the GitHub MCP. Returns every file entry under
    ``prefix``. Cycle-guarded + depth-capped so a malformed listing can't
    spin forever."""
    out: list[dict] = []
    stack: list[tuple[str, int]] = [(prefix, 0)]
    visited: set[str] = set()
    while stack:
        current, depth = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        result = await _gh(
            "get_file_contents",
            {"owner": GITHUB_OWNER, "repo": GITHUB_REPO, "path": current, "branch": branch},
        )
        # If the user passed a single-file path, return it directly.
        if isinstance(result, dict) and (result.get("type") == "file"):
            entry = dict(result)
            entry.setdefault("path", current)
            entry.setdefault("name", current.rsplit("/", 1)[-1])
            out.append(entry)
            continue
        # Directory listing: either a list, or a dict wrapping a list.
        entries = result if isinstance(result, list) else (
            result.get("entries") or result.get("content")
            if isinstance(result, dict) else None
        )
        if not isinstance(entries, list):
            print(f"  WARNING: unexpected listing for {current!r}: {type(result).__name__}")
            continue
        if not entries:
            print(f"  (empty) {current}")
            continue
        for entry in entries:
            etype = (entry.get("type") or "").lower()
            ent_path = entry.get("path") or _join(current, entry.get("name") or "")
            if etype == "file":
                out.append({**entry, "path": ent_path})
            elif etype in ("dir", "directory", "tree"):
                if depth + 1 > max_depth:
                    continue
                if not ent_path or ent_path == current or ent_path in visited:
                    continue
                stack.append((ent_path, depth + 1))
            # Anything else (symlink/submodule) is silently skipped.
    return out


async def _read_text(path: str, branch: str) -> tuple[str, bool]:
    """Returns ``(text, is_binary_or_error)``. The flag is True only if
    we genuinely couldn't extract usable text.

    Handles the three response shapes the GitHub MCP can return for a
    single-file ``get_file_contents`` call:

      1. **Plain string** — the file's content, returned directly by
         servers that auto-decode text files (langchain-mcp-adapters'
         ``_unpack`` passed it through because it didn't parse as JSON).
      2. **List of MCP content blocks** — same, but wrapped; we
         concatenate the ``text`` fields.
      3. **GitHub-REST-API-style dict** — ``{type, encoding, content,
         sha, ...}``. ``encoding=='base64'`` triggers the obvious
         decode path.

    Plus a heuristic decoder for case 3 with the encoding field missing
    (some MCP server forks drop it but still return base64). Without the
    heuristic, an MD file would land in the LLM as a base64 blob and
    get a "corrupted content" summary — exactly the bug we just hit.
    """
    result = await _gh(
        "get_file_contents",
        {"owner": GITHUB_OWNER, "repo": GITHUB_REPO, "path": path, "branch": branch},
    )

    # ----- DEBUG -----------------------------------------------------------
    # Dump the response shape + a head/tail snippet so we can see exactly
    # what the MCP gave us when extraction goes sideways. Set
    # PEARSTORE_DEBUG=0 in the env to silence.
    if os.environ.get("PEARSTORE_DEBUG", "1") not in ("0", "false", "False"):
        _debug_dump_response(path, result)
    # ----------------------------------------------------------------------

    raw_text: str = ""

    if isinstance(result, str):
        raw_text = result
    elif isinstance(result, list):
        # MCP content-block list slipped through _unpack (the inner text
        # didn't parse as JSON). Concatenate every text block.
        chunks: list[str] = []
        for block in result:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    chunks.append(t)
        raw_text = "".join(chunks)
    elif isinstance(result, dict):
        # The MCP/langchain layer auto-decodes the body but leaves the
        # GitHub REST API's ``encoding`` field set to "base64" anyway, so
        # we ignore that flag and infer base64-ness from content shape
        # below.
        for key in ("content", "text", "decoded_content"):
            v = result.get(key)
            if isinstance(v, str) and v:
                raw_text = v
                break
    else:
        return "", True

    if not raw_text:
        return "", True

    # The GitHub MCP / langchain-mcp-adapters layer auto-decodes the body
    # for us, but the `encoding` field gets passed through verbatim from
    # the GitHub REST API response — i.e. it says "base64" even when the
    # content is already plain UTF-8. Don't trust the flag: trust the
    # content shape. ``_maybe_decode_base64`` only commits to a decode
    # when the input is actually in the base64 charset, so plain
    # Markdown / Python / JSON / etc passes through unchanged and a
    # truly-still-base64 blob (rare in practice) gets decoded.
    decoded = _maybe_decode_base64(raw_text)
    return decoded, False


def _debug_dump_response(path: str, result: Any) -> None:
    """Pretty-print the raw MCP response so we can see what the server
    actually returned. Goes to stderr-style ``print`` so it interleaves
    with the walker's progress lines."""
    print(f"\n  --- DEBUG _read_text({path}) ---")
    print(f"      type: {type(result).__name__}")
    if isinstance(result, dict):
        print(f"      keys: {sorted(result.keys())}")
        for k in ("type", "encoding", "size", "name", "sha"):
            if k in result:
                print(f"      {k}: {result[k]!r}")
        for k in ("content", "text", "decoded_content"):
            v = result.get(k)
            if isinstance(v, str):
                head = v[:160].replace("\n", "\\n")
                print(f"      {k}[:160]: {head!r} (len={len(v)})")
    elif isinstance(result, list):
        print(f"      length: {len(result)}")
        for i, block in enumerate(result[:3]):
            if isinstance(block, dict):
                t = block.get("text")
                head = (t[:160] if isinstance(t, str) else repr(block))
                print(f"      [{i}] type={block.get('type')!r}  head: {head!r}")
            else:
                print(f"      [{i}] (non-dict block): {block!r}")
    elif isinstance(result, str):
        head = result[:200].replace("\n", "\\n")
        print(f"      head: {head!r} (len={len(result)})")
    else:
        print(f"      repr: {result!r}")
    print()


def _maybe_decode_base64(text: str) -> str:
    """Heuristic: return ``text`` unchanged unless it really looks like
    a base64 blob and decodes to something more text-like.

    The GitHub REST API base64-encodes file contents and wraps every 60
    chars with a newline; we strip that whitespace before checking so
    legitimate base64 isn't rejected on shape grounds.
    """
    stripped = text.strip()
    if not stripped:
        return text
    compact = "".join(stripped.split())
    if len(compact) < 32 or len(compact) % 4 != 0:
        return text
    # base64 charset: A-Z a-z 0-9 + / and the = padding char.
    if not all(c.isalnum() or c in "+/=" for c in compact):
        return text
    try:
        decoded_bytes = base64.b64decode(compact, validate=True)
    except Exception:
        return text
    try:
        decoded = decoded_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return text
    # Only commit to the decoded version if it looks more like prose
    # (has spaces or newlines in its head). Random base64 of binary data
    # almost never decodes to such a string.
    head = decoded[:1500]
    if (" " in head) or ("\n" in head):
        return decoded
    return text


import re

# llama3.1:8b loves to open replies with "Here is a summary…" no matter
# how the prompt is worded; this regex strips a handful of common
# preambles after the LLM call so the rendered Markdown doesn't repeat
# the same boilerplate against every file.
_PREAMBLE_RX = re.compile(
    r"^\s*(?:"
    r"here(?:'s| is) (?:a |the )?(?:one[- ]to[- ]two[- ]sentences? |concise |brief )?summar(?:y|ies?)[^:]*[:.\-]?\s*"
    r"|this (?:file|document|doc|page) (?:covers|describes|outlines|provides)[\s:.\-]+"
    r"|summary[\s:]+"
    r"|tl;?dr[\s:]+"
    r")",
    re.IGNORECASE,
)


async def _summarize_file(path: str, content: str) -> str:
    """One-or-two-sentence summary via the local LLM. Bounded to ~6k
    chars of input so the prompt fits comfortably in llama3.1:8b's window.

    The output is the summary text only — no preamble like "Here is a
    summary…" or "This file describes…". The system prompt forbids
    those, and any that slip through get stripped post-hoc by the
    ``_PREAMBLE_RX`` regex below.
    """
    excerpt = content[:6000]
    msg = await llm.ainvoke([
        SystemMessage(content=(
            "You write tight one-to-two-sentence summaries of documentation files. "
            "Be specific about what the file covers. Don't invent details.\n\n"
            "Output ONLY the summary text. Do NOT prefix with phrases like "
            "'Here is a summary…', 'This file…', 'Summary:', or any "
            "acknowledgement. Start directly with substantive content."
        )),
        HumanMessage(content=f"PATH: {path}\n\nCONTENT:\n{excerpt}"),
    ])
    text = msg.content if isinstance(msg.content, str) else str(msg.content)
    text = text.strip().replace("\n", " ")
    # Belt and suspenders: strip preambles the model adds despite the prompt.
    text = _PREAMBLE_RX.sub("", text).lstrip()
    return text


async def _run_python_walk(path: str, branch: str) -> str:
    print(f"  Walking {GITHUB_OWNER}/{GITHUB_REPO}:{path}@{branch} via Python (LLM only summarises)…")
    files = await _walk(path, branch)
    files = [f for f in files if _is_text_file(f.get("path") or "")]
    files.sort(key=lambda f: f.get("path") or "")
    print(f"  Found {len(files)} text file(s) (extensions: {sorted(_TEXT_EXTS)}); summarising…")

    # Group by parent directory for readable Markdown.
    grouped: dict[str, list[tuple[str, str]]] = {}
    for f in files:
        fpath = f["path"]
        try:
            text, _skip = await _read_text(fpath, branch)
        except Exception as exc:  # noqa: BLE001
            summary = f"_(read failed: {exc})_"
        else:
            if not text.strip():
                summary = "_(empty file)_"
            else:
                try:
                    summary = await _summarize_file(fpath, text)
                except Exception as exc:  # noqa: BLE001
                    summary = f"_(summary failed: {exc})_"
        parent = fpath.rsplit("/", 1)[0] if "/" in fpath else ""
        grouped.setdefault(parent, []).append((fpath, summary))

    # Render hierarchical Markdown rooted at ``path``.
    lines: list[str] = [f"# `{path}` — {GITHUB_OWNER}/{GITHUB_REPO}@{branch}", ""]
    for parent in sorted(grouped):
        rel = parent[len(path) + 1:] if parent.startswith(path + "/") else (
            "" if parent == path else parent
        )
        lines.append(f"## {rel + '/' if rel else '(root)'}")
        for fpath, summary in grouped[parent]:
            name = fpath.rsplit("/", 1)[-1]
            lines.append(f"- **{name}** — {summary}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"GitHub MCP smoke test against {GITHUB_OWNER}/{GITHUB_REPO}")
    parser.add_argument(
        "--path",
        default=os.environ.get("PEARSTORE_PATH", "documentation"),
        help="Path inside the repo to inspect (file or directory). Default: documentation/sd",
    )
    parser.add_argument(
        "--branch",
        default=GITHUB_BRANCH,
        help=f"Branch to read from. Default: {GITHUB_BRANCH}",
    )
    parser.add_argument(
        "--mode",
        choices=("python", "agent"),
        default=os.environ.get("PEARSTORE_MODE", "python"),
        help=(
            "How to drive the walk. 'python' (default): deterministic stack-based "
            "DFS through the MCP, LLM only summarises each file. 'agent': hand "
            "control to the LangGraph agent (works against larger models, "
            "unreliable on llama3.1:8b — small models hallucinate 'no files found' "
            "instead of recursing)."
        ),
    )
    parser.add_argument(
        "--query",
        default=None,
        help="Override the default agent prompt (only used in --mode=agent).",
    )
    args = parser.parse_args()

    print(f"🤖 GitHub MCP smoke test against {GITHUB_OWNER}/{GITHUB_REPO}")
    if args.mode == "python":
        report = asyncio.run(_run_python_walk(args.path, args.branch))
        print()
        print(report)
    else:
        query = args.query or _build_default_query(args.path, args.branch)
        print(f"\nUser: {query}\n")
        latest_msg = asyncio.run(_run_cli(query))
        if latest_msg is not None:
            print(f"Agent response:\n{latest_msg.content}")
