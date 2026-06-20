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
    kind: str            # "dataclass" | "type_alias" | "pydantic"
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
class DataStore:
    """An in-memory or persisted data store the service owns. Used by
    the enrichment pipeline to fill SD ``data-store`` page schemas
    without going through RAG.

    The POC detects three shapes:

    * **dict / list / set** module-level variables used as in-memory
      state (pear-store pattern: ``users = {}; users[id] = {...}``).
    * **SQLAlchemy ``Table(...)``** declarations (when the project
      uses SA core).
    * **CREATE TABLE** statements found in string literals.

    ``fields`` is the union of observed field/column names across all
    detection paths. ``sample_values`` carries representative initial
    values when present (a literal init dict, a ``Column("id", ...)``
    type argument, etc.) so the page composer can include type hints.
    """
    name: str
    kind: str            # "dict" | "list" | "set" | "sqlite_table" | "sqlalchemy_table"
    fields: list[str] = field(default_factory=list)
    field_types: dict[str, str] = field(default_factory=dict)
    source_path: str = ""
    line: int = 0
    sample_values: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "fields": list(self.fields),
            "field_types": dict(self.field_types),
            "source_path": self.source_path,
            "line": self.line,
            "sample_values": dict(self.sample_values),
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
    data_stores: list[DataStore] = field(default_factory=list)
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
            "data_stores": [d.to_dict() for d in self.data_stores],
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
    # Pydantic: any base named ``BaseModel`` (or ``pydantic.BaseModel``)
    # gives the same field shape as a dataclass — annotated assignments
    # in the class body are the schema.
    is_pydantic = any(
        (isinstance(b, ast.Name) and b.id == "BaseModel")
        or (isinstance(b, ast.Attribute) and b.attr == "BaseModel")
        for b in node.bases
    )
    if not is_dataclass and not is_pydantic:
        return None
    fields: list[dict[str, Any]] = []
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            fields.append({
                "name": stmt.target.id,
                "type": _annotation_text(stmt.annotation),
                "has_default": stmt.value is not None,
            })
    kind = "dataclass" if is_dataclass else "pydantic"
    return DataStructure(name=node.name, kind=kind, fields=fields, source_path=source_path)


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


# ---------------------------------------------------------------------------
# Data-store extraction — module-level state stores + SQL CREATE TABLE +
# SQLAlchemy Table(...) declarations
# ---------------------------------------------------------------------------

# Module-level names that are obviously NOT data stores: typing aliases,
# logger handles, app/route registrations, regex patterns. Skipping
# these saves the LLM compose call from inventing a "logger schema".
_NOT_A_STORE_NAMES = {
    "app", "bp", "blueprint", "router", "log", "logger", "logging",
    "config", "settings", "metadata", "engine", "Base",
    "__all__", "__version__",
}


def _is_container_literal(node: ast.AST | None) -> str | None:
    """Return ``"dict"`` / ``"list"`` / ``"set"`` if ``node`` is a
    container literal (or empty container constructor); else ``None``."""
    if isinstance(node, ast.Dict):
        return "dict"
    if isinstance(node, ast.List):
        return "list"
    if isinstance(node, ast.Set):
        return "set"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "dict":
            return "dict"
        if node.func.id == "list":
            return "list"
        if node.func.id == "set":
            return "set"
    return None


def _annotation_implies_container(node: ast.AST | None) -> str | None:
    """``users: dict[str, dict] = {}`` → ``"dict"``. Best-effort."""
    if node is None:
        return None
    text = _annotation_text(node) or ""
    head = text.split("[", 1)[0].strip().lower()
    if head in {"dict", "list", "set", "frozenset", "tuple"}:
        return head if head != "frozenset" else "set"
    return None


def _dict_literal_keys(node: ast.AST) -> list[str]:
    """Pull string-keyed field names from a dict literal. Non-string
    keys (computed) are ignored — they don't tell us a schema."""
    out: list[str] = []
    if not isinstance(node, ast.Dict):
        return out
    for k in node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            out.append(k.value)
        elif isinstance(k, ast.Str):  # pragma: no cover — pre-3.8 leftovers
            out.append(k.s)
    return out


