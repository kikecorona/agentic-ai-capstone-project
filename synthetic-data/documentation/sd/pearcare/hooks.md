# PearCare hooks

There are two hooks. They are how the claim service does anything in
the real world.

## `replacement_hook.replace(claim_id, user_id, app_id, original_order_id)`

Mints a fresh entitlement on the same `app_id` by calling the main
fulfillment service:

```
POST {fulfillment}/fulfill
{
  "order_id": "replacement-<claim_id>",
  "user_id":  <user_id>,
  "app_ids":  [<app_id>]
}
```

The synthetic order id is what later distinguishes a replacement from a
real purchase in the receipt store. The user's existing license stays
in place — having two licenses for the same app is fine in PearCare's
data model.

## `repair_vendor_hook.dispatch(...)`

It picks a vendor and gives back a ticket. We don't really call any vendor.
TODO: write this up.
