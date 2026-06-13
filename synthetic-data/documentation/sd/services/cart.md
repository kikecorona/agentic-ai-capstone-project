# Cart service

Per-user shopping cart. The cart is a flat list of `app_id` references —
no quantities, no variants, no bundles. Apps are unique within a cart
(adding the same app twice is a no-op).

## Endpoints

| Method | Path                                  | Notes |
| ------ | ------------------------------------- | ----- |
| GET    | `/carts/<user_id>`                    | Empty cart returns `{items: []}`. |
| POST   | `/carts/<user_id>/items`              | Body: `{app_id}`. Idempotent on duplicates. |
| DELETE | `/carts/<user_id>/items/<app_id>`     | 404 if item not present. |
| POST   | `/carts/<user_id>/clear`              | Called by order-svc after checkout. |

## Lifecycle

A cart is implicitly created the first time it is read or written. There
is no explicit create / delete. Carts are not durable across service
restarts.

## Notes

* The cart does not check that `app_id` exists in catalog. If a user
  sneaks in a bogus id, order-svc will catch it at checkout.
* No expiry / abandoned-cart sweep runs in this demo.
