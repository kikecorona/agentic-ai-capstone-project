"""B&P enrichment pipeline (§9.3 — enrich-existing flow).

Replaces the old "input doc → composed page" path. Given an existing
BP page, this module:

  1. **Detects gaps** — asks the LLM to score the page against a
     rubric of required sections (Overview / Use Cases / Capabilities /
     Integrations / Open Questions). Anything missing or non-substantive
     becomes a `Gap`.

  2. **Fills each gap** — queries RAG (`domain=both`) for relevant
     context and asks the LLM to compose a focused section. If RAG
     returns ``status=exhausted`` or ``status=low_confidence`` the gap
     is escalated as an SME-PLACEHOLDER block (§9.5).

  3. **Preserves answered SME content** — the caller can pass in
     ``answered_sme_blocks`` from the doc-index; if a gap collides with
     a previously-answered SME block, the answered prose is reused
     verbatim instead of re-asking.

The merge-back into the original page is done by ``compose.merge_into_existing``;
this module produces the per-gap filler texts and metadata, the caller
applies them to the page.
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

log = get_logger("bp.enrich")


# ─── Rubric ────────────────────────────────────────────────────────────

# BP pages are heterogeneous like SD's — `documentation/bp/` holds
# product overviews, business-cases, user-flow walks, strategy docs.
# Each kind needs different sections. Rubrics are FALLBACKS — the
# primary path asks the LLM to classify + propose sections + grade in
# a single call, with the SD doc-index as side-info; these rubrics are
# the seed shown to the LLM and the safety-net when the call fails.
BP_PAGE_RUBRICS: dict[str, list[tuple[str, str]]] = {
    "product": [
        ("Overview", "What this product is and what problem it solves."),
        ("Use Cases", "Concrete scenarios the product supports."),
        ("Capabilities", "Key features and surfaces (UI, API, integrations)."),
        ("Integrations", "Services this product depends on or integrates with."),
        ("Open Questions", "Anything unresolved that needs SME input."),
    ],
    "business-case": [
        ("Overview", "What this case is about and why it matters now."),
        ("Problem", "The user / business problem this addresses."),
        ("Stakeholders", "Who's affected, who decides, who funds."),
        ("Success metrics", "How we know it worked."),
        ("Risks", "What could go wrong + mitigations."),
        ("Open Questions", "Anything unresolved that needs SME input."),
    ],
    "flow": [
        ("Overview", "What user journey or business flow this describes."),
        ("Steps", "Step-by-step walk-through of the flow."),
        ("Triggers", "What kicks the flow off."),
        ("Decision points", "Where the flow branches and on what signal."),
        ("Open Questions", "Anything unresolved that needs SME input."),
    ],
    "strategy": [
        ("Overview", "What this strategic doc covers and the time horizon."),
        ("Goals", "Outcomes we're aiming at."),
        ("Approach", "How we'll get there."),
        ("Risks", "What threatens success + how we'll respond."),
        ("Open Questions", "Anything unresolved that needs SME input."),
    ],
    "other": [
        ("Overview", "Top-level description of what this page covers."),
        ("Details", "The substantive content — sections appropriate for the topic."),
        ("Open Questions", "Anything unresolved that needs SME input."),
    ],
}

PAGE_KINDS = tuple(BP_PAGE_RUBRICS.keys())


def classify_by_path(page_uri: str) -> str:
    """Heuristic-only classifier from the page URI. Used as the LLM
    prompt seed and as a deterministic fallback if the LLM fails."""
    p = (page_uri or "").lower()
    if "/business-cases/" in p or "/business_cases/" in p or "/cases/" in p:
        return "business-case"
    if "/flows/" in p or "/journeys/" in p or "/journey/" in p:
        return "flow"
    if "/strategy/" in p or "/strategies/" in p or "/brand/" in p:
        return "strategy"
    if "/products/" in p or "/product/" in p or "/features/" in p:
        return "product"
    return "other"


# Backwards-compat alias — the old flat list maps to the product rubric.
BP_REQUIRED_SECTIONS = BP_PAGE_RUBRICS["product"]


# ─── Gap detection ─────────────────────────────────────────────────────

@dataclass
class Gap:
    section_title: str          # heading text the gap maps to (e.g. "Use Cases")
    why_gap: str                # LLM rationale (≤ 200 chars)
    fill_prompt: str            # RAG-friendly question to seed retrieval
    # Where to look for the fill content. ``sd-mcp`` means the answer
    # is directly available via the SD MCP cross-reference (e.g.
    # which services back this product) — bypass RAG and compose from
    # the SD summary. ``rag`` means we need prose context not in SD's
    # doc-index. The LLM judge picks per gap; ``rag`` is the
    # conservative default.
    fill_strategy: str = "rag"


@dataclass
class GapPlan:
    gaps: list[Gap] = field(default_factory=list)
    is_substantive: bool = False
    # NEW — what kind of page the gap-detector decided this is. One of
    # ``product`` / ``business-case`` / ``flow`` / ``strategy`` /
    # ``other``. Logged as an OTel span attribute on ``enrich_page``.
    page_kind: str = "other"
    # Sections the LLM judged this page should have, in order. Logged
    # for telemetry / debugging.
    expected_sections: list[str] = field(default_factory=list)


_GAP_DETECT_SYSTEM = """\
You are a documentation reviewer for B&P (business & product) pages in
an org's docs repo. Pages are heterogeneous — some document a single
product or feature, others walk a business case, a user flow, or a
strategic decision. The same fixed rubric does NOT apply to all of them.

