"""PearCare claim service.

Tracks the lifecycle of a single claim from filing through resolution.
Persistence: SQLite at `data/pearcare-claim.db`.

State machine:

    filed -> triaged -> approved        (terminal)
    triaged -> denied                   (terminal)
    filed -> denied                     (auto-triage said deny)

Resolution path is decided at triage time:

    * `support`     — covered by every tier, no hook fires.
    * `repair`      — fires `repair_vendor_hook` to dispatch a vendor.
    * `replacement` — fires `replacement_hook` to mint a new license
                      via the main fulfillment service.

Endpoints:

    POST /claims                      — file a new claim
    GET  /claims/<claim_id>           — read a claim
    POST /claims/<claim_id>/triage    — decide the resolution path
    POST /claims/<claim_id>/approve   — approve and run the appropriate hook
    POST /claims/<claim_id>/deny      — deny the claim
    GET  /users/<user_id>/claims      — every claim a user has filed
"""
import os, sys, json, uuid, re
from datetime import datetime, timezone
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import requests
from flask import Flask, jsonify, request, abort

from shared.db import connect, init_schema
from shared.models import SERVICES
from shared.otel import instrument_flask
from services.pearcare.claim.hooks import repair_vendor_hook, replacement_hook

app = Flask(__name__)
TRACER, METER = instrument_flask(app, "pearcare-claim")
claim_outcomes = METER.counter("pearcare_claim_outcomes_total")
DB = connect("pearcare-claim")
init_schema(DB, """
CREATE TABLE IF NOT EXISTS claims (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    enrollment_id TEXT NOT NULL,
    app_id        TEXT,
    order_id      TEXT,
    issue         TEXT,
    status        TEXT NOT NULL,
    resolution    TEXT,
    vendor        TEXT,
    vendor_ticket TEXT,
    eta_days      INTEGER,
    replacement   TEXT,
    filed_at      TEXT,
    triaged_at    TEXT,
    resolved_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_user ON claims(user_id);
""")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row(claim_id):
    r = DB.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    if d.get("replacement"):
        d["replacement"] = json.loads(d["replacement"])
    return d


def _enrollment(enrollment_id):
    r = requests.get(f"{SERVICES['pearcare_plan']}/enrollments/{enrollment_id}", timeout=5)
    if r.status_code != 200:
        abort(400, description="unknown enrollment")
    return r.json()


def _auto_triage(issue: str, enrollment: dict) -> str:
    text = (issue or "").lower()
    tier = enrollment["tier"]
    if any(w in text for w in ["lost", "stolen", "theft"]):
        return "replacement" if tier == "pearcare_loss" else "deny"
    if any(w in text for w in ["broken", "damage", "cracked", "crash", "boot"]):
        return "repair"
    if re.search(r"\b(question|how do|help|setup)\b", text):
        return "support"
    return "support"


@app.post("/claims")
def file_claim():
    body = request.get_json(force=True)
    enrollment = _enrollment(body["enrollment_id"])
    if enrollment["status"] != "active":
        abort(409, description="enrollment is not active")

    cid = "clm_" + uuid.uuid4().hex[:18]
    DB.execute(
        "INSERT INTO claims(id, user_id, enrollment_id, app_id, order_id, issue, "
        "status, filed_at) VALUES (?,?,?,?,?,?,'filed',?)",
        (cid, body["user_id"], enrollment["id"], enrollment["app_id"],
         enrollment.get("order_id"), body.get("issue", ""), _now()),
    )
    return jsonify(_row(cid)), 201


@app.post("/claims/<claim_id>/triage")
def triage(claim_id):
    c = _row(claim_id)
    if not c: abort(404)
    if c["status"] != "filed":
        abort(409, description=f"cannot triage in state {c['status']}")
    enrollment = _enrollment(c["enrollment_id"])
    decision = (request.get_json(silent=True) or {}).get("resolution") \
               or _auto_triage(c["issue"], enrollment)

    if decision == "deny":
        DB.execute(
            "UPDATE claims SET status='denied', resolution='deny', "
            "triaged_at=?, resolved_at=? WHERE id=?",
            (_now(), _now(), claim_id),
        )
        claim_outcomes.add(1, {"resolution": "deny", "tier": enrollment["tier"]})
    else:
        DB.execute(
            "UPDATE claims SET status='triaged', resolution=?, triaged_at=? WHERE id=?",
            (decision, _now(), claim_id),
        )
        claim_outcomes.add(1, {"resolution": decision, "tier": enrollment["tier"]})
    return jsonify(_row(claim_id))


@app.post("/claims/<claim_id>/approve")
def approve(claim_id):
    c = _row(claim_id)
    if not c: abort(404)
    if c["status"] != "triaged":
        abort(409, description=f"cannot approve in state {c['status']}")

    if c["resolution"] == "repair":
        r = repair_vendor_hook.dispatch(c["id"], c["app_id"], c["issue"])
        DB.execute(
            "UPDATE claims SET vendor=?, vendor_ticket=?, eta_days=? WHERE id=?",
            (r["vendor"], r["ticket"], r["eta_days"], claim_id),
        )
    elif c["resolution"] == "replacement":
        rep = replacement_hook.replace(c["id"], c["user_id"], c["app_id"], c["order_id"])
        DB.execute(
            "UPDATE claims SET replacement=? WHERE id=?",
            (json.dumps(rep), claim_id),
        )

    DB.execute(
        "UPDATE claims SET status='approved', resolved_at=? WHERE id=?",
        (_now(), claim_id),
    )
    return jsonify(_row(claim_id))


@app.post("/claims/<claim_id>/deny")
def deny(claim_id):
    cur = DB.execute(
        "UPDATE claims SET status='denied', resolution='deny', resolved_at=? WHERE id=?",
        (_now(), claim_id),
    )
    if cur.rowcount == 0:
        abort(404)
    return jsonify(_row(claim_id))


@app.get("/claims/<claim_id>")
def get_claim(claim_id):
    c = _row(claim_id)
    if not c:
        abort(404)
    return jsonify(c)


@app.get("/users/<user_id>/claims")
def user_claims(user_id):
    rows = DB.execute(
        "SELECT id FROM claims WHERE user_id = ? ORDER BY filed_at DESC", (user_id,)
    ).fetchall()
    return jsonify([_row(r["id"]) for r in rows])


@app.get("/health")
def health():
    n = DB.execute("SELECT COUNT(*) AS c FROM claims").fetchone()["c"]
    return jsonify({"service": "pearcare-claim", "ok": True, "claims": n})


if __name__ == "__main__":
    SERVICES.setdefault("pearcare_plan", "http://localhost:15101")
    SERVICES.setdefault("pearcare_claim", "http://localhost:15102")
    app.run(host="0.0.0.0", port=15102, debug=False)