def _dict_literal_field_types(node: ast.AST) -> dict[str, str]:
    """Best-effort type hints from dict-literal *values*: a string value
    → ``"str"``, an int → ``"int"``, etc. Used to label inferred schema
    fields when the source has no explicit type annotation."""
    out: dict[str, str] = {}
    if not isinstance(node, ast.Dict):
        return out
    for k, v in zip(node.keys, node.values):
        if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
            continue
        if isinstance(v, ast.Constant):
            t = type(v.value).__name__
            if t == "NoneType":
                t = "Optional[str]"
            out[k.value] = t
        elif isinstance(v, ast.List):
            out[k.value] = "list"
        elif isinstance(v, ast.Dict):
            out[k.value] = "dict"
        elif isinstance(v, ast.Call) and isinstance(v.func, ast.Name):
            out[k.value] = v.func.id
    return out


_CREATE_TABLE_RX = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[`\"\]]?\s*\((?P<body>.*?)\)\s*;?",
    re.IGNORECASE | re.DOTALL,
)


def _parse_create_table(sql: str) -> tuple[str, list[str], dict[str, str]] | None:
    """Tiny CREATE TABLE parser — table name + column list. Misses
    constraint clauses, but the page-composer doesn't care."""
    m = _CREATE_TABLE_RX.search(sql)
    if not m:
        return None
    name = m.group("name")
    body = m.group("body")
    cols: list[str] = []
    types: dict[str, str] = {}
    # Split on commas at depth 0 (so foreign-key clauses don't break us).
    depth = 0
    parts: list[str] = []
    cur: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip()); cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    for part in parts:
        if not part:
            continue
        # Skip table-level constraints.
        head = part.split(None, 1)[0].lower()
        if head in {"primary", "foreign", "unique", "check", "constraint", "index"}:
            continue
        toks = part.split(None, 2)
        col = toks[0].strip("`\"[]")
        col_type = toks[1] if len(toks) > 1 else "?"
        cols.append(col)
        types[col] = col_type
    return name, cols, types


