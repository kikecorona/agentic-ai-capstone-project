"""Replacement-fulfillment hook.

When a claim is approved as a `replacement`, this hook coordinates with
the (existing) fulfillment service to mint a fresh license + download
URLs for the user, on the same `app_id` that the original purchase
covered. The user's original entitlement is left in place — having two
licenses for the same app is fine in PearCare's data model and is also
how we handle "share with a family member as a replacement".
"""
import requests
from shared.models import SERVICES


def replace(claim_id: str, user_id: str, app_id: str, original_order_id: str) -> dict:
    """Provision a replacement entitlement for the user."""
    body = {
        "order_id": f"replacement-{claim_id}",
        "user_id": user_id,
        "app_ids": [app_id],
    }
    r = requests.post(f"{SERVICES['fulfillment']}/fulfill", json=body, timeout=10)
    r.raise_for_status()
    return r.json()
