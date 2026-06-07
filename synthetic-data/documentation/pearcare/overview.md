# PearCare overview

PearCare is the warranty product that sits next to the Pear Store. It
lets a user buy a protection plan against an app they own, and lets
them file claims when something goes wrong. PearCare is its own
sub-system in the codebase — own services, own ports, own data — but
it borrows three things from the storefront:

| What                | From which service       | Why |
| ------------------- | ------------------------ | --- |
| App metadata        | catalog-svc              | so a plan page can show the app it covers |
| User identity       | account-svc              | enrollments and claims are keyed by `user_id` |
| Replacement licenses | fulfillment-svc          | when a claim resolves to a replacement |

## Services

* **`pearcare-plan`** (port 15101) — the catalog of plans, plus
  per-user enrollments. See `plan-service.md`.
* **`pearcare-claim`** (port 15102) — claim filing, triage, approval,
  and the hooks that resolve a claim into a real-world action. See
  `claim-service.md`.

## Plan tiers

| Tier             | Coverage |
| ---------------- | -------- |
| `pearcare`       | Accidental damage, support |
| `pearcare_plus`  | + priority support, unlimited claims, data recovery |
| `pearcare_loss`  | + loss / theft replacement |

A plan is offered for a specific subset of apps — see `APP_PLAN_MAP`
in `plan/seed_data.py`. Most apps in the storefront have **no** plan;
only premium creative, pro dev, and a handful of premium games do.

## End-to-end story

1. User opens an app detail page. The frontend calls
   `pearcare-plan /plans/for-app/<id>`. If plans exist, a radio group
   of plans is rendered next to the buy button.
2. User picks a plan and adds the app to their cart. The plan choice
   is held in the session.
3. User checks out. The order service runs a normal storefront
   purchase. Once the order is in `fulfilled` state, the frontend
   calls `pearcare-plan /enrollments` for each pending plan, which
   creates an enrollment dated from now.
4. Later, the user files a claim from their account page. The frontend
   calls `pearcare-claim /claims`. The claim is auto-triaged, and from
   there an operator (or in this demo, a follow-up call) approves the
   claim. The hook fires and the user either gets a vendor ticket or a
   replacement license.

## Why a separate sub-system

The storefront cares about catalog, money, and downloads. PearCare
cares about enrollments, time windows, and claim eligibility. Coupling
them tightly would mean the storefront's order-svc state machine had
to grow `enrolled` and `claim-pending` states it does not need. A
separate sub-system also means PearCare can be deployed and rolled back
independently, which is important the first few months a warranty
product is live.
