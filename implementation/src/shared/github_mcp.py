"""Sync wrapper around the upstream ``@modelcontextprotocol/server-github`` MCP.

Production target for §8.5's "BP and SD share a single GitHub repo" — both
specialists read inputs and write generated pages through the GitHub MCP
instead of the filesystem-backed ``Local*Store`` we use during validation.

The reference GitHub MCP server is a stdio-only Node package
(``@modelcontextprotocol/server-github``); we spawn it via ``npx`` from
``langchain-mcp-adapters``'s ``MultiServerMCPClient``, resolve its tool
list once on startup, and route every call through the same async-bridge
pattern :class:`MCPHttpClient` already uses for HTTP-based peers.

Two helper methods cover the entire BP/SD surface:

  * :meth:`read_text_file` — single-file ``get_file_contents``, base64-decoded.
  * :meth:`list_directory` — array form of ``get_file_contents``.
  * :meth:`create_or_update_file` — atomic write + previous-blob-sha tracking.

A small in-memory ``sha`` cache is kept so repeated writes against the
same path don't pay an extra ``get_file_contents`` round-trip just to
discover the previous sha.
"""

from __future__ import annotations

import base64
import json
import threading
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient

from .mcp_http_client import _AsyncBridge


class GitHubMCPError(Exception):
    """Generic GitHub MCP failure."""


class FileNotFoundOnGitHub(GitHubMCPError):
    """The ``get_file_contents`` call returned a 404."""


