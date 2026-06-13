# fulfillment — telemetry (MEDIUM)

`fulfillment` emits the standard Flask request-level signals plus a
business counter for licenses issued. There is no per-app
instrumentation inside `/fulfill`.

## Spans

Only the root span is emitted. `name` follows
`fulfillment <method> <route>`. Common ones:

- `fulfillment POST /fulfill`
- `fulfillment GET /receipts/<order_id>`
- `fulfillment GET /users/<user_id>/library`

The service honours inbound `traceparent`, so when `order` calls
`/fulfill` the root span attaches to the checkout trace as a sibling
of the payment span.

## Metrics

| Name                                       | Kind      | Attributes                                                |
| ------------------------------------------ | --------- | --------------------------------------------------------- |
| `fulfillment_requests_total`               | counter   | `http.method`, `http.route`, `http.status_code`, `status` |
| `fulfillment_request_duration_ms`          | histogram | same                                                      |
| `fulfillment_licenses_issued_total`        | counter   | `order_id`. Increments by `len(app_ids)` per `/fulfill`.  |

## Trace propagation

Inbound: yes (root span attaches to caller's trace).
Outbound: none — fulfillment talks to the in-process license / iOS /
macOS hooks but those calls are not HTTP and not spanned today.

## Known gaps

- **No per-app spans inside `/fulfill`.** The root span tells you
  fulfillment took 80 ms, but it does not tell you whether the time
  went to `license_hook`, the iOS distribution hook, or the macOS
  distribution hook. If one of those hooks regresses you cannot tell
  from telemetry which one.
- **`fulfillment_licenses_issued_total` has no `app_id` attribute.**
  Aggregating by app requires going to the database, not the metrics.
- **No counter for receipt reads vs library reads.** Both are GETs
  and end up under `fulfillment_requests_total`, sliced only by
  `http.route`.
