"""``analyze_code`` — the workhorse of the SD background pipeline (§9.2.3.1).

Five sub-steps in a fixed order:

  1. **pull_source** — pull the target service's tree via the SourceStore.
     Files are content-hashed; the aggregate ``source_revision`` is the
     SHA over the ordered (path, hash) pairs so re-runs over the same
     files produce the same revision.
  2. **parse_ast** — parse each ``.py`` file with the stdlib ``ast`` module
     into a uniform internal node representation.
  3. **extract_endpoints** — walk the AST for Flask ``@app.route`` and
     ``@blueprint.route`` decorators plus ``@dataclass`` definitions.
  4. **extract_calls** — pattern-match outbound calls: HTTP via
     ``requests.{verb}(url)``, DB via ``<conn>.execute(sql)`` where
     ``<conn>`` traces back to ``sqlite3.connect(...)``. Statically-
     resolvable URLs / table names become the dependency target; the
     rest are tagged ``dynamic``.
  5. **llm_augment** — for each endpoint, ask the LLM for a one-paragraph
     prose description of what the endpoint does (bounded to ~1k input
     tokens so the prompt fits comfortably in the local LLM's context).

Output: a :class:`ServiceAnalysis` blob consumed by the rest of the SD
graph (verify_telemetry, ToT dep graph, resolve_bp_links, write_doc).
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from src.shared.llm import get_chat_llm
from src.shared.service_log import get_logger

from .sources import SourceFile, SourceStore

log = get_logger("rag.sd.analyze_code")


# ---------------------------------------------------------------------------
# Output shapes
# ---------------------------------------------------------------------------

@dataclass
class Endpoint:
    method: str
    path: str
    handler: str
    params: list[str]
    return_type: str | None
    source_path: str
    line_range: tuple[int, int]
    dynamic_path: bool = False
    blueprint: str | None = None

    def key(self) -> str:
        """Stable id used as a key in :attr:`ServiceAnalysis.prose`."""
        return f"{self.method} {self.path} ({self.source_path})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "handler": self.handler,
            "params": list(self.params),
            "return_type": self.return_type,
            "source_path": self.source_path,
            "line_range": list(self.line_range),
            "dynamic_path": self.dynamic_path,
            "blueprint": self.blueprint,
        }


@dataclass
class DataStructure:
    name: str
    kind: str            # "dataclass" | "type_alias"
    fields: list[dict[str, Any]]
    source_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "fields": list(self.fields),
            "source_path": self.source_path,
        }


@dataclass
class DownstreamCall:
    from_handler: str           # the handler function name where the call lives
    kind: str                   # "http" | "db"
    target: str | None          # canonical target if statically resolvable
    raw: str                    # short raw expression text for context
    source_path: str
    line: int
    dynamic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_handler": self.from_handler,
            "kind": self.kind,
            "target": self.target,
            "raw": self.raw,
            "source_path": self.source_path,
            "line": self.line,
            "dynamic": self.dynamic,
        }


@dataclass
class ParseFailure:
    source_path: str
    error: str

    def to_dict(self) -> dict[str, str]:
        return {"source_path": self.source_path, "error": self.error}


@dataclass
class ServiceAnalysis:
    service: str
    source_revision: str
    endpoints: list[Endpoint] = field(default_factory=list)
    data_structures: list[DataStructure] = field(default_factory=list)
    downstream_calls: list[DownstreamCall] = field(default_factory=list)
    prose: dict[str, str] = field(default_factory=dict)
    parse_failures: list[ParseFailure] = field(default_factory=list)
    files_seen: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "source_revision": self.source_revision,
            "endpoints": [e.to_dict() for e in self.endpoints],
            "data_structures": [d.to_dict() for d in self.data_structures],
            "downstream_calls": [c.to_dict() for c in self.downstream_calls],
            "prose": dict(self.prose),
            "parse_failures": [p.to_dict() for p in self.parse_failures],
            "files_seen": list(self.files_seen),
        }


# ---------------------------------------------------------------------------
# Step 1: pull_source — runs through the SourceStore + computes a revision
# ---------------------------------------------------------------------------

def pull_source(store: SourceStore, service: str) -> tuple[list[SourceFile], str]:
    files = store.pull_service(service, py_only=True)
    rev_input = "|".join(f"{f.relative_path}:{f.content_hash}" for f in sorted(files, key=lambda f: f.relative_path))
    revision = hashlib.sha1(rev_input.encode("utf-8")).hexdigest()[:12]
    log.info(f"pull_source service={service} files={len(files)} revision={revision}")
    return files, revision


# ---------------------------------------------------------------------------
# Step 2-4: AST-driven extraction
# ---------------------------------------------------------------------------

_HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "options")


def _decorator_func_name(d: ast.AST) -> str | None:
    """Return ``app.route`` / ``bp.route`` / etc. for a decorator node."""
    if isinstance(d, ast.Call):
        d = d.func
    if isinstance(d, ast.Attribute):
        owner = d.value
        if isinstance(owner, ast.Name):
            return f"{owner.id}.{d.attr}"
    if isinstance(d, ast.Name):
        return d.id
    return None


def _decorator_args(d: ast.AST) -> tuple[list[ast.AST], dict[str, ast.AST]]:
    if isinstance(d, ast.Call):
        kwargs = {kw.arg: kw.value for kw in (d.keywords or []) if kw.arg}
        return list(d.args), kwargs
    return [], {}


def _string_value(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # f-string — partial, dynamic.
        parts: list[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("{…}")
        return "".join(parts) if parts else None
    return None


def _extract_methods(kwargs: dict[str, ast.AST]) -> list[str]:
    methods_node = kwargs.get("methods")
    if isinstance(methods_node, (ast.List, ast.Tuple)):
        return [
            elt.value.upper()
            for elt in methods_node.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ]
    return ["GET"]


def _extract_dataclass(node: ast.ClassDef, source_path: str) -> DataStructure | None:
    is_dataclass = any(
        isinstance(d, ast.Name) and d.id == "dataclass"
        or isinstance(d, ast.Call) and _decorator_func_name(d) == "dataclass"
        or isinstance(d, ast.Attribute) and d.attr == "dataclass"
        for d in node.decorator_list
    )
    if not is_dataclass:
        return None
    fields: list[dict[str, Any]] = []
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            fields.append({
                "name": stmt.target.id,
                "type": _annotation_text(stmt.annotation),
                "has_default": stmt.value is not None,
            })
    return DataStructure(name=node.name, kind="dataclass", fields=fields, source_path=source_path)


def _annotation_text(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 — defensive
        return None


def _function_signature(node: ast.FunctionDef) -> tuple[list[str], str | None]:
    args = [a.arg for a in node.args.args]
    return_type = _annotation_text(node.returns) if node.returns is not None else None
    return args, return_type


# ---------------------------------------------------------------------------
# AST visitors
# ---------------------------------------------------------------------------

class _ServiceVisitor(ast.NodeVisitor):
    """One pass over a single file. Records endpoints + data structures +
    a registry of names that resolve to ``sqlite3.connect(...)`` so the
    call extractor can tell DB execs from arbitrary ``execute`` calls."""

    def __init__(self, source_path: str):
        self.source_path = source_path
        self.endpoints: list[Endpoint] = []
        self.data_structures: list[DataStructure] = []
        self.calls: list[DownstreamCall] = []
        # Names assigned to sqlite3.connect(...) — both at module level and
        # within the same handler. POC scope is intentionally narrow.
        self._sqlite_conns: set[str] = set()
        # Stack so we can attribute calls back to their enclosing handler.
        self._fn_stack: list[str] = []

    # -- top-level dispatch -----------------------------------------------

    def visit_Module(self, node: ast.Module) -> None:  # noqa: N802 — ast API
        # Pre-pass for module-level ``conn = sqlite3.connect(...)``.
        for stmt in node.body:
            self._record_sqlite_conn(stmt)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        ds = _extract_dataclass(node, self.source_path)
        if ds is not None:
            self.data_structures.append(ds)
        # Don't descend into class bodies for endpoints — the spec only
        # tracks Flask route decorators on free functions.

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        # Look for ``@(app|bp).route(path, methods=[...])`` decorators.
        for d in node.decorator_list:
            owner_method = _decorator_func_name(d)
            if not owner_method:
                continue
            owner, _, method = owner_method.partition(".")
            if method != "route":
                continue
            args, kwargs = _decorator_args(d)
            path_node = args[0] if args else kwargs.get("path") or kwargs.get("rule")
            path_value = _string_value(path_node)
            dynamic_path = (path_value is None) or ("{…}" in path_value)
            if path_value is None:
                # Fallback: capture the source expression for context.
                path_value = _annotation_text(path_node) or "<dynamic>"
            params, return_type = _function_signature(node)
            line_range = (node.lineno, node.end_lineno or node.lineno)
            for verb in _extract_methods(kwargs):
                self.endpoints.append(Endpoint(
                    method=verb,
                    path=path_value,
                    handler=node.name,
                    params=params,
                    return_type=return_type,
                    source_path=self.source_path,
                    line_range=line_range,
                    dynamic_path=dynamic_path,
                    blueprint=owner if owner != "app" else None,
                ))
        # Walk the body to collect calls + per-function sqlite_conn assignments.
        self._fn_stack.append(node.name)
        for stmt in node.body:
            self._record_sqlite_conn(stmt)
        self.generic_visit(node)
        self._fn_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    # -- call extraction --------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        handler = self._fn_stack[-1] if self._fn_stack else "<module>"
        func = node.func
        # HTTP: requests.<verb>(url, ...)
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                and func.value.id == "requests" and func.attr.lower() in _HTTP_VERBS:
            url_node = node.args[0] if node.args else None
            url = _string_value(url_node)
            target = _http_target(url) if url else None
            dynamic = url is None or "{…}" in (url or "")
            self.calls.append(DownstreamCall(
                from_handler=handler,
                kind="http",
                target=target,
                raw=f"requests.{func.attr}({_brief(url_node)})",
                source_path=self.source_path,
                line=node.lineno,
                dynamic=dynamic,
            ))
        # DB: <conn>.execute(sql, ...) where <conn> traces to sqlite3.connect.
        if isinstance(func, ast.Attribute) and func.attr == "execute":
            owner = func.value
            owner_name: str | None = None
            if isinstance(owner, ast.Name):
                owner_name = owner.id
            elif isinstance(owner, ast.Call):
                # Inline: sqlite3.connect(...).execute(...)
                if _is_sqlite_connect_call(owner):
                    owner_name = "<inline>"
            if owner_name == "<inline>" or (owner_name in self._sqlite_conns):
                sql_node = node.args[0] if node.args else None
                sql = _string_value(sql_node) or ""
                target = _sql_target(sql) if sql else None
                self.calls.append(DownstreamCall(
                    from_handler=handler,
                    kind="db",
                    target=target,
                    raw=f"{owner_name or '?'}.execute({_brief(sql_node)})",
                    source_path=self.source_path,
                    line=node.lineno,
                    dynamic=(target is None),
                ))
        self.generic_visit(node)

    # -- helpers ----------------------------------------------------------

    def _record_sqlite_conn(self, stmt: ast.AST) -> None:
        """Record any ``name = sqlite3.connect(...)`` so subsequent
        ``name.execute(...)`` calls resolve to a SQLite connection."""
        if isinstance(stmt, ast.Assign):
            if not _is_sqlite_connect_call(stmt.value):
                return
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    self._sqlite_conns.add(target.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if _is_sqlite_connect_call(stmt.value):
                self._sqlite_conns.add(stmt.target.id)


def _is_sqlite_connect_call(node: ast.AST | None) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr == "connect" and isinstance(f.value, ast.Name) and f.value.id == "sqlite3"
    return False


def _http_target(url: str) -> str | None:
    """Reduce a static URL to a service host stem so the dep graph isn't
    tied to a specific path. ``http://payments-api/charge`` →
    ``payments-api``."""
    m = re.match(r"https?://([^/?#]+)", url)
    if m:
        host = m.group(1)
        # Drop port + .internal suffixes.
        host = host.split(":", 1)[0]
        return host
    if "/" in url and not url.startswith("/"):
        return url.split("/", 1)[0]
    return None


_SQL_TABLE_RX = re.compile(
    r"\b(?:from|join|into|update)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _sql_target(sql: str) -> str | None:
    m = _SQL_TABLE_RX.search(sql or "")
    return m.group(1).lower() if m else None


def _brief(node: ast.AST | None) -> str:
    if node is None:
        return "?"
    text = _annotation_text(node) or "?"
    return text if len(text) <= 60 else text[:57] + "..."


# ---------------------------------------------------------------------------
# Step 5: llm_augment — per-endpoint prose description
# ---------------------------------------------------------------------------

def _llm_augment(endpoints: list[Endpoint], file_text: dict[str, str], calls_by_handler: dict[str, list[DownstreamCall]]) -> dict[str, str]:
    """Produce one prose paragraph per endpoint. Bounded prompt size so
    the local LLM stays comfortable (§9.2.3.1 ~1k input tokens)."""
    if not endpoints:
        return {}
    llm = get_chat_llm("rag.sd.analyze_code.llm_augment", temperature=0.0, json_mode=True)
    prose: dict[str, str] = {}
    for ep in endpoints:
        body = _slice_body(file_text.get(ep.source_path, ""), ep.line_range)
        downstream = calls_by_handler.get(ep.handler, [])
        downstream_lines = "\n".join(
            f"- {c.kind} → {c.target or '(dynamic)'}  «{c.raw}»"
            for c in downstream
        ) or "(none detected)"
        prompt = (
            "Produce a JSON object {\"prose\": \"...\"} with a one-paragraph "
            "(≤80 words) plain-English description of what this endpoint does. "
            "Mention any non-obvious behaviour. Do NOT invent details that "
            "aren't in the body or calls.\n\n"
            f"ENDPOINT: {ep.method} {ep.path} (handler {ep.handler})\n"
            f"SOURCE FILE: {ep.source_path}\n"
            f"DOWNSTREAM CALLS:\n{downstream_lines}\n\n"
            f"BODY (excerpt):\n{body[:1500]}"
        )
        try:
            msg = llm.invoke([
                SystemMessage(content="You write concise endpoint summaries."),
                HumanMessage(content=prompt),
            ])
            data = json.loads(msg.content if isinstance(msg.content, str) else str(msg.content))
            prose[ep.key()] = (data.get("prose") or "").strip()
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            log.error(f"llm_augment failed for {ep.key()}: {exc}")
            prose[ep.key()] = ""
    return prose


def _slice_body(text: str, line_range: tuple[int, int]) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    lo = max(0, line_range[0] - 1)
    hi = min(len(lines), line_range[1])
    return "\n".join(lines[lo:hi])


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------

def analyze_service(
    *,
    service: str,
    store: SourceStore,
    augment: bool = True,
    files_filter: list[str] | None = None,
) -> ServiceAnalysis:
    """Run the full pipeline against a service tree.

    ``files_filter`` lets the focused-analyze-code path on the query side
    (§9.2.3) restrict analysis to a subset of files (e.g. just the file
    backing the endpoint a question is about), keeping the LLM prompt
    small.
    """
    files, revision = pull_source(store, service)
    if files_filter is not None:
        keep = set(files_filter)
        files = [f for f in files if f.relative_path in keep]
    files_seen = [f.relative_path for f in files]

    endpoints: list[Endpoint] = []
    data_structures: list[DataStructure] = []
    calls: list[DownstreamCall] = []
    file_text: dict[str, str] = {}
    parse_failures: list[ParseFailure] = []

    for f in files:
        if not f.is_python:
            continue
        file_text[f.relative_path] = f.text
        try:
            tree = ast.parse(f.text, filename=f.relative_path)
        except SyntaxError as exc:
            log.warn(f"parse_ast: {f.relative_path} skipped: {exc}")
            parse_failures.append(ParseFailure(source_path=f.relative_path, error=str(exc)))
            continue
        v = _ServiceVisitor(f.relative_path)
        v.visit(tree)
        endpoints.extend(v.endpoints)
        data_structures.extend(v.data_structures)
        calls.extend(v.calls)

    log.info(
        f"analyze_service service={service} endpoints={len(endpoints)} "
        f"data_structures={len(data_structures)} calls={len(calls)} "
        f"failures={len(parse_failures)}"
    )

    calls_by_handler: dict[str, list[DownstreamCall]] = {}
    for c in calls:
        calls_by_handler.setdefault(c.from_handler, []).append(c)

    prose: dict[str, str] = {}
    if augment and endpoints:
        prose = _llm_augment(endpoints, file_text, calls_by_handler)

    return ServiceAnalysis(
        service=service,
        source_revision=revision,
        endpoints=endpoints,
        data_structures=data_structures,
        downstream_calls=calls,
        prose=prose,
        parse_failures=parse_failures,
        files_seen=files_seen,
    )
