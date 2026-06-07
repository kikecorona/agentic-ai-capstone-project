# Pear Store — Documentation

This tree is **deliberately uneven**. It exists to be fed into a
documentation-quality / documentation-gap agent so the agent has
something to fix. If you are reading the docs as a human and a section
seems thin, sloppy, or outright wrong, that is on purpose.

## What lives here

```
documentation/
├── architecture/
│   ├── overview.md                 (HIGH)
│   ├── data-flow-purchase.md       (HIGH)
│   └── data-flow-pearcare-claim.md (MEDIUM)
├── services/
│   ├── catalog.md                  (HIGH)
│   ├── account.md                  (LOW)
│   ├── cart.md                     (MEDIUM)
│   ├── order.md                    (HIGH)
│   ├── payment.md                  (LOW — terse, missing fields)
│   ├── fulfillment.md              (HIGH)
│   ├── review.md                   (VERY LOW — typos, partial)
│   └── search.md                   (HIGH)
├── database/
│   ├── catalog-db.md               (HIGH — full schema + rationale)
│   ├── account-db.md               (VERY LOW — three sentences)
│   ├── cart-db.md                  (VERY LOW — one sentence)
│   ├── payment-db.md               (LOW — no schema, no constraints)
│   ├── review-db.md                (LOW — typos, hedges)
│   └── pearcare-claim-db.md        (VERY LOW — fragment)
│   (no doc files for: order, fulfillment, pearcare-plan)
├── telemetry/
│   ├── README.md                   (index + gap map)
│   ├── overview.md                 (HIGH)
│   ├── order.md                    (HIGH)
│   ├── payment.md                  (HIGH)
│   ├── fulfillment.md              (MEDIUM)
│   ├── pearcare-claim.md           (MEDIUM)
│   ├── catalog.md                  (LOW)
│   └── search.md                   (VERY LOW — claims metrics that don't exist)
│   (no doc files for: account, cart, review, pearcare-plan)
├── pearcare/
│   ├── overview.md                 (HIGH)
│   ├── plan-service.md             (HIGH)
│   ├── claim-service.md            (MEDIUM)
│   ├── hooks.md                    (LOW — repair-vendor hook is barely covered)
│   └── integration.md              (HIGH)
├── business-cases/
│   ├── catalog-discovery.md        (HIGH)
│   ├── purchase-flow.md            (HIGH)
│   ├── refunds.md                  (LOW — placeholder-ish)
│   ├── developer-payouts.md        (MEDIUM)
│   ├── piracy-and-licensing.md     (LOW — typos)
│   ├── ratings-trust.md            (VERY LOW)
│   ├── pearcare-attach-rate.md     (HIGH)
│   ├── pearcare-claim-economics.md (MEDIUM)
│   └── pearcare-cancellations.md   (LOW — vague)
└── runbooks/
    ├── deploy.md                   (MEDIUM)
    ├── incident-response.md        (LOW — terse)
    └── pearcare-fraud.md           (VERY LOW)
```

## The gap map

### Service docs

| Doc                                | Quality   | What's wrong / missing |
| ---------------------------------- | --------- | ----------------------- |
| `services/account.md`              | LOW       | one paragraph; no endpoint contract; no error model |
| `services/payment.md`              | LOW       | provider hooks not documented; no failure semantics; missing fields |
| `services/review.md`               | VERY LOW  | typos, no endpoint reference, claims fields that don't exist |
| `pearcare/hooks.md`                | LOW       | replacement hook described, repair-vendor hook is almost a stub |

### Database docs

| Doc                                | Quality   | What's wrong / missing |
| ---------------------------------- | --------- | ----------------------- |
| `database/catalog-db.md`           | HIGH      | full schema, seeding behavior, op notes, known gaps — use as a template |
| `database/account-db.md`           | VERY LOW  | no schema, no column types, no constraints, no PK/index info |
| `database/cart-db.md`              | VERY LOW  | one sentence; nothing about the composite PK or why it makes adds idempotent |
| `database/payment-db.md`           | LOW       | no DDL, no FK relationships, no provider state diagram |
| `database/review-db.md`            | LOW       | typos, hedges ("i think"), no schema, no rating-push contract |
| `database/pearcare-claim-db.md`    | VERY LOW  | fragment; nothing about the JSON `replacement` column shape, no FK to enrollments |
| (missing) `database/order-db.md`           | MISSING   | two-table design (orders + order_items), state machine, FK / cascade behavior |
| (missing) `database/fulfillment-db.md`     | MISSING   | receipts (JSON blob) + licenses (per-user-per-app) and why they coexist |
| (missing) `database/pearcare-plan-db.md`   | MISSING   | enrollment table; coverage stored as JSON; snapshotted plan fields |

### Business cases

| Doc                                | Quality   | What's wrong / missing |
| ---------------------------------- | --------- | ----------------------- |
| `business-cases/refunds.md`        | LOW       | mostly TODOs |
| `business-cases/piracy-and-licensing.md`   | LOW | rambling; some claims don't match the code |
| `business-cases/ratings-trust.md`  | VERY LOW  | a few sentences, no detail |
| `business-cases/pearcare-cancellations.md` | LOW | no numbers, no policy |

### Runbooks

| Doc                                | Quality   | What's wrong / missing |
| ---------------------------------- | --------- | ----------------------- |
| `runbooks/incident-response.md`    | LOW       | tells you to "page someone", that's about it |
| `runbooks/pearcare-fraud.md`       | VERY LOW  | placeholder |

### Telemetry

| Doc                                | Quality   | What's wrong / missing |
| ---------------------------------- | --------- | ----------------------- |
| `telemetry/overview.md`            | HIGH      | conventions, signals, propagation, tier definition |
| `telemetry/order.md`               | HIGH      | full span + metric inventory; use as a template |
| `telemetry/payment.md`             | HIGH      | provider semantics, refund flow, propagation |
| `telemetry/fulfillment.md`         | MEDIUM    | request signals are correct; per-app spans missing from the *code* and called out as gap |
| `telemetry/pearcare-claim.md`      | MEDIUM    | hooks not spanned; metric-name inconsistency flagged |
| `telemetry/catalog.md`             | LOW       | three lines; doesn't explain the missing traces or the 404 path |
| `telemetry/search.md`              | VERY LOW  | typos, hedges ("i think"), claims `search_queries_total` / `search_query_duration_ms` / `search_zero_result_total` — none of which exist in the code |
| (missing) `telemetry/account.md`   | MISSING   | service is uninstrumented; no doc says so |
| (missing) `telemetry/cart.md`      | MISSING   | same |
| (missing) `telemetry/review.md`    | MISSING   | same |
| (missing) `telemetry/pearcare-plan.md` | MISSING | same |

Everything else is intended to be a reasonable target the gap-filler
can match against. `database/catalog-db.md` is the deliberate exemplar
for what a good per-service DB doc looks like; the rest of the
`database/` directory is what the gap-filler should bring up to par.
`telemetry/order.md` plays the same role for telemetry docs.