For each page you review, do four things:

1. CLASSIFY the page kind. Pick exactly ONE of:
   - "product": single product / feature page (capabilities, use cases,
     integrations).
   - "business-case": a case for / against doing something
     (problem, stakeholders, metrics, risks).
   - "flow": a user journey or business flow walk-through
     (steps, triggers, decision points).
   - "strategy": strategic / brand doc with goals + approach.
   - "other": none of the above; pick the closest neighbour.

   Use the PAGE PATH and existing headings as your strongest signal,
   followed by the body shape. The PATH-HINT below is a heuristic from
   the URI — re-classify if the content disagrees.

2. PROPOSE expected sections appropriate for the page kind, in
   meaningful order (3–6 sections). Skip sections that don't apply
   given the SD MCP side-info (e.g. don't include "Integrations" on a
   strategy doc that doesn't reference services).

3. For each expected section, decide if the existing page has
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
   FILL gaps, not rewrite human-authored content.

   For each gap, write a fill_prompt — a focused retrieval question
   (it'll be passed to a RAG retriever).

4. For each gap, choose a FILL STRATEGY:
   - "sd-mcp": the answer is directly available via the SD MCP
     cross-reference for this product (e.g. "which services back
     this product?"). The agent will compose the section from the
     SD doc-index summary WITHOUT going through RAG retrieval.
     Pick this when the gap is about service / integration mapping
     and the SD summary clearly contains the relevant referenced
     services. Don't pick it for prose-heavy sections like Overview
     or Risks.
   - "rag": the answer needs prose context (overview, business
     intent, success metrics narrative). The agent will run a RAG
     retrieve with the fill_prompt.
   When in doubt, pick "rag" — fabricating an "sd-mcp" answer when
   there's no relevant SD reference produces worse output than
   escalating to an SME.

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
      "fill_strategy": "sd-mcp" | "rag"
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
    sd_summary: dict[str, Any] | None = None,
    llm,
) -> GapPlan:
    """Classify the page kind and identify gap sections in one LLM call.

    Heuristic-by-path picks an initial page-kind from the URI; the LLM
    then re-classifies (or confirms) and proposes per-kind sections,
    skipping ones that don't apply given the SD MCP summary side-info.

    Falls back gracefully on LLM failure: a heuristic page-kind plus
    no surfaced gaps — better to produce a no-op refresh than to
    inject inappropriate placeholders.
    """
    path_hint = classify_by_path(page_uri or "")
    seed_rubric = BP_PAGE_RUBRICS.get(path_hint, BP_PAGE_RUBRICS["other"])
    seed_lines = "\n".join(f"- {name}: {desc}" for name, desc in seed_rubric)

    # Compact SD summary so it fits the prompt budget.
    if sd_summary:
        pages = sd_summary.get("pages") or []
        sd_block = (
            f"SD MCP SUMMARY (services + their referenced products):\n"
            + "\n".join(
                f"  - {p.get('service') or p.get('page_uri')!r}: refs={p.get('referenced_products') or []}"
                for p in pages[:24]
            )
            + ("" if pages else "  (empty — no SD pages indexed yet)\n")
        )
    else:
        sd_block = "SD MCP SUMMARY: (not available — list_pages call failed or returned nothing)\n"

    user_msg = (
        f"PAGE PATH: {page_uri or '(unknown)'}\n"
        f"PAGE TITLE: {page_title or '(unknown)'}\n"
        f"PATH-HINT (heuristic, may be wrong): {path_hint}\n\n"
        f"{sd_block}\n"
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
        log.error(
            f"detect_gaps: LLM call failed: {exc}; falling back to path hint without gaps"
        )
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
        # Validate fill_strategy. Downgrade "sd-mcp" to "rag" when we
        # have no SD summary to draw from — better to retrieve than
        # fabricate.
        strategy = (g.get("fill_strategy") or "rag").strip().lower()
        if strategy not in {"sd-mcp", "rag"}:
            strategy = "rag"
        if strategy == "sd-mcp" and not sd_summary:
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
    section_md: str             # ready-to-merge "## title\n\n<body>" text
    sources: list[dict[str, Any]] = field(default_factory=list)
    is_sme_placeholder: bool = False
    question_id: str | None = None


def fill_gap(
    gap: Gap,
    *,
    page_uri: str,
    page_title: str | None,
    rag,
    sd_summary: dict[str, Any] | None = None,
    answered_sme_blocks: dict[str, dict[str, Any]] | None = None,
) -> FilledGap:
    """Fill a single gap. Routes on ``gap.fill_strategy``:

    * ``"sd-mcp"`` → compose the section directly from ``sd_summary``
      via the LLM, no RAG retrieve. Used when the detector judged the
      answer is in the SD doc-index (e.g. which services back this
      product). Requires a non-empty ``sd_summary``; falls through
      to RAG if the summary is missing.
    * ``"rag"`` → run the auto-RAG retrieve loop with the fill prompt.
      Falls back to SME-PLACEHOLDER on ``low_confidence`` / ``exhausted``.

    If ``answered_sme_blocks`` carries a prior answer for the same gap
    ``question_id``, the answered prose is reused verbatim (§9.5 SME
    flow continuity).
    """
    qid = _question_id(gap, page_uri)

    # SME continuity: if this same gap was previously SME-answered,
    # reuse the answered prose verbatim instead of re-asking.
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

    # SD-MCP-first path: detector marked this gap as directly
    # answerable from the SD doc-index (e.g. service / integration
    # mapping for this product). Compose from the summary directly.
    if gap.fill_strategy == "sd-mcp" and sd_summary:
        try:
            return _fill_from_sd_summary(
                gap, qid=qid, page_uri=page_uri, page_title=page_title,
                sd_summary=sd_summary,
            )
        except Exception as exc:  # noqa: BLE001
            log.warn(
                f"fill_gap: sd-mcp composition for {gap.section_title!r} "
                f"failed ({exc}); falling through to RAG"
            )
            # Fall through to the RAG path below.

    # RAG path — auto-RAG retrieve with the fill prompt.
    try:
        result = rag.retrieve(
            query=gap.fill_prompt,
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

    # status=ok → use the RAG answer as the section body.
    section_md = f"## {gap.section_title}\n\n{answer}\n"
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


_FILL_FROM_SD_SYSTEM = """\
You write technical documentation for B&P (business & product) pages.
Given an SD MCP SUMMARY — services + the products they reference — write
a single Markdown section for a B&P page. Constraints:

* Output ONLY the section body. Do NOT include the heading; the caller
  prepends `## <section_title>` itself.
* Use ONLY facts from the SD summary. Don't invent services, products,
  or relationships. If the summary doesn't cover the section, write
  what you can and explicitly note "(further detail TBD — not in SD
  cross-reference)" rather than fabricating.
* Be concrete. When listing integrating services, use a bulleted list
  ordered by service name; mention the relationship if the summary
  records one (e.g. "backed by", "depends on").
"""


def _fill_from_sd_summary(
    gap: Gap,
    *,
    qid: str,
    page_uri: str,
    page_title: str | None,
    sd_summary: dict[str, Any],
) -> FilledGap:
    """Compose a section directly from the SD MCP summary via the LLM,
    bypassing RAG. Used when the gap-detector marked the gap as
    sd-mcp-fillable. The summary is the only source of facts; the LLM
    is system-prompted to refuse fabrication.

    Note: we fetch a sibling LLM with ``json_mode=False`` here because
    the gap-detect LLM (``json_mode=True``) would force JSON output —
    wrong for prose composition.
    """
    prose_llm = get_chat_llm(
        module="bp.enrich.compose",
        temperature=0.3,
        json_mode=False,
    )
    summary_block = json.dumps(sd_summary, indent=2, default=str)[:4000]
    user_msg = (
        f"PAGE TITLE: {page_title or '(unknown)'}\n"
        f"PAGE PATH: {page_uri}\n\n"
        f"SECTION TO WRITE: {gap.section_title}\n"
        f"SECTION INTENT: {gap.fill_prompt}\n\n"
        f"SD MCP SUMMARY:\n```json\n{summary_block}\n```"
    )
    raw = prose_llm.invoke([
        SystemMessage(content=_FILL_FROM_SD_SYSTEM),
        HumanMessage(content=user_msg),
    ])
    body = (raw.content if hasattr(raw, "content") else str(raw)).strip()
    body = re.sub(
        rf"^\s*##\s*{re.escape(gap.section_title)}\s*\n+",
        "",
        body,
        count=1,
        flags=re.IGNORECASE,
    )
    if not body:
        return _escalate(gap, qid=qid, page_uri=page_uri, best_guess=None)
    section_md = f"## {gap.section_title}\n\n{body}\n"
    log.info(
        f"_fill_from_sd_summary: composed {gap.section_title!r} for {page_uri} "
        f"[strategy=sd-mcp]"
    )
    return FilledGap(
        gap=gap,
        section_md=section_md,
        sources=[{"source_uri": "sd-mcp://list_pages", "kind": "sd-summary"}],
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
    return f"q-bp-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:10]}"


# ─── Side-info revision ────────────────────────────────────────────────

def side_info_revision(sd_summary: dict[str, Any] | None) -> str:
    """Compute a short hash summarizing the SD doc-index state used as
    side-info during enrichment. Stored on each BP page so the next
    refresh can detect "page didn't change but SD did" and re-enrich."""
    if not sd_summary:
        return ""
    canonical = json.dumps(sd_summary, sort_keys=True, default=str)
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

    The format is symmetric with ``SME-PLACEHOLDER`` fences from
    ``compose.render_placeholder_block``: when ``patch_page`` swaps a
    placeholder for an SME answer, it wraps the answer in
    ``<!-- SME-ANSWERED:<qid> START -->`` … ``END --></tt>`` so we can
    detect and preserve it on subsequent refreshes.
    """
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
    detect it on the next refresh and preserve the prose verbatim
    instead of overwriting it with newly-composed filler.

    The wrapping is the canonical signal that "this section is
    human-authored and should not be regenerated"; replacement text
    that lacks the fences will still display correctly but will get
    overwritten on the next enrichment pass.
    """
    body = (prose or "").strip()
    if not body:
        # Empty replacement → return a bare-but-valid fence pair so the
        # subsequent regex match still trips and we don't re-ask.
        body = "_(no answer recorded)_"
    return (
        f"<!-- SME-ANSWERED:{question_id} START -->\n"
        f"{body}\n"
        f"<!-- SME-ANSWERED:{question_id} END -->"
    )
