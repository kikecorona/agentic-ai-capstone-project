# pearcare-claim — telemetry (MEDIUM)

PearCare's claim service emits per-request signals plus a counter
that tracks triage decisions. The hook layer (`repair_vendor_hook`,
`replacement_hook`) is **not** wrapped in spans.

## Spans

Root spans only:

- `pearcare-claim POST /claims`
- `pearcare-claim POST /claims/<claim_id>/triage`
- `pearcare-claim POST /claims/<claim_id>/approve`
- `pearcare-claim POST /claims/<claim_id>/deny`
- `pearcare-claim GET /claims/<claim_id>`
- `pearcare-claim GET /users/<user_id>/claims`

## Metrics

| Name                                           | Kind      | Attributes                                            |
| ---------------------------------------------- | --------- | ----------------------------------------------------- |
| `pearcare-claim_requests_total`                | counter   | `http.method`, `http.route`, `http.status_code`, `status` |
| `pearcare-claim_request_duration_ms`           | histogram | same                                                  |
| `pearcare_claim_outcomes_total`                | counter   | `resolution` ∈ `{repair, replacement, support, deny}`, `tier` ∈ `{pearcare_basic, pearcare_plus, pearcare_loss}` |

`pearcare_claim_outcomes_total` increments on `/triage` after the
auto-triage decision is made. There is **not** a separate counter
on `/approve`, so the volume of approvals is implied from the
request counter.

## Known gaps

- The `repair_vendor_hook` and `replacement_hook` calls inside
  `/approve` are not spanned. A long approve span = a slow vendor
  call, but you cannot tell *which* vendor without going to the
  vendor system.
- `/approve` outcomes (vendor dispatched vs replacement issued)
  are not counted — they have to be inferred from the response body.
- `pearcare_claim_outcomes_total` uses an underscore-style metric
  name (`pearcare_claim_…`) while the request-level metrics use
  the hyphenated service name (`pearcare-claim_…`). This is a
  consistency wart; fix it together.
