# Business case: PearCare attach rate

## What "attach rate" means

The fraction of paid app purchases that include a PearCare plan.

```
attach_rate = orders_with_at_least_one_enrollment / paid_orders_with_eligible_app
```

The denominator only counts orders containing **eligible** apps —
i.e. apps that have entries in `APP_PLAN_MAP`. Computing attach rate
against the full storefront would conflate "we don't offer plans on
this app" with "the user said no", and would understate the metric
for cases like "user bought one eligible and three ineligible apps in
one cart".

## Why this matters

Warranty programs have a small set of inputs and three outputs that
matter: attach rate, claim rate, and average claim cost. Of those,
attach rate is the only one we can directly influence with product
changes. Claim rate is mostly a function of the underlying app
quality.

A 1-percentage-point swing in attach rate on premium apps materially
moves total PearCare revenue, because the SKUs are priced at 5–25%
of the underlying app price. We expect the curve to be steepest at
the app detail page, which is the only surface where the user
actually sees the offer.

## Levers we have today

* **Default presentation.** The detail page renders plans inline with
  the buy button. We currently default to "No protection" — explicit
  opt-in. Switching to a recommended-default would lift attach rate
  but raises a customer-trust question; we have not made that change.
* **Plan ladder shape.** Today eligible apps offer either a 3-rung
  (basic-12 / plus-12 / loss-12) or 2-rung (basic-12 / basic-24)
  ladder. Decoy effect studies suggest 3-rung outperforms 2-rung even
  if the middle SKU sells less than 5% of the time.
* **Eligible-app expansion.** Each app added to `APP_PLAN_MAP` adds
  surface area. We have been conservative: only premium / pro apps
  qualify today.

## Levers we don't have yet

* Post-purchase plan offer. Apple's "you can add AppleCare within 60
  days" model. Would require a new endpoint on plan-svc.
* Reminders close to expiry. No notification service exists yet.

## Pitfalls

* **Plan visibility on free apps.** A free app showing a $20 plan would
  be confusing. The `APP_PLAN_MAP` deliberately excludes free apps;
  do not relax this without a UX review.
* **Hidden plan churn.** Monthly billing (`plan.plus.month`) creates
  cancellation surface; see `pearcare-cancellations.md`.
