"""SD enrichment pipeline (§9.2 — enrich-existing flow).

Replaces the old "source-code → composed page" path. Given an existing
SD page, this module:

  1. **Detects gaps** — asks the LLM to score the page against a rubric
     of required sections (Overview / Endpoints / Downstream services /
     Data model / Observability / Open Questions). Anything missing or
     non-substantive becomes a `Gap`.

  2. **Fills each gap** — for SD, the side-info is the freshly-pulled
     ``ServiceAnalysis`` of the service's source code (endpoints,
     downstream calls, file inventory) plus a focused RAG retrieve.
     Substantive answers merge in; low-confidence / exhausted retrieval
     escalates as ``SME-PLACEHOLDER`` blocks (§9.5).

  3. **Preserves answered SME content** — symmetric with BP: the
     enrichment loop reads ``answered_sme_blocks`` from the doc-index
     and detects ``SME-ANSWERED:<qid>`` fences in the page body, so
     SME-authored prose carries forward verbatim.

The merge-back into the original page is done by
``compose.merge_into_existing``; this module produces the per-gap
filler texts and metadata, the caller applies them.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from src.shared.llm import get_chat_llm
from src.shared.service_log import get_logger

from .compose import PageEscalation, render_placeholder_block

log = get_logger("sd.enrich")


# ─── Rubric ────────────────────────────────────────────────────────────

# SD pages aren't all per-service specs — the same `documentation/sd/`
# tree holds architecture overviews (service maps, design decisions),
# data-flow walks, data-store specs, and the per-service pages we
# originally rubric'd for. Each kind needs a different list of sections,
# so we classify the page first (heuristic-by-path → LLM fallback) and
# pick the matching rubric.
#
# These rubrics are FALLBACKS — the primary detection path asks the LLM
# to propose sections dynamically given the existing content + the
# source-code analysis (so a service with no HTTP code doesn't get
# pestered for an "Endpoints" section). They're used:
#   1. as the seed list shown to the LLM in the gap-detect prompt;
#   2. as the rubric when the LLM call fails / returns non-JSON.
SD_PAGE_RUBRICS: dict[str, list[tuple[str, str]]] = {
    "service": [
        ("Overview", "What this service is and what responsibility it owns."),
        ("Endpoints", "REST/RPC surface — methods, paths, handlers."),
        ("Downstream services", "Other services this one calls plus what for."),
        ("Data model", "Key entities + their primary attributes."),
        ("Observability", "Logs / metrics / traces this service emits."),
        ("Open Questions", "Anything unresolved that needs SME input."),
    ],
    "architecture-overview": [
        ("Overview", "What this slice of the system is and how it's shaped."),
        ("Service map", "Diagram or list of services + how they talk to each other."),
        ("Ownership boundaries", "What each service owns and is responsible for."),
        ("Why this shape", "Design rationale — why services are split this way."),
        ("What is not in this system", "Explicit non-goals / out-of-scope work."),
    ],
    "data-flow": [
        ("Overview", "What flow this page describes and why it matters."),
        ("Sequence", "Step-by-step or sequence-diagrammed walk-through."),
        ("Triggers", "What kicks this flow off (UI action, cron, upstream event)."),
        ("Failure modes", "What goes wrong + how the flow recovers or escalates."),
        ("Open Questions", "Anything unresolved that needs SME input."),
    ],
    "data-store": [
        ("Overview", "What this database stores and which service owns it."),
        ("Schema", "Tables / collections / fields and their types."),
        ("Access patterns", "Who reads/writes, query shapes, hot keys."),
        ("Consistency / retention", "Durability, retention, indexes, backups."),
        ("Open Questions", "Anything unresolved that needs SME input."),
    ],
    "other": [
        ("Overview", "Top-level description of what this page covers."),
        ("Details", "The substantive content — sections appropriate for the topic."),
        ("Open Questions", "Anything unresolved that needs SME input."),
    ],
}

PAGE_KINDS = tuple(SD_PAGE_RUBRICS.keys())


def classify_by_path(page_uri: str) -> str:
    """Heuristic-only classifier from the page URI. Used both to seed
    the LLM gap-detect prompt with a sensible default page-kind and as
    a deterministic fallback if the LLM call fails."""
    p = (page_uri or "").lower()
    if "/architecture/" in p or "architecture.md" in p or "/overview" in p:
        return "architecture-overview"
    if "/database/" in p or "-db.md" in p or "/data-store/" in p or "/db/" in p:
        return "data-store"
    if "/flows/" in p or "data-flow" in p or "data_flow" in p or "/flow/" in p:
        return "data-flow"
    if "/services/" in p or "/service/" in p:
        return "service"
    return "other"


# Backwards-compat alias — older imports of `SD_REQUIRED_SECTIONS`
# resolve to the per-service rubric, which matches the prior behaviour.
SD_REQUIRED_SECTIONS = SD_PAGE_RUBRICS["service"]


# ─── Gap detection ─────────────────────────────────────────────────────

@dataclass
class Gap:
    section_title: str
    why_gap: str
    fill_prompt: str
    # Where to look for the fill content. ``source`` means the answer
    # is directly extractable from the source-code analysis (endpoints,
    # downstream calls, file inventory) — bypass RAG and compose from
    # the analysis. ``rag`` means we need prose context not in the
    # code (overview, rationale, observability narrative). The LLM
    # judge picks per gap; ``rag`` is the conservative default.
    fill_strategy: str = "rag"


@dataclass
class GapPlan:
    gaps: list[Gap] = field(default_factory=list)
    is_substantive: bool = False
    # NEW — what kind of page the gap-detector decided this is. Logged
    # for visibility; used by the caller for telemetry. One of
    # ``service`` / ``architecture-overview`` / ``data-flow`` /
    # ``data-store`` / ``other``.
    page_kind: str = "other"
    # Sections the LLM judged this page should have, in order. Useful
    # for debugging when the produced gaps look wrong — gives operators
    # a window into why the LLM picked these vs other plausible ones.
    expected_sections: list[str] = field(default_factory=list)


_GAP_DETECT_SYSTEM = """\
You are a documentation reviewer for SD (system design / engineering)
pages in an org's docs repo. Pages are heterogeneous — some document a
single runtime service, others describe an architecture overview, a
data-flow walk-through, or a data store / database. The same fixed
rubric does NOT apply to all of them.

