"""Catalog service.

Owns the canonical record of every app available in the store: name,
developer, category, price, version, and rating snapshot. Other services
treat this as the source of truth for app metadata and never cache more
than they need to.

Endpoints:

    GET  /apps                       — list all apps (paginated)
    GET  /apps/<app_id>              — single app
    GET  /apps/by-category/<name>    — filter by category
    GET  /categories                 — list all categories
    GET  /top-charts?limit=N         — most-downloaded apps
    POST /apps/<app_id>/rating       — push a new (rating, count) snapshot
                                        (review service calls this)

Persistence: SQLite at `data/catalog.db`. The seed file is loaded once
on first start (when the `apps` table is empty); subsequent restarts
read from the existing DB.
"""
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..")))

from flask import Flask, jsonify, request, abort

from shared.db import connect, init_schema
from shared.otel import instrument_flask
from services.catalog.seed_data import SEED_APPS

app = Flask(__name__)
# catalog is intentionally instrumented at the lowest tier — metrics
# only, no spans. The SD agent should flag this as a coverage gap.
instrument_flask(app, "catalog", emit_traces=False)
DB = connect("catalog")
init_schema(DB, """
CREATE TABLE IF NOT EXISTS apps (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    developer     TEXT NOT NULL,
    category      TEXT NOT NULL,
    price_cents   INTEGER NOT NULL,
    description   TEXT,
    icon_emoji    TEXT,
    rating        REAL    DEFAULT 0,
    rating_count  INTEGER DEFAULT 0,
    bundle_id     TEXT,
    version       TEXT,
    size_mb       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_apps_category ON apps(category);
""")


def _seed_if_empty():
    n = DB.execute("SELECT COUNT(*) AS c FROM apps").fetchone()["c"]
    if n:
        return
    rows = [(a.id, a.name, a.developer, a.category, a.price_cents, a.description,
             a.icon_emoji, a.rating, a.rating_count, a.bundle_id, a.version, a.size_mb)
            for a in SEED_APPS]
    DB.executemany(
        "INSERT INTO apps(id, name, developer, category, price_cents, description, "
        "icon_emoji, rating, rating_count, bundle_id, version, size_mb) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)


_seed_if_empty()


def _row_to_dict(r):
    return dict(r) if r else None


@app.get("/apps")
def list_apps():
    page = int(request.args.get("page", 1))
    page_size = int(request.args.get("page_size", 50))
    total = DB.execute("SELECT COUNT(*) AS c FROM apps").fetchone()["c"]
    offset = (page - 1) * page_size
    rows = DB.execute("SELECT * FROM apps ORDER BY id LIMIT ? OFFSET ?",
                      (page_size, offset)).fetchall()
    return jsonify({
        "page": page, "page_size": page_size, "total": total,
        "items": [_row_to_dict(r) for r in rows],
    })


@app.get("/apps/<app_id>")
def get_app(app_id):
    r = DB.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    if not r:
        abort(404, description=f"unknown app {app_id}")
    return jsonify(_row_to_dict(r))


@app.get("/apps/by-category/<name>")
def by_category(name):
    rows = DB.execute("SELECT * FROM apps WHERE LOWER(category) = LOWER(?)",
                      (name,)).fetchall()
    return jsonify({"category": name, "items": [_row_to_dict(r) for r in rows]})


@app.get("/categories")
def list_categories():
    rows = DB.execute("SELECT DISTINCT category FROM apps ORDER BY category").fetchall()
    return jsonify([r["category"] for r in rows])


@app.get("/top-charts")
def charts():
    limit = int(request.args.get("limit", 10))
    rows = DB.execute(
        "SELECT * FROM apps ORDER BY rating_count DESC LIMIT ?", (limit,)
    ).fetchall()
    return jsonify([_row_to_dict(r) for r in rows])


@app.post("/apps/<app_id>/rating")
def update_rating(app_id):
    body = request.get_json(force=True)
    cur = DB.execute(
        "UPDATE apps SET rating = ?, rating_count = ? WHERE id = ?",
        (float(body["rating"]), int(body["rating_count"]), app_id),
    )
    if cur.rowcount == 0:
        abort(404)
    r = DB.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    return jsonify(_row_to_dict(r))


@app.get("/health")
def health():
    n = DB.execute("SELECT COUNT(*) AS c FROM apps").fetchone()["c"]
    return jsonify({"service": "catalog", "ok": True, "apps": n})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=15001, debug=False)
