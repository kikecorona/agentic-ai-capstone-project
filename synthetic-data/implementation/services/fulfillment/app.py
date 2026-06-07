"""Fulfillment service.

Once payment is captured, the order service calls `POST /fulfill` with
the list of app ids and the user. For each app, fulfillment:

    1. Mints a license via `license_hook` (proof of purchase).
    2. Issues a per-platform download URL via `ios_distribution_hook`
       and `macos_distribution_hook`.
    3. Records the receipt + license rows.

Persistence: SQLite at `data/fulfillment.db`. Two tables:

    receipts  — order-level entitlements blob (JSON of the response payload)
    licenses  — one row per (user, app) so the user's library is a cheap
                read.

Endpoints:

    POST /fulfill                   — provision licenses + URLs for an order
    GET  /receipts/<order_id>       — receipt for one order
    GET  /users/<user_id>/library   — every app a user has ever bought
"""
import os, sys, json
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from flask import Flask, jsonify, request, abort

from shared.db import connect, init_schema
from shared.otel import instrument_flask
from services.fulfillment.hooks import (
    ios_distribution_hook,
    macos_distribution_hook,
    license_hook,
)

app = Flask(__name__)
TRACER, METER = instrument_flask(app, "fulfillment")
licenses_issued = METER.counter("fulfillment_licenses_issued_total")
DB = connect("fulfillment")
init_schema(DB, """
CREATE TABLE IF NOT EXISTS receipts (
    order_id TEXT PRIMARY KEY,
    user_id  TEXT NOT NULL,
    data     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipts_user ON receipts(user_id);

CREATE TABLE IF NOT EXISTS licenses (
    license_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    app_id     TEXT NOT NULL,
    order_id   TEXT NOT NULL,
    kind       TEXT
);
CREATE INDEX IF NOT EXISTS idx_licenses_user ON licenses(user_id);
""")


@app.post("/fulfill")
def fulfill():
    body = request.get_json(force=True)
    order_id = body["order_id"]
    user_id = body["user_id"]
    app_ids = body["app_ids"]

    entitlements = []
    for aid in app_ids:
        license = license_hook.issue_license(aid, user_id, order_id)
        downloads = [
            ios_distribution_hook.issue_download(aid, user_id),
            macos_distribution_hook.issue_download(aid, user_id),
        ]
        entitlements.append({"app_id": aid, "license": license, "downloads": downloads})
        DB.execute(
            "INSERT OR REPLACE INTO licenses(license_id, user_id, app_id, order_id, kind) "
            "VALUES (?,?,?,?,?)",
            (license["license_id"], user_id, aid, order_id, license["kind"]),
        )

    receipt = {"order_id": order_id, "user_id": user_id, "entitlements": entitlements}
    DB.execute(
        "INSERT OR REPLACE INTO receipts(order_id, user_id, data) VALUES (?,?,?)",
        (order_id, user_id, json.dumps(receipt)),
    )
    licenses_issued.add(len(entitlements), {"order_id": order_id})
    return jsonify(receipt)


@app.get("/receipts/<order_id>")
def receipt(order_id):
    r = DB.execute("SELECT data FROM receipts WHERE order_id = ?", (order_id,)).fetchone()
    if not r:
        abort(404)
    return jsonify(json.loads(r["data"]))


@app.get("/users/<user_id>/library")
def library(user_id):
    rows = DB.execute(
        "SELECT app_id, license_id, order_id, kind FROM licenses WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    apps = {r["app_id"]: dict(r) for r in rows}
    return jsonify({"user_id": user_id, "apps": apps})


@app.get("/health")
def health():
    n = DB.execute("SELECT COUNT(*) AS c FROM receipts").fetchone()["c"]
    return jsonify({"service": "fulfillment", "ok": True, "receipts": n})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=15006, debug=False)
