# account svc
# users live here, login also
# TODO: hash passwords, real sessions
import os, sys, uuid
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from flask import Flask, jsonify, request, abort
from shared.db import connect, init_schema

app = Flask(__name__)
DB = connect("account")
init_schema(DB, """
CREATE TABLE IF NOT EXISTS users (
    id                   TEXT PRIMARY KEY,
    email                TEXT NOT NULL UNIQUE,
    display_name         TEXT,
    password             TEXT,
    payment_method_token TEXT
);
""")


def _seed():
    n = DB.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if n:
        return
    for email, name, pw in [
        ("ada@pearstore.dev", "Ada Lovelace", "enigma"),
        ("grace@pearstore.dev", "Grace Hopper", "cobol"),
    ]:
        DB.execute(
            "INSERT INTO users(id, email, display_name, password) VALUES (?,?,?,?)",
            (str(uuid.uuid4()), email, name, pw),
        )
_seed()


@app.post("/users")
def register():
    body = request.get_json(force=True)
    email = body["email"].strip().lower()
    if DB.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        abort(409, description="email already registered")
    uid = str(uuid.uuid4())
    DB.execute(
        "INSERT INTO users(id, email, display_name, password) VALUES (?,?,?,?)",
        (uid, email, body.get("display_name", email.split("@")[0]),
         body.get("password", "")),
    )
    return jsonify({"id": uid, "email": email,
                    "display_name": body.get("display_name", email.split("@")[0])}), 201


@app.post("/login")
def login():
    body = request.get_json(force=True)
    email = body["email"].strip().lower()
    r = DB.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not r or r["password"] != body.get("password", ""):
        abort(401)
    return jsonify({"id": r["id"], "email": r["email"], "display_name": r["display_name"]})


@app.get("/users/<user_id>")
def get_user(user_id):
    r = DB.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not r:
        abort(404)
    return jsonify({"id": r["id"], "email": r["email"], "display_name": r["display_name"],
                    "payment_method_token": r["payment_method_token"]})


@app.post("/users/<user_id>/payment-method")
def set_payment_method(user_id):
    body = request.get_json(force=True)
    cur = DB.execute("UPDATE users SET payment_method_token = ? WHERE id = ?",
                     (body["token"], user_id))
    if cur.rowcount == 0:
        abort(404)
    return jsonify({"ok": True})


@app.get("/health")
def health():
    n = DB.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    return jsonify({"service": "account", "ok": True, "users": n})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=15002, debug=False)
