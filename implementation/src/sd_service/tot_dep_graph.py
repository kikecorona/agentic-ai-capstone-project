"""ToT dependency-graph selector (§9.2.3.3).

Generates K=3 candidate dependency graphs per service, scores each, and
returns the winner. With the Monitoring MCP out of POC scope (§8.5),
scoring collapses to a code-only rubric: edge coverage of the detected
calls plus a stability heuristic that prefers fewer dynamic / orphan
edges.

Beam search runs at the spec's POC defaults (B=2-3, D=2-3): every
surviving candidate at level *d* expands into structural variants for
level *d+1* — drop the lowest-confidence edges, merge near-duplicate
targets, etc. — and the top B are kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.shared.service_log import get_logger

from .analyze_code import DownstreamCall, ServiceAnalysis

log = get_logger("rag.sd.tot_dep_graph")


# ---------------------------------------------------------------------------
# Edge / graph dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DepEdge:
    kind: str            # "http" | "db"
    target: str          # canonical target name (host stem or table)
    handlers: tuple[str, ...]   # handlers that contribute to this edge
    dynamic: bool        # at least one contributing call was dynamic
    call_count: int      # how many call sites map to this edge

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "handlers": list(self.handlers),
            "dynamic": self.dynamic,
            "call_count": self.call_count,
        }


@dataclass
class DepGraph:
    label: str
    edges: list[DepEdge]
    score: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "score": round(self.score, 3),
            "edges": [e.to_dict() for e in self.edges],
            "notes": list(self.notes),
        }


@dataclass
class ToTResult:
    winner: DepGraph
    runner_ups: list[DepGraph]
    trail: list[dict[str, Any]]
    follow_ups: list[str]   # runner-up edges that differ from the winner
    low_confidence: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "winner": self.winner.to_dict(),
            "runner_ups": [g.to_dict() for g in self.runner_ups],
            "trail": list(self.trail),
            "follow_ups": list(self.follow_ups),
            "low_confidence": self.low_confidence,
        }


# ---------------------------------------------------------------------------
# Edge folding — collapse calls into edges
# ---------------------------------------------------------------------------

def _fold_edges(calls: list[DownstreamCall], *, include_dynamic: bool, min_call_count: int = 1) -> list[DepEdge]:
    """Collapse raw call records into deduped (kind, target) edges.

    ``include_dynamic`` controls whether calls without a statically
    resolved target contribute. ``min_call_count`` filters out edges
    backed by a single call site, used by the hub-prioritised variant.
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for c in calls:
        if not c.target:
            if not include_dynamic:
                continue
            target = "dynamic"
        else:
            target = c.target
        key = (c.kind, target)
        bucket = grouped.setdefault(key, {"handlers": set(), "dynamic": False, "count": 0})
        bucket["handlers"].add(c.from_handler)
        bucket["dynamic"] = bucket["dynamic"] or c.dynamic or (target == "dynamic")
        bucket["count"] += 1
    edges: list[DepEdge] = []
    for (kind, target), bucket in grouped.items():
        if bucket["count"] < min_call_count:
            continue
        edges.append(DepEdge(
            kind=kind,
            target=target,
            handlers=tuple(sorted(bucket["handlers"])),
            dynamic=bool(bucket["dynamic"]),
            call_count=int(bucket["count"]),
        ))
    edges.sort(key=lambda e: (e.kind, e.target))
    return edges


# ---------------------------------------------------------------------------
# Candidate generation — the K=3 starting variants (§9.2.3.3 step 1)
# ---------------------------------------------------------------------------

def _initial_candidates(analysis: ServiceAnalysis) -> list[DepGraph]:
    raw = _fold_edges(analysis.downstream_calls, include_dynamic=True, min_call_count=1)
    pruned_dynamic = _fold_edges(analysis.downstream_calls, include_dynamic=False, min_call_count=1)
    hub_only = _fold_edges(analysis.downstream_calls, include_dynamic=False, min_call_count=2)
    return [
        DepGraph(label="raw_code", edges=raw, notes=["every detected call site"]),
        DepGraph(label="dynamic_pruned", edges=pruned_dynamic, notes=["drop dynamic-target edges"]),
        DepGraph(label="hub_only", edges=hub_only, notes=["only edges backed by ≥2 call sites"]),
    ]


def _variant_expansions(g: DepGraph, analysis: ServiceAnalysis) -> list[DepGraph]:
    """At depth > 1, expand a survivor into structural variants.

    Two cheap moves:
      * Drop the dynamic edges (if any) and rescore — sometimes a graph
        with one fewer noisy edge is materially cleaner.
      * Bump ``min_call_count`` so single-site edges fall away — closer
        to ``hub_only`` but starting from this candidate's filter.
    """
    out: list[DepGraph] = []
    if any(e.dynamic for e in g.edges):
        no_dyn = [e for e in g.edges if not e.dynamic]
        out.append(DepGraph(
            label=f"{g.label}+no_dynamic",
            edges=no_dyn,
            notes=g.notes + ["drop dynamic edges"],
        ))
    if any(e.call_count == 1 for e in g.edges):
        multi = [e for e in g.edges if e.call_count >= 2]
        out.append(DepGraph(
            label=f"{g.label}+multi_call",
            edges=multi,
            notes=g.notes + ["edges backed by ≥2 call sites"],
        ))
    return out