def _is_sqlalchemy_table_call(node: ast.AST | None) -> bool:
    """``Table("name", metadata, Column(...), ...)`` from SA core."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Name) and f.id == "Table":
        return True
    if isinstance(f, ast.Attribute) and f.attr == "Table":
        return True
    return False


def _columns_from_sqlalchemy_table(call: ast.Call) -> tuple[list[str], dict[str, str]]:
    """Pull ``Column("name", Type, ...)`` triples out of a Table call."""
    cols: list[str] = []
    types: dict[str, str] = {}
    for arg in call.args[2:]:
        if not (isinstance(arg, ast.Call)
                and ((isinstance(arg.func, ast.Name) and arg.func.id == "Column")
                     or (isinstance(arg.func, ast.Attribute) and arg.func.attr == "Column"))):
            continue
        if not arg.args:
            continue
        first = arg.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            col = first.value
            cols.append(col)
            t = arg.args[1] if len(arg.args) > 1 else None
            if t is not None:
                if isinstance(t, ast.Name):
                    types[col] = t.id
                elif isinstance(t, ast.Call) and isinstance(t.func, ast.Name):
                    types[col] = t.func.id
                elif isinstance(t, ast.Attribute):
                    types[col] = t.attr
    return cols, types


def extract_data_stores(tree: ast.Module, source_path: str) -> list[DataStore]:
    """Walk the module AST for in-memory data stores, SQL CREATE TABLE
    statements, and SQLAlchemy ``Table(...)`` declarations. Returns a
    list of DataStore entries — duplicates across files get merged in
    ``analyze_service`` further down.
    """
    stores: dict[str, DataStore] = {}

    # Pass 1 — module-level declarations.
    for stmt in tree.body:
        # ``users = {}`` or ``users = {"a": 1}``.
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if isinstance(target, ast.Name):
                name = target.id
                if name in _NOT_A_STORE_NAMES or name.startswith("_"):
                    # Allow leading-underscore stores like _USERS — common
                    # pattern for module-private state. Re-allow:
                    if not (name.startswith("_") and name.lstrip("_").isupper()):
                        if name in _NOT_A_STORE_NAMES:
                            continue
                # SQLAlchemy ``users = Table(...)``.
                if _is_sqlalchemy_table_call(stmt.value):
                    cols, types = _columns_from_sqlalchemy_table(stmt.value)
                    table_name = (
                        stmt.value.args[0].value
                        if stmt.value.args and isinstance(stmt.value.args[0], ast.Constant)
                        and isinstance(stmt.value.args[0].value, str)
                        else name
                    )
                    stores[name] = DataStore(
                        name=table_name,
                        kind="sqlalchemy_table",
                        fields=cols,
                        field_types=types,
                        source_path=source_path,
                        line=stmt.lineno,
                    )
                    continue
                # In-memory dict/list/set state.
                kind = _is_container_literal(stmt.value)
                if kind:
                    fields = _dict_literal_keys(stmt.value) if kind == "dict" else []
                    types = _dict_literal_field_types(stmt.value) if kind == "dict" else {}
                    stores[name] = DataStore(
                        name=name,
                        kind=kind,
                        fields=fields,
                        field_types=types,
                        source_path=source_path,
                        line=stmt.lineno,
                    )
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
            if name in _NOT_A_STORE_NAMES:
                continue
            kind = (
                _is_container_literal(stmt.value)
                or _annotation_implies_container(stmt.annotation)
            )
            if kind:
                fields = (
                    _dict_literal_keys(stmt.value) if kind == "dict" and stmt.value else []
                )
                types = (
                    _dict_literal_field_types(stmt.value) if kind == "dict" and stmt.value else {}
                )
                stores[name] = DataStore(
                    name=name,
                    kind=kind,
                    fields=fields,
                    field_types=types,
                    source_path=source_path,
                    line=stmt.lineno,
                )

    # Pass 2 — observe what gets WRITTEN into each store. Catches the
    # pear-store pattern where the dict is initialized empty and
    # populated later: ``users[uid] = {"id": ..., "email": ..., ...}``.
    class _AssignWalker(ast.NodeVisitor):
        def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
            for tgt in node.targets:
                if isinstance(tgt, ast.Subscript) and isinstance(tgt.value, ast.Name):
                    sname = tgt.value.id
                    if sname in stores and isinstance(node.value, ast.Dict):
                        ds = stores[sname]
                        for f in _dict_literal_keys(node.value):
                            if f not in ds.fields:
                                ds.fields.append(f)
                        for k, v in _dict_literal_field_types(node.value).items():
                            ds.field_types.setdefault(k, v)
            self.generic_visit(node)

    _AssignWalker().visit(tree)

    # Pass 3 — string literals containing CREATE TABLE.
    class _StringWalker(ast.NodeVisitor):
        def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
            if isinstance(node.value, str) and "create table" in node.value.lower():
                parsed = _parse_create_table(node.value)
                if parsed is not None:
                    table_name, cols, types = parsed
                    key = f"sqlite::{table_name}"
                    if key not in stores:
                        stores[key] = DataStore(
                            name=table_name,
                            kind="sqlite_table",
                            fields=cols,
                            field_types=types,
                            source_path=source_path,
                            line=node.lineno,
                        )

    _StringWalker().visit(tree)

    return list(stores.values())


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
    data_stores: list[DataStore] = []
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
        # Module-level data-store extraction is a separate pass — same
        # AST, runs after the visitor so we don't have to teach
        # ``_ServiceVisitor`` about every shape (in-mem dict + SQLAlchemy
        # Table + CREATE TABLE strings have different visit hooks).
        data_stores.extend(extract_data_stores(tree, f.relative_path))

    # Merge data-stores that share a name across files (rare in services
    # with one app.py, but the reset-and-import pattern can split a
    # store across modules). Last-write-wins on field types.
    merged_stores: dict[tuple[str, str], DataStore] = {}
    for ds in data_stores:
        key = (ds.kind, ds.name)
        if key in merged_stores:
            existing = merged_stores[key]
            for f in ds.fields:
                if f not in existing.fields:
                    existing.fields.append(f)
            existing.field_types.update(ds.field_types)
        else:
            merged_stores[key] = ds
    data_stores = list(merged_stores.values())

    log.info(
        f"analyze_service service={service} endpoints={len(endpoints)} "
        f"data_structures={len(data_structures)} data_stores={len(data_stores)} "
        f"calls={len(calls)} failures={len(parse_failures)}"
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
        data_stores=data_stores,
        downstream_calls=calls,
        prose=prose,
        parse_failures=parse_failures,
        files_seen=files_seen,
    )
