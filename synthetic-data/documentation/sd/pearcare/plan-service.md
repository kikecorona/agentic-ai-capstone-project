# PearCare plan service

Owns plan definitions and per-user enrollments.

## Data model

`Plan`:

| Field             | Type      | Notes |
| ----------------- | --------- | ----- |
| `id`              | string    | Stable, e.g. `plan.plus.12`. |
| `name`            | string    | User-facing label. |
| `tier`            | string    | `pearcare`, `pearcare_plus`, `pearcare_loss`. |
| `price_cents`     | int       | One-time charge, USD cents. |
| `term_months`     | int       | Coverage window. `1` if `monthly_billing`. |
| `coverage`        | list[str] | Human-readable coverage labels. |
| `monthly_billing` | bool      | True for the `plan.plus.month` SKU. |

`Enrollment` (created by `POST /enrollments`):

| Field            | Type     | Notes |
| ---------------- | -------- | ----- |
| `id`             | string   | `enr_<hex>`. |
| `user_id`        | string   | From account-svc. |
| `app_id`         | string   | From catalog-svc. |
| `order_id`       | string   | Optional; from order-svc. |
| `plan_id`        | string   | Snapshotted at enrollment time. |
| `plan_name`      | string   | Snapshot, used in receipts. |
| `tier`           | string   | Snapshot. |
| `coverage`       | list[str]| Snapshot. |
| `term_months`    | int      | Snapshot. |
| `monthly_billing`| bool     | Snapshot. |
| `starts_at`      | iso8601  | UTC. |
| `expires_at`     | iso8601  | `starts_at + term_months × 30 days`. |
| `status`         | string   | `active` or `cancelled`. |

The plan fields are snapshotted onto the enrollment so we can evaluate
a future claim even if the plan definition has been edited or removed.

## Endpoints

| Method | Path                               | Notes |
| ------ | ---------------------------------- | ----- |
| GET    | `/plans`                           | All plans. |
| GET    | `/plans/<plan_id>`                 | One plan. |
| GET    | `/plans/for-app/<app_id>`          | Plans offered for that app — drives the storefront UI. |
| POST   | `/enrollments`                     | Body: `{user_id, plan_id, app_id, order_id?}`. |
| GET    | `/enrollments/<enrollment_id>`     | One enrollment. |
| GET    | `/users/<user_id>/enrollments`     | A user's coverage list. |
| POST   | `/enrollments/<enrollment_id>/cancel` | Sets `status=cancelled`. |

## Which apps offer plans

Plans are offered per-app via the `APP_PLAN_MAP` in
`plan/seed_data.py`. Most catalog apps have **no plan**, by design:
warranty only makes sense for paid premium apps where a refund or
replacement has real cost. Premium creative apps (DAW, video editor,
architecture suite) get the full ladder. Premium games can also get
the loss tier.

## Notes

* Cancellation is a status flag; we do not delete enrollments. Past
  claims stay associated with the cancelled enrollment.
* No prorated refunds are computed at cancel — that policy is
  intentionally left to a future revision.
