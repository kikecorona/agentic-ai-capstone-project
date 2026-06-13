# Catalog database

File: `data/catalog.db` (SQLite, WAL mode).

The catalog DB is the canonical store for every app available in the
storefront. It is read by every other service and written by exactly
two callers: the catalog service itself (on first-boot seeding) and the
review service (rating snapshot updates via the catalog API).

## Schema

```sql
CREATE TABLE apps (
    id            TEXT PRIMARY KEY,    -- stable, e.g. "app.001"
    name          TEXT NOT NULL,
    developer     TEXT NOT NULL,
    category      TEXT NOT NULL,
    price_cents   INTEGER NOT NULL,    -- 0 = free
    description   TEXT,
    icon_emoji    TEXT,
    rating        REAL    DEFAULT 0,   -- 1–5; pushed by review-svc
    rating_count  INTEGER DEFAULT 0,
    bundle_id     TEXT,                -- reverse DNS, e.g. "com.foo.bar"
    version       TEXT,                -- semver-ish
    size_mb       INTEGER
);

CREATE INDEX idx_apps_category ON apps(category);
```

## Seeding

On first boot, when `SELECT COUNT(*) FROM apps` returns 0, the service
imports every entry from `services/catalog/seed_data.py` and inserts
them in one batch. Subsequent restarts read from the existing rows;
editing the seed file does **not** retroactively update the database.
To re-seed, delete `data/catalog.db` and restart catalog-svc.

## Why ratings are denormalized here

A search query for "weather" needs to rank a couple thousand candidate
apps. Storing the latest aggregate (`rating`, `rating_count`) directly
on the `apps` row means search-svc does not need to join with
review-svc per result. Reviews and catalog agree on a one-way push:
review-svc updates catalog whenever a new review changes the average.

## Operational notes

* WAL is enabled (`PRAGMA journal_mode=WAL`) so search-svc reads do not
  block catalog-svc writes.
* `check_same_thread=False` is required because Flask reuses the
  connection across worker threads. All writes are short and
  autocommitted.
* No migrations framework is used. The schema is applied with
  `CREATE TABLE IF NOT EXISTS`; non-additive changes will need manual
  handling.

## Known gaps

* No `updated_at` column. We cannot tell when a row was last touched.
* Rating updates do not validate the caller — anyone who can reach the
  catalog API can clobber an app's average. In production this would be
  locked to review-svc.
