"""Cart service.

Holds a per-user list of `app_id`s the user intends to purchase.
Persisted in `data/cart.db`. Apps are unique within a user's cart
(adding the same app twice is a no-op).

Endpoints:

    GET    /carts/<user_id>            — read the user's cart
    POST   /carts/<user_id>/items      — add {"app_id": "..."} to the cart
    DELETE /carts/<user_id>/items/<id> — remove a single item
    POST   /carts/<user_id>/clear      — empty the cart (called by order svc
                                         after a successful checkout)
"""
import os, sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from flask import Flask, jsonify, request, abort
from shared.db import connect, init_schema

app = Flask(__name__)
DB = connect("cart")
init_schema(DB, """
CREATE TABLE IF NOT EXISTS cart_items (
    user_id  TEXT NOT NULL,
    app_id   TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (user_id, app_id)
);
""")


@app.get("/carts/<user_id>")
def get_cart(user_id):
    rows = DB.execute(
        "SELECT app_id, added_at FROM cart_items WHERE user_id = ? ORDER BY added_at",
        (user_id,),
    ).fetchall()
    return jsonify({"user_id": user_id, "items": [dict(r) for r in rows]})


@app.post("/carts/<user_id>/items")
def add_item(user_id):
    body = request.get_json(force=True)
    app_id = body["app_id"]
    cur = DB.execute(
        "INSERT OR IGNORE INTO cart_items(user_id, app_id, added_at) VALUES (?,?,?)",
        (user_id, app_id, datetime.now(timezone.utc).isoformat()),
    )
    return jsonify({"ok": True, "duplicate": cur.rowcount == 0})


@app.delete("/carts/<user_id>/items/<app_id>")
def remove_item(user_id, app_id):
    cur = DB.execute(
        "DELETE FROM cart_items WHERE user_id = ? AND app_id = ?",
        (user_id, app_id),
    )
    if cur.rowcount == 0:
        abort(404)
    return jsonify({"ok": True})


@app.post("/carts/<user_id>/clear")
def clear_cart(user_id):
    DB.execute("DELETE FROM cart_items WHERE user_id = ?", (user_id,))
    return jsonify({"ok": True})


@app.get("/health")
def health():
    return jsonify({"service": "cart", "ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=15003, debug=False)