# ---------------------------------------------------------------------------
# Scoring rubric — code-only (§9.2.3.2 fallback)
# ---------------------------------------------------------------------------

def _score(graph: DepGraph, analysis: ServiceAnalysis) -> float:
    """0–1 rubric. Three additive components, weighted:

      * **edge coverage** — fraction of (statically-resolved) calls that
        are represented by an edge. Best when the graph captures every
        confident dependency.
      * **dynamic penalty** — graphs with many dynamic edges score lower
        because dynamic targets are SME-review territory.
      * **simplicity** — slight preference for fewer edges when coverage
        is already complete (matches §7.3 tie-breaker: prefer the simpler
        graph).
    """
    static_calls = [c for c in analysis.downstream_calls if c.target and not c.dynamic]
    static_targets = {(c.kind, c.target) for c in static_calls}
    edge_targets = {(e.kind, e.target) for e in graph.edges if e.target != "dynamic"}

    if static_targets:
        coverage = len(static_targets & edge_targets) / float(len(static_targets))
    else:
        coverage = 1.0 if not graph.edges else 0.5  # ambiguous

    dynamic_edges = sum(1 for e in graph.edges if e.dynamic or e.target == "dynamic")
    total_edges = max(1, len(graph.edges))
    dynamic_ratio = dynamic_edges / total_edges
    dynamic_penalty = 1.0 - dynamic_ratio

    # Simplicity: 1.0 at exactly N static targets, decays slowly past.
    simplicity = 1.0 if not edge_targets else min(1.0, len(static_targets) / max(1, len(edge_targets)))

    score = 0.6 * coverage + 0.25 * dynamic_penalty + 0.15 * simplicity
    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# Beam search (§9.2.3.3 steps 2-5)
# ---------------------------------------------------------------------------

def select_dep_graph(
    analysis: ServiceAnalysis,
    *,
    beam: int = 2,
    depth: int = 2,
    threshold: float = 0.8,
) -> ToTResult:
    """Pick the best dependency graph for ``analysis``.

    Caps default to the spec's POC values (B=2-3, D=2-3 §7.4). The
    threshold of 0.8 matches §7.3's pruning bar for dependency graphs;
    when no candidate clears it, the highest-scoring graph is still
    returned with ``low_confidence=True`` so the SD page surfaces an
    SME placeholder for review (§9.2.3.3 final paragraph).
    """
    log.info(
        f"select_dep_graph service={analysis.service} "
        f"calls={len(analysis.downstream_calls)} beam={beam} depth={depth} "
        f"threshold={threshold}"
    )

    trail: list[dict[str, Any]] = []
    candidates = _initial_candidates(analysis)
    for c in candidates:
        c.score = _score(c, analysis)
        trail.append({"depth": 1, "label": c.label, "edges": len(c.edges), "score": round(c.score, 3)})

    candidates.sort(key=lambda g: g.score, reverse=True)
    survivors = candidates[:beam]
    best = survivors[0] if survivors else DepGraph(label="empty", edges=[])

    for d in range(2, depth + 1):
        if best.score >= threshold and d > 2:
            break
        next_pool: list[DepGraph] = list(survivors)
        for surv in list(survivors):
            for variant in _variant_expansions(surv, analysis):
                # De-dupe by edge set so identical variants don't bloat the pool.
                if any(_same_edge_set(variant, c) for c in next_pool):
                    continue
                variant.score = _score(variant, analysis)
                next_pool.append(variant)
                trail.append({
                    "depth": d,
                    "label": variant.label,
                    "edges": len(variant.edges),
                    "score": round(variant.score, 3),
                })
        next_pool.sort(key=lambda g: g.score, reverse=True)
        survivors = next_pool[:beam]
        if survivors and survivors[0].score > best.score:
            best = survivors[0]

    runner_ups = [g for g in survivors if g is not best][: max(1, beam - 1)]
    follow_ups = _follow_up_edges(best, runner_ups)
    low_conf = best.score < threshold

    if low_conf:
        log.warn(
            f"select_dep_graph: no candidate cleared {threshold}; using "
            f"{best.label} score={best.score:.3f} (low_confidence)"
        )
    else:
        log.info(
            f"select_dep_graph done: winner={best.label} score={best.score:.3f} "
            f"edges={len(best.edges)} runner_ups={[g.label for g in runner_ups]}"
        )
    return ToTResult(
        winner=best,
        runner_ups=runner_ups,
        trail=trail,
        follow_ups=follow_ups,
        low_confidence=low_conf,
    )


def _same_edge_set(a: DepGraph, b: DepGraph) -> bool:
    return {(e.kind, e.target) for e in a.edges} == {(e.kind, e.target) for e in b.edges}


def _follow_up_edges(winner: DepGraph, runner_ups: list[DepGraph]) -> list[str]:
    """Edges that appear in a runner-up but NOT in the winner — the spec
    calls these "follow-up tasks for the next refresh"."""
    winner_keys = {(e.kind, e.target) for e in winner.edges}
    follow_ups: list[str] = []
    seen: set[tuple[str, str]] = set()
    for g in runner_ups:
        for e in g.edges:
            key = (e.kind, e.target)
            if key in winner_keys or key in seen:
                continue
            seen.add(key)
            follow_ups.append(f"{e.kind} → {e.target} (only in {g.label})")
    return follow_ups
