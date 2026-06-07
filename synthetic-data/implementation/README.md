# Pear Store — Implementation

A small Apple App Store–style e-commerce system, split across one frontend,
eight backend microservices for the storefront, and a sibling **PearCare**
warranty system (two services). Each service is a self-contained Flask app
with its own SQLite database under `synthetic-data/data/`. The goal is to
mirror the *shape* of a real digital storefront — catalog, accounts, cart,
orders, payments, fulfillment, reviews, search, plus a parallel warranty
product — at small scale.

## Layout

```
implementation/
├── frontend/                 # Storefront website (Flask + Jinja templates)
├── services/
│   ├── catalog/              # App listings, categories, top charts
│   ├── account/              # User registration and login
│   ├── cart/                 # Shopping cart per user
│   ├── order/                # Order lifecycle and history
│   ├── payment/              # Payment authorization + provider hooks
│   ├── fulfillment/          # License + download URL provisioning
│   ├── review/               # Star ratings and written reviews
│   ├── search/               # Full-text search over the catalog
│   └── pearcare/             # ── Sibling warranty system ──
│       ├── plan/             #   Warranty plans + per-user enrollments
│       └── claim/            #   Claim filing, triage, repair / replacement
├── shared/                   # Shared dataclasses + service registry + OTel shim
├── requirements.txt
├── start_all.sh              # Boots every service on its port
└── stop_all.sh               # Stops everything started by start_all.sh
```

## Ports

| Service          | Port |
| ---------------- | ---- |
| frontend         | 15000 |
| catalog          | 15001 |
| account          | 15002 |
| cart             | 15003 |
| order            | 15004 |
| payment          | 15005 |
| fulfillment      | 15006 |
| review           | 15007 |
| search           | 15008 |
| pearcare-plan    | 15101 |
| pearcare-claim   | 15102 |

## Run it

```bash
pip install -r requirements.txt
./start_all.sh                # starts every service in the background
open http://localhost:15000   # the storefront
```

To stop everything: `./stop_all.sh`.

## Persistence

Each stateful service owns one SQLite file under `synthetic-data/data/`:

```
data/
├── catalog.db
├── account.db
├── cart.db
├── order.db
├── payment.db
├── fulfillment.db
├── review.db
├── pearcare-plan.db
└── pearcare-claim.db
```

Catalog and account seed themselves on first boot (when the relevant
table is empty). To reset everything to a clean state, stop the
services and delete `data/*.db` (and any `*.db-wal` / `*.db-shm`
sidecars). Restarting will re-create the schema and re-seed.

The search service has no DB — it pulls a fresh catalog snapshot on
every query.

## Telemetry

A subset of the services is wired up to publish OpenTelemetry-shaped
spans and metrics through a tiny in-process shim
(`shared/otel.py`). Output JSONL files land under
`synthetic-data/telemetry/<service>/` and are written live as the
services take traffic.

Coverage is **deliberately uneven** to mirror the documentation tree:

| Service          | Coverage tier | What it emits                                            |
| ---------------- | ------------- | -------------------------------------------------------- |
| order            | HIGH          | root + child spans (cart, catalog, payment, fulfillment), full metrics, business counters |
| payment          | HIGH          | root + per-provider child spans, authorize/refund counters, amount histogram |
| fulfillment      | MEDIUM        | root spans + metrics; license counter; **no per-app spans** |
| pearcare-claim   | MEDIUM        | root spans + metrics + triage outcome counter; **hooks not spanned** |
| catalog          | LOW           | metrics only — no traces                                 |
| account, cart, review, search, pearcare-plan | NONE | uninstrumented                                           |

See `synthetic-data/telemetry/README.md` for the schema, the gap map,
and pre-baked fixture files.

## How PearCare integrates

The storefront talks to PearCare in three places:

1. **App detail page** — fetches `GET /plans/for-app/<id>` from
   `pearcare-plan` and renders any plans for that app inline with the
   buy button. Most apps have no plans; pro / premium apps do.
2. **Cart → checkout** — when the user picks a plan on an app's detail
   page, the choice is stashed in the session. After the main `order`
   service completes checkout, the frontend calls
   `POST /enrollments` against `pearcare-plan` for each pending plan.
3. **Account page** — lists the user's active enrollments and exposes
   a "File claim" form, which posts to `pearcare-claim` and triggers
   either a vendor repair or a replacement entitlement (the latter
   re-enters the main `fulfillment` service).

So PearCare is its own thing — own data model, own services, own ports —
but it borrows the storefront for distribution and the main fulfillment
service when a claim resolves to a replacement.

## A note on data quality

This system is built to be used as synthetic input for a
documentation-gap detection agent. **Service documentation is
intentionally uneven** — some services are documented thoroughly,
others have terse, incomplete, or misleading docs. See
`documentation/README.md` for the gap map.
