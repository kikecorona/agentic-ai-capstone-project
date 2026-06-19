"""Span emission helper for services in the architecture.

§9.6 says span emission is a *cross-cutting concern* — the per-service
designs (§9.1–§9.4) "don't need to thread it explicitly". This module is
that thread: a context manager that wraps any inbound/outbound MCP call,
times it, captures status, and persists a span.

Two backends are supported, selected at construction time:

  * **In-process** (default for the POC) — writes directly to the same
    ``SpanStore`` SQLite file the OTel MCP server reads from. No extra
    network hop, no subprocess. Multiple services on the same workstation
    share the file safely (SpanStore serialises writes).

  * **MCP** — calls the OTel MCP's ``record_span`` tool. Used when the
    emitter is in a different process from the collector or when we want
    to exercise the MCP contract end-to-end (the validation harness uses
    this).

Trace and parent-span IDs are propagated through ``contextvars`` so a
nested ``span(...)`` block automatically inherits the active trace and
records its caller as ``parent_span_id`` — that is how a Portal → OC →
B&P → RAG chain ends up sharing one ``mcp.trace_id`` (§9.6 span attributes).

Usage::

    from src.shared.otel_client import OTelClient

    otel = OTelClient.from_env()  # default: in-process to $OTEL_DB_PATH

    with otel.span(service="rag_service", mcp_method="retrieve",
                   mcp_domain="bp") as span:
        # ... do work ...
        span.set_status("ok")
        span.set_payload_summary({"sources_count": 3})
        span.set_attribute("rewrites", 1)
"""

from __future__ import annotations

import contextvars
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol

from src.otel_mcp.store import Span, SpanStore


# ---------------------------------------------------------------------------
# Trace / span context propagation
# ---------------------------------------------------------------------------

# The current trace ID and the span_id of the active span. Used so that nested
# `span(...)` blocks pick up the right `trace_id` and `parent_span_id` without
# the caller having to thread them explicitly.
_active_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "otel_active_trace_id", default=None
)
_active_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "otel_active_span_id", default=None
)


def current_trace_id() -> str | None:
    return _active_trace_id.get()


def set_trace_id(trace_id: str | None) -> contextvars.Token:
    """Force the trace ID — used when a service receives an inbound call
    that already carries an ``mcp.trace_id`` so the new span joins the
    upstream trace instead of starting a fresh one."""
    return _active_trace_id.set(trace_id)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class _SpanSink(Protocol):
    def write(self, span: Span) -> None: ...


class _StoreSink:
    """Writes spans straight to the SQLite store — same file the OTel MCP
    server reads from. Used in-process by every service in the POC."""

    def __init__(self, store: SpanStore):
        self._store = store

    def write(self, span: Span) -> None:
        self._store.record(span)


class _CallableSink:
    """Wraps any callable that takes a kwargs dict (for example an MCP tool
    bound through ``langchain-mcp-adapters``). The validation harness wires
    this up to exercise the ``record_span`` tool of the OTel MCP."""

    def __init__(self, recorder):
        self._recorder = recorder

    def write(self, span: Span) -> None:
        self._recorder(**span.to_dict())


# ---------------------------------------------------------------------------
# Span builder (returned to the `with` block)
# ---------------------------------------------------------------------------

@dataclass
class _SpanBuilder:
    """Mutable handle returned to the user inside the ``with`` block.

    The actual ``Span`` is constructed once on context exit; everything
    here just accumulates attributes the caller wants on the eventual span.
    """

    span_id: str
    trace_id: str
    parent_span_id: str | None
    service: str
    mcp_method: str
    started_at: float
    mcp_domain: str | None = None
    mcp_status: str | None = None
    payload_summary: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def set_status(self, status: str) -> None:
        self.mcp_status = status

    def set_domain(self, domain: str) -> None:
        self.mcp_domain = domain

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_attributes(self, attrs: dict[str, Any]) -> None:
        self.attributes.update(attrs)

    def set_payload_summary(self, summary: dict[str, Any]) -> None:
        # §9.6 privacy: caller is responsible for keeping this to counts,
        # IDs, and statuses — never raw query text or document content.
        self.payload_summary.update(summary)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class OTelClient:
    """Service-side handle for emitting spans.

    Construct via :py:meth:`from_env` for the default in-process SQLite
    backend, or via :py:meth:`from_callable` to route through any callable
    (e.g. a ``record_span`` tool bound to the OTel MCP).
    """

    def __init__(self, sink: _SpanSink):
        self._sink = sink

    # -- factories ----------------------------------------------------------

    @classmethod
    def from_env(cls, db_path: str | Path | None = None) -> "OTelClient":
        path = db_path or os.environ.get("OTEL_DB_PATH", "./data/otel/spans.db")
        return cls(_StoreSink(SpanStore(path)))

    @classmethod
    def from_store(cls, store: SpanStore) -> "OTelClient":
        return cls(_StoreSink(store))

    @classmethod
    def from_callable(cls, recorder) -> "OTelClient":
        """Build a client that emits via any callable taking the kwargs
        ``record_span`` accepts. ``langchain-mcp-adapters`` exposes the
        OTel MCP's tools as awaitables, but for sync callers a small
        wrapper is enough — see ``scripts/validate_otel.py``."""
        return cls(_CallableSink(recorder))

    # -- emission -----------------------------------------------------------

    @contextmanager
    def span(
        self,
        service: str,
        mcp_method: str,
        *,
        mcp_domain: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[_SpanBuilder]:
        """Open a span. The span is recorded when the ``with`` block exits.

        On exception inside the block the span is recorded with
        ``mcp_status='error'`` and ``error`` populated, then the exception
        is re-raised — fire-and-forget per §9.6 ("a failure in OTEL_MCP
        never blocks a service-to-service call").
        """
        span_id = uuid.uuid4().hex
        trace_id = _active_trace_id.get() or uuid.uuid4().hex
        parent_span_id = _active_span_id.get()

        trace_token = _active_trace_id.set(trace_id)
        span_token = _active_span_id.set(span_id)
        builder = _SpanBuilder(
            span_id=span_id,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            service=service,
            mcp_method=mcp_method,
            mcp_domain=mcp_domain,
            started_at=time.time(),
            attributes=dict(attributes or {}),
        )
        started_at = builder.started_at
        try:
            yield builder
        except Exception as exc:  # noqa: BLE001 — cross-cutting wrapper
            builder.error = f"{type(exc).__name__}: {exc}"
            if builder.mcp_status is None:
                builder.mcp_status = "error"
            raise
        finally:
            ended_at = time.time()
            try:
                self._sink.write(
                    Span(
                        span_id=builder.span_id,
                        trace_id=builder.trace_id,
                        parent_span_id=builder.parent_span_id,
                        service=builder.service,
                        mcp_method=builder.mcp_method,
                        mcp_domain=builder.mcp_domain,
                        mcp_status=builder.mcp_status,
                        mcp_latency_ms=max(0.0, (ended_at - started_at) * 1000.0),
                        started_at=started_at,
                        ended_at=ended_at,
                        payload_summary=builder.payload_summary,
                        attributes=builder.attributes,
                        error=builder.error,
                    )
                )
            except Exception:  # noqa: BLE001 — swallow per §9.6 resilience note
                # Span emission is fire-and-forget; failure here must not
                # mask the wrapped call's outcome.
                pass
            _active_span_id.reset(span_token)
            _active_trace_id.reset(trace_token)
