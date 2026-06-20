"""Routing helpers — deterministic for the POC, easy to swap for an
LLM-driven ``reason`` step later.

The Orchestrator is the supervisor (§9.4); routing is one of the few
decisions it actually owns. Three routing questions come up:

  * **Where does a Portal query go?** — BP, SD, or both. Done by simple
    keyword classification: queries mentioning service-shaped tokens
    lean SD; product/feature/owner-shaped tokens lean BP; ambiguous
    queries fan out to both. The architecture's ``domain_hint`` envelope
    seeds the answer when the caller already knows.
  * **Where does a refresh event go?** — derived from the source path:
    inputs under ``inputs/`` or ``business-cases/`` are BP territory;
    files under ``services/`` or ``src/`` are SD; ambiguous events fan
    out to both.
  * **Which specialist owns a given page?** — by URI prefix. ``bp/...``
    means BP, ``sd/...`` means SD. Used during ``ingest_sme_reply`` to
    route ``patch_page`` calls back to the right specialist.

These are *defaults*. The Orchestrator's caller can always override the
target on a per-request basis (the REST envelope's ``domain_hint``
overrides the Portal-query classifier).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DispatchTarget:
    bp: bool
    sd: bool

    @property
    def label(self) -> str:
        if self.bp and self.sd:
            return "both"
        if self.bp:
            return "bp"
        if self.sd:
            return "sd"
        return "none"

    def to_domain_filter(self) -> str:
        return self.label if self.label in {"bp", "sd", "both"} else "both"


_BP_HINTS = (
    "product", "products", "feature", "features", "owner", "owners",
    "business", "marketing", "team", "roadmap",
)
_SD_HINTS = (
    "service", "services", "endpoint", "endpoints", "api", "apis",
    "downstream", "upstream", "dependency", "dependencies",
    "schema", "database", "queue",
)
_SERVICE_RX = re.compile(r"\b[a-z][a-z0-9]+(?:[-_][a-z0-9]+)*-service\b", re.IGNORECASE)


def pick_dispatch_target_for_query(
    query: str,
    *,
    domain_hint: str | None = None,
) -> DispatchTarget:
    """Classify a Portal query into BP / SD / both.

    Caller-supplied ``domain_hint`` (``bp`` / ``sd`` / ``both``) wins
    when present. Otherwise we lean on a tiny keyword set + the
    ``foo-service`` regex; if neither side dominates we fan out to both.
    """
    if domain_hint == "bp":
        return DispatchTarget(bp=True, sd=False)
    if domain_hint == "sd":
        return DispatchTarget(bp=False, sd=True)
    if domain_hint == "both":
        return DispatchTarget(bp=True, sd=True)

    text = (query or "").lower()
    bp_score = sum(1 for w in _BP_HINTS if w in text)
    sd_score = sum(1 for w in _SD_HINTS if w in text)
    if _SERVICE_RX.search(text):
        sd_score += 1

    if bp_score and not sd_score:
        return DispatchTarget(bp=True, sd=False)
    if sd_score and not bp_score:
        return DispatchTarget(bp=False, sd=True)
    # Either both signals or neither — fan out so the answer is grounded
    # in whatever the index has.
    return DispatchTarget(bp=True, sd=True)


def pick_dispatch_target_for_refresh(event: dict) -> DispatchTarget:
    """Decide which specialist(s) should pick up a refresh event.

    ``event`` matches the §9.4.2 envelope shape. Three signal sources,
    in priority order:

      1. **Explicit ``domain``** field on the event (``"sd"`` /
         ``"bp"`` / ``"both"``) — the UI sets this when it wants to
         break the refresh into per-domain calls (the recommended
         shape — see §9.4.2). Always wins.
      2. ``doc_id_or_commit_sha`` / ``source`` URI hint — used by
         the Update Trigger when it knows which file changed.
      3. Default — fan out to both specialists.
    """
    domain = (event or {}).get("domain")
    if isinstance(domain, str):
        d = domain.strip().lower()
        if d in {"sd", "system-design"}:
            return DispatchTarget(bp=False, sd=True)
        if d in {"bp", "b&p", "business-product"}:
            return DispatchTarget(bp=True, sd=False)
        if d == "both":
            return DispatchTarget(bp=True, sd=True)
        # Unknown value → fall through to URI / default heuristics.

    src = (event or {}).get("doc_id_or_commit_sha") or (event or {}).get("source") or ""
    src = str(src).lower()
    if not src:
        # Empty event → full refresh; fan out to both specialists.
        return DispatchTarget(bp=True, sd=True)
    if src.startswith(("inputs/", "documentation/bp/", "bp/", "business-cases/")):
        return DispatchTarget(bp=True, sd=False)
    if src.startswith((
        "documentation/sd/", "sd/", "services/", "src/", "code/",
    )):
        return DispatchTarget(bp=False, sd=True)
    if src.endswith(".py") or src.endswith(".java") or src.endswith(".go"):
        return DispatchTarget(bp=False, sd=True)
    if src.endswith(".md") and "/" in src:
        # Generic markdown — most likely BP input doc, but conservative
        # and fan out so neither domain misses a relevant change.
        return DispatchTarget(bp=True, sd=True)
    return DispatchTarget(bp=True, sd=True)


def owning_specialist_for_page(page_uri: str) -> str | None:
    """Map a generated page URI to the owning specialist.

    Used during ``ingest_sme_reply`` so the orchestrator routes the
    ``patch_page`` call to the specialist that wrote the original page
    (B&P never patches an SD page and vice versa, per §8.4
    cross-domain isolation).
    """
    if not page_uri:
        return None
    p = page_uri.lstrip("/")
    if p.startswith(("bp/", "documentation/bp/")):
        return "bp"
    if p.startswith(("sd/", "documentation/sd/")):
        return "sd"
    return None
