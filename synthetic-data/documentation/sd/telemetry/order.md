# order — telemetry (HIGH)

The `order` service is the most heavily instrumented in the fleet.
It is the originator of the checkout trace, so getting its signals
right is what makes end-to-end tracing legible.

Wired in `synthetic-data/implementation/services/order/app.py`:

```python
from shared.otel import instrument_flask, child_span, traceparent_header

app = Flask(__name__)
TRACER, METER = instrument_flask(app, "order")
checkout_outcomes = METER.counter("order_checkout_outcomes_total")
checkout_total_cents = METER.histogram("order_checkout_total_cents")
```

## Spans

### Root span

Emitted by `instrument_flask` for every request:

| `name`                                       | When it fires                              |
| -------------------------------------------- | ------------------------------------------ |
| `order POST /checkout`                       | a checkout call                            |
| `order GET /orders/<order_id>`               | reading an order                           |
| `order GET /users/<user_id>/orders`          | listing a user's orders                    |
| `order POST /orders/<order_id>/refund`       | refund                                     |
| `order GET /health`                          | health probe                               |

Attributes: `http.method`, `http.route`, `http.status_code`, `status`
(`"success"` if `http.status_code < 400`, else `"failure"`).

### Child spans (checkout path)

`/checkout` opens four child spans, one per downstream call. Each
inherits the root's `trace_id` and uses the root span as its parent.

| `name`                                  | Attributes                                   | Notes                                |
| --------------------------------------- | -------------------------------------------- | ------------------------------------ |
| `downstream.cart.read`                  | `user_id`                                    | `GET cart/carts/<user_id>`           |
| `downstream.catalog.price_lookup`       | `item_count`                                 | one HTTP call **per item**           |
| `downstream.payment.authorize`          | `order_id`, `amount_cents`                   | `status=ERROR` on decline            |
| `downstream.fulfillment.fulfill`        | `order_id`, `app_count`                      | only opens after a successful auth   |

### Child spans (refund path)

| `name`                                  | Attributes                                   |
| --------------------------------------- | -------------------------------------------- |
| `downstream.payment.refund`             | `payment_id`                                 |

## Metrics

| Name                                  | Kind        | Attributes                                                                 |
| ------------------------------------- | ----------- | -------------------------------------------------------------------------- |
| `order_requests_total`                | counter     | `http.method`, `http.route`, `http.status_code`, `status`                  |
| `order_request_duration_ms`           | histogram   | same as above                                                              |
| `order_checkout_outcomes_total`       | counter     | `outcome` ∈ `{fulfilled, payment_failed, empty_cart}`                      |
| `order_checkout_total_cents`          | histogram   | `item_count`. Recorded **only on `fulfilled`** outcomes.                   |

## End-to-end example — happy path

A successful checkout produces five spans sharing a single
`trace_id`:

```
order POST /checkout                       (root)
├── downstream.cart.read
├── downstream.catalog.price_lookup
├── downstream.payment.authorize           (parented in `payment` too via traceparent)
└── downstream.fulfillment.fulfill         (parented in `fulfillment`)
```

Plus three metric points: `order_requests_total{status=success}`,
`order_request_duration_ms`, `order_checkout_outcomes_total{outcome=fulfilled}`,
and `order_checkout_total_cents`.

## End-to-end example — payment declined

The payment span is `status=ERROR`, the root span is also
`status=ERROR` (HTTP 402 → `status=failure` attribute), no
`fulfillment.fulfill` child opens, and:

- `order_requests_total{status=failure, http.status_code=402}` += 1
- `order_checkout_outcomes_total{outcome=payment_failed}` += 1
- `order_checkout_total_cents` is **not** recorded.

## Failure semantics

| Situation              | Span `status` | `attributes.status` | HTTP code | Outcome counter         |
| ---------------------- | ------------- | ------------------- | --------- | ----------------------- |
| Happy checkout         | OK            | success             | 200       | `outcome=fulfilled`     |
| Empty cart             | ERROR         | failure             | 400       | `outcome=empty_cart`    |
| Payment authorize 4xx  | ERROR         | failure             | 402       | `outcome=payment_failed`|
| Fulfillment crash      | ERROR         | failure             | 500       | (none)                  |

The fulfillment-crash row is currently a known dark spot — the
service raises before it can increment a counter. Tracked in
`../runbooks/incident-response.md` (LOW).

## Trace propagation

`order` sets a `traceparent` header on every outgoing call (cart,
catalog, payment, fulfillment). Whether each downstream stitches into
the same trace depends on whether it is instrumented:

| Downstream    | Stitched? | Reason                                |
| ------------- | --------- | ------------------------------------- |
| payment       | yes       | reads `traceparent`, emits root span  |
| fulfillment   | yes       | same                                  |
| cart          | no        | service is uninstrumented             |
| catalog       | partial   | catalog emits metrics but no spans    |

## Known gaps

- The refund flow has no business-level outcome counter
  (`order_refund_outcomes_total`). The doc gap agent should propose
  one.
- The price-lookup child span is one span around the *loop* of N
  calls, not one span per app. Per-item granularity would help when
  catalog is slow on a specific app.
- `payment_failed` does not break out the provider — the counter
  collapses Stripe / PayPal / PearCard declines together.
