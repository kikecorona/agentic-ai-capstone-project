"""Compose an SD page from a :class:`ServiceAnalysis` + the ToT winner.

Output is a Markdown page that lives under ``sd/services/`` in the docs
repo (§8.5). Every page has the same shape so readers can scan a
service-doc the same way they scan any other:

  1. **Front matter** — title, generation banner, source revision pointer.
  2. **Endpoints** — every detected route with its prose blurb.
  3. **Data structures** — dataclasses + key type-hinted shapes.
  4. **Downstream dependencies** — the winning ToT graph rendered as a
     bullet list, with runner-up edges as follow-up tasks.
  5. **Related products** — relative Markdown links to BP pages this
     service backs (resolved through ``BP_MCP.find_products_for_service``).
  6. **Open questions** — fenced §9.5.1 placeholder blocks for every
     unresolved cross-reference, dynamic route, or low-confidence ToT
     winner. The orchestrator's ``patch_page`` step replaces these
     blocks once an SME answers.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from .analyze_code import Endpoint, ServiceAnalysis
from .tot_dep_graph import DepEdge, ToTResult


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

@dataclass
class PageEscalation:
    """An open question the page surfaces inline (§9.5.1)."""
    question_id: str
    placeholder_id: str
    topic: str
    question: str
    best_guess: str | None
    page_uri: str

    def envelope(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "placeholder_id": self.placeholder_id,
            "topic": self.topic,
            "question": self.question,
            "best_guess": self.best_guess,
            "originating_page": self.page_uri,
        }


@dataclass
class ComposedPage:
    page_uri: str
    title: str
    content: str
    endpoints: list[dict[str, Any]]
    downstream_services: list[str]
    referenced_products: list[str]
    open_placeholders: list[str]
    escalations: list[PageEscalation] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Placeholder block rendering / replacement
# ---------------------------------------------------------------------------

def render_placeholder_block(esc: PageEscalation, *, asked_at: float) -> str:
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


_FENCE_RX_TPL = (
    r"<!--\s*SME-PLACEHOLDER:{qid}\s+START\s*-->"
    r".*?"
    r"<!--\s*SME-PLACEHOLDER:{qid}\s+END\s*-->"
)


def replace_placeholder_block(content: str, *, question_id: str, replacement: str) -> tuple[str, bool]:
    pattern = re.compile(_FENCE_RX_TPL.format(qid=re.escape(question_id)), re.DOTALL)
    new_content, n = pattern.subn(replacement.rstrip() + "\n", content, count=1)
    return new_content, n > 0


# ---------------------------------------------------------------------------
# Page render
# ---------------------------------------------------------------------------

def compose_page(
    *,
    page_uri: str,
    analysis: ServiceAnalysis,
    tot: ToTResult,
    related_products: list[dict[str, Any]],
    last_updated: float,
    content_hash: str,
) -> ComposedPage:
    title = analysis.service
    asked_at = last_updated

    escalations: list[PageEscalation] = []

    # ---- Endpoints section ------------------------------------------------
    endpoint_blocks: list[str] = []
    endpoint_records: list[dict[str, Any]] = []
    for ep in analysis.endpoints:
        endpoint_records.append(ep.to_dict())
        prose = analysis.prose.get(ep.key(), "").strip()
        prose_md = f"\n  {prose}" if prose else ""
        bp_label = f" (blueprint `{ep.blueprint}`)" if ep.blueprint else ""
        params_md = f", params=[{', '.join(ep.params)}]" if ep.params else ""
        return_md = f" → `{ep.return_type}`" if ep.return_type else ""
        endpoint_blocks.append(
            f"- **`{ep.method} {ep.path}`** — handler `{ep.handler}`{bp_label}{params_md}{return_md}{prose_md}"
        )
        if ep.dynamic_path:
            qid = _question_id(page_uri, f"dynamic-route-{ep.handler}", asked_at, len(escalations))
            escalations.append(PageEscalation(
                question_id=qid,
                placeholder_id=qid,
                topic=f"Dynamic route in `{ep.handler}`",
                question=(
                    f"The route `{ep.method} {ep.path}` for handler `{ep.handler}` could not "
                    "be statically resolved. What concrete path(s) does this endpoint serve?"
                ),
                best_guess=f"Source: {ep.source_path}:{ep.line_range[0]}",
                page_uri=page_uri,
            ))

    # ---- Data structures section ------------------------------------------
    ds_blocks: list[str] = []
    for ds in analysis.data_structures:
        fields_md = ", ".join(f"`{f['name']}: {f.get('type') or 'Any'}`" for f in ds.fields)
        ds_blocks.append(f"- **`{ds.name}`** ({ds.kind}, `{ds.source_path}`) — {fields_md or '(no fields)'}")

    # ---- Dependencies section --------------------------------------------
    dep_blocks: list[str] = []
    for e in tot.winner.edges:
        suffix = " · *dynamic*" if e.dynamic else ""
        handlers = ", ".join(f"`{h}`" for h in e.handlers)
        dep_blocks.append(f"- **{e.kind}** → `{e.target}` (called from {handlers}, {e.call_count} site(s)){suffix}")
        if e.dynamic:
            qid = _question_id(page_uri, f"dynamic-call-{e.kind}-{e.target}", asked_at, len(escalations))
            escalations.append(PageEscalation(
                question_id=qid,
                placeholder_id=qid,
                topic=f"Dynamic {e.kind} call to `{e.target}`",
                question=(
                    f"The {e.kind} call to `{e.target}` from {handlers} contains a dynamic "
                    "expression. What concrete target(s) does it hit at runtime?"
                ),
                best_guess=None,
                page_uri=page_uri,
            ))

    # If the ToT winner is low-confidence, raise an SME placeholder so a
    # human can pick the right graph. §9.2.3.3 final paragraph.
    if tot.low_confidence:
        qid = _question_id(page_uri, "tot-dep-graph", asked_at, len(escalations))
        escalations.append(PageEscalation(
            question_id=qid,
            placeholder_id=qid,
            topic=f"Low-confidence dependency graph for `{analysis.service}`",
            question=(
                "No ToT candidate cleared the agreement threshold. Please confirm "
                "the dependency graph or correct it."
            ),
            best_guess=f"chosen={tot.winner.label} score={tot.winner.score:.3f}",
            page_uri=page_uri,
        ))

    # ---- Related products -------------------------------------------------
    products_blocks: list[str] = []
    referenced_products: list[str] = []
    for entry in related_products or []:
        product_uri = entry.get("page_uri")
        product_label = entry.get("product") or product_uri
        role = entry.get("role")
        if not product_uri:
            continue
        referenced_products.append(product_uri)
        role_md = f" — {role}" if role else ""
        # Cross-domain link: SD page lives at sd/services/...; we link
        # back into bp/products/... via two ../ hops to keep the link
        # relative and survives a clone.
        products_blocks.append(f"- [`{product_label}`](../../{product_uri}){role_md}")

    # ---- Compose ---------------------------------------------------------
    parts: list[str] = []
    parts.append(f"# {title}\n")
    parts.append(
        f"> *Auto-generated SD page.* Service `{analysis.service}` · revision "
        f"`{analysis.source_revision}` · content hash `{content_hash[:12]}` · "
        f"last updated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(last_updated))}.\n"
    )
    parts.append("## Endpoints\n")
    parts.append("\n".join(endpoint_blocks) + "\n" if endpoint_blocks else "_None detected._\n")

    if ds_blocks:
        parts.append("## Data structures\n")
        parts.append("\n".join(ds_blocks) + "\n")

    parts.append("## Downstream dependencies\n")
    if dep_blocks:
        parts.append(f"_ToT winner: **`{tot.winner.label}`** (score {tot.winner.score:.3f})._\n")
        parts.append("\n".join(dep_blocks) + "\n")
    else:
        parts.append("_No downstream dependencies detected._\n")
    if tot.follow_ups:
        parts.append("\n*Follow-up edges* (only in runner-up graphs, candidates for next refresh):\n")
        for fu in tot.follow_ups:
            parts.append(f"- {fu}")
        parts.append("")

    parts.append("\n## Related products\n")
    if products_blocks:
        parts.append("\n".join(products_blocks) + "\n")
    else:
        parts.append("_No B&P pages currently reference this service._\n")

    if analysis.parse_failures:
        parts.append("\n## Parse failures\n")
        for pf in analysis.parse_failures:
            parts.append(f"- `{pf.source_path}` — {pf.error}")
        parts.append("")

    placeholder_blocks: list[str] = [
        render_placeholder_block(esc, asked_at=asked_at) for esc in escalations
    ]
    if placeholder_blocks:
        parts.append("\n## Open questions\n")
        parts.extend(b + "\n" for b in placeholder_blocks)

    content = "\n".join(parts).rstrip() + "\n"

    return ComposedPage(
        page_uri=page_uri,
        title=title,
        content=content,
        endpoints=endpoint_records,
        downstream_services=[e.target for e in tot.winner.edges],
        referenced_products=referenced_products,
        open_placeholders=[esc.question_id for esc in escalations],
        escalations=escalations,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _question_id(page_uri: str, candidate: str, asked_at: float, ordinal: int) -> str:
    date = time.strftime("%Y-%m-%d", time.gmtime(asked_at))
    slug = re.sub(r"[^a-z0-9]+", "-", candidate.lower()).strip("-")
    return f"Q-{date}-{slug}-{ordinal:03d}"
