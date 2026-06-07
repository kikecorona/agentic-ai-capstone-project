# Business case: PearCare claim economics

PearCare is profitable when premiums collected exceed claim costs over
the term of a plan. The breakdown matters more than the headline.

## Claim cost by resolution path

| Path        | Cost we incur | Notes |
| ----------- | ------------- | ----- |
| support     | small (CS time) | A few hundred per 1k claims at most. |
| repair      | medium        | Vendor flat fees. Set in vendor contracts; not in this codebase. |
| replacement | full app price | We mint a license; no money to a vendor, but the app developer is paid for the replacement (per `developer-payouts.md` — that part is not built yet). |

## What the auto-triage does to economics

The default `_auto_triage` rule biases towards `support`. That is the
right default because support claims are nearly free and the alternative
is a real-money outflow on a claim that should never have qualified.

The risk is the other direction: a real damage claim that the operator
or the rule classifies as `support` and therefore underpays the user.
We do not currently track customer-satisfaction signals on claim
resolution. A reasonable next step would be a per-claim follow-up
rating.

## Open issues

* No expiry check at file-time. A user could file against an expired
  enrollment and we would happily approve it.
* No cap on per-enrollment claim count. PearCare+ promises "unlimited
  claims" but in practice this needs a soft limit + manual review
  threshold.
* Vendor cost is not stored anywhere in our system; we cannot compute
  margin without joining against an external billing report.