For each page you review, do three things:

1. CLASSIFY the page kind. Pick exactly ONE of:
   - "service": documents one runtime service (REST/RPC handlers, deps, data).
   - "architecture-overview": describes how multiple services compose
     (service maps, ownership boundaries, design decisions).
   - "data-flow": walks one end-to-end flow across services / actors.
   - "data-store": documents a database / cache (schema, owner, access).
   - "other": none of the above; pick the closest neighbour.

   Use the PAGE PATH and existing headings as your strongest signal,
   followed by the body shape. The PATH-HINT below is a heuristic from
   the URI — re-classify if the content disagrees.

2. PROPOSE expected sections appropriate for the page kind, in
   meaningful order (3–6 sections). Skip sections that don't apply
   given the source-code analysis (e.g., do NOT include "Endpoints"
   for an architecture-overview that orchestrates services rather
   than running HTTP itself; skip "Schema" on a service page that
   doesn't own a database).

3. For each expected section, decide if the existing page already has
   substantive content. **A section is a gap ONLY when one of these is
   true:**
     - the section heading is missing entirely from the page;
     - the section heading is present but the body is empty;
     - the body contains an SME-PLACEHOLDER fence
       (``<!-- SME-PLACEHOLDER:... -->``);
     - the body is one of the explicit placeholder markers: ``TBD``,
       ``TODO``, ``FIXME``, ``WIP``, ``N/A``, "to be filled / to be
       determined", "add (more) details", "placeholder", "(empty)".

   **DO NOT** flag a section as a gap because you think it could be
   "more substantive", "longer", "more detailed", or "better". If
   prose is already there, leave it alone — the agent's job is to
   FILL gaps, not rewrite human-authored content. Subjective judgments
   that mark filled sections as gaps cause the merger to overwrite
   real content with placeholders.

   For each gap, write a fill_prompt that's a focused retrieval question
   (it'll be passed to a RAG retriever; mention key nouns from the page
   and the source-code analysis when relevant).

4. For each gap, choose a FILL STRATEGY:
   - "source": the answer is directly available in the SOURCE-CODE
     ANALYSIS provided (e.g., endpoints, downstream calls, file
     inventory on a service page). The agent will compose the section
     from the analysis WITHOUT going through RAG retrieval. Pick this
     when the analysis genuinely contains the answer; do not pick it
     for an architecture-overview or data-flow page where the analysis
     describes only one service.
   - "rag": the answer needs prose context not in the source code
     (overview, design rationale, observability narrative, business
     intent). The agent will run a RAG retrieve with the fill_prompt.
   When in doubt, pick "rag" — fabricating a "source" answer when the
   analysis is empty produces worse output than escalating to an SME.

Respond with JSON only, no prose, in this exact shape:
{
  "page_kind": "<one of the five keys above>",
  "expected_sections": [
    {"title": "Overview", "applies_because": "<one-sentence rationale>"}
  ],
  "gaps": [
    {
      "section_title": "<heading text matching one of expected_sections>",
      "why_gap": "<one-sentence rationale>",
      "fill_prompt": "<focused retrieval question>",
      "fill_strategy": "source" | "rag"
    }
  ],
  "is_substantive": <true if no gaps, false otherwise>
}
"""


def detect_gaps(
    page_content: str,
    *,
    page_uri: str | None = None,
    page_title: str | None = None,
    analysis_summary: dict[str, Any] | None = None,
    llm,
) -> GapPlan:
    """Classify the page kind and identify gap sections in one LLM call.

    Heuristic-by-path picks an initial page-kind from the URI; the LLM
    then re-classifies (or confirms) and proposes per-kind sections,
    skipping ones that don't apply given the source-code analysis.

    Falls back gracefully on LLM failure: a heuristic page-kind plus
    the default rubric for that kind, with no gaps surfaced — better
    to produce a no-op refresh than to inject inappropriate placeholders.
    """
    path_hint = classify_by_path(page_uri or "")
    seed_rubric = SD_PAGE_RUBRICS.get(path_hint, SD_PAGE_RUBRICS["other"])
    seed_lines = "\n".join(f"- {name}: {desc}" for name, desc in seed_rubric)

    # Compact analysis summary so it fits the prompt budget.
    if analysis_summary:
        endpoints = analysis_summary.get("endpoints") or []
        downstream = analysis_summary.get("downstream_services") or []
        data_stores = analysis_summary.get("data_stores") or []
        data_structures = analysis_summary.get("data_structures") or []
        # Compact pretty-print of data stores: name (kind, n fields)
        # then field list. Useful for ``data-store`` pages where the
        # Schema section is meant to come straight from this.
        if data_stores:
            ds_lines = []
            for ds in data_stores[:12]:
                fields = ds.get("fields") or []
                types = ds.get("field_types") or {}
                if fields:
                    field_summary = ", ".join(
                        f"{f}:{types.get(f, '?')}" for f in fields[:16]
                    )
                else:
                    field_summary = "(no fields observed yet)"
                ds_lines.append(
                    f"  - {ds.get('name')} ({ds.get('kind')}, "
                    f"{len(fields)} fields): {field_summary}"
                )
            data_stores_block = "DATA STORES (from source analysis):\n" + "\n".join(ds_lines)
        else:
            data_stores_block = "DATA STORES: (none detected in source)"

        if data_structures:
            ds2_lines = []
            for d in data_structures[:8]:
                ds2_lines.append(
                    f"  - {d.get('name')} ({d.get('kind')}): "
                    + ", ".join(f.get("name", "?") for f in (d.get("fields") or [])[:12])
                )
            data_structures_block = "DATA STRUCTURES (dataclasses / pydantic):\n" + "\n".join(ds2_lines)
        else:
            data_structures_block = "DATA STRUCTURES: (none)"

        analysis_block = (
            f"SOURCE-CODE ANALYSIS:\n"
            f"  service: {analysis_summary.get('service') or '(none — non-service page)'}\n"
            f"  endpoints: "
            + (
                ", ".join(
                    f"{e.get('method', '')} {e.get('path', '')}".strip()
                    for e in endpoints[:8]
                )
                if endpoints
                else "(none — page may not document HTTP)"
            )
            + "\n"
            f"  downstream calls: "
            + (", ".join(downstream[:8]) if downstream else "(none)")
            + "\n"
            f"{data_stores_block}\n"
            f"{data_structures_block}\n"
        )
    else:
        analysis_block = "SOURCE-CODE ANALYSIS: (not available — no matching service or pull failed)\n"

    user_msg = (
        f"PAGE PATH: {page_uri or '(unknown)'}\n"
        f"PAGE TITLE: {page_title or '(unknown)'}\n"
        f"PATH-HINT (heuristic, may be wrong): {path_hint}\n\n"
        f"{analysis_block}\n"
        f"DEFAULT RUBRIC FOR HINTED KIND ({path_hint}):\n{seed_lines}\n\n"
        f"PAGE CONTENT:\n```markdown\n{page_content[:8000]}\n```"
    )

    try:
        raw = llm.invoke([
            SystemMessage(content=_GAP_DETECT_SYSTEM),
            HumanMessage(content=user_msg),
        ])
        text = raw.content if hasattr(raw, "content") else str(raw)
    except Exception as exc:  # noqa: BLE001
        log.error(f"detect_gaps: LLM call failed: {exc}; falling back to path hint without gaps")
        return GapPlan(is_substantive=True, page_kind=path_hint)

    try:
        data = json.loads(_strip_code_fence(text))
    except Exception as exc:  # noqa: BLE001
        log.warn(
            f"detect_gaps: LLM returned non-JSON ({exc}); "
            f"first 120 chars: {text[:120]!r}; falling back to path hint without gaps"
        )
        return GapPlan(is_substantive=True, page_kind=path_hint)

    page_kind = (data.get("page_kind") or path_hint).strip().lower()
    if page_kind not in PAGE_KINDS:
        page_kind = path_hint

    expected_sections_raw = data.get("expected_sections") or []
    expected_sections: list[str] = []
    for s in expected_sections_raw:
        if isinstance(s, dict):
            t = (s.get("title") or "").strip()
            if t:
                expected_sections.append(t)

    raw_gaps = data.get("gaps") or []
    gaps: list[Gap] = []
    seen_titles: set[str] = set()
    for g in raw_gaps:
        if not isinstance(g, dict):
            continue
        title = (g.get("section_title") or "").strip()
        if not title or title.lower() in seen_titles:
            continue
        seen_titles.add(title.lower())
        # Validate fill_strategy — fall back to "rag" on anything else.
        strategy = (g.get("fill_strategy") or "rag").strip().lower()
        if strategy not in {"source", "rag"}:
            strategy = "rag"
        # If the LLM picked "source" but we have no analysis to draw
        # from, downgrade to "rag" — better to retrieve than fabricate.
        if strategy == "source" and not analysis_summary:
            strategy = "rag"
        gaps.append(
            Gap(
                section_title=title,
                why_gap=(g.get("why_gap") or "")[:200],
                fill_prompt=(g.get("fill_prompt") or title)[:500],
                fill_strategy=strategy,
            )
        )
    is_substantive = bool(data.get("is_substantive", not gaps))
    log.info(
        f"detect_gaps: kind={page_kind} expected={len(expected_sections)} "
        f"gaps={len(gaps)} is_substantive={is_substantive} "
        f"({', '.join(g.section_title for g in gaps) if gaps else 'all sections present'})"
    )
    return GapPlan(
        gaps=gaps,
        is_substantive=is_substantive and not gaps,
        page_kind=page_kind,
        expected_sections=expected_sections,
    )


def _strip_code_fence(s: str) -> str:
    """Tolerate ```json ... ``` wrappings the LLM sometimes adds."""
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json|JSON)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s


# ─── Fill loop ─────────────────────────────────────────────────────────

@dataclass
class FilledGap:
    gap: Gap
    section_md: str
    sources: list[dict[str, Any]] = field(default_factory=dict)
    is_sme_placeholder: bool = False
    question_id: str | None = None


def fill_gap(
    gap: Gap,
    *,
    page_uri: str,
    page_title: str | None,
    service: str | None,
    analysis_summary: dict[str, Any] | None,
    rag,
    llm=None,
    answered_sme_blocks: dict[str, dict[str, Any]] | None = None,
) -> FilledGap:
    """Fill a single gap. Routes on ``gap.fill_strategy``:

    * ``"source"`` → compose the section directly from
      ``analysis_summary`` via the LLM, no RAG retrieve. Used when the
      detector judged the answer is in the source code (endpoints,
      downstream calls, etc.). Requires ``llm`` and ``analysis_summary``;
      falls through to RAG if either is missing.
    * ``"rag"`` → run the auto-RAG retrieve loop with the fill prompt.
      The analysis is appended as a context hint so the retriever /
      grader can disambiguate. Falls back to SME-PLACEHOLDER on
      ``low_confidence`` / ``exhausted``.

    ``answered_sme_blocks`` carries SME-authored prose forward across
    refreshes — if the same gap was previously SME-answered, the prose
    is reused verbatim instead of re-asking.
    """
    qid = _question_id(gap, page_uri)

    # SME continuity.
    if answered_sme_blocks and qid in answered_sme_blocks:
        prose = answered_sme_blocks[qid].get("prose") or ""
        if prose.strip():
            log.info(f"fill_gap: reusing SME-answered prose for {qid}")
            return FilledGap(
                gap=gap,
                section_md=f"## {gap.section_title}\n\n{prose.strip()}\n",
                sources=[],
                is_sme_placeholder=False,
                question_id=qid,
            )

    # Source-first path: detector marked this gap as directly
    # answerable from the source-code analysis. Compose the section
    # from the analysis without going through RAG.
    if gap.fill_strategy == "source" and analysis_summary and llm is not None:
        try:
            return _fill_from_analysis(
                gap, qid=qid, page_uri=page_uri, page_title=page_title,
                service=service, analysis_summary=analysis_summary, llm=llm,
            )
        except Exception as exc:  # noqa: BLE001
            log.warn(
                f"fill_gap: source-first composition for {gap.section_title!r} "
                f"failed ({exc}); falling through to RAG"
            )
            # Fall through to the RAG path below.

    # RAG path — build a query with the analysis as a context hint so
    # the retriever / grader can disambiguate.
    hint_lines = []
    if service:
        hint_lines.append(f"Service: {service}")
    if analysis_summary:
        endpoints = analysis_summary.get("endpoints") or []
        downstream = analysis_summary.get("downstream_services") or []
        if endpoints:
            hint_lines.append(
                "Endpoints: "
                + ", ".join(
                    f"{e.get('method', '')} {e.get('path', '')}"
                    for e in endpoints[:6]
                )
            )
        if downstream:
            hint_lines.append("Downstream calls: " + ", ".join(downstream[:8]))
    contextual_query = (
        gap.fill_prompt
        + (("\n\nContext:\n" + "\n".join(hint_lines)) if hint_lines else "")
    )

    try:
        result = rag.retrieve(
            query=contextual_query,
            domain_filter="both",
            mode="background",
        )
    except Exception as exc:  # noqa: BLE001
        log.error(f"fill_gap: rag.retrieve failed for {gap.section_title!r}: {exc}")
        return _escalate(gap, qid=qid, page_uri=page_uri, best_guess=None)

    status = result.get("status", "exhausted")
    answer = (result.get("answer") or "").strip()
    sources = result.get("sources") or []

    if status == "exhausted" or not answer:
        return _escalate(gap, qid=qid, page_uri=page_uri, best_guess=None)
    if status == "low_confidence":
        return _escalate(gap, qid=qid, page_uri=page_uri, best_guess=answer)

    from src.shared.citations import link_citations

    linked_answer = link_citations(answer, sources=sources, page_uri=page_uri)
    section_md = f"## {gap.section_title}\n\n{linked_answer}\n"
    log.info(
        f"fill_gap: filled {gap.section_title!r} for {page_uri} "
        f"({len(sources)} source(s)) [strategy=rag]"
    )
    return FilledGap(
        gap=gap,
        section_md=section_md,
        sources=sources,
        is_sme_placeholder=False,
        question_id=qid,
    )


_FILL_FROM_ANALYSIS_SYSTEM = """\
You write technical documentation for SD (system design) pages.
Given a SOURCE-CODE ANALYSIS — endpoints, downstream calls, data shapes
extracted directly from the service's source — write a single Markdown
section for an SD page. Constraints:

* Output ONLY the section body. Do NOT include the heading; the caller
  prepends `## <section_title>` itself.
* Start with the substantive content. Do NOT begin with phrases like
  "Based on the analysis", "Here are the", "According to the source",
  "The following section describes", or any similar framing — the
  reader sees the body inline in the page, not as a chat reply.
* Do NOT end with meta-commentary ("Note that the analysis does not
  cover...", "It is worth mentioning that..."). Either include the
  fact in the body or omit it.
* Use ONLY facts from the analysis. Don't invent endpoints, methods, or
  downstream services. If the analysis is empty or doesn't cover the
  section, write what you can and explicitly note "(further detail
  TBD — not in source-code analysis)" rather than fabricating.
* Be concrete and structural — when documenting endpoints, use a
  Markdown table with columns Method / Path / Handler. When documenting
  downstream services, use a bulleted list ordered by name. Keep prose
  tight; the section is rendered alongside diagrams the page may
  already have.
"""


def _fill_from_analysis(
    gap: Gap,
    *,
    qid: str,
    page_uri: str,
    page_title: str | None,
    service: str | None,
    analysis_summary: dict[str, Any],
    llm,
) -> FilledGap:
    """Compose a section directly from the source-code analysis via the
    LLM, bypassing RAG. Used when the gap-detector marked the gap as
    source-fillable. The analysis is the only source of facts; the LLM
    is system-prompted to refuse fabrication.

    Note: the ``llm`` arg passed in is the gap-detect LLM
    (``json_mode=True``) which would force the response to be JSON —
    wrong for prose composition. We fetch a sibling instance with
    ``json_mode=False`` here. Both share the LoggedLLM audit channel
    so the two calls show up separately in the X-Ray.
    """
    prose_llm = get_chat_llm(
        module="sd.enrich.compose",
        temperature=0.3,
        json_mode=False,
    )
    analysis_block = json.dumps(analysis_summary, indent=2, default=str)[:4000]
    user_msg = (
        f"PAGE TITLE: {page_title or '(unknown)'}\n"
        f"PAGE PATH: {page_uri}\n"
        f"SERVICE: {service or '(unknown)'}\n\n"
        f"SECTION TO WRITE: {gap.section_title}\n"
        f"SECTION INTENT: {gap.fill_prompt}\n\n"
        f"SOURCE-CODE ANALYSIS:\n```json\n{analysis_block}\n```"
    )
    raw = prose_llm.invoke([
        SystemMessage(content=_FILL_FROM_ANALYSIS_SYSTEM),
        HumanMessage(content=user_msg),
    ])
    body = (raw.content if hasattr(raw, "content") else str(raw)).strip()
    # Defensive — strip any heading the model might have prepended.
    body = re.sub(
        rf"^\s*##\s*{re.escape(gap.section_title)}\s*\n+",
        "",
        body,
        count=1,
        flags=re.IGNORECASE,
    )
    if not body:
        # LLM gave us nothing useful — escalate rather than commit a
        # blank section.
        return _escalate(gap, qid=qid, page_uri=page_uri, best_guess=None)

    section_md = f"## {gap.section_title}\n\n{body}\n"
    log.info(
        f"_fill_from_analysis: composed {gap.section_title!r} for {page_uri} "
        f"[strategy=source]"
    )
    return FilledGap(
        gap=gap,
        section_md=section_md,
        # Source: the analysis itself. We surface a single synthetic
        # "source" entry pointing at the source-tree so downstream
        # citation rendering has something to anchor on.
        sources=[{
            "source_uri": f"source-code://{service}" if service else "source-code://(unknown)",
            "kind": "analysis",
        }],
        is_sme_placeholder=False,
        question_id=qid,
    )


def _escalate(
    gap: Gap, *, qid: str, page_uri: str, best_guess: str | None
) -> FilledGap:
    """Render an SME-PLACEHOLDER section for a gap we couldn't fill."""
    esc = PageEscalation(
        question_id=qid,
        placeholder_id=qid,
        topic=gap.section_title,
        question=gap.fill_prompt,
        best_guess=best_guess,
        page_uri=page_uri,
    )
    block = render_placeholder_block(esc, asked_at=time.time())
    section_md = f"## {gap.section_title}\n\n{block}\n"
    log.info(
        f"_escalate: SME-PLACEHOLDER {qid} for {page_uri} "
        f"section={gap.section_title!r} best_guess={'yes' if best_guess else 'no'}"
    )
    return FilledGap(
        gap=gap,
        section_md=section_md,
        sources=[],
        is_sme_placeholder=True,
        question_id=qid,
    )


def _question_id(gap: Gap, page_uri: str) -> str:
    """Stable id derived from the gap → so the same gap on subsequent
    refreshes maps to the same SME question (and the orchestrator can
    dedup against the existing pending_sme_questions row)."""
    seed = f"{page_uri}::{gap.section_title.lower()}"
    return f"q-sd-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:10]}"


# ─── Side-info revision ────────────────────────────────────────────────

def side_info_revision(analysis_summary: dict[str, Any] | None) -> str:
    """Compute a short hash summarising the source-code analysis used
    as side-info during enrichment. Stored on each SD page so the next
    refresh can detect "page didn't change but the underlying code did"
    and re-enrich."""
    if not analysis_summary:
        return ""
    canonical = json.dumps(analysis_summary, sort_keys=True, default=str)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


# ─── Detection of answered-SME blocks in existing page content ────────

_SME_ANSWERED_RE = re.compile(
    r"<!--\s*SME-ANSWERED:(?P<qid>[a-zA-Z0-9_-]+)\s+START\s*-->"
    r"(?P<body>.*?)"
    r"<!--\s*SME-ANSWERED:(?P=qid)\s+END\s*-->",
    re.DOTALL,
)


def extract_answered_sme_blocks(page_content: str) -> dict[str, dict[str, Any]]:
    """Pull out any pre-existing ``SME-ANSWERED:<qid>`` blocks so the
    enrichment pass can preserve them verbatim across refreshes.
    Symmetric with the BP-side helper of the same name."""
    out: dict[str, dict[str, Any]] = {}
    for m in _SME_ANSWERED_RE.finditer(page_content or ""):
        qid = m.group("qid")
        prose = (m.group("body") or "").strip()
        if not qid or not prose:
            continue
        out[qid] = {
            "hash": hashlib.sha1(prose.encode("utf-8")).hexdigest()[:12],
            "prose": prose,
        }
    return out


def wrap_sme_answer(*, question_id: str, prose: str) -> str:
    """Wrap an SME reply in matching ``SME-ANSWERED:question_id`` fences
    so :func:`extract_answered_sme_blocks` (and the enrichment loop)
    detect it on the next refresh and preserve the prose verbatim."""
    body = (prose or "").strip()
    if not body:
        body = "_(no answer recorded)_"
    return (
        f"<!-- SME-ANSWERED:{question_id} START -->\n"
        f"{body}\n"
        f"<!-- SME-ANSWERED:{question_id} END -->"
    )
