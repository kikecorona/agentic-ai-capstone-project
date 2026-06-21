"""Autonomous RAG loop (§9.1.3.1).

LangGraph ``StateGraph`` implementing the four nodes from the spec —
**decide → retrieve → grade → rewrite** — plus answer generation and a
post-generation faithfulness re-grade. The loop is bounded by R rewrites
(default 2) and returns one of three statuses:

  * ``ok``             — answer cleared the grader and the faithfulness check.
  * ``low_confidence`` — rewrite budget exhausted but a best-effort answer is
                          included with the closest matches and the rewrite
                          trail.
  * ``exhausted``      — even the closest matches are below the
                          "no signal at all" floor; no answer is returned.

The loop also surfaces **index-quality flags**: chunks that survive
retrieval but repeatedly fail the grader. The calling specialist consumes
these to decide whether to re-index a source with a different chunking
strategy on the next refresh, closing the loop with §9.1.3.2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from src.shared.llm import get_chat_llm
from src.shared.service_log import get_logger
from .store import EmbeddingsStore, StoredChunk

log = get_logger("rag.auto_rag")


# ---------------------------------------------------------------------------
# Loop config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AutoRAGConfig:
    rewrite_budget: int = 2          # R cap (§9.1.3.1)
    top_k: int = 10                  # K (§9.1.3.1)
    grade_threshold: float = 2.0     # 0–3 grader scale
    no_signal_distance: float = 1.6  # "exhausted" when best distance > this
    bad_chunk_repeat_threshold: int = 2  # surfaces an index-quality flag


# ---------------------------------------------------------------------------
# Result shape returned to the caller (RAGService)
# ---------------------------------------------------------------------------

@dataclass
class AutoRAGResult:
    status: str  # ok | low_confidence | exhausted
    answer: str | None
    sources: list[dict[str, Any]]
    retrieval_trail: list[dict[str, Any]]
    grader_scores: list[dict[str, Any]]
    index_quality_flags: list[dict[str, Any]]
    rewrites_used: int


# ---------------------------------------------------------------------------
# State shape (LangGraph)
# ---------------------------------------------------------------------------

def _merge_lists(left: list, right: list) -> list:
    return list(left) + list(right)


class AutoRAGState(TypedDict, total=False):
    # Input
    query: str
    original_query: str
    domain_filter: str | None
    mode: str

    # Working state
    rewrites_used: int
    last_retrieved: list[StoredChunk]
    closest_matches: list[StoredChunk]  # best-distance batch seen so far
    grader_pass: bool
    grader_results: list[dict[str, Any]]   # per-chunk for the latest retrieval
    answer: str | None
    faithfulness_pass: bool | None
    faithfulness_attempts: int
    needs_rewrite_for_faithfulness: bool

    # Trails (append-only, merged via reducer)
    retrieval_trail: Annotated[list[dict[str, Any]], _merge_lists]
    grader_scores: Annotated[list[dict[str, Any]], _merge_lists]
    index_quality_flags: Annotated[list[dict[str, Any]], _merge_lists]

    # Bookkeeping for index-quality flags: chunks that have failed the
    # grader at least once across retrievals in this loop.
    seen_bad_chunks: dict[str, int]

    # Output
    status: str
    final_sources: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# LLM-backed sub-nodes
# ---------------------------------------------------------------------------

def _route_query(query: str) -> str:
    """Static-context vs retrieval router (§9.1.3.1 step 1).

    POC heuristic: every research-style question requires retrieval; only
    obvious greetings / meta-questions skip it. Cheap and bounded — bigger
    routers can replace this without changing the contract.
    """
    q = query.strip().lower()
    if not q:
        return "no_retrieval"
    greetings = ("hi", "hello", "hey", "thanks", "thank you", "who are you", "what can you do")
    if any(q == g or q.startswith(g + " ") or q.startswith(g + ",") for g in greetings):
        return "no_retrieval"
    return "retrieve"


def _grade_chunks(query: str, chunks: list[StoredChunk]) -> list[dict[str, Any]]:
    """Score every retrieved chunk 0–3 with one LLM call.

    Score scale (per §9.1.3.1 step 3):
      * 0 — irrelevant.
      * 1 — tangential mention.
      * 2 — partially relevant; could support part of an answer.
      * 3 — directly answers the question.
    """
    if not chunks:
        return []
    llm = get_chat_llm("rag.auto_rag.grader", temperature=0.0, json_mode=True)
    enumerated = "\n\n".join(
        f"[CHUNK {i}] (domain={c.domain}, source={c.source_uri})\n{c.text}"
        for i, c in enumerate(chunks)
    )
    prompt = (
        "You are a strict relevance grader. For each CHUNK, score 0-3:\n"
        "  0 = irrelevant. The chunk does not address the query's subject.\n"
        "  1 = tangential. Shares vague topical overlap but contains no\n"
        "      content that would appear in a correct answer.\n"
        "  2 = partial. Concrete content that supports part of a correct answer.\n"
        "  3 = direct. The chunk substantively answers the query.\n\n"
        "DEFAULT TO A LOW SCORE. Only score 2 or 3 when the chunk has content\n"
        "the answer would cite. If the query asks about a topic the chunk does\n"
        "not discuss (different system, different domain, different concept),\n"
        "score 0 — surface-level word overlap is NOT relevance.\n\n"
        "EXAMPLES:\n"
        '  Query: "What is the migration plan for the legacy COBOL mainframe payroll runs?"\n'
        '  Chunk: "Renewals run on the 1st of each month with a 24 hour processing window."\n'
        "  → score 0. The chunk is about subscription billing renewals, not COBOL\n"
        "  mainframe migration or payroll. The shared word \"run\" is incidental.\n\n"
        '  Query: "What endpoints does the billing-service expose?"\n'
        '  Chunk: "The billing-service exposes endpoints for charging saved payment\n'
        '    methods, issuing refunds, and listing invoices."\n'
        "  → score 3. The chunk directly enumerates the requested endpoints.\n\n"
        f"QUERY: {query}\n\n{enumerated}\n\n"
        "Return ONLY a JSON object: {\"scores\": [{\"chunk\": <int>, \"score\": <0-3>, "
        "\"reason\": \"...\"}]}"
    )
    try:
        msg = llm.invoke([SystemMessage(content="You grade retrieval relevance."), HumanMessage(content=prompt)])
        data = json.loads(msg.content)
        out: list[dict[str, Any]] = []
        for entry in data.get("scores", []):
            try:
                idx = int(entry["chunk"])
                score = float(entry["score"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= idx < len(chunks):
                out.append({
                    "chunk_id": chunks[idx].chunk_id,
                    "source_uri": chunks[idx].source_uri,
                    "domain": chunks[idx].domain,
                    "score": max(0.0, min(3.0, score)),
                    "reason": str(entry.get("reason", "")).strip(),
                })
        return out
    except Exception as exc:  # noqa: BLE001
        log.error(f"grader failed: {exc}")
        return []


def _rewrite_query(original: str, current: str, domain_filter: str | None, attempts: list[str]) -> str:
    """Produce a new query — acronym expansion, synonyms, narrower scope —
    used when the grader produces nothing usable (§9.1.3.1 step 4)."""
    llm = get_chat_llm("rag.auto_rag.rewriter", temperature=0.3, json_mode=True)
    prior = "; ".join(attempts) if attempts else "(none)"
    domain = domain_filter or "any"
    prompt = (
        "Rewrite the QUERY to improve retrieval. Expand acronyms, add synonyms "
        "or related domain terms, and consider narrowing scope. Avoid repeating "
        "PRIOR ATTEMPTS. Return ONLY a JSON object: {\"rewrite\": \"...\"}.\n\n"
        f"ORIGINAL: {original}\nCURRENT: {current}\nDOMAIN: {domain}\n"
        f"PRIOR ATTEMPTS: {prior}"
    )
    try:
        msg = llm.invoke([SystemMessage(content="You rewrite retrieval queries."), HumanMessage(content=prompt)])
        data = json.loads(msg.content)
        out = _coerce_rewrite(data.get("rewrite"))
        return out or current
    except Exception as exc:  # noqa: BLE001
        log.error(f"rewriter failed: {exc}")
        return current


def _coerce_rewrite(value: Any) -> str:
    """Tolerate LLMs that return ``{"rewrite": ...}`` with the value
    nested as a dict / list / non-string. We unwrap the most common
    shapes and fall back to an empty string when nothing usable is
    found — the caller then keeps the current query unchanged.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        # Common nested shapes: {"text": "..."}, {"query": "..."},
        # {"rewrite": "..."}.
        for key in ("text", "query", "rewrite", "value"):
            inner = value.get(key)
            if isinstance(inner, str):
                return inner.strip()
        return ""
    if isinstance(value, list):
        return _coerce_rewrite(value[0]) if value else ""
    return ""


