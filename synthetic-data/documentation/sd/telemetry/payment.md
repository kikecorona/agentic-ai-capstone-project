# payment — telemetry (HIGH)

Wired in `synthetic-data/implementation/services/payment/app.py`:

```python
TRACER, METER = instrument_flask(app, "payment")
auth_outcomes   = METER.counter("payment_authorize_outcomes_total")
refund_outcomes = METER.counter("payment_refund_outcomes_total")
amount_cents    = METER.histogram("payment_amount_cents")
```

## Spans

### Root spans

| `name`                                | When it fires                                |
| ------------------------------------- | -------------------------------------------- |
| `payment POST /authorize`             | `/authorize` — picks a provider, charges     |
| `payment POST /refund`                | `/refund` — looks up the payment, refunds    |
| `payment GET /payments/<pid>`         | read                                         |
| `payment GET /health`                 | health probe                                 |

`status=ERROR` when the provider declines (HTTP 402) or the payment
record is missing (HTTP 404).

### Child spans

One per provider call:

| `name`                                | When it fires                                | Attributes                                |
| ------------------------------------- | -------------------------------------------- | ----------------------------------------- |
| `provider.stripe.authorize`           | inside `/authorize` for a `tok_*` token      | `provider="stripe"`, `amount_cents`       |
| `provider.paypal.authorize`           | inside `/authorize` for a `pp_*` token       | `provider="paypal"`, `amount_cents`       |
| `provider.pearcard.authorize`         | inside `/authorize` for a `pc_*` token       | `provider="pearcard"`, `amount_cents`     |
| `provider.<name>.refund`              | inside `/refund` for the matching provider   | `provider`, `payment_id`                  |

If the provider declines the charge, the child span is `status=ERROR`
and carries `error="card_declined"` (or whichever string the hook
returned).

## Metrics

| Name                                       | Kind        | Attributes                                                       |
| ------------------------------------------ | ----------- | ---------------------------------------------------------------- |
| `payment_requests_total`                   | counter     | `http.method`, `http.route`, `http.status_code`, `status`        |
| `payment_request_duration_ms`              | histogram   | same                                                             |
| `payment_authorize_outcomes_total`         | counter     | `provider`, `outcome` ∈ `{authorized, declined}`                 |
| `payment_refund_outcomes_total`            | counter     | `provider`, `outcome` ∈ `{refunded}`                             |
| `payment_amount_cents`                     | histogram   | `provider`. **Recorded only on successful authorizations.**      |

## Notes on the provider attribute

`provider` is derived from the token prefix:

```
tok_*  → stripe
pp_*   → paypal
pc_*   → pearcard
(none) → random pick (synthetic only)
```

This means a request without an explicit `token` will round-robin
across providers, which is **not** a realistic production behaviour;
in the demo it is what gives the synthetic data its provider mix.

## Trace propagation

`payment` reads inbound `traceparent` headers and stitches into the
caller's trace, so a checkout from `order` produces a single
multi-service trace. Outbound calls — e.g. provider hooks — are
in-process and don't need propagation.

## Failure semantics

| Situation                  | Span `status` | `attributes.status` | HTTP code | Counter                                            |
| -------------------------- | ------------- | ------------------- | --------- | -------------------------------------------------- |
| Authorize OK               | OK            | success             | 200       | `payment_authorize_outcomes_total{outcome=authorized}` |
| Authorize declined         | ERROR         | failure             | 402       | `payment_authorize_outcomes_total{outcome=declined}`   |
| Authorize unknown token    | ERROR         | failure             | 402       | declined (provider rejects)                        |
| Refund OK                  | OK            | success             | 200       | `payment_refund_outcomes_total{outcome=refunded}`  |
| Refund of unknown payment  | ERROR         | failure             | 404       | (none)                                             |

## Known gaps

- A 4xx on `/refund` for an unknown payment id has no outcome
  counter — the gap agent should suggest one (e.g. an
  `outcome=not_found` value on the existing counter).
- Latency is not bucketed per provider. Stripe is consistently fast
  (~30 ms p50) and PearCard is consistently slow (~55 ms p50), but
  that is only visible if you slice the histogram by `provider`.
