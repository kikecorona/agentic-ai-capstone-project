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

from src.shared.service_log import get_logger

from .compose import PageEscalation, render_placeholder_block

log = get_logger("sd.enrich")


# ─── Rubric ────────────────────────────────────────────────────────────

# Section name → human-readable description. The LLM judge sees this
# verbatim when scoring an SD page; the order also gives us a stable
# order to APPEND missing sections in (when ``compose.merge_into_existing``
# falls back to append rather than replace-in-place).
SD_REQUIRED_SECTIONS: list[tuple[str, str]] = [
    ("Overview", "What this service is and what responsibility it owns."),
    ("Endpoints", "REST/RPC surface — methods, paths, handlers."),
    ("Downstream services", "Other services this one calls plus what for."),
    ("Data model", "Key entities + their primary attributes."),
    ("Observability", "Logs / metrics / traces this service emits."),
    ("Open Questions", "Anything unresolved that needs SME input."),
]


# ─── Gap detection ─────────────────────────────────────────────────────

@dataclass
class Gap:
    section_title: str
    why_gap: str
    fill_prompt: str


@dataclass
class GapPlan:
    gaps: list[Gap] = field(default_factory=list)
    is_substantive: bool = False


_GAP_DETECT_SYSTEM = """\
You are a documentation reviewer for SD (system design / engineering)
service pages. Judge whether each REQUIRED SECTION provides
substantive, accurate content. A section is "substantive" if it has at
least one paragraph that describes what the header implies, beyond
placeholder text (TBD, TODO, empty bullets). SME-PLACEHOLDER blocks
always count as gaps.

Respond with JSON only, no prose, in this exact shape:
{
  "gaps": [
    {
      "section_title": "<heading text>",
      "why_gap": "<one-sentence rationale>",
      "fill_prompt": "<RAG/source-code question to fill the gap>"
    }
  ],
  "is_substantive": <true if no gaps, false otherwise>
}
"""


def detect_gaps(page_content: str, *, page_title: str | None, llm) -> GapPlan:
    """Ask the LLM to judge structural completeness against the rubric.

    Returns a GapPlan with zero or more Gap entries; a non-empty list
    means the page is judged incomplete and the listed sections need
    fill content.
    """
    rubric_lines = "\n".join(
        f"- {name}: {desc}" for name, desc in SD_REQUIRED_SECTIONS
    )
    user_msg = (
        f"PAGE TITLE: {page_title or '(unknown)'}\n\n"
        f"REQUIRED SECTIONS:\n{rubric_lines}\n\n"
        f"PAGE CONTENT:\n```markdown\n{page_content[:8000]}\n```"
    )

    try:
        raw = llm.invoke([
            SystemMessage(content=_GAP_DETECT_SYSTEM),
            HumanMessage(content=user_msg),
        ])
        text = raw.content if hasattr(raw, "content") else str(raw)
    except Exception as exc:  # noqa: BLE001
        log.error(f"detect_gaps: LLM call failed: {exc}")
        return GapPlan(is_substantive=True)

    try:
        data = json.loads(_strip_code_fence(text))
    except Exception as exc:  # noqa: BLE001
        log.warn(
            f"detect_gaps: LLM returned non-JSON ({exc}); "
            f"first 120 chars: {text[:120]!r}; treating as substantive"
        )
        return GapPlan(is_substantive=True)

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
        gaps.append(
            Gap(
                section_title=title,
                why_gap=(g.get("why_gap") or "")[:200],
                fill_prompt=(g.get("fill_prompt") or title)[:500],
            )
        )
    is_substantive = bool(data.get("is_substantive", not gaps))
    log.info(
        f"detect_gaps: {len(gaps)} gap(s); is_substantive={is_substantive} "
        f"({', '.join(g.section_title for g in gaps) if gaps else 'all sections present'})"
    )
    return GapPlan(gaps=gaps, is_substantive=is_substantive and not gaps)


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
    answered_sme_blocks: dict[str, dict[str, Any]] | None = None,
) -> FilledGap:
    """Fill a single gap by retrieving from RAG, with the freshly-pulled
    source-code analysis as side-info. Falls back to an SME-PLACEHOLDER
    when retrieval is exhausted / low-confidence.

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

    # Build a query that includes a hint about the service + a
    # compact analysis summary so RAG / the LLM grader can disambiguate.
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

    section_md = f"## {gap.section_title}\n\n{answer}\n"
    log.info(
        f"fill_gap: filled {gap.section_title!r} for {page_uri} "
        f"({len(sources)} source(s))"
    )
    return FilledGap(
        gap=gap,
        section_md=section_md,
        sources=sources,
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
