"""Pear Store storefront — the public-facing website.

This Flask app does no business logic of its own. It calls the backend
services (catalog, search, account, cart, order, review, fulfillment,
and PearCare's plan + claim services) and renders the responses with
Jinja templates. All session state lives in Flask's signed cookie.

Routes mirror the navigation:

    /                        — top charts + categories grid
    /apps/<id>               — app detail page (reviews + buy)
    /search                  — search results
    /cart                    — current cart + checkout button
    /account                 — sign in / register / library
    /orders/<id>             — receipt page (post-purchase)
"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import requests
from flask import Flask, render_template, request, redirect, url_for, session, flash
from shared.models import SERVICES

app = Flask(__name__)
app.secret_key = "pearstore-demo-not-secure"
app.config["TEMPLATES_AUTO_RELOAD"] = True


def _api(svc, path, method="GET", **kw):
    url = f"{SERVICES[svc]}{path}"
    return requests.request(method, url, timeout=10, **kw)


def _user():
    return session.get("user")


@app.context_processor
def inject_globals():
    return {"current_user": _user()}


@app.get("/")
def index():
    charts = _api("catalog", "/top-charts?limit=12").json()
    cats = _api("catalog", "/categories").json()
    return render_template("index.html", charts=charts, categories=cats)


@app.get("/apps/<app_id>")
def app_detail(app_id):
    a = _api("catalog", f"/apps/{app_id}").json()
    reviews = _api("review", f"/reviews/{app_id}").json()
    plans = _api("pearcare_plan", f"/plans/for-app/{app_id}").json() if "pearcare_plan" in SERVICES else []
    return render_template("app_detail.html", app=a, reviews=reviews, plans=plans)


@app.get("/category/<name>")
def category(name):
    items = _api("catalog", f"/apps/by-category/{name}").json()
    return render_template("category.html", category=name, items=items)


@app.get("/search")
def search():
    q = request.args.get("q", "")
    results = _api("search", f"/search?q={q}").json() if q else {"items": []}
    return render_template("search.html", q=q, results=results)


@app.post("/cart/add")
def cart_add():
    if not _user():
        return redirect(url_for("account"))
    _api("cart", f"/carts/{_user()['id']}/items", method="POST",
         json={"app_id": request.form["app_id"]})
    # also stash a pearcare plan if one was selected
    plan_id = request.form.get("plan_id")
    if plan_id:
        session.setdefault("pending_plans", []).append(
            {"plan_id": plan_id, "app_id": request.form["app_id"]})
        session.modified = True
    flash("Added to cart")
    return redirect(request.referrer or url_for("index"))


@app.get("/cart")
def cart():
    if not _user():
        return redirect(url_for("account"))
    c = _api("cart", f"/carts/{_user()['id']}").json()
    full = []
    for it in c["items"]:
        full.append(_api("catalog", f"/apps/{it['app_id']}").json())
    return render_template("cart.html", items=full,
                           pending_plans=session.get("pending_plans", []))


@app.post("/checkout")
def checkout():
    if not _user():
        return redirect(url_for("account"))
    r = _api("order", "/checkout", method="POST", json={"user_id": _user()["id"]})
    if r.status_code >= 400:
        flash(f"Payment failed: {r.json()}")
        return redirect(url_for("cart"))
    payload = r.json()
    order_id = payload["order"]["id"]
    # enroll any pending pearcare plans for the apps in this order
    for pp in session.get("pending_plans", []):
        try:
            _api("pearcare_plan", "/enrollments", method="POST",
                 json={"user_id": _user()["id"], "plan_id": pp["plan_id"],
                       "app_id": pp["app_id"], "order_id": order_id})
        except Exception:
            pass
    session["pending_plans"] = []
    return redirect(url_for("order_view", order_id=order_id))


@app.get("/orders/<order_id>")
def order_view(order_id):
    o = _api("order", f"/orders/{order_id}").json()
    receipt = _api("fulfillment", f"/receipts/{order_id}")
    receipt = receipt.json() if receipt.status_code == 200 else None
    return render_template("order.html", order=o, receipt=receipt)


@app.route("/account", methods=["GET", "POST"])
def account():
    if request.method == "POST":
        action = request.form["action"]
        if action == "login":
            r = _api("account", "/login", method="POST",
                     json={"email": request.form["email"],
                           "password": request.form["password"]})
            if r.status_code == 200:
                session["user"] = r.json()
                flash("Welcome back.")
                return redirect(url_for("index"))
            flash("Bad credentials")
        elif action == "register":
            r = _api("account", "/users", method="POST",
                     json={"email": request.form["email"],
                           "password": request.form["password"],
                           "display_name": request.form.get("display_name", "")})
            if r.status_code == 201:
                session["user"] = r.json()
                return redirect(url_for("index"))
            flash(f"Registration failed: {r.json()}")
        elif action == "logout":
            session.clear()
            return redirect(url_for("index"))

    library = []
    pearcare = []
    if _user():
        lib = _api("fulfillment", f"/users/{_user()['id']}/library").json()
        for app_id in lib.get("apps", {}):
            library.append(_api("catalog", f"/apps/{app_id}").json())
        try:
            pearcare = _api("pearcare_plan",
                            f"/users/{_user()['id']}/enrollments").json()
        except Exception:
            pearcare = []
    return render_template("account.html", library=library, pearcare=pearcare)


@app.post("/pearcare/claim")
def file_claim():
    if not _user():
        return redirect(url_for("account"))
    r = _api("pearcare_claim", "/claims", method="POST",
             json={"user_id": _user()["id"],
                   "enrollment_id": request.form["enrollment_id"],
                   "issue": request.form["issue"]})
    if r.status_code >= 400:
        flash(f"Claim failed: {r.json()}")
    else:
        flash(f"Claim filed: {r.json()['id']}")
    return redirect(url_for("account"))


if __name__ == "__main__":
    # PearCare services are added later; register them lazily so the
    # frontend works even before they're built.
    SERVICES.setdefault("pearcare_plan", "http://localhost:15101")
    SERVICES.setdefault("pearcare_claim", "http://localhost:15102")
    app.run(host="0.0.0.0", port=15000, debug=False)
