"""Minimal, dependency-free OpenTelemetry-shaped instrumentation.

This is **not** the real `opentelemetry` SDK. It mimics the parts of the
API the demo services need (tracer, counter, histogram, traceparent
propagation) and writes OTLP-flavoured JSON Lines to disk so a
documentation-gap agent has something concrete to read.

Output layout
-------------
    synthetic-data/telemetry/<service>/
        traces.jsonl       one JSON object per span
        metrics.jsonl      one JSON object per metric data point

Each line is self-describing — `kind`, `name`, `service`, `attributes`,
and a timestamp — so a downstream tool can ingest the files without
knowing the schema in advance.

Coverage is intentionally uneven across services. See
`synthetic-data/documentation/sd/telemetry/README.md` for the gap map.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

# All telemetry lives next to the synthetic database files so the whole
# `synthetic-data/` tree stays self-contained. One subdirectory per
# instrumented service, created lazily on first emit.
TELEMETRY_DIR = (
    Path(__file__).resolve().parent.parent.parent / "telemetry"
)

_lock = threading.Lock()


def _service_dir(service: str) -> Path:
    d = TELEMETRY_DIR / service
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(service: str, kind: str, payload: dict) -> None:
    path = _service_dir(service) / f"{kind}.jsonl"
    with _lock:
        with open(path, "a") as f:
            f.write(json.dumps(payload, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class Counter:
    def __init__(self, service: str, name: str):
        self.service = service
        self.name = name

    def add(self, value: float = 1, attributes: dict | None = None) -> None:
        _write(self.service, "metrics", {
            "kind": "counter",
            "name": self.name,
            "service": self.service,
            "value": value,
            "attributes": attributes or {},
            "ts": time.time(),
        })


class Histogram:
    def __init__(self, service: str, name: str):
        self.service = service
        self.name = name

    def record(self, value: float, attributes: dict | None = None) -> None:
        _write(self.service, "metrics", {
            "kind": "histogram",
            "name": self.name,
            "service": self.service,
            "value": value,
            "attributes": attributes or {},
            "ts": time.time(),
        })


class Meter:
    def __init__(self, service: str):
        self.service = service

    def counter(self, name: str) -> Counter:
        return Counter(self.service, name)

    def histogram(self, name: str) -> Histogram:
        return Histogram(self.service, name)


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------

class Tracer:
    def __init__(self, service: str):
        self.service = service

    @contextmanager
    def span(self, name: str, attributes: dict | None = None,
             trace_id: str | None = None, parent_id: str | None = None):
        """Emit a span. Caller may pass `trace_id`/`parent_id` to attach to a
        request-scoped trace; otherwise a new trace is started."""
        tid = trace_id or uuid.uuid4().hex
        sid = uuid.uuid4().hex[:16]
        start_ns = time.time_ns()
        status = "OK"
        error = None
        try:
            yield {"trace_id": tid, "span_id": sid, "parent_id": parent_id}
        except Exception as e:
            status = "ERROR"
            error = repr(e)
            raise
        finally:
            end_ns = time.time_ns()
            _write(self.service, "traces", {
                "name": name,
                "service": self.service,
                "trace_id": tid,
                "span_id": sid,
                "parent_id": parent_id,
                "start_ns": start_ns,
                "end_ns": end_ns,
                "duration_ms": (end_ns - start_ns) / 1e6,
                "status": status,
                "attributes": attributes or {},
                "error": error,
            })


# ---------------------------------------------------------------------------
# Flask middleware — root span + request counter + latency histogram
# ---------------------------------------------------------------------------

_TRACEPARENT_HDR = "traceparent"


def _parse_traceparent(value: str):
    parts = value.split("-")
    if len(parts) != 4:
        return None, None
    return parts[1], parts[2]  # trace_id, parent_span_id


def instrument_flask(flask_app, service: str, *,
                     emit_traces: bool = True,
                     emit_metrics: bool = True):
    """Wire request-level OTel into a Flask app.

    Returns `(tracer, meter)` so the caller can add custom child spans or
    business-level metrics. `emit_traces` / `emit_metrics` exist so a
    service can opt in to only one signal — useful for the "low quality"
    services that publish counters but no spans.
    """
    tracer = Tracer(service)
    meter = Meter(service)

    requests_total = meter.counter(f"{service}_requests_total") if emit_metrics else None
    duration_ms = meter.histogram(f"{service}_request_duration_ms") if emit_metrics else None

    from flask import request, g

    @flask_app.before_request
    def _otel_start():
        g._otel_start_ns = time.time_ns()
        tp = request.headers.get(_TRACEPARENT_HDR)
        tid, parent = (None, None)
        if tp:
            tid, parent = _parse_traceparent(tp)
        g._otel_trace_id = tid or uuid.uuid4().hex
        g._otel_span_id = uuid.uuid4().hex[:16]
        g._otel_parent_id = parent

    @flask_app.after_request
    def _otel_end(response):
        if not hasattr(g, "_otel_start_ns"):
            return response
        end_ns = time.time_ns()
        elapsed_ms = (end_ns - g._otel_start_ns) / 1e6
        ok = response.status_code < 400
        attrs = {
            "http.method": request.method,
            "http.route": request.path,
            "http.status_code": response.status_code,
            "status": "success" if ok else "failure",
        }
        if requests_total is not None:
            requests_total.add(1, attrs)
        if duration_ms is not None:
            duration_ms.record(elapsed_ms, attrs)
        if emit_traces:
            _write(service, "traces", {
                "name": f"{service} {request.method} {request.path}",
                "service": service,
                "trace_id": g._otel_trace_id,
                "span_id": g._otel_span_id,
                "parent_id": g._otel_parent_id,
                "start_ns": g._otel_start_ns,
                "end_ns": end_ns,
                "duration_ms": elapsed_ms,
                "status": "OK" if ok else "ERROR",
                "attributes": attrs,
            })
        return response

    return tracer, meter


def child_span(tracer: Tracer, name: str, attributes: dict | None = None):
    """Open a span attached to the current Flask request's trace.

    Usage:
        with child_span(tracer, "downstream.payment.authorize"):
            requests.post(...)
    """
    from flask import g
    trace_id = getattr(g, "_otel_trace_id", None)
    parent_id = getattr(g, "_otel_span_id", None)
    return tracer.span(name, attributes=attributes,
                       trace_id=trace_id, parent_id=parent_id)


def traceparent_header() -> dict:
    """Return a `traceparent` header dict for the current request's trace.
    Pass into `requests.get(..., headers=traceparent_header())` so the
    downstream service stitches into the same trace."""
    from flask import g
    tid = getattr(g, "_otel_trace_id", None)
    sid = getattr(g, "_otel_span_id", None)
    if not tid or not sid:
        return {}
    return {_TRACEPARENT_HDR: f"00-{tid}-{sid}-01"}
