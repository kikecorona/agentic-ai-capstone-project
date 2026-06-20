"""Compose a B&P page from a normalised input doc + cross-references.

The output of every refresh is a Markdown page that lives in the BP
folder of the docs repo (§8.5 page storage). Per §9.3 the page must:

  * be a *generated* product/feature page derived from the org's input
    docs (not just a verbatim copy);
  * carry **inline cross-references** to SD pages — relative Markdown
    links resolved through the SD MCP at write time;
  * include **fenced placeholder blocks** (§9.5.1 format) for any
    cross-reference SD couldn't resolve, so the gap is visible to readers
    and machine-locatable for the orchestrator's ``patch_page`` step.

Composition has three concerns:

  1. **Service candidates** — which services does the input doc imply?
     Driven by an LLM pass on the doc; keyword fallback when the LLM is
     unreachable so the rest of the pipeline still produces *something*.
  2. **Cross-reference resolution** — for each candidate, ask the SD MCP
     for the canonical service. Resolved → inline link. Unresolved →
     fenced placeholder + an escalation envelope returned upstream.
  3. **Markdown render** — title, body, "Related services" section,
     placeholder blocks at the bottom. Deterministic so two consecutive
     refreshes over the same input + SD state produce identical output
     (clean Git diffs).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from src.shared.llm import get_chat_llm
from src.shared.service_log import get_logger

from .clients import SDClient

log = get_logger("rag.bp.compose")


# ---------------------------------------------------------------------------
# Service candidate extraction
# ---------------------------------------------------------------------------

# Cheap regex fallback used when the LLM is unreachable or returns garbage.
# Matches plain ``foo-service`` or ``foo_service`` tokens anywhere in the doc.
_SERVICE_RX = re.compile(r"\b([a-z][a-z0-9]+(?:[-_][a-z0-9]+)*-service)\b", re.IGNORECASE)


def extract_service_candidates(doc: str, *, fallback_only: bool = False) -> list[str]:
    """Return a deduped list of plausible service names mentioned in the doc.

    LLM-first because the input docs are written by humans and use
    arbitrary naming; regex-fallback so the pipeline never blocks on an
    Ollama hiccup.
    """
    fallback = sorted({m.group(1).lower() for m in _SERVICE_RX.finditer(doc)})
    if fallback_only:
        return fallback

    llm = get_chat_llm("rag.bp.compose.service_candidates", temperature=0.0, json_mode=True)
    prompt = (
        "Read the document below and produce a JSON object with a single key "
        "'services' whose value is a list of every backend service name the "
        "document mentions or implies. Use the canonical kebab-case form, "
        "e.g. 'billing-service'. If you cannot find any, return an empty list.\n\n"
        "Return ONLY: {\"services\": [\"...\"]}\n\n"
        f"DOCUMENT:\n{doc}"
    )
    try:
        msg = llm.invoke([SystemMessage(content="You extract canonical service references."), HumanMessage(content=prompt)])
        data = json.loads(msg.content if isinstance(msg.content, str) else str(msg.content))
        names = [str(s).strip().lower() for s in data.get("services", []) if str(s).strip()]
        # Always merge in the regex fallback so a too-conservative LLM can't
        # silently drop a service that's clearly named in the source.
        merged = sorted(set(names) | set(fallback))
        return merged
    except Exception as exc:  # noqa: BLE001 — never crash compose on LLM hiccup
        log.error(f"service candidate extraction failed, falling back to regex: {exc}")
        return fallback


# ---------------------------------------------------------------------------
# Cross-reference resolution
# ---------------------------------------------------------------------------

@dataclass
class ResolvedReference:
    """Result of asking SD about one product/service candidate."""
    candidate: str
    resolved: bool
    page_uri: str | None = None     # populated when resolved=True
    role: str | None = None
    note: str | None = None         # populated when resolved=False (escalation reason)


def resolve_sd_links(*, product_id: str | None, candidates: list[str], sd: SDClient) -> list[ResolvedReference]:
    """Resolve each candidate against the SD MCP.

    Two strategies:
      * **By product** — if the BP page maps to a known ``product_id``,
        ask SD ``find_services_for_product(product_id)`` once and merge
        with the candidate list.
      * **By name** — for any candidate not covered by the product
        lookup, do a per-name resolution through ``get_page`` (or the
        future SD-side name resolver) and fall back to escalation if SD
        has no record.
    """
    by_service: dict[str, dict[str, Any]] = {}
    if product_id:
        try:
            for entry in sd.find_services_for_product(product_id) or []:
                svc = entry.get("service")
                if svc:
                    by_service[str(svc).lower()] = entry
        except Exception as exc:  # noqa: BLE001
            log.error(f"resolve_sd_links: find_services_for_product({product_id!r}) failed: {exc}")

    out: list[ResolvedReference] = []
    seen: set[str] = set()
    for cand in candidates:
        key = cand.lower()
        if key in seen:
            continue
        seen.add(key)
        match = by_service.get(key)
        if match:
            out.append(ResolvedReference(
                candidate=cand,
                resolved=True,
                page_uri=match.get("page_uri"),
                role=match.get("role"),
            ))
            continue
        # Unresolved through the product lookup — try get_page for an SD page
        # named after the service. SD will eventually expose a name resolver;
        # for the POC we just record the gap.
        out.append(ResolvedReference(
            candidate=cand,
            resolved=False,
            note=f"SD has no entry for {cand!r}",
        ))

    return out


# ---------------------------------------------------------------------------
# Page render
# ---------------------------------------------------------------------------

@dataclass
class PageEscalation:
    """An unresolved gap that becomes an SME escalation envelope."""
    question_id: str
    placeholder_id: str
    topic: str
    question: str
    best_guess: str | None
    page_uri: str


@dataclass
class ComposedPage:
    page_uri: str
    title: str
    content: str
    referenced_services: list[str]
    open_placeholders: list[str]
    escalations: list[PageEscalation] = field(default_factory=list)


def render_placeholder_block(esc: PageEscalation, *, asked_at: float) -> str:
    """Render a §9.5.1 placeholder block. The HTML-comment fences carry
    the ``question_id`` so the orchestrator's ``patch_page`` step can find
    and replace the block deterministically."""
    asked = time.strftime("%Y-%m-%d", time.gmtime(asked_at))
    best = esc.best_guess or "(none)"
    return (
        f"<!-- SME-PLACEHOLDER:{esc.question_id} START -->\n"
        f"> ⏳ **Waiting for SME** — *Topic:* {esc.topic}\n"
        f">\n"
        f"> *Question:* {esc.question}\n"
        f"> *Best guess (low-confidence):* {best}\n"
        f"> *Asked:* on {asked} · *Status:* pending · *Question ID:* `{esc.question_id}`\n"
        f"<!-- SME-PLACEHOLDER:{esc.question_id} END -->"
    )


def compose_page(
    *,
    page_uri: str,
    title: str | None,
    source_uri: str,
    body: str,
    references: list[ResolvedReference],
    last_updated: float,
    content_hash: str,
) -> ComposedPage:
    """Render the BP page Markdown.

    The page has four sections:

      1. **Front matter** — title, generation banner, source pointer.
      2. **Body** — the normalised input doc verbatim. Keeping the body
         verbatim (rather than re-summarising) makes the audit obvious:
         what the agent published is what the org wrote, plus annotations.
      3. **Related services** — resolved SD links.
      4. **Open placeholders** — fenced blocks for unresolved references.
    """
    title = title or page_uri.rsplit("/", 1)[-1]

    resolved = [r for r in references if r.resolved]
    unresolved = [r for r in references if not r.resolved]

    referenced_services = sorted({r.candidate for r in resolved})

    # Build escalations + placeholder blocks for every unresolved reference.
    escalations: list[PageEscalation] = []
    placeholder_blocks: list[str] = []
    open_placeholders: list[str] = []
    asked_at = last_updated
    for i, ref in enumerate(unresolved):
        qid = _question_id(page_uri=page_uri, candidate=ref.candidate, asked_at=asked_at, ordinal=i)
        esc = PageEscalation(
            question_id=qid,
            placeholder_id=qid,
            topic=f"Cross-reference to {ref.candidate}",
            question=(
                f"Does the service `{ref.candidate}` exist in System Design? "
                f"If so, which page documents it?"
            ),
            best_guess=ref.note,
            page_uri=page_uri,
        )
        escalations.append(esc)
        placeholder_blocks.append(render_placeholder_block(esc, asked_at=asked_at))
        open_placeholders.append(qid)

    # Render markdown.
    parts: list[str] = []
    parts.append(f"# {title}\n")
    parts.append(
        f"> *Auto-generated B&P page.* Source: `{source_uri}` · content hash "
        f"`{content_hash[:12]}` · last updated "
        f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(last_updated))}.\n"
    )
    parts.append("## Overview\n")
    parts.append(body.strip() + "\n")

    parts.append("## Related services\n")
    if resolved:
        for r in resolved:
            link = r.page_uri or ""
            label = f"`{r.candidate}`"
            role = f" — {r.role}" if r.role else ""
            if link:
                # Cross-domain links land at /sd/... in the docs repo; we keep
                # them relative so they survive a clone or repo move.
                parts.append(f"- [{label}](../../{link}){role}\n")
            else:
                parts.append(f"- {label}{role}\n")
    else:
        parts.append("_None resolved through SD at this time._\n")

    if placeholder_blocks:
        parts.append("\n## Open questions\n")
        parts.extend(b + "\n" for b in placeholder_blocks)

    content = "\n".join(parts).rstrip() + "\n"

    return ComposedPage(
        page_uri=page_uri,
        title=title,
        content=content,
        referenced_services=referenced_services,
        open_placeholders=open_placeholders,
        escalations=escalations,
    )


# ---------------------------------------------------------------------------
# Placeholder replacement (used by patch_page)
# ---------------------------------------------------------------------------

_FENCE_RX_TPL = (
    r"<!--\s*SME-PLACEHOLDER:{qid}\s+START\s*-->"
    r".*?"
    r"<!--\s*SME-PLACEHOLDER:{qid}\s+END\s*-->"
)


def replace_placeholder_block(content: str, *, question_id: str, replacement: str) -> tuple[str, bool]:
    """Find the fenced ``SME-PLACEHOLDER:question_id`` block and replace it.

    Returns ``(new_content, replaced)``; ``replaced=False`` means the
    fence wasn't found, so the orchestrator can flag a stale entry.
    """
    pattern = re.compile(_FENCE_RX_TPL.format(qid=re.escape(question_id)), re.DOTALL)
    new_content, n = pattern.subn(replacement.rstrip() + "\n", content, count=1)
    return new_content, n > 0


# ---------------------------------------------------------------------------
# §9.3 enrichment — merge filled gap-sections into an existing page
# ---------------------------------------------------------------------------

def merge_into_existing(
    *,
    existing_content: str,
    sections: list[tuple[str, str]],
    open_questions_anchor: str = "Open Questions",
) -> str:
    """Merge a list of ``(section_title, section_md)`` blocks into an
    existing Markdown page.

    For each entry:
      * If a top-level heading (``## <section_title>``) is already
        present, the body up to the next ``## ``-level heading (or
        end-of-document) is replaced with the new ``section_md``.
      * Otherwise the new section is appended **before** the
        ``Open Questions`` section if one exists, else at the end.

    The new ``section_md`` is expected to start with its own
    ``## <heading>\\n\\n``; the function strips and re-renders the
    heading itself to keep the inserted markup canonical.
    """
    out = existing_content if existing_content.endswith("\n") else existing_content + "\n"
    for title, section_md in sections:
        # Normalise: strip leading "## title" from the incoming block
        # so we can re-emit a canonical heading and avoid duplicates.
        body = _strip_leading_heading(section_md, title).strip()
        canonical = f"## {title}\n\n{body}\n"
        out = _replace_or_insert_section(
            out,
            heading=title,
            canonical=canonical,
            anchor_before=open_questions_anchor,
        )
    return out


def _strip_leading_heading(md: str, expected_title: str) -> str:
    pat = re.compile(
        rf"^\s*##\s+{re.escape(expected_title)}\s*\n+",
        re.IGNORECASE,
    )
    return pat.sub("", md, count=1)


# Heuristic for "this section's body is empty / a placeholder / a TODO,
# so it's safe to overwrite during enrichment." A section with
# substantive prose is NEVER overwritten — the agent might mistakenly
# flag a complete section as a gap, and we can't let that delete
# human-authored content.
_SME_FENCE_RX = re.compile(
    r"<!--\s*SME-PLACEHOLDER:[a-zA-Z0-9_-]+\s+START\s*-->.*?"
    r"<!--\s*SME-PLACEHOLDER:[a-zA-Z0-9_-]+\s+END\s*-->",
    re.DOTALL | re.IGNORECASE,
)
_BOILERPLATE_LINE_RX = re.compile(
    r"^\s*(?:[-*>]\s*)?(?:"
    r"tbd|todo|fixme|wip|n/?a|"
    r"to be (?:filled|determined|added|written|completed)\.?|"
    r"add (?:more )?details(?: about this(?: here)?)?\.?|"
    r"more details to come\.?|"
    r"placeholder\.?|"
    r"\(empty\)|\(none\)"
    r")\s*\.?\s*$",
    re.IGNORECASE,
)


def _section_body_is_fillable(body: str) -> bool:
    """Return True when ``body`` is empty / only TBD / only TODO /
    only SME-PLACEHOLDER blocks — i.e. the section is genuinely a gap
    we can safely overwrite. Returns False when the body has any
    substantive prose; in that case the merger keeps the existing
    content untouched even if the gap-detector tagged the section as
    needing fill (defence against destructive false positives).
    """
    s = (body or "").strip()
    if not s:
        return True
    # Strip all SME-PLACEHOLDER blocks; if nothing else is left, fillable.
    s = _SME_FENCE_RX.sub("", s).strip()
    if not s:
        return True
    # Strip HTML comments so a sole `<!-- TODO -->` doesn't count as prose.
    s = re.sub(r"<!--.*?-->", "", s, flags=re.DOTALL).strip()
    if not s:
        return True
    # Drop boilerplate marker lines; if substantive text remains, NOT fillable.
    keep = []
    for line in s.splitlines():
        if not line.strip():
            continue
        if _BOILERPLATE_LINE_RX.match(line):
            continue
        keep.append(line)
    return not keep


def _replace_or_insert_section(
    page: str,
    *,
    heading: str,
    canonical: str,
    anchor_before: str,
) -> str:
    """Replace an existing ``## heading`` block or insert a new one.

    **Safety rule** (added after a destructive incident where a buggy
    gap-detector flagged substantive sections as gaps and the merge
    overwrote them): if the existing section already has prose,
    refuse to replace and return the page unchanged. Only sections
    whose bodies pass ``_section_body_is_fillable`` (empty / TBD /
    SME-PLACEHOLDER) get overwritten.
    """
    # Use look-ahead to capture body up to the next "## " heading.
    pattern = re.compile(
        rf"(^|\n)##\s+{re.escape(heading)}\s*\n.*?(?=\n##\s+|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    m = pattern.search(page)
    if m:
        whole_block = m.group(0)
        # Strip the heading line to recover just the body for the
        # fillable check.
        body_only = re.sub(
            rf"^.*?##\s+{re.escape(heading)}\s*\n",
            "",
            whole_block,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not _section_body_is_fillable(body_only):
            log.info(
                f"merge_into_existing: section '{heading}' has substantive content; "
                "skipping merge to preserve existing prose"
            )
            return page
        return pattern.sub(lambda m: m.group(1) + canonical.rstrip(), page, count=1)
    # Not present → insert before "## Open Questions" if it exists.
    anchor_re = re.compile(
        rf"(^|\n)##\s+{re.escape(anchor_before)}\s*\n",
        re.IGNORECASE,
    )
    m = anchor_re.search(page)
    if m:
        idx = m.start() + len(m.group(1))
        return page[:idx] + canonical + "\n" + page[idx:]
    # Fallback: append at the end, ensuring a single blank line gap.
    sep = "" if page.endswith("\n\n") else ("\n" if page.endswith("\n") else "\n\n")
    return page + sep + canonical


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _question_id(*, page_uri: str, candidate: str, asked_at: float, ordinal: int) -> str:
    """Stable-ish ``question_id`` for placeholder blocks.

    Same page + same candidate produce the same id within a refresh
    cycle, which keeps Git diffs sane when only metadata changed.
    """
    date = time.strftime("%Y-%m-%d", time.gmtime(asked_at))
    slug = re.sub(r"[^a-z0-9]+", "-", candidate.lower()).strip("-")
    return f"Q-{date}-{slug}-{ordinal:03d}"
