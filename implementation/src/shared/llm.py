"""Local LLM + embedder factories — shared across all modules.

Per architecture [§8.6](../../PROJECT_ARCHITECTURE.md#86-llm-strategy) every
LLM call across the agent — Auto-RAG router/grader/rewriter, ToT probes,
faithfulness re-grade, Orchestrator's ``reason`` step, SD's ``analyze_code``
LLM augment, B&P's compose-answer — runs against the same local Ollama
instance. The embedder is also Ollama-backed so the POC needs no extra
HuggingFace / sentence-transformers dependency.

This module lives under ``src.shared`` so every component (RAG, B&P, SD,
Orchestrator, Portal) imports the same factory and gets two things for
free:

  * **Caching** — chat clients and the embedder are LRU-cached per
    ``(temperature, json_mode)`` so an entire refresh cycle reuses one
    HTTP keep-alive connection.
  * **Auditing** — every chat invoke runs through :py:class:`LoggedLLM`,
    which records ``(module, request, response, started_at, latency_ms)``
    into the SQLite log at ``$LLM_LOG_PATH``. The ``module`` tag is
    caller-supplied so the audit log shows *which sub-step* asked the LLM
    a question, not just *which service*.

Model knobs live in environment variables (see ``.env.example``):

  * ``OLLAMA_HOST``       — Ollama base URL.
  * ``LLM_MODEL``         — chat model (default ``llama3.1:8b``).
  * ``RAG_EMBED_MODEL``   — embedder model (default ``chroma/all-minilm-l6-v2-f32``).
  * ``LLM_LOG_PATH``      — SQLite call log (default ``./data/llm/calls.db``).
"""

from __future__ import annotations

import json
import os
import time
from functools import lru_cache
from typing import Any, Iterable

from chromadb.utils.embedding_functions import OllamaEmbeddingFunction
from langchain_core.messages import BaseMessage
from langchain_ollama import ChatOllama

from .llm_log import LLMCallLog


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def llm_model() -> str:
    return os.environ.get("LLM_MODEL", "llama3.1:8b")


def embed_model() -> str:
    return os.environ.get("RAG_EMBED_MODEL", "chroma/all-minilm-l6-v2-f32")


# ---------------------------------------------------------------------------
# Cached underlying clients
# ---------------------------------------------------------------------------

@lru_cache(maxsize=8)
def _build_chat_llm(temperature: float, json_mode: bool) -> ChatOllama:
    kwargs: dict[str, Any] = {
        "model": llm_model(),
        "base_url": ollama_host(),
        "temperature": temperature,
        "request_timeout": 60.0,
    }
    if json_mode:
        kwargs["format"] = "json"
    return ChatOllama(**kwargs)


@lru_cache(maxsize=2)
def get_embedding_function() -> OllamaEmbeddingFunction:
    """Return Chroma's Ollama embedder bound to our local Ollama. Embedding
    calls are *not* logged into the LLM call log — they run on a separate
    model, are tiny per-call, and would drown out the chat traffic."""
    return OllamaEmbeddingFunction(url=ollama_host(), model_name=embed_model())


# ---------------------------------------------------------------------------
# LoggedLLM — wraps ChatOllama.invoke / ainvoke with audit logging
# ---------------------------------------------------------------------------

