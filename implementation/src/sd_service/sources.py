"""Source-tree access for the SD specialist.

§9.2.3.1 ``pull_source`` reads each target service's source files via the
**GitHub MCP** in production. For the POC and the validation harness we
keep the contract narrow behind a Protocol so a local filesystem-backed
implementation works end-to-end without a GitHub PAT. The
:class:`GitHubSourceStore` placeholder marks the production target.

Service URIs are POSIX-style: ``billing-service`` for a service tree,
``billing-service/handler.py`` for a single file inside it. The store
lists every file under a service prefix recursively, content-hashed so
re-runs over the same revision are free (per §9.2.3.1's caching note).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable


@dataclass(frozen=True)
class SourceFile:
    """One file pulled from a service tree."""
    service: str
    relative_path: str       # path inside the service root, e.g. "handlers/charge.py"
    text: str
    content_hash: str

    @property
    def is_python(self) -> bool:
        return self.relative_path.endswith(".py")


@runtime_checkable
class SourceStore(Protocol):
    """Contract every source backend honours."""

    def list_services(self) -> list[str]: ...

    def list_files(self, service: str) -> list[str]: ...

    def read_file(self, service: str, relative_path: str) -> SourceFile: ...

    def pull_service(self, service: str, *, py_only: bool = True) -> list[SourceFile]: ...


class LocalSourceStore:
    """Filesystem-backed SourceStore.

    Treats every immediate subdirectory of ``root`` as a service and every
    file beneath it as part of that service's tree. Hidden files and
    ``__pycache__`` directories are skipped; the harness writes synthetic
    services straight into ``root/<service-name>/...``."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- listing ------------------------------------------------------------

    def list_services(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(
            p.name for p in self.root.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )

    def list_files(self, service: str) -> list[str]:
        svc_root = self._svc_root(service)
        if not svc_root.exists():
            return []
        out: list[str] = []
        for p in sorted(svc_root.rglob("*")):
            if not p.is_file():
                continue
            if any(part.startswith(".") or part == "__pycache__" for part in p.relative_to(svc_root).parts):
                continue
            out.append(str(p.relative_to(svc_root)).replace("\\", "/"))
        return out

    # -- reads --------------------------------------------------------------

    def read_file(self, service: str, relative_path: str) -> SourceFile:
        full = self._safe_join(self._svc_root(service), relative_path)
        text = full.read_text(encoding="utf-8")
        return SourceFile(
            service=service,
            relative_path=relative_path,
            text=text,
            content_hash=_hash(text),
        )

    def pull_service(self, service: str, *, py_only: bool = True) -> list[SourceFile]:
        out: list[SourceFile] = []
        for rel in self.list_files(service):
            if py_only and not rel.endswith(".py"):
                continue
            try:
                out.append(self.read_file(service, rel))
            except OSError:
                # Skip unreadable files — analyze_code records partial-parse
                # failures separately, but a file we can't even open isn't
                # worth blocking the rest of the pipeline.
                continue
        return out

    # -- helpers ------------------------------------------------------------

    def _svc_root(self, service: str) -> Path:
        return self._safe_join(self.root, service)

    @staticmethod
    def _safe_join(root: Path, relative: str) -> Path:
        rel = Path(relative)
        if rel.is_absolute() or any(part == ".." for part in rel.parts):
            raise ValueError(f"path must be relative, got {relative!r}")
        return (root / rel).resolve()


class GitHubSourceStore:
    """``SourceStore`` backed by the upstream GitHub MCP.

    Production target for §9.2.3.1 ``pull_source``. The repo's source
    tree lives under a configurable ``root_path`` (default
    ``services``); each immediate subdirectory beneath that is treated
    as a service the SD specialist documents.

    Reads are streamed lazily — the constructor only resolves the GitHub
    MCP tool list, no traffic against the repo until a method is called.
    Per-file content + sha are cached after the first read to avoid
    redundant ``get_file_contents`` round-trips when ``analyze_code``
    rolls through the same files twice in one refresh.
    """

    def __init__(self, *, github, root_path: str = "services"):
        self._gh = github
        self._root = root_path.strip("/")
        # (branch, full_path) → (text, sha) cache.
        self._file_cache: dict[tuple[str, str], tuple[str, str | None]] = {}

    # -- listing ------------------------------------------------------------

    def list_services(self) -> list[str]:
        entries = self._gh.list_directory(self._root)
        out: list[str] = []
        for e in entries:
            etype = (e.get("type") or "").lower()
            name = (e.get("name") or "").lstrip("/")
            if etype in ("dir", "directory", "tree") and name and not name.startswith("."):
                out.append(name)
        return sorted(out)

    def list_files(self, service: str) -> list[str]:
        gh_path = _join(self._root, service)
        files = self._gh.walk(gh_path, file_filter=_skip_hidden)
        rel: list[str] = []
        for entry in files:
            full_path = entry.get("path") or ""
            stripped = _strip_prefix(full_path, gh_path)
            if stripped:
                rel.append(stripped)
        return sorted(rel)

    # -- reads --------------------------------------------------------------

    def read_file(self, service: str, relative_path: str) -> SourceFile:
        full_path = _join(_join(self._root, service), relative_path)
        cache_key = (self._gh.branch, full_path)
        cached = self._file_cache.get(cache_key)
        if cached is not None:
            text, sha = cached
        else:
            text, sha = self._gh.read_text_file(full_path)
            self._file_cache[cache_key] = (text, sha)
        return SourceFile(
            service=service,
            relative_path=relative_path,
            text=text,
            content_hash=sha or _hash(text),
        )

    def pull_service(self, service: str, *, py_only: bool = True) -> list[SourceFile]:
        out: list[SourceFile] = []
        for rel in self.list_files(service):
            if py_only and not rel.endswith(".py"):
                continue
            try:
                out.append(self.read_file(service, rel))
            except Exception:
                # Skip unreadable / binary files — analyze_code records
                # parse failures separately, but a fetch error mid-walk
                # shouldn't kill the rest of the refresh.
                continue
        return out


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
    if not name or name.startswith(".") or name == "__pycache__":
        return False
    return True


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()
