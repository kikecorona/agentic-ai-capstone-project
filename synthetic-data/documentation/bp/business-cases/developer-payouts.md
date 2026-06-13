# Business case: developer payouts

Pear Store is a marketplace; developers expect to be paid. This is
out of scope for the current implementation, but the system shape
already has hooks where a payout module would slot in.

## Where the money sits today

* Every successful purchase creates a `Payment` row in payment-svc with
  `amount_cents` and a provider `charge_id`.
* Every fulfilled order has a receipt in fulfillment-svc with the list
  of `app_ids` that were sold.

## What a payout module would do

A nightly job (or, more realistically, a periodic settlement task)
would:

1. Walk receipts since last settlement.
2. For each `app_id` in the receipts, look up the developer in catalog.
3. Aggregate amounts per developer, less the platform fee (TBD; the
   industry default of 30% is a starting point but is not encoded
   anywhere yet).
4. Issue a payout via a provider (ACH, SEPA, or a wallet platform —
   we'd add a hook similar to `stripe_hook` / `paypal_hook`).

## Open questions

* **Refunds and chargebacks.** A payout has to net out refunds; today
  refunds are manual.
* **Tax forms.** 1099 / equivalents per region.
* **Currency.** Today everything is USD cents. Multi-currency catalog
  pricing is a follow-up.

## Why it isn't in this build

We chose to focus on the user-facing flow. Developer accounting is
its own product surface (developer portal, statements, dispute UX) and
would double the codebase without changing the storefront story. It
is on the roadmap.