class GitHubMCPClient:
    """Long-running stdio session to the upstream GitHub MCP server.

    Constructor spawns ``npx -y @modelcontextprotocol/server-github`` and
    blocks until the tool list is resolved (typical 2–5s on a warm npm
    cache, longer on first run when the package downloads).
    """

    def __init__(
        self,
        *,
        github_token: str,
        owner: str,
        repo: str,
        branch: str = "main",
        timeout_s: float = 120.0,
    ):
        if not github_token or not github_token.strip():
            raise GitHubMCPError("github_token is required")
        if not owner or not repo:
            raise GitHubMCPError("owner and repo are required")
        self._token = github_token
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self._timeout = timeout_s
        self._bridge = _AsyncBridge()
        self._lock = threading.Lock()
        self._client: MultiServerMCPClient | None = None
        self._tools: dict[str, Any] = {}
        self._sha_cache: dict[tuple[str, str], str] = {}  # (branch, path) → blob sha
        self._open()

    # ----------------------------------------------------------- lifecycle

    def _open(self) -> None:
        async def _init():
            client = MultiServerMCPClient(
                {
                    "github": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-github"],
                        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": self._token},
                        "transport": "stdio",
                    }
                }
            )
            tools = await client.get_tools()
            return client, {t.name: t for t in tools}

        self._client, self._tools = self._bridge.run(_init(), timeout=self._timeout)

    def close(self) -> None:
        with self._lock:
            self._bridge.close()

    # ---------------------------------------------------------------- core

    def _call(self, tool_name: str, args: dict[str, Any]) -> Any:
        tool = self._tools.get(tool_name)
        if tool is None:
            raise GitHubMCPError(
                f"GitHub MCP tool {tool_name!r} not available; have: {sorted(self._tools)}"
            )

        async def _run():
            raw = await tool.ainvoke(args)
            return _unpack(raw)

        with self._lock:
            return self._bridge.run(_run(), timeout=self._timeout)

    # ----------------------------------------------------- get_file_contents

    def get_file(self, path: str, *, branch: str | None = None) -> Any:
        """Raw ``get_file_contents`` call. Returns either a file-shaped dict
        or a list of directory entries depending on the path's nature.

        Raises :class:`FileNotFoundOnGitHub` on 404.
        """
        path = path.lstrip("/")
        try:
            return self._call(
                "get_file_contents",
                {
                    "owner": self.owner,
                    "repo": self.repo,
                    "path": path,
                    "branch": branch or self.branch,
                },
            )
        except Exception as exc:  # noqa: BLE001
            if _looks_like_404(exc):
                raise FileNotFoundOnGitHub(path) from exc
            raise

    def read_text_file(
        self, path: str, *, branch: str | None = None
    ) -> tuple[str, str | None]:
        """Read a file as UTF-8 text. Returns ``(content, sha)`` where
        ``sha`` is the blob sha — pass it back into
        :meth:`create_or_update_file` to update the file atomically.

        Robust to all three response shapes the GitHub MCP can return for
        a single-file ``get_file_contents`` call:

          * **dict** — GitHub-REST-API style ``{type, encoding, content,
            sha, …}``. NOTE: the ``encoding`` field is unreliable — the
            MCP / langchain adapter often pre-decodes the body but leaves
            the flag set to ``"base64"`` anyway. We ignore the flag and
            run :func:`_maybe_decode_base64` on the content shape, which
            only commits to a decode when the input is actually in the
            base64 charset.
          * **list of MCP content blocks** — concatenate the ``text``
            fields. No sha available in this shape.
          * **plain string** — already-decoded content. No sha.

        Raises :class:`GitHubMCPError` only when the response is none of
        the above (truly unexpected) or the path turns out to be a
        directory rather than a file.
        """
        result = self.get_file(path, branch=branch)

        raw_text: str = ""
        sha: str | None = None

        if isinstance(result, str):
            raw_text = result
        elif isinstance(result, list):
            # MCP content-block list slipped through ``_unpack``. If every
            # block looks like a directory entry (has ``type=='dir'``),
            # treat it as a directory error; otherwise concatenate text.
            if any(isinstance(b, dict) and (b.get("type") in ("dir", "directory", "tree")) for b in result):
                raise GitHubMCPError(f"path {path!r} is a directory, not a file")
            chunks: list[str] = []
            for block in result:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text")
                    if isinstance(t, str):
                        chunks.append(t)
            raw_text = "".join(chunks)
        elif isinstance(result, dict):
            if result.get("type") not in (None, "file"):
                raise GitHubMCPError(
                    f"path {path!r} is type={result.get('type')!r}, expected file"
                )
            for key in ("content", "text", "decoded_content"):
                v = result.get(key)
                if isinstance(v, str) and v:
                    raw_text = v
                    break
            sha = result.get("sha") if isinstance(result.get("sha"), str) else None
        else:
            raise GitHubMCPError(
                f"unexpected get_file_contents result for {path!r}: {type(result).__name__}"
            )

        if not raw_text:
            return "", sha

        # Trust content shape, not the metadata flag — see docstring.
        text = _maybe_decode_base64(raw_text)
        if sha:
            self._sha_cache[(branch or self.branch, path.lstrip("/"))] = sha
        return text, sha

    def list_directory(
        self, path: str = "", *, branch: str | None = None
    ) -> list[dict[str, Any]]:
        """List the immediate entries under ``path``.

        Each entry has ``type`` (``"file"`` / ``"dir"``), ``name``,
        ``path``, ``sha``, ``size``. ``path`` may be empty to list the
        repo root. Returns ``[]`` if the path is missing.
        """
        try:
            result = self.get_file(path, branch=branch)
        except FileNotFoundOnGitHub:
            return []
        if isinstance(result, dict) and result.get("type") == "file":
            raise GitHubMCPError(f"path {path!r} is a file, not a directory")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            entries = result.get("entries") or result.get("content")
            if isinstance(entries, list):
                return entries
        raise GitHubMCPError(
            f"unexpected directory listing for {path!r}: {type(result).__name__}"
        )

    def walk(
        self,
        prefix: str = "",
        *,
        branch: str | None = None,
        file_filter=None,
        max_depth: int = 16,
    ) -> list[dict[str, Any]]:
        """Recursive directory walk. Returns every ``type=='file'`` entry
        beneath ``prefix``; ``file_filter`` is an optional callable
        ``(entry: dict) -> bool``.

        Hardened against two pathological MCP responses: directory entries
        with no resolvable next path (would otherwise re-push ``current``
        and spin forever), and unexpectedly deep / cyclic listings (capped
        by ``max_depth`` plus a visited-path set).
        """
        out: list[dict[str, Any]] = []
        # Stack carries (path, depth) tuples so we can enforce ``max_depth``.
        stack: list[tuple[str, int]] = [(prefix, 0)]
        visited: set[str] = set()
        while stack:
            current, depth = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            for entry in self.list_directory(current, branch=branch):
                etype = (entry.get("type") or "").lower()
                if etype == "file":
                    if file_filter is None or file_filter(entry):
                        out.append(entry)
                elif etype in ("dir", "directory", "tree"):
                    if depth + 1 > max_depth:
                        continue  # depth cap — silently truncate
                    nxt = entry.get("path") or _join(current, entry.get("name") or "")
                    # Guard against (a) empty next paths, (b) self-references
                    # that would re-walk the same dir, and (c) cycles.
                    if not nxt or nxt == current or nxt in visited:
                        continue
                    stack.append((nxt, depth + 1))
                # Anything else (symlink, submodule) is skipped.
        return out

    # ------------------------------------------------- create_or_update_file

    def create_or_update_file(
        self,
        path: str,
        content: str,
        message: str,
        *,
        sha: str | None = None,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """Create the file if missing, update it if present. Returns
        ``{commit_sha, blob_sha}`` plus the raw MCP result for callers
        that want it.

        ``sha`` is the previous blob sha for the file (required by the
        GitHub API on updates). When omitted, we look it up from the
        in-memory cache; when there's no cached sha and the file already
        exists, we do one ``get_file_contents`` call to fetch it.
        """
        path = path.lstrip("/")
        target_branch = branch or self.branch

        if sha is None:
            sha = self._sha_cache.get((target_branch, path))
            if sha is None:
                # Best-effort lookup. A 404 means it's a creation.
                try:
                    _, fetched_sha = self.read_text_file(path, branch=target_branch)
                    sha = fetched_sha
                except FileNotFoundOnGitHub:
                    sha = None

        args: dict[str, Any] = {
            "owner": self.owner,
            "repo": self.repo,
            "path": path,
            "content": content,
            "message": message,
            "branch": target_branch,
        }
        if sha:
            args["sha"] = sha

        result = self._call("create_or_update_file", args)
        commit_sha: str | None = None
        blob_sha: str | None = None
        if isinstance(result, dict):
            commit = result.get("commit") or {}
            content_obj = result.get("content") or {}
            if isinstance(commit, dict):
                commit_sha = commit.get("sha")
            if isinstance(content_obj, dict):
                blob_sha = content_obj.get("sha")
        if blob_sha:
            self._sha_cache[(target_branch, path)] = blob_sha
        return {"commit_sha": commit_sha, "blob_sha": blob_sha, "raw": result}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unpack(raw: Any) -> Any:
    """Normalise a langchain-mcp-adapters tool result into a Python value.

    The adapter returns either a plain dict / list, a string of JSON, or
    a list of MCP content blocks (``[{type: 'text', text: '...json...'}]``).
    We collapse every form into a Python value the caller can index.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        # Could be an MCP content-block list OR a directory listing.
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
        # Fall through: it's already a list value.
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _looks_like_404(exc: Exception) -> bool:
    text = str(exc).lower()
    return "404" in text or "not found" in text or "does not exist" in text


def _maybe_decode_base64(text: str) -> str:
    """Heuristic: return ``text`` unchanged unless it really looks like a
    base64 blob and decodes to something more text-like.

    Why this exists: the GitHub MCP / langchain-mcp-adapters layer
    auto-decodes file bodies into UTF-8 but cargo-cults the GitHub REST
    API's ``encoding: 'base64'`` field straight through. Trusting the
    flag would feed plain Markdown into ``b64decode`` and crash on the
    first ``#`` or ``—``. Trusting the content shape — base64 charset,
    length multiple of 4, decoded result has natural-text characters —
    handles every shape the upstream server returns today.
    """
    stripped = text.strip()
    if not stripped:
        return text
    compact = "".join(stripped.split())
    if len(compact) < 32 or len(compact) % 4 != 0:
        return text
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
    head = decoded[:1500]
    # Only commit to the decoded version if it looks like prose. Random
    # base64 of binary data almost never decodes to such a string.
    if (" " in head) or ("\n" in head):
        return decoded
    return text


def _join(base: str, leaf: str) -> str:
    base = (base or "").strip("/")
    leaf = (leaf or "").strip("/")
    if not base:
        return leaf
    if not leaf:
        return base
    return f"{base}/{leaf}"
