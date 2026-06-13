# Pear Store — Telemetry

This directory is where the OpenTelemetry shim
(`synthetic-data/implementation/shared/otel.py`) writes spans and
metrics from the running services. One subdirectory per service,
created lazily on first emit:

```
telemetry/
├── README.md
├── <service>/
│   ├── traces.jsonl       one JSON object per span
│   └── metrics.jsonl      one JSON object per metric data point
...
```

Files are append-only JSON Lines and grow unbounded as the services
take traffic. Wipe the per-service directories whenever you want a
clean slate.

## Coverage map

Coverage is **deliberately uneven** so the documentation-gap agent has
gaps to find:

| Service          | Tier   | Spans                              | Metrics                                                 |
| ---------------- | ------ | ---------------------------------- | ------------------------------------------------------- |
| order            | HIGH   | root + child per downstream call   | `order_requests_total`, `order_request_duration_ms`, `order_checkout_outcomes_total`, `order_checkout_total_cents` |
| payment          | HIGH   | root + child per provider          | `payment_requests_total`, `payment_request_duration_ms`, `payment_authorize_outcomes_total`, `payment_refund_outcomes_total`, `payment_amount_cents` |
| fulfillment      | MEDIUM | root only (gap — no per-app spans) | `fulfillment_requests_total`, `fulfillment_request_duration_ms`, `fulfillment_licenses_issued_total` |
| pearcare-claim   | MEDIUM | root only (gap — hooks not spanned) | `pearcare-claim_requests_total`, `pearcare-claim_request_duration_ms`, `pearcare_claim_outcomes_total` |
| catalog          | LOW    | none (gap — `emit_traces=False`)   | `catalog_requests_total`, `catalog_request_duration_ms` |
| account, cart, review, search, pearcare-plan | NONE | —                       | —                                                       |

See `synthetic-data/documentation/sd/telemetry/` for per-service detail
and the gap map.

## Schema

Spans (`traces.jsonl`):

```json
{"name":"order POST /checkout","service":"order",
 "trace_id":"...","span_id":"...","parent_id":null,
 "start_ns":..., "end_ns":..., "duration_ms":287.412,
 "status":"OK",
 "attributes":{"http.method":"POST","http.route":"/checkout",
               "http.status_code":200,"status":"success"},
 "error":null}
```

Metrics (`metrics.jsonl`):

```json
{"kind":"counter","name":"order_requests_total","service":"order",
 "value":1,"attributes":{"status":"success", ...},"ts":1748000000.0}

{"kind":"histogram","name":"order_request_duration_ms","service":"order",
 "value":287.412,"attributes":{"status":"success", ...},"ts":1748000000.0}
```

## Generating data

Boot the stack and drive traffic:

```bash
cd synthetic-data/implementation
./start_all.sh
# hit some endpoints — see synthetic-data/implementation/README.md
./stop_all.sh
```

The shim creates the per-service subdirectories the first time each
instrumented service receives a request.
