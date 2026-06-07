"""PearCare plan service.

Owns warranty plan definitions (in code) and per-user enrollments
(in SQLite at `data/pearcare-plan.db`). The plan catalogue itself is
read-only at runtime; only `enrollments` is mutated.

Endpoints:

    GET  /plans                              — every plan
    GET  /plans/<plan_id>                    — single plan
    GET  /plans/for-app/<app_id>             — plans offered for an app
    POST /enrollments                        — start coverage for a user
    GET  /enrollments/<enrollment_id>        — single enrollment
    GET  /users/<user_id>/enrollments        — all of a user's coverage
    POST /enrollments/<enrollment_id>/cancel — flip status to cancelled

Coverage starts at enrollment time and ends at
`enrollment.created_at + term_months × 30 days`. The plan fields are
snapshotted onto the enrollment row so claims can be evaluated even
after a plan definition is later edited.
"""
import os, sys, json, uuid
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from flask import Flask, jsonify, request, abort

from shared.db import connect, init_schema
from services.pearcare.plan.seed_data import PLANS, PLANS_BY_ID, APP_PLAN_MAP

app = Flask(__name__)
DB = connect("pearcare-plan")
init_schema(DB, """
CREATE TABLE IF NOT EXISTS enrollments (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    app_id          TEXT NOT NULL,
    order_id        TEXT,
    plan_id         TEXT NOT NULL,
    plan_name       TEXT,
    tier            TEXT,
    coverage        TEXT,
    term_months     INTEGER,
    monthly_billing INTEGER,
    starts_at       TEXT,
    expires_at      TEXT,
    status          TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_enr_user ON enrollments(user_id);
CREATE INDEX IF NOT EXISTS idx_enr_app  ON enrollments(app_id);
""")


def _now():
    return datetime.now(timezone.utc)


def _row_to_dict(r):
    if not r:
        return None
    d = dict(r)
    d["coverage"] = json.loads(d["coverage"]) if d.get("coverage") else []
    d["monthly_billing"] = bool(d.get("monthly_billing"))
    return d


@app.get("/plans")
def list_plans():
    return jsonify([p.to_dict() for p in PLANS])


@app.get("/plans/<plan_id>")
def get_plan(plan_id):
    if plan_id not in PLANS_BY_ID:
        abort(404)
    return jsonify(PLANS_BY_ID[plan_id].to_dict())


@app.get("/plans/for-app/<app_id>")
def plans_for_app(app_id):
    plan_ids = APP_PLAN_MAP.get(app_id, [])
    return jsonify([PLANS_BY_ID[pid].to_dict() for pid in plan_ids])


@app.post("/enrollments")
def enroll():
    body = request.get_json(force=True)
    plan_id = body["plan_id"]
    if plan_id not in PLANS_BY_ID:
        abort(400, description="unknown plan_id")
    plan = PLANS_BY_ID[plan_id]
    starts = _now()
    expires = starts + timedelta(days=30 * plan.term_months)
    eid = "enr_" + uuid.uuid4().hex[:18]

    DB.execute(
        "INSERT INTO enrollments(id, user_id, app_id, order_id, plan_id, plan_name, "
        "tier, coverage, term_months, monthly_billing, starts_at, expires_at, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'active')",
        (eid, body["user_id"], body["app_id"], body.get("order_id"),
         plan.id, plan.name, plan.tier, json.dumps(plan.coverage),
         plan.term_months, 1 if plan.monthly_billing else 0,
         starts.isoformat(), expires.isoformat()),
    )
    return jsonify(_row_to_dict(
        DB.execute("SELECT * FROM enrollments WHERE id = ?", (eid,)).fetchone()
    )), 201


@app.get("/enrollments/<enrollment_id>")
def get_enrollment(enrollment_id):
    r = DB.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    if not r:
        abort(404)
    return jsonify(_row_to_dict(r))


@app.get("/users/<user_id>/enrollments")
def user_enrollments(user_id):
    rows = DB.execute(
        "SELECT * FROM enrollments WHERE user_id = ? ORDER BY starts_at DESC",
        (user_id,),
    ).fetchall()
    return jsonify([_row_to_dict(r) for r in rows])


@app.post("/enrollments/<enrollment_id>/cancel")
def cancel(enrollment_id):
    cur = DB.execute(
        "UPDATE enrollments SET status = 'cancelled' WHERE id = ?",
        (enrollment_id,),
    )
    if cur.rowcount == 0:
        abort(404)
    r = DB.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
    return jsonify(_row_to_dict(r))


@app.get("/health")
def health():
    n = DB.execute("SELECT COUNT(*) AS c FROM enrollments").fetchone()["c"]
    return jsonify({"service": "pearcare-plan", "ok": True,
                    "plans": len(PLANS), "enrollments": n})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=15101, debug=False)
