"""License hook.

Issues a per-user license token bound to a specific app + order. The
token is the canonical proof of purchase: receipts, family sharing, and
restore-purchases all key off it.
"""
import uuid


def issue_license(app_id: str, user_id: str, order_id: str) -> dict:
    return {
        "license_id": "lic_" + uuid.uuid4().hex[:20],
        "app_id": app_id,
        "user_id": user_id,
        "order_id": order_id,
        "kind": "perpetual",
    }
