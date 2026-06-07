# reviews
import os, sys, uuid, statistics
from datetime import datetime
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import requests
from flask import Flask, jsonify, request

from shared.db import connect, init_schema
from shared.models import SERVICES

app = Flask(__name__)
DB = connect("review")
init_schema(DB, """
CREATE TABLE IF NOT EXISTS reviews (
    id      TEXT PRIMARY KEY,
    app_id  TEXT NOT NULL,
    user_id TEXT NOT NULL,
    stars   INTEGER NOT NULL,
    title   TEXT,
    body    TEXT,
    at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_app ON reviews(app_id);
""")


@app.post("/reviews")
def post():
    b = request.get_json(force=True)
    rid = str(uuid.uuid4())
    DB.execute(
        "INSERT INTO reviews(id, app_id, user_id, stars, title, body, at) "
        "VALUES (?,?,?,?,?,?,?)",
        (rid, b["app_id"], b["user_id"], int(b["stars"]),
         b.get("title", ""), b.get("body", ""), datetime.utcnow().isoformat()),
    )
    rs = [r["stars"] for r in DB.execute(
        "SELECT stars FROM reviews WHERE app_id = ?", (b["app_id"],)).fetchall()]
    try:
        requests.post(f"{SERVICES['catalog']}/apps/{b['app_id']}/rating",
                      json={"rating": round(statistics.mean(rs), 2),
                            "rating_count": len(rs)}, timeout=2)
    except Exception:
        pass
    r = DB.execute("SELECT * FROM reviews WHERE id = ?", (rid,)).fetchone()
    return jsonify(dict(r)), 201


@app.get("/reviews/<app_id>")
def get(app_id):
    rows = DB.execute(
        "SELECT * FROM reviews WHERE app_id = ? ORDER BY at DESC", (app_id,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/health")
def h():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=15007)
