# Catalog service

Source of truth for app metadata: name, developer, category, price,
icon, description, version, size, and the latest aggregated rating.

## Data model

`App` (defined in `shared/models.py`):

| Field          | Type    | Notes |
| -------------- | ------- | ----- |
| `id`           | string  | stable, e.g. `"app.001"`. |
| `name`         | string  | Display name. |
| `developer`    | string  | Developer / publisher name. |
| `category`     | string  | One of the values in `/categories`. |
| `price_cents`  | int     | `0` = free, otherwise USD cents. |
| `description`  | string  | Short marketing blurb. |
| `icon_emoji`   | string  | Stand-in for an icon asset. |
| `rating`       | float   | 1–5 stars; pushed by review-svc. |
| `rating_count` | int     | Number of ratings underneath `rating`. |
| `bundle_id`    | string  | Reverse DNS id (`com.foo.bar`). |
| `version`      | string  | Semver-ish. |
| `size_mb`      | int     | Approximate install size. |

## Endpoints

| Method | Path                              | Notes |
| ------ | --------------------------------- | ----- |
| GET    | `/apps`                           | Paginated. `?page=N&page_size=N`. |
| GET    | `/apps/<app_id>`                  | 404 on unknown id. |
| GET    | `/apps/by-category/<name>`        | Case-insensitive match. |
| GET    | `/categories`                     | Sorted unique list. |
| GET    | `/top-charts?limit=N`             | Ranked by `rating_count`. |
| POST   | `/apps/<app_id>/rating`           | Body: `{rating: float, rating_count: int}`. Used by review-svc. |
| GET    | `/health`                         | Health probe. |

## Seeding

`seed_data.py` defines fifty fake apps spread across the categories
Weather, Finance, Health, Navigation, Productivity, Lifestyle, Music,
Education, Photo & Video, Books, Games, Developer Tools, Utilities,
Travel, Reference, Food & Drink, News, Medical. The seed runs at module
import time — restart the service to wipe and reload.

## Why ratings are denormalized

A search query for "weather" needs to rank a couple thousand candidate
apps. Catalog stores each app's most recent aggregate so search-svc
does not need to fan out to review-svc per result. Reviews and catalog
agree on a one-way push: review-svc updates catalog whenever a new
review changes the average. There is no read-back path, and that is
intentional.

## Known limitations

* No write API for adding new apps; the catalog is read-only at runtime.
* No rate limiting on the rating endpoint — any caller can clobber an
  app's average. In production this would be locked to review-svc.
