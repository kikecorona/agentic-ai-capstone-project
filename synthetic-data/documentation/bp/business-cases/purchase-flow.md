# Business case: the purchase flow

## Goal

A signed-in user with an app in their cart can press one button and end
up with that app in their library. Every step that asks them to type or
re-confirm something is a leak in the funnel.

## Why one orchestrator

The purchase flow is the place where retries, partial failures, and
"are we paid yet?" all collide. We made an early call: only one
service — order-svc — knows the full shape. Cart-svc never speaks to
payment. Fulfillment never speaks to cart. The payment service does
not know what an order is in any deep sense; it just knows there is an
amount and a token.

The benefit is operational: when something is wrong with a checkout,
there is exactly one service to look at. The cost is that order-svc is
the single most important service in the system to keep healthy.

## Money in, license out

Concretely the flow is:

1. The user presses *Check out*. The frontend posts to order-svc.
2. order-svc reads the cart, prices it from catalog, and creates a
   pending order.
3. order-svc authorizes payment. Decline → the order is preserved as
   `failed` for audit, and the frontend shows the provider error.
4. order-svc calls fulfillment, which mints licenses and per-platform
   download URLs.
5. order-svc clears the cart. The frontend redirects to the receipt page.

## Money lost windows

* **Payment authorized but fulfillment failed.** The user is charged
  and the order sits in `paid`. *Today this requires a manual
  refund* — see `../../sd/runbooks/incident-response.md`. Closing this gap is
  the highest-priority follow-up.
* **Catalog price drift.** If a price changes between "added to cart"
  and "checkout", we charge the new price. We chose this trade-off
  because the alternative — pricing at cart-add time — would mean
  carrying state in cart-svc that has to expire. Acceptable for now.

## Future enhancements

* Idempotent checkout. POST `/checkout` with the same `Idempotency-Key`
  should return the same order id, even on a retry. This is needed
  before we put a button-mashing user behind a flaky network.
* Saved payment methods on the account record. Right now every
  authorize call synthesizes a token.
* Receipt email via a future notification service.
