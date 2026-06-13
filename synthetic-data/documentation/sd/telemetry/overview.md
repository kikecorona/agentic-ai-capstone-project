# Telemetry overview (HIGH)

Every backend service in the Pear Store has the **option** to publish
OpenTelemetry-shaped signals through a small in-process shim
(`shared/otel.py`). The shim is dependency-free: it does not import
the real `opentelemetry` package. It exists so the synthetic
demo can produce realistic telemetry shapes without taking on the
weight of the full SDK.

> **Don't copy this pattern into a real system.** A production
> deployment would use the real OpenTelemetry SDK with an OTLP
> exporter pointing at a collector. The shim's job is to make the
> *observability layer* of the synthetic data look familiar enough
> for a documentation-gap agent to reason about.

## Signals

Two OTel signal types are exercised:

### Tracing

Every instrumented service registers a Flask `before_request`/
`after_request` pair that emits one **root span** per HTTP request:

| Field             | Meaning                                                           |
| ----------------- | ----------------------------------------------------------------- |
| `name`            | `"<service> <method> <route>"` (e.g. `"order POST /checkout"`)    |
| `trace_id`        | hex; lifted from the inbound `traceparent` header if present      |
| `span_id`         | hex                                                               |
| `parent_id`       | the span id from `traceparent`, or `null` for a request that originates a trace |
| `start_ns` / `end_ns` / `duration_ms` | wall-clock window of the request                      |
| `status`          | `"OK"` if HTTP < 400, `"ERROR"` otherwise                         |
| `attributes`      | `http.method`, `http.route`, `http.status_code`, plus a derived `status` of `"success"` / `"failure"` |

Services may also emit **child spans** with `child_span(tracer, name, attrs)`.
Children get the request's trace id and parent id automatically. Today
only `order` and `payment` emit child spans — see `order.md` and
`payment.md`.

### Metrics

Every instrumented service publishes two request-level signals:

| Metric                                | Kind        | Attributes                                                    |
| ------------------------------------- | ----------- | ------------------------------------------------------------- |
| `<service>_requests_total`            | counter     | `http.method`, `http.route`, `http.status_code`, `status`     |
| `<service>_request_duration_ms`       | histogram   | same as above                                                 |

`status` is `"success"` if `http.status_code < 400`, else `"failure"`.
That is how success vs failure rate is computed downstream.

Services may also publish **business metrics**. Those are listed in
the per-service docs — for example `order_checkout_outcomes_total`,
`payment_amount_cents`, or `fulfillment_licenses_issued_total`.

## Trace propagation

Outgoing HTTP from an instrumented service should set a
`traceparent` header so the downstream span stitches into the
same trace. The helper:

```python
from shared.otel import traceparent_header
requests.get(url, headers=traceparent_header(), timeout=5)
```

Today `order` is the only originator that propagates everywhere it
calls. Whether the downstream service honours the header depends on
whether *it* is instrumented.

## Output format

All signals are written to `synthetic-data/telemetry/<service>/` as
append-only JSON Lines (`traces.jsonl`, `metrics.jsonl`). One record
per line, no batching. The directory is created lazily the first
time each service emits.

## Coverage tiers

The fleet is split into four tiers on purpose:

| Tier    | Services            | What they emit                                           |
| ------- | ------------------- | -------------------------------------------------------- |
| HIGH    | order, payment      | root spans, child spans for downstreams, full metrics    |
| MEDIUM  | fulfillment, pearcare-claim | root spans + metrics; business counters; **no child spans** |
| LOW     | catalog             | metrics only — no traces                                 |
| NONE    | account, cart, review, search, pearcare-plan | nothing                                                  |

The tiering is what makes it possible to run the demo against a
gap-detection agent: the agent should notice that `account` /
`cart` / `review` / `search` / `pearcare-plan` produce no telemetry,
that catalog has no traces, and that the medium-tier services are
opaque past the request boundary.
