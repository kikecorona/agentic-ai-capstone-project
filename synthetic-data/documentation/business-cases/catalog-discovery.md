# Business case: catalog discovery

## Goal

Get a user from "I opened the store" to "I opened an app detail page"
in as few decisions as possible. Discovery is the top of the purchase
funnel; if it leaks, every downstream conversion metric leaks with it.

## Audience

Two persona shapes:

* **Browsers** — no specific app in mind. They scroll Top Charts and
  category strips. Conversion is driven by *what we surface*.
* **Hunters** — they typed a name into the search bar. Conversion is
  driven by *whether we found it*. Latency matters more than ranking.

## How the system supports it

* `GET /top-charts` on catalog-svc gives the home page a stable,
  popularity-ordered slice. We don't personalize; ordering is purely
  `rating_count`. This is intentional for now — until we have logged-in
  behavior, personalization would be guessing.
* Categories are the secondary navigation. They live on catalog-svc to
  keep the home page render to one upstream call.
* Search is a separate service so we can iterate on ranking without
  redeploying catalog. The home page never calls search.
* We push the rating snapshot from review-svc back into catalog so
  ranking does not require a fan-out per result.

## Business levers

* **Editorial overrides** (not yet implemented): pin certain apps to the
  top of charts during a launch window. Would belong in catalog-svc as
  a `POST /charts/pin` admin endpoint.
* **Category strips on the home page**: a future home page could pull
  the top three apps per category. Today we only show the flat top
  chart and a category navigation row.
* **Search synonyms**: today's tokenizer is exact-match-only. A
  synonyms file would meaningfully improve hunter conversion.

## Measurable outcomes

* Time-to-first-detail-page-view (T1) — the dominant funnel metric.
  Targets to keep the system under pressure to stay quick.
* Search-to-detail conversion (search results clicked / searches).
* Top-chart click-through rate.
