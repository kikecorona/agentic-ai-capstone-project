"""Chunking strategies + ToT chunking-strategy selector.

Implements the candidate strategies from §6.2 (per-paragraph, per-section,
per-N-chars, summary-only, hybrid) and the ToT loop from §9.1.3.2:

  1. **Generate** K=4 candidate strategies for the document.
  2. **Embed** each candidate's chunks into an *ephemeral* in-memory index.
  3. **Probe** — N student-style Q&A pairs from an LLM read of the doc.
  4. **Score** — fraction of probe questions whose top-K hit lands on a
     chunk that contains the answer excerpt (similarity-over-M).
  5. **Prune** candidates below 0.7.
  6. **Iterate** — beam B=2 expanded with width / hybrid variants, depth D=2.
  7. **Persist** — winning strategy returned to the caller (RAG Service).

If no candidate clears the threshold at depth D, the highest-scoring one
is returned with a ``low_confidence`` flag — same as §9.1.3.2 spec.

The probe step's question generator is content-driven, so domain prose
differences (B&P narrative vs SD structured prose) flow through naturally.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from langchain_core.messages import HumanMessage, SystemMessage

from src.shared.llm import get_chat_llm, get_embedding_function
from src.shared.service_log import get_logger

log = get_logger("rag.chunking")


# ---------------------------------------------------------------------------
# Chunkers — pure functions over the document text
# ---------------------------------------------------------------------------

PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+", re.MULTILINE)
SECTION_SPLIT = re.compile(r"^(#{1,6}\s.*)$", re.MULTILINE)


def chunk_per_paragraph(doc: str, *, min_chars: int = 0) -> list[str]:
    """Split on blank lines. ``min_chars`` (off by default) merges chunks
    smaller than the threshold into the previous one — leave at 0 so the
    ToT scorer compares per-paragraph fairly against per-section."""
    parts = [p.strip() for p in PARAGRAPH_SPLIT.split(doc) if p.strip()]
    if not parts or min_chars <= 0:
        return parts
    out: list[str] = []
    for p in parts:
        if out and len(p) < min_chars:
            out[-1] = (out[-1] + "\n\n" + p).strip()
        else:
            out.append(p)
    return out


def chunk_per_section(doc: str) -> list[str]:
    """Split on Markdown headings. Each chunk holds a heading + its body."""
    if not SECTION_SPLIT.search(doc):
        # No markdown headings — fall back to paragraphs so we still produce
        # multiple chunks rather than one giant blob.
        return chunk_per_paragraph(doc)
    pieces: list[str] = []
    last = 0
    headings = list(SECTION_SPLIT.finditer(doc))
    if headings and headings[0].start() > 0:
        prelude = doc[: headings[0].start()].strip()
        if prelude:
            pieces.append(prelude)
    for i, m in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(doc)
        section = doc[m.start():end].strip()
        if section:
            pieces.append(section)
    return pieces


def chunk_per_n_chars(doc: str, *, n: int = 800, overlap: int = 80) -> list[str]:
    """Sliding window with small overlap so an answer that straddles a
    boundary still appears in at least one chunk."""
    n = max(100, n)
    overlap = max(0, min(overlap, n // 4))
    if len(doc) <= n:
        return [doc.strip()] if doc.strip() else []
    out: list[str] = []
    start = 0
    while start < len(doc):
        end = min(start + n, len(doc))
        out.append(doc[start:end].strip())
        if end == len(doc):
            break
        start = end - overlap
    return [c for c in out if c]


def chunk_summary_only(doc: str) -> list[str]:
    """LLM-produced summary + key topics + main takeaways (per §6.3).

    Returns up to three chunks: the summary paragraph, the topics list,
    and the takeaways list. Falls back to a single-chunk doc snippet on
    LLM error so the rest of the pipeline still has something to embed.
    """
    llm = get_chat_llm("rag.chunking.summary", temperature=0.0, json_mode=True)
    prompt = (
        "Read the document below and return ONLY a JSON object with three "
        "fields: 'summary' (one paragraph, 80–150 words), 'topics' (a list "
        "of 3–6 short topic phrases the doc covers), and 'takeaways' (a "
        "list of 3–6 single-sentence main takeaways).\n\n"
        f"DOCUMENT:\n{doc}"
    )
    try:
        msg = llm.invoke([SystemMessage(content="You are a careful summariser."), HumanMessage(content=prompt)])
        data = json.loads(msg.content)
        chunks: list[str] = []
        summary = (data.get("summary") or "").strip()
        if summary:
            chunks.append(summary)
        topics = data.get("topics") or []
        if topics:
            chunks.append("Topics: " + "; ".join(str(t) for t in topics))
        takeaways = data.get("takeaways") or []
        if takeaways:
            chunks.append("Takeaways:\n- " + "\n- ".join(str(t) for t in takeaways))
        if chunks:
            return chunks
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        log.error(f"summary_only chunker failed: {exc}")
    # Fallback: just take the first 600 chars so the candidate isn't empty.
    return [doc[:600].strip()] if doc.strip() else []


# ---------------------------------------------------------------------------
# Strategy descriptors
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChunkingStrategy:
    """A named chunking recipe + the params that produced it.

    Persisted into chunk metadata as ``chunking_strategy`` so retrieval-time
    grading can reuse the recipe (§9.1.3.2 step 7).
    """

    name: str
    params: dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        if not self.params:
            return self.name
        kv = ",".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.name}[{kv}]"

    def apply(self, doc: str) -> list[str]:
        if self.name == "per_paragraph":
            return chunk_per_paragraph(doc, **self.params)
        if self.name == "per_section":
            return chunk_per_section(doc, **self.params)
        if self.name == "per_n_chars":
            return chunk_per_n_chars(doc, **self.params)
        if self.name == "summary_only":
            return chunk_summary_only(doc, **self.params)
        if self.name == "hybrid_section_summary":
            sec = chunk_per_section(doc)
            sumr = chunk_summary_only(doc)
            return sec + sumr
        raise ValueError(f"unknown chunking strategy: {self.name}")


def initial_candidates() -> list[ChunkingStrategy]:
    """K=4 starting candidates per §9.1.3.2 step 1."""
    return [
        ChunkingStrategy("per_paragraph"),
        ChunkingStrategy("per_section"),
        ChunkingStrategy("per_n_chars", {"n": 800, "overlap": 80}),
        ChunkingStrategy("per_n_chars", {"n": 1200, "overlap": 100}),
    ]


def variant_expansions(strategy: ChunkingStrategy) -> list[ChunkingStrategy]:
    """At depth D > 1 expand each survivor into related variants — different
    chunk sizes, hybrid combinations (§9.1.3.2 step 6)."""
    out: list[ChunkingStrategy] = []
    if strategy.name == "per_n_chars":
        n = int(strategy.params.get("n", 800))
        out.append(ChunkingStrategy("per_n_chars", {"n": max(300, n // 2), "overlap": 60}))
        out.append(ChunkingStrategy("per_n_chars", {"n": min(2400, n * 2), "overlap": 120}))
    if strategy.name == "per_paragraph":
        out.append(ChunkingStrategy("per_paragraph", {"min_chars": 200}))
    if strategy.name == "per_section":
        out.append(ChunkingStrategy("hybrid_section_summary"))
    return out


# ---------------------------------------------------------------------------
# Probes — student-style Q&A pairs (§9.1.3.2 step 3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Probe:
    question: str
    answer: str
    excerpt: str  # phrase that should appear in the right chunk


def generate_probes(doc: str, n: int = 4) -> list[Probe]:
    """Ask the LLM for ``n`` Q&A pairs, each with a verbatim ``excerpt``
    from the doc that the right chunk should contain. Falls back to an
    empty list on parse failure — the ToT loop treats that as
    ``low_confidence`` and indexes with the highest-scoring strategy
    anyway (per spec)."""
    if not doc.strip():
        return []
    llm = get_chat_llm("rag.chunking.probes", temperature=0.2, json_mode=True)
    prompt = (
        f"Read the document below and produce {n} student-style Q&A pairs that "
        "test understanding. Each answer must include a short verbatim 'excerpt' "
        "(5–20 words) copied EXACTLY from the document that contains the answer.\n\n"
        "Return ONLY a JSON object: {\"pairs\": [{\"q\": \"...\", \"a\": \"...\", "
        "\"excerpt\": \"...\"}, ...]}\n\n"
        f"DOCUMENT:\n{doc}"
    )
    try:
        msg = llm.invoke([SystemMessage(content="You write probing comprehension questions."), HumanMessage(content=prompt)])
        data = json.loads(msg.content)
        out: list[Probe] = []
        for pair in data.get("pairs", []):
            q = (pair.get("q") or "").strip()
            a = (pair.get("a") or "").strip()
            ex = (pair.get("excerpt") or "").strip()
            if q and ex and ex in doc:  # Excerpt must be verifiable in source.
                out.append(Probe(question=q, answer=a, excerpt=ex))
        return out
    except Exception as exc:  # noqa: BLE001
        log.error(f"probe generation failed: {exc}")
        return []


# ---------------------------------------------------------------------------
# Scoring — similarity-over-M against an ephemeral index
# ---------------------------------------------------------------------------

def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a)) or 1.0
    nb = float(np.linalg.norm(b)) or 1.0
    return float(np.dot(a, b) / (na * nb))


def score_strategy(strategy: ChunkingStrategy, doc: str, probes: list[Probe], *, top_k: int = 1) -> tuple[float, list[str]]:
    """Returns (score in [0,1], chunks). Score = fraction of probes whose
    top-K retrieved chunk(s) contain the verbatim excerpt.

    Embeds in memory using the same embedding function the persisted store
    uses, so the score predicts retrieval quality after the strategy is
    persisted (§9.1.3.2 step 4).
    """
    chunks = strategy.apply(doc)
    if not chunks:
        return 0.0, []
    if not probes:
        # Without probes we can't measure retrieval — return a small
        # constant so the ToT loop still picks something.
        return 0.0, chunks

    embed = get_embedding_function()
    chunk_vecs = np.asarray(embed(chunks), dtype=float)
    questions = [p.question for p in probes]
    q_vecs = np.asarray(embed(questions), dtype=float)

    hits = 0
    k = max(1, min(top_k, len(chunks)))
    for q_vec, probe in zip(q_vecs, probes):
        sims = np.array([_cosine(q_vec, c) for c in chunk_vecs])
        order = np.argsort(-sims)[:k]
        for idx in order:
            if probe.excerpt and probe.excerpt in chunks[int(idx)]:
                hits += 1
                break
    return hits / float(len(probes)), chunks


# ---------------------------------------------------------------------------
# ToT loop — beam search over strategies (§9.1.3.2)
# ---------------------------------------------------------------------------

@dataclass
class ToTResult:
    strategy: ChunkingStrategy
    chunks: list[str]
    score: float
    low_confidence: bool
    trail: list[dict[str, Any]]


def select_chunking_strategy(
    doc: str,
    *,
    k: int = 4,
    beam: int = 2,
    depth: int = 2,
    threshold: float = 0.7,
    probe_count: int = 4,
    probe_factory: Callable[[str, int], list[Probe]] | None = None,
) -> ToTResult:
    """Run the ToT chunking-strategy selector and return the winner.

    Caps default to the §7.4 / §9.1.3.2 POC values: K=4, B=2–3, D=2–3.
    All loop bounds are explicit so the call is bounded by ~K + B*D LLM
    embeddings + 1 probe-generation call.
    """
    probe_factory = probe_factory or generate_probes
    log.info(
        f"ToT chunking selection start: doc={len(doc)} chars, "
        f"K={k}, beam={beam}, depth={depth}, threshold={threshold}"
    )
    probes = probe_factory(doc, probe_count)
    if not probes:
        log.warn("ToT probe generation produced 0 pairs; scoring will be flat")
    else:
        log.info(f"ToT generated {len(probes)} probe Q&A pair(s)")

    trail: list[dict[str, Any]] = []
    candidates = initial_candidates()[:k]

    scored: list[tuple[ChunkingStrategy, float, list[str]]] = []
    for cand in candidates:
        score, chunks = score_strategy(cand, doc, probes)
        scored.append((cand, score, chunks))
        trail.append({
            "depth": 1,
            "strategy": cand.label(),
            "chunks": len(chunks),
            "score": round(score, 3),
        })

    # Beam-prune at depth 1.
    scored.sort(key=lambda t: t[1], reverse=True)
    survivors = scored[:beam]

    # Depth iterations.
    best = scored[0] if scored else (ChunkingStrategy("per_paragraph"), 0.0, [])
    for d in range(2, depth + 1):
        # If the current best already clears the threshold we can stop early.
        if best[1] >= threshold and d > 2:
            break
        next_pool: list[tuple[ChunkingStrategy, float, list[str]]] = list(survivors)
        for surv_strategy, _, _ in survivors:
            for variant in variant_expansions(surv_strategy):
                # Don't re-run an identical strategy.
                if any(c[0].label() == variant.label() for c in next_pool):
                    continue
                v_score, v_chunks = score_strategy(variant, doc, probes)
                next_pool.append((variant, v_score, v_chunks))
                trail.append({
                    "depth": d,
                    "strategy": variant.label(),
                    "chunks": len(v_chunks),
                    "score": round(v_score, 3),
                })
        next_pool.sort(key=lambda t: t[1], reverse=True)
        survivors = next_pool[:beam]
        if survivors and survivors[0][1] > best[1]:
            best = survivors[0]

    if not best[2]:
        # Last-ditch — paragraphs always work.
        log.error(
            "ToT produced no usable candidate; falling back to per_paragraph "
            "with score=0 (likely empty or unreadable doc)"
        )
        fallback = ChunkingStrategy("per_paragraph")
        chunks = fallback.apply(doc) or [doc]
        return ToTResult(
            strategy=fallback,
            chunks=chunks,
            score=0.0,
            low_confidence=True,
            trail=trail + [{"depth": 0, "strategy": "fallback per_paragraph", "chunks": len(chunks), "score": 0.0}],
        )

    winner_label = best[0].label()
    winner_score = best[1]
    is_low_conf = winner_score < threshold
    if is_low_conf:
        log.warn(
            f"ToT no candidate cleared threshold {threshold}; persisting "
            f"highest-scoring strategy={winner_label} score={winner_score:.3f} "
            "(low_confidence)"
        )
    else:
        log.info(
            f"ToT chunking selection done: strategy={winner_label} "
            f"score={winner_score:.3f} chunks={len(best[2])}"
        )
    return ToTResult(
        strategy=best[0],
        chunks=best[2],
        score=winner_score,
        low_confidence=is_low_conf,
        trail=trail,
    )


def new_embedding_revision() -> str:
    """A short revision tag stored alongside each chunk so we can compare
    runs of the same strategy (e.g., after an embed-model swap)."""
    return uuid.uuid4().hex[:12]