class LoggedLLM:
    """Thin proxy around a cached ChatOllama bound to a ``module`` tag.

    Every ``invoke`` / ``ainvoke`` records one row in the ``LLMCallLog``
    so an operator can later reconstruct exactly what each module asked
    the LLM during a refresh cycle, in what order, and how long it took.
    The wrapper deliberately keeps the surface minimal — callers that
    need streaming or tool-bound variants can grab :py:attr:`raw` and
    handle logging themselves.
    """

    def __init__(
        self,
        llm: ChatOllama,
        *,
        module: str,
        temperature: float,
        json_mode: bool,
        logger: LLMCallLog | None = None,
    ):
        self._llm = llm
        self._module = module
        self._temperature = temperature
        self._json_mode = json_mode
        self._logger = logger or LLMCallLog.default()

    # -- public sync invoke -------------------------------------------------

    def invoke(self, messages: Iterable[BaseMessage] | str, **kwargs: Any) -> Any:
        request_text = self._serialise(messages)
        started = time.time()
        try:
            response = self._llm.invoke(messages, **kwargs)
        except Exception as exc:  # noqa: BLE001 — record then re-raise
            self._record(
                request=request_text,
                response="",
                started_at=started,
                latency_ms=(time.time() - started) * 1000.0,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        self._record(
            request=request_text,
            response=self._extract_text(response),
            started_at=started,
            latency_ms=(time.time() - started) * 1000.0,
        )
        return response

    # -- public async invoke ------------------------------------------------

    async def ainvoke(self, messages: Iterable[BaseMessage] | str, **kwargs: Any) -> Any:
        request_text = self._serialise(messages)
        started = time.time()
        try:
            response = await self._llm.ainvoke(messages, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._record(
                request=request_text,
                response="",
                started_at=started,
                latency_ms=(time.time() - started) * 1000.0,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        self._record(
            request=request_text,
            response=self._extract_text(response),
            started_at=started,
            latency_ms=(time.time() - started) * 1000.0,
        )
        return response

    # -- escape hatches -----------------------------------------------------

    @property
    def raw(self) -> ChatOllama:
        """Expose the underlying ChatOllama for callers that need bind_tools,
        streaming, etc. Bypasses logging — use sparingly."""
        return self._llm

    @property
    def module(self) -> str:
        return self._module

    # -- internals ----------------------------------------------------------

    def _record(
        self,
        *,
        request: str,
        response: str,
        started_at: float,
        latency_ms: float,
        error: str | None = None,
    ) -> None:
        # Span emission is fire-and-forget — a logging failure must NOT mask
        # the wrapped invoke's outcome (mirrors §9.6 OTel resilience note).
        try:
            self._logger.record(
                module=self._module,
                request=request,
                response=response,
                started_at=started_at,
                latency_ms=latency_ms,
                model=llm_model(),
                temperature=self._temperature,
                json_mode=self._json_mode,
                error=error,
            )
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _serialise(messages: Iterable[BaseMessage] | str) -> str:
        if isinstance(messages, str):
            return messages
        out: list[dict[str, str]] = []
        for m in messages:
            role = getattr(m, "type", None) or getattr(m, "role", None) or m.__class__.__name__
            content = getattr(m, "content", None)
            if not isinstance(content, str):
                content = str(content)
            out.append({"role": str(role), "content": content})
        return json.dumps(out, ensure_ascii=False)

    @staticmethod
    def _extract_text(response: Any) -> str:
        # ChatOllama returns an AIMessage with .content (string for our prompts).
        content = getattr(response, "content", None)
        if isinstance(content, str):
            return content
        if content is None:
            return str(response)
        return str(content)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_chat_llm(
    module: str,
    *,
    temperature: float = 0.0,
    json_mode: bool = False,
    logger: LLMCallLog | None = None,
) -> LoggedLLM:
    """Return a logged LLM bound to a ``module`` tag.

    Convention for the tag: ``<service>.<area>.<step>`` so the audit log
    reads as a sentence — e.g. ``rag.auto_rag.grader``,
    ``bp.background.compose_answer``, ``sd.analyze_code.llm_augment``.
    The tag is required so no module can accidentally land unlabeled rows
    in the log.
    """
    if not module or not module.strip():
        raise ValueError("get_chat_llm: a non-empty module tag is required")
    underlying = _build_chat_llm(temperature, json_mode)
    return LoggedLLM(
        underlying,
        module=module.strip(),
        temperature=temperature,
        json_mode=json_mode,
        logger=logger,
    )