def _generate_answer(query: str, chunks: list[StoredChunk]) -> str:
    """Compose an answer grounded only in the retrieved chunks.

    The answer is consumed in two places: query-mode (Portal chatbot)
    where a tight conversational reply is fine, and background-mode
    enrichment (B&P / SD ``fill_from_rag``) where the prose is dropped
    directly into a doc section. The latter is intolerant of
    LLM-style preambles ("Based on the sources..."), trailing
    meta-commentary ("Note that..."), and refusal hedges — so the
    system prompt forbids all three explicitly.
    """
    llm = get_chat_llm("rag.auto_rag.generator", temperature=0.0)
    sources = "\n\n".join(
        f"[S{i + 1}] (domain={c.domain}, source={c.source_uri})\n{c.text}"
        for i, c in enumerate(chunks)
    )
    prompt = (
        "Write the answer to the QUERY using ONLY the SOURCES below. Cite "
        "each claim inline with [S1], [S2], etc.\n\n"
        f"QUERY: {query}\n\nSOURCES:\n{sources}"
    )
    system = (
        "You write technical documentation prose grounded in cited sources. "
        "Constraints:\n"
        "* Start with the substantive content. Do NOT begin with phrases "
        "like \"Based on the sources\", \"According to\", \"Here are the "
        "key points\", \"The provided sources indicate\", or any similar "
        "framing.\n"
        "* Do NOT end with meta-commentary about the sources (\"Note that "
        "there is no explicit discussion of...\", \"It is worth noting "
        "that the sources don't mention...\", etc.). Either include the "
        "fact in the body or omit it.\n"
        "* If the sources don't support an answer, write the partial "
        "answer the sources DO support and append \"(further detail TBD)\" "
        "rather than refusing or apologising.\n"
        "* Cite every factual claim inline with [S1], [S2], etc. matching "
        "the SOURCES list. Be concise."
    )
    msg = llm.invoke([SystemMessage(content=system), HumanMessage(content=prompt)])
    return msg.content.strip() if isinstance(msg.content, str) else str(msg.content)


