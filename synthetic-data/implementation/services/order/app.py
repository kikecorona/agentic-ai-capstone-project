"""Order service.

Drives a purchase from "cart of apps" all the way through "license
provisioned and downloadable".

Persistence: SQLite at `data/order.db`. Two tables:

    orders       — header row per checkout
    order_items  — one row per app_id in an order, with stable position

The state machine is intentionally tiny:

    pending -> paid -> fulfilled
    pending -> failed (terminal)
    paid    -> refunded (terminal)
    fulfilled -> refunded (terminal)

Checkout flow (POST /checkout):

    1. Read cart from cart-svc.
    2. Resolve every app's price from catalog-svc; sum into `total_cents`.
    3. Create a pending Order.
    4. Authorize payment via payment-svc; if it fails, mark the order
       failed and return the payment error.
    5. Mark the order paid, then call fulfillment-svc to provision
       licenses + download URLs.
    6. Mark the order fulfilled and clear the cart.
"""
import os, sys, uuid
from datetime import datetime, timezone
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import requests
from flask import Flask, jsonify, request, abort

from shared.db import connect, init_schema
from shared.models import SERVICES
from shared.otel import instrument_flask, child_span, traceparent_header

app = Flask(__name__)
TRACER, METER = instrument_flask(app, "order")
checkout_outcomes = METER.counter("order_checkout_outcomes_total")
checkout_total_cents = METER.histogram("order_checkout_total_cents")
DB = connect("order")
init_schema(DB, """
CREATE TABLE IF NOT EXISTS orders (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    total_cents INTEGER NOT NULL,
    status      TEXT NOT NULL,
    payment_id  TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);

CREATE TABLE IF NOT EXISTS order_items (
    order_id TEXT NOT NULL,
    app_id   TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (order_id, position),
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
);
""")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _load(order_id):
    o = DB.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not o:
        return None
    items = [r["app_id"] for r in DB.execute(
        "SELECT app_id FROM order_items WHERE order_id = ? ORDER BY position",
        (order_id,)).fetchall()]
    d = dict(o)
    d["items"] = items
    return d


@app.post("/checkout")
def checkout():
    body = request.get_json(force=True)
    user_id = body["user_id"]

    # 1. cart
    with child_span(TRACER, "downstream.cart.read", {"user_id": user_id}):
        cart_resp = requests.get(f"{SERVICES['cart']}/carts/{user_id}",
                                 headers=traceparent_header(), timeout=5)
        cart_resp.raise_for_status()
        items = cart_resp.json().get("items", [])
    if not items:
        checkout_outcomes.add(1, {"outcome": "empty_cart"})
        abort(400, description="cart is empty")

    # 2. resolve prices
    total = 0
    app_ids = []
    with child_span(TRACER, "downstream.catalog.price_lookup",
                    {"item_count": len(items)}):
        for it in items:
            app_id = it["app_id"]
            a = requests.get(f"{SERVICES['catalog']}/apps/{app_id}",
                             headers=traceparent_header(), timeout=5).json()
            total += int(a["price_cents"])
            app_ids.append(app_id)

    # 3. pending order
    order_id = str(uuid.uuid4())
    DB.execute(
        "INSERT INTO orders(id, user_id, total_cents, status, created_at) "
        "VALUES (?,?,?,?,?)",
        (order_id, user_id, total, "pending", _now()),
    )
    DB.executemany(
        "INSERT INTO order_items(order_id, app_id, position) VALUES (?,?,?)",
        [(order_id, aid, i) for i, aid in enumerate(app_ids)],
    )

    # 4. authorize payment
    with child_span(TRACER, "downstream.payment.authorize",
                    {"order_id": order_id, "amount_cents": total}):
        pay_resp = requests.post(
            f"{SERVICES['payment']}/authorize",
            json={"order_id": order_id, "user_id": user_id, "amount_cents": total},
            headers=traceparent_header(),
            timeout=10,
        )
    if pay_resp.status_code >= 400:
        DB.execute("UPDATE orders SET status = ? WHERE id = ?", ("failed", order_id))
        checkout_outcomes.add(1, {"outcome": "payment_failed"})
        return jsonify({"order": _load(order_id), "error": pay_resp.json()}), 402
    payment_id = pay_resp.json()["payment_id"]
    DB.execute(
        "UPDATE orders SET status = ?, payment_id = ? WHERE id = ?",
        ("paid", payment_id, order_id),
    )

    # 5. fulfill
    with child_span(TRACER, "downstream.fulfillment.fulfill",
                    {"order_id": order_id, "app_count": len(app_ids)}):
        fulfill_resp = requests.post(
            f"{SERVICES['fulfillment']}/fulfill",
            json={"order_id": order_id, "user_id": user_id, "app_ids": app_ids},
            headers=traceparent_header(),
            timeout=10,
        )
        fulfill_resp.raise_for_status()
    DB.execute("UPDATE orders SET status = ? WHERE id = ?", ("fulfilled", order_id))

    # 6. clear cart
    requests.post(f"{SERVICES['cart']}/carts/{user_id}/clear",
                  headers=traceparent_header(), timeout=5)

    checkout_outcomes.add(1, {"outcome": "fulfilled"})
    checkout_total_cents.record(total, {"item_count": len(app_ids)})
    return jsonify({"order": _load(order_id), "fulfillment": fulfill_resp.json()})


@app.get("/orders/<order_id>")
def get_order(order_id):
    o = _load(order_id)
    if not o:
        abort(404)
    return jsonify(o)


@app.get("/users/<user_id>/orders")
def user_orders(user_id):
    rows = DB.execute(
        "SELECT id FROM orders WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    return jsonify([_load(r["id"]) for r in rows])


@app.post("/orders/<order_id>/refund")
def refund(order_id):
    o = _load(order_id)
    if not o:
        abort(404)
    if o["status"] not in {"paid", "fulfilled"}:
        abort(409, description=f"cannot refund order in state {o['status']}")
    if o.get("payment_id"):
        with child_span(TRACER, "downstream.payment.refund",
                        {"payment_id": o["payment_id"]}):
            requests.post(f"{SERVICES['payment']}/refund",
                          json={"payment_id": o["payment_id"]},
                          headers=traceparent_header(), timeout=5)
    DB.execute("UPDATE orders SET status = ? WHERE id = ?", ("refunded", order_id))
    return jsonify(_load(order_id))


@app.get("/health")
def health():
    n = DB.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
    return jsonify({"service": "order", "ok": True, "orders": n})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=15004, debug=False)
