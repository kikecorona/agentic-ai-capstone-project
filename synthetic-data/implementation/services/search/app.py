"""Search service.

Pulls the full catalog from catalog-svc on startup and on every search
(simple and consistent for a demo; in production we'd subscribe to a
change feed and maintain a real index).

Endpoints:

    GET /search?q=...&category=...&price=free|paid&limit=N

Ranking: a tiny bag-of-tokens scorer over `name`, `developer`,
`description`. Ties break on `rating_count` — a popularity tiebreaker
matters when many apps match a generic query like "weather".
"""
import os, sys, re
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import requests
from flask import Flask, jsonify, request
from shared.models import SERVICES

app = Flask(__name__)
TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(s):
    return TOKEN_RE.findall((s or "").lower())


def _load_catalog():
    r = requests.get(f"{SERVICES['catalog']}/apps?page_size=1000", timeout=5)
    r.raise_for_status()
    return r.json().get("items", [])


def _score(query_tokens, app_obj):
    haystack = _tokens(app_obj["name"]) + _tokens(app_obj["developer"]) + _tokens(app_obj["description"])
    score = 0
    for qt in query_tokens:
        for ht in haystack:
            if ht == qt:
                score += 3
            elif ht.startswith(qt):
                score += 1
    return score


@app.get("/search")
def search():
    q = request.args.get("q", "").strip()
    category = request.args.get("category")
    price = request.args.get("price")  # "free" | "paid" | None
    limit = int(request.args.get("limit", 20))

    apps = _load_catalog()
    if category:
        apps = [a for a in apps if a["category"].lower() == category.lower()]
    if price == "free":
        apps = [a for a in apps if a["price_cents"] == 0]
    elif price == "paid":
        apps = [a for a in apps if a["price_cents"] > 0]

    if not q:
        ranked = sorted(apps, key=lambda a: a["rating_count"], reverse=True)
        return jsonify({"query": q, "items": ranked[:limit]})

    qt = _tokens(q)
    scored = []
    for a in apps:
        s = _score(qt, a)
        if s > 0:
            scored.append((s, a["rating_count"], a))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return jsonify({"query": q, "items": [a for _, _, a in scored[:limit]]})


@app.get("/health")
def health():
    return jsonify({"service": "search", "ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=15008, debug=False)