def _check_faithfulness(
    query: str, answer: str, chunks: list[StoredChunk]
) -> tuple[bool, list[str], bool, str]:
    """Re-grade the generated answer along two axes (§9.1.3.1 step 3):

      * **supported** — is every factual claim grounded in the sources?
      * **answers_query** — does the answer *substantively* address the
        query, or is it a faithful-but-useless disclaimer ("the sources
        don't mention X", "no information available")?

    A *faithful refusal is still a refusal* — it's an answer the user
    can't act on. Treating it as ``ok`` would burn the rewrite budget
    we deliberately allocated for exactly this case, so we surface it
    as a re-grade failure and let the loop fall back to ``low_confidence``
    (or rewrite, if there's budget left).

    Returns ``(pass, unsupported_claims, answers_query, non_answer_reason)``.
    ``pass`` is the AND of ``supported`` and ``answers_query``.
    """
    llm = get_chat_llm("rag.auto_rag.faithfulness", temperature=0.0, json_mode=True)
    sources = "\n\n".join(
        f"[S{i + 1}]\n{c.text}" for i, c in enumerate(chunks)
    )
    prompt = (
        "Re-grade the ANSWER along two axes:\n"
        "  1. supported — is every factual claim in the ANSWER grounded in\n"
        "     at least one SOURCE? List any claims that are not.\n"
        "  2. answers_query — does the ANSWER substantively address the\n"
        "     QUERY? An answer is NOT substantive if it merely states the\n"
        "     sources don't contain the information, declines to answer,\n"
        "     restates the question, or otherwise admits absence rather\n"
        "     than providing concrete facts. A graceful refusal is still\n"
        "     a refusal — score answers_query=false.\n\n"
        "Return ONLY a JSON object:\n"
        "  {\"supported\": <bool>, \"unsupported_claims\": [\"...\"],\n"
        "   \"answers_query\": <bool>, \"non_answer_reason\": \"...\"}\n\n"
        f"QUERY: {query}\nANSWER: {answer}\n\nSOURCES:\n{sources}"
    )
    try:
        msg = llm.invoke([
            SystemMessage(content="You verify factual support and answerability."),
            HumanMessage(content=prompt),
        ])
        data = json.loads(msg.content)
        supported = bool(data.get("supported", False))
        unsupported = [str(c) for c in data.get("unsupported_claims", [])]
        answers_query = bool(data.get("answers_query", True))
        reason = str(data.get("non_answer_reason") or "").strip()
        return (supported and answers_query), unsupported, answers_query, reason
    except Exception as exc:  # noqa: BLE001
        log.error(f"faithfulness re-grade failed: {exc}")
        # Fail-open as ``pass`` on parse errors so a transient JSON glitch
        # doesn't downgrade an otherwise-good answer.
        return True, [], True, ""


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_auto_rag_graph(store: EmbeddingsStore, config: AutoRAGConfig | None = None):
    """Compile the Auto-RAG StateGraph. The graph is reused across requests
    — state is per-invocation and threaded by LangGraph."""
    cfg = config or AutoRAGConfig()

    # ---- nodes ------------------------------------------------------------

    def decide(state: AutoRAGState) -> dict:
        decision = _route_query(state["query"])
        return {"retrieval_trail": [{"step": "decide", "decision": decision}]}

    def retrieve(state: AutoRAGState) -> dict:
        chunks = store.query(
            query_text=state["query"],
            domain_filter=state.get("domain_filter"),
            top_k=cfg.top_k,
        )
        # Track best closest matches by min distance — used for fallback.
        closest = state.get("closest_matches") or []
        new_closest = list(chunks)
        if closest:
            # Keep whichever set has the smaller min distance.
            old_min = min((c.distance for c in closest), default=float("inf"))
            new_min = min((c.distance for c in new_closest), default=float("inf"))
            if old_min <= new_min:
                new_closest = closest
        return {
            "last_retrieved": chunks,
            "closest_matches": new_closest,
            "retrieval_trail": [{
                "step": "retrieve",
                "query": state["query"],
                "domain_filter": state.get("domain_filter"),
                "chunks_returned": len(chunks),
                "min_distance": min((c.distance for c in chunks), default=None),
            }],
        }

    def grade(state: AutoRAGState) -> dict:
        chunks = state.get("last_retrieved") or []
        scored = _grade_chunks(state["query"], chunks)
        passing = [s for s in scored if s["score"] >= cfg.grade_threshold]
        # Bookkeeping for index-quality flags: a chunk that scores below
        # threshold counts as a "bad" appearance. Chunks accumulating bad
        # appearances surface as index-quality flags downstream.
        seen = dict(state.get("seen_bad_chunks") or {})
        for s in scored:
            if s["score"] < cfg.grade_threshold:
                seen[s["chunk_id"]] = seen.get(s["chunk_id"], 0) + 1
        return {
            "grader_pass": bool(passing),
            "grader_results": scored,
            "grader_scores": scored,  # appended into the trail via reducer
            "seen_bad_chunks": seen,
        }

    def rewrite(state: AutoRAGState) -> dict:
        rewrites_used = int(state.get("rewrites_used", 0))
        attempts = [
            entry.get("rewrite", "")
            for entry in state.get("retrieval_trail") or []
            if entry.get("step") == "rewrite"
        ]
        new_query = _rewrite_query(
            original=state.get("original_query", state["query"]),
            current=state["query"],
            domain_filter=state.get("domain_filter"),
            attempts=attempts,
        )
        log.info(
            f"Auto-RAG rewrite #{rewrites_used + 1}/{cfg.rewrite_budget} "
            f"(domain_filter={state.get('domain_filter')})"
        )
        return {
            "query": new_query,
            "rewrites_used": rewrites_used + 1,
            "needs_rewrite_for_faithfulness": False,
            "retrieval_trail": [{"step": "rewrite", "rewrite": new_query, "rewrites_used": rewrites_used + 1}],
        }

    def generate(state: AutoRAGState) -> dict:
        # Pass only the chunks that passed the grader; if none passed (no_retrieval
        # path or grader empty), fall back to whatever was retrieved.
        chunks = state.get("last_retrieved") or []
        passing_ids = {s["chunk_id"] for s in (state.get("grader_results") or []) if s["score"] >= cfg.grade_threshold}
        used = [c for c in chunks if c.chunk_id in passing_ids] or chunks
        answer = _generate_answer(state["query"], used)
        return {"answer": answer, "last_retrieved": used}

    def faithfulness(state: AutoRAGState) -> dict:
        chunks = state.get("last_retrieved") or []
        ok, unsupported, answers_query, non_answer_reason = _check_faithfulness(
            state["query"], state.get("answer") or "", chunks
        )
        attempts = int(state.get("faithfulness_attempts", 0)) + 1
        if not ok:
            # Distinguish the two failure modes in the log so a reader can
            # tell at a glance whether the rewrite is chasing hallucinations
            # or chasing a graceful refusal.
            axis = (
                "unsupported_claims" if unsupported and answers_query
                else ("non_answer" if not answers_query else "unsupported")
            )
            log.warn(
                f"Auto-RAG re-grade FAILED on attempt {attempts} ({axis}): "
                f"unsupported={len(unsupported)} answers_query={answers_query} "
                f"reason={non_answer_reason!r}"
            )
        return {
            "faithfulness_pass": ok,
            "faithfulness_attempts": attempts,
            "needs_rewrite_for_faithfulness": (not ok),
            "grader_scores": [{
                "step": "faithfulness",
                "supported": ok or (not unsupported),
                "answers_query": answers_query,
                "unsupported_claims": unsupported,
                "non_answer_reason": non_answer_reason,
            }],
        }

    def respond_ok(state: AutoRAGState) -> dict:
        chunks = state.get("last_retrieved") or []
        flags = _index_quality_flags(state, cfg)
        return {
            "status": "ok",
            "final_sources": [_source_dict(c) for c in chunks],
            "index_quality_flags": flags,
        }

    def respond_fallback(state: AutoRAGState) -> dict:
        # Decide low_confidence vs exhausted. Use closest_matches because
        # last_retrieved may be from the most-recent rewrite which can be
        # weaker than earlier ones.
        closest = state.get("closest_matches") or []
        best_distance = min((c.distance for c in closest), default=float("inf"))
        is_exhausted = (not closest) or best_distance > cfg.no_signal_distance
        status = "exhausted" if is_exhausted else "low_confidence"
        sources = [] if is_exhausted else [_source_dict(c) for c in closest]
        flags = _index_quality_flags(state, cfg)
        # When low_confidence, surface the best-effort answer the loop
        # produced if any. When exhausted, no answer per spec.
        answer = None if is_exhausted else state.get("answer")
        log.warn(
            f"Auto-RAG fallback status={status} "
            f"rewrites_used={state.get('rewrites_used', 0)}/{cfg.rewrite_budget} "
            f"closest_matches={len(closest)} "
            f"index_quality_flags={len(flags)}"
        )
        return {
            "status": status,
            "final_sources": sources,
            "answer": answer,
            "index_quality_flags": flags,
        }

    # ---- routing helpers --------------------------------------------------

    def after_decide(state: AutoRAGState) -> str:
        decision = (state.get("retrieval_trail") or [{}])[-1].get("decision", "retrieve")
        return "retrieve" if decision == "retrieve" else "generate"

    def after_grade(state: AutoRAGState) -> str:
        if state.get("grader_pass"):
            return "generate"
        if int(state.get("rewrites_used", 0)) < cfg.rewrite_budget:
            return "rewrite"
        return "respond_fallback"

    def after_faithfulness(state: AutoRAGState) -> str:
        if state.get("faithfulness_pass"):
            return "respond_ok"
        if int(state.get("rewrites_used", 0)) < cfg.rewrite_budget and int(state.get("faithfulness_attempts", 0)) <= 1:
            return "rewrite"
        return "respond_fallback"

    # ---- graph wiring -----------------------------------------------------

    g = StateGraph(AutoRAGState)
    g.add_node("decide", decide)
    g.add_node("retrieve", retrieve)
    g.add_node("grade", grade)
    g.add_node("rewrite", rewrite)
    g.add_node("generate", generate)
    g.add_node("faithfulness", faithfulness)
    g.add_node("respond_ok", respond_ok)
    g.add_node("respond_fallback", respond_fallback)

    g.add_edge(START, "decide")
    g.add_conditional_edges("decide", after_decide, {"retrieve": "retrieve", "generate": "generate"})
    g.add_edge("retrieve", "grade")
    g.add_conditional_edges("grade", after_grade, {
        "generate": "generate",
        "rewrite": "rewrite",
        "respond_fallback": "respond_fallback",
    })
    g.add_edge("rewrite", "retrieve")
    g.add_edge("generate", "faithfulness")
    g.add_conditional_edges("faithfulness", after_faithfulness, {
        "respond_ok": "respond_ok",
        "rewrite": "rewrite",
        "respond_fallback": "respond_fallback",
    })
    g.add_edge("respond_ok", END)
    g.add_edge("respond_fallback", END)

    return g.compile()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _source_dict(c: StoredChunk) -> dict[str, Any]:
    return {
        "chunk_id": c.chunk_id,
        "domain": c.domain,
        "source_uri": c.source_uri,
        "distance": round(c.distance, 4),
        "snippet": (c.text[:240] + "…") if len(c.text) > 240 else c.text,
    }


