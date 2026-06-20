"""Page storage abstraction for the B&P specialist.

§9.3.1 says B&P "owns the **BP pages in GitHub**" — for the production
target every page write goes through the GitHub MCP. Per
[§8.5 POC notes](../../../PROJECT_ARCHITECTURE.md#85-considerations-for-the-poc)
we run pages out of a single Git repo at ``documentation/bp/`` and
``documentation/sd/``; for the local validation harness we want the same
contract without a GitHub PAT, so we slip a small ``PageStore`` Protocol
in front of every read/write.

Two implementations ship:

  * :class:`LocalPageStore` — filesystem-backed, used by the POC and the
    validation script. Keeps separate roots for *input docs* (read-only
    org material) and *generated B&P pages* (read/write).
  * :class:`GitHubPageStore` — placeholder for the production target.
    Not wired up until we add the GitHub MCP integration; the empty
    stub here is just a marker so the surface stays visible.

Page URIs are POSIX-style relative paths inside the chosen root, e.g.
``bp/products/catalog-discovery.md``. Trailing newlines are normalised
on every write so the audit stays diff-friendly.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class PageStore(Protocol):
    """Contract every page backend honours.

    Implementations decide how reads/writes happen (local FS, GitHub MCP,
    eventually some object store); the BPService talks only to this
    surface so it never knows the difference.
    """

    def read_input(self, source_uri: str) -> str: ...

    def list_inputs(self, prefix: str = "") -> list[str]: ...

    def read_page(self, page_uri: str) -> str | None: ...

    def write_page(self, page_uri: str, content: str) -> str: ...

    def page_exists(self, page_uri: str) -> bool: ...


class LocalPageStore:
    """Filesystem-backed PageStore.

    ``inputs_root`` holds the read-only org material the agent ingests
    (analogous to the curated docs that arrive through the GitHub MCP in
    production). ``pages_root`` holds the generated B&P pages — the agent
    writes here freely. Both directories are created on construction so
    callers don't have to.

    The ``write_page`` return value is a *content* hash rather than a
    real commit sha. Production deployments on top of GitHub will return
    the actual commit; the doc index treats the value opaquely so this
    swap is invisible to upstream callers.
    """

    def __init__(self, *, inputs_root: str | Path, pages_root: str | Path):
        self.inputs_root = Path(inputs_root).resolve()
        self.pages_root = Path(pages_root).resolve()
        self.inputs_root.mkdir(parents=True, exist_ok=True)
        self.pages_root.mkdir(parents=True, exist_ok=True)

    # -- inputs (read-only) -------------------------------------------------

    def read_input(self, source_uri: str) -> str:
        path = self._safe_join(self.inputs_root, source_uri)
        return path.read_text(encoding="utf-8")

    def list_inputs(self, prefix: str = "") -> list[str]:
        prefix_path = self._safe_join(self.inputs_root, prefix) if prefix else self.inputs_root
        if not prefix_path.exists():
            return []
        if prefix_path.is_file():
            return [self._rel(prefix_path, self.inputs_root)]
        out: list[str] = []
        for p in sorted(prefix_path.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                out.append(self._rel(p, self.inputs_root))
        return out

    # -- pages (read / write) -----------------------------------------------

    def read_page(self, page_uri: str) -> str | None:
        path = self._safe_join(self.pages_root, page_uri)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def page_exists(self, page_uri: str) -> bool:
        return self._safe_join(self.pages_root, page_uri).exists()

    def write_page(self, page_uri: str, content: str) -> str:
        path = self._safe_join(self.pages_root, page_uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Normalise trailing newline so every write produces a clean diff.
        if not content.endswith("\n"):
            content = content + "\n"
        path.write_text(content, encoding="utf-8")
        # Stand-in for a commit sha. The doc index treats it as opaque,
        # so swapping in the real GitHub commit later is a no-op for
        # upstream callers.
        return _short_hash(content)

    def list_pages(self, prefix: str = "") -> list[str]:
        prefix_path = self._safe_join(self.pages_root, prefix) if prefix else self.pages_root
        if not prefix_path.exists():
            return []
        if prefix_path.is_file():
            return [self._rel(prefix_path, self.pages_root)]
        return sorted(self._rel(p, self.pages_root) for p in prefix_path.rglob("*") if p.is_file())

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _safe_join(root: Path, relative: str) -> Path:
        rel = Path(relative)
        if rel.is_absolute() or any(part == ".." for part in rel.parts):
            raise ValueError(f"page URI must be a relative path, got {relative!r}")
        return (root / rel).resolve()

    @staticmethod
    def _rel(path: Path, root: Path) -> str:
        return str(path.relative_to(root)).replace("\\", "/")


class GitHubPageStore:
    """``PageStore`` backed by the upstream GitHub MCP — every read AND
    write goes through ``get_file_contents`` / ``create_or_update_file``.

    Production target for §8.5: BP and SD share a single docs repo with
    ``documentation/bp/`` (B&P pages — read for enrichment AND written
    back) and ``documentation/sd/`` (SD pages — same shape). The agent
    iterates over the existing pages, fills detected gaps using
    side-info (SD MCP / source code) and RAG, and writes back in place.
    Both prefixes are configurable so a deployment can keep inputs and
    outputs in different folders.

    Both prefixes are stored as POSIX strings inside the repo. URIs the
    rest of the codebase passes (e.g. ``bp/products/discovery.md``) are
    relative to the *pages root*; this class prepends the configured
    prefixes before talking to GitHub.
    """

    def __init__(
        self,
        *,
        github,
        inputs_prefix: str = "documentation/bp",
        pages_prefix: str = "documentation/bp",
    ):
        self._gh = github
        self._inputs_prefix = inputs_prefix.strip("/")
        self._pages_prefix = pages_prefix.strip("/")

    # -- inputs (read-only) -------------------------------------------------

    def read_input(self, source_uri: str) -> str:
        path = _join(self._inputs_prefix, source_uri)
        text, _sha = self._gh.read_text_file(path)
        return text

    def list_inputs(self, prefix: str = "") -> list[str]:
        gh_path = _join(self._inputs_prefix, prefix.strip("/"))
        files = self._gh.walk(gh_path, file_filter=_skip_hidden)
        rel: list[str] = []
        for entry in files:
            full_path = entry.get("path") or ""
            if not full_path:
                continue
            stripped = _strip_prefix(full_path, self._inputs_prefix)
            if stripped:
                rel.append(stripped)
        return sorted(rel)

    # -- pages (read / write) -----------------------------------------------

    def read_page(self, page_uri: str) -> str | None:
        path = _join(self._pages_prefix, page_uri)
        try:
            text, _sha = self._gh.read_text_file(path)
        except _gh_not_found_cls():
            return None
        return text

    def page_exists(self, page_uri: str) -> bool:
        return self.read_page(page_uri) is not None

    def write_page(self, page_uri: str, content: str) -> str:
        path = _join(self._pages_prefix, page_uri)
        if not content.endswith("\n"):
            content = content + "\n"
        message = f"agent: update {page_uri}"
        result = self._gh.create_or_update_file(path, content, message)
        # Prefer the commit sha (matches §9.3 doc-index ``commit_sha``
        # bookkeeping); fall back to the blob sha if the MCP didn't
        # surface a commit object.
        return result.get("commit_sha") or result.get("blob_sha") or "unknown"

    def list_pages(self, prefix: str = "") -> list[str]:
        gh_path = _join(self._pages_prefix, prefix.strip("/"))
        files = self._gh.walk(gh_path, file_filter=_skip_hidden)
        rel: list[str] = []
        for entry in files:
            full_path = entry.get("path") or ""
            stripped = _strip_prefix(full_path, self._pages_prefix)
            if stripped:
                rel.append(stripped)
        return sorted(rel)


def _gh_not_found_cls():
    """Resolved lazily so this module doesn't import shared.github_mcp at
    import-time (the GitHub MCP wrapper drags in langchain-mcp-adapters)."""
    from src.shared.github_mcp import FileNotFoundOnGitHub
    return FileNotFoundOnGitHub


def _join(prefix: str, suffix: str) -> str:
    prefix = (prefix or "").strip("/")
    suffix = (suffix or "").strip("/")
    if not prefix:
        return suffix
    if not suffix:
        return prefix
    return f"{prefix}/{suffix}"


def _strip_prefix(path: str, prefix: str) -> str:
    path = (path or "").lstrip("/")
    prefix = (prefix or "").strip("/")
    if not prefix:
        return path
    if path == prefix:
        return ""
    p = prefix + "/"
    return path[len(p):] if path.startswith(p) else ""


def _skip_hidden(entry: dict) -> bool:
    name = (entry.get("name") or "").lstrip("/")
    return bool(name) and not name.startswith(".")


def _short_hash(content: str) -> str:
    return hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]
