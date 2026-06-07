# payment svc
# does the money stuff
import os, sys, uuid, random
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from flask import Flask, jsonify, request, abort

from shared.db import connect, init_schema
from shared.otel import instrument_flask, child_span
from services.payment.hooks import stripe_hook, paypal_hook, pearcard_hook

PROVIDERS = {"stripe": stripe_hook, "paypal": paypal_hook, "pearcard": pearcard_hook}

app = Flask(__name__)
TRACER, METER = instrument_flask(app, "payment")
auth_outcomes = METER.counter("payment_authorize_outcomes_total")
refund_outcomes = METER.counter("payment_refund_outcomes_total")
amount_cents = METER.histogram("payment_amount_cents")
DB = connect("payment")
init_schema(DB, """
CREATE TABLE IF NOT EXISTS payments (
    payment_id   TEXT PRIMARY KEY,
    order_id     TEXT,
    user_id      TEXT,
    amount_cents INTEGER NOT NULL,
    provider     TEXT,
    charge_id    TEXT,
    status       TEXT,
    refund_id    TEXT
);
""")


def _pick_provider(token):
    if token and token.startswith("pp_"): return "paypal"
    if token and token.startswith("pc_"): return "pearcard"
    return "stripe"


def _row(pid):
    r = DB.execute("SELECT * FROM payments WHERE payment_id = ?", (pid,)).fetchone()
    return dict(r) if r else None


@app.post("/authorize")
def authorize():
    body = request.get_json(force=True)
    token = body.get("token") or random.choice(["tok_visa", "pp_user", "pc_user"])
    provider_name = _pick_provider(token)
    provider = PROVIDERS[provider_name]

    with child_span(TRACER, f"provider.{provider_name}.authorize",
                    {"provider": provider_name,
                     "amount_cents": body["amount_cents"]}):
        res = provider.authorize(body["amount_cents"], token)
    if not res["ok"]:
        auth_outcomes.add(1, {"provider": provider_name, "outcome": "declined"})
        return jsonify(res), 402

    pid = "pay_" + uuid.uuid4().hex[:16]
    DB.execute(
        "INSERT INTO payments(payment_id, order_id, user_id, amount_cents, "
        "provider, charge_id, status) VALUES (?,?,?,?,?,?,?)",
        (pid, body.get("order_id"), body.get("user_id"), body["amount_cents"],
         provider_name, res["charge_id"], "authorized"),
    )
    auth_outcomes.add(1, {"provider": provider_name, "outcome": "authorized"})
    amount_cents.record(body["amount_cents"], {"provider": provider_name})
    return jsonify(_row(pid))


@app.post("/refund")
def refund():
    body = request.get_json(force=True)
    pid = body["payment_id"]
    p = _row(pid)
    if not p:
        abort(404)
    provider = PROVIDERS[p["provider"]]
    with child_span(TRACER, f"provider.{p['provider']}.refund",
                    {"provider": p["provider"], "payment_id": pid}):
        r = provider.refund(p["charge_id"])
    DB.execute(
        "UPDATE payments SET status = ?, refund_id = ? WHERE payment_id = ?",
        ("refunded", r["refund_id"], pid),
    )
    refund_outcomes.add(1, {"provider": p["provider"], "outcome": "refunded"})
    return jsonify(_row(pid))


@app.get("/payments/<pid>")
def get_payment(pid):
    p = _row(pid)
    if not p:
        abort(404)
    return jsonify(p)


@app.get("/health")
def h():
    n = DB.execute("SELECT COUNT(*) AS c FROM payments").fetchone()["c"]
    return jsonify({"service": "payment", "ok": True, "payments": n})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=15005, debug=False)
