# Fulfillment service

After payment is captured, fulfillment turns "the user paid for these
apps" into "the user can install these apps". For every app in an
order, fulfillment:

1. Mints a license via `license_hook.issue_license`. The license is the
   user's permanent proof of purchase.
2. Issues a per-platform download URL via `ios_distribution_hook` and
   `macos_distribution_hook`.
3. Records a receipt under the `order_id` and adds the app to the
   user's `library`.

A user's library is the union of every app they have a license for —
this is what "Restore purchases" reads from on a new device.

## Endpoints

| Method | Path                              | Notes |
| ------ | --------------------------------- | ----- |
| POST   | `/fulfill`                        | Body: `{order_id, user_id, app_ids[]}`. |
| GET    | `/receipts/<order_id>`            | One receipt with all entitlements. |
| GET    | `/users/<user_id>/library`        | Map of `app_id → license`. |

## Hooks

* `ios_distribution_hook.issue_download(app_id, user_id)` →
  `{platform, url, expires_at}`. URL is a stub.
* `macos_distribution_hook.issue_download(app_id, user_id)` → same shape.
* `license_hook.issue_license(app_id, user_id, order_id)` →
  `{license_id, app_id, user_id, order_id, kind: "perpetual"}`.

The hooks are intentionally thin so a real platform can be plugged in
behind them without changing the rest of the service.

## Why this is reused by PearCare

A "replacement" claim in PearCare is, end-to-end, equivalent to an
unpaid re-purchase: a fresh license keyed to the same `app_id`. The
PearCare claim service calls `POST /fulfill` directly, passing
`order_id = "replacement-<claim_id>"` so the receipt is distinguishable
from a real purchase. The user's library accumulates the new license
just as if they had bought the app twice.

## Known limitations

* Download URLs are deterministic strings; nothing actually serves them.
* No revocation. Cancelling or refunding does not invalidate the license.
* `expires_at` on download URLs is an hour from issue but is not
  enforced anywhere.
