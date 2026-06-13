# Search service

Lightweight full-text search over the catalog. Indexes name, developer,
and description.

## Endpoint

`GET /search?q=...&category=...&price=free|paid&limit=N`

| Param      | Required | Notes |
| ---------- | -------- | ----- |
| `q`        | no       | Free-text query. Empty `q` returns the most popular apps. |
| `category` | no       | Exact category match (case-insensitive). |
| `price`    | no       | `free` for `price_cents == 0`, `paid` for the rest. |
| `limit`    | no       | Defaults to 20. |

## Ranking

Tokens come from a permissive `[a-z0-9]+` regex over lowercased text.
For each query token we look at every haystack token from `name +
developer + description`:

* exact match: +3
* prefix match: +1

Apps are sorted by `(score desc, rating_count desc)`. The popularity
tiebreaker matters when many apps match a generic query like "weather"
or "todo".

## Index freshness

The index is rebuilt by calling catalog-svc on every search. This is
fine for fifty apps; in a real catalog we'd subscribe to a change feed.
The contract here is "search reflects whatever catalog returned right
now".

## Not supported

* Misspellings. `wether` will not find "Weather".
* Synonyms. `runner` does not match "jogging".
* Personalization. The same query returns the same results for everyone.