def _index_quality_flags(state: AutoRAGState, cfg: AutoRAGConfig) -> list[dict[str, Any]]:
    """Surface chunks that survived retrieval but repeatedly failed the
    grader (§9.1.3.1: "the same document repeatedly survives retrieval but
    fails the grader")."""
    seen = state.get("seen_bad_chunks") or {}
    flags: list[dict[str, Any]] = []
    bad_results = {
        s["chunk_id"]: s for s in (state.get("grader_scores") or [])
        if isinstance(s, dict) and "chunk_id" in s
    }
    for chunk_id, count in seen.items():
        if count >= cfg.bad_chunk_repeat_threshold and chunk_id in bad_results:
            res = bad_results[chunk_id]
            flags.append({
                "chunk_id": chunk_id,
                "domain": res.get("domain"),
                "source_uri": res.get("source_uri"),
                "fail_count": count,
                "reason": "survived retrieval but failed grader repeatedly",
            })
    return flags


def run_auto_rag(
    graph,
    *,
    query: str,
    domain_filter: str | None,
    mode: str,
    cfg: AutoRAGConfig | None = None,
) -> AutoRAGResult:
    """Drive the compiled graph and pack its terminal state into an
    ``AutoRAGResult`` for the caller."""
    cfg = cfg or AutoRAGConfig()
    initial: AutoRAGState = {
        "query": query,
        "original_query": query,
        "domain_filter": domain_filter,
        "mode": mode,
        "rewrites_used": 0,
        "faithfulness_attempts": 0,
        "retrieval_trail": [],
        "grader_scores": [],
        "index_quality_flags": [],
        "seen_bad_chunks": {},
    }
    final_state = graph.invoke(initial)
    return AutoRAGResult(
        status=final_state.get("status", "exhausted"),
        answer=final_state.get("answer"),
        sources=final_state.get("final_sources") or [],
        retrieval_trail=final_state.get("retrieval_trail") or [],
        grader_scores=final_state.get("grader_scores") or [],
        index_quality_flags=final_state.get("index_quality_flags") or [],
        rewrites_used=int(final_state.get("rewrites_used", 0)),
    )
