# Telemetry — index and gap map

This is the documentation companion to `synthetic-data/telemetry/`.
It describes **what each service emits**, what is missing, and how
reliable the doc itself is. The quality is intentionally uneven.

```
documentation/telemetry/
├── README.md                  ← you are here
├── overview.md                (HIGH)
├── order.md                   (HIGH)
├── payment.md                 (HIGH)
├── fulfillment.md             (MEDIUM)
├── pearcare-claim.md          (MEDIUM)
├── catalog.md                 (LOW)
└── search.md                  (VERY LOW — claims metrics that don't exist)
(no doc files for: account, cart, review, pearcare-plan)
```

## Quick coverage matrix

| Service          | Code emits        | Doc quality     | Notes                                                    |
| ---------------- | ----------------- | --------------- | -------------------------------------------------------- |
| order            | spans + metrics   | HIGH            | reference doc — use as the template                      |
| payment          | spans + metrics   | HIGH            | metric names match the code, including refund            |
| fulfillment      | spans + metrics   | MEDIUM          | doc is correct but says nothing about per-app spans      |
| pearcare-claim   | spans + metrics   | MEDIUM          | doc covers requests, not the hooks                       |
| catalog          | metrics only      | LOW             | three-paragraph doc; doesn't explain the missing traces  |
| search           | NOTHING           | VERY LOW        | the doc claims a `search_queries_total` counter — it does not exist |
| account          | NOTHING           | (missing)       | gap — neither code nor doc                               |
| cart             | NOTHING           | (missing)       | gap                                                      |
| review           | NOTHING           | (missing)       | gap                                                      |
| pearcare-plan    | NOTHING           | (missing)       | gap                                                      |

## What a good telemetry doc looks like

See `order.md`. It covers, in order:

1. **What the service is and what it instruments.**
2. **Span inventory** — name, when it fires, attributes.
3. **Metric inventory** — name, kind, attributes, what each value means.
4. **End-to-end trace example** — one full happy-path checkout and one
   payment-declined trace, both stitched via `traceparent`.
5. **Failure semantics** — which conditions land as `status=ERROR`
   vs `status=OK with attributes.status=failure`, and why.
6. **Known gaps and follow-ups.**

Anything missing one of those sections is a candidate for the gap
agent to fill in.
