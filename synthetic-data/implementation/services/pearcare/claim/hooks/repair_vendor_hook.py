# repair vendor hook
# pretends to dispatch a repair to an authorized vendor
# does nothing real
import random, uuid

VENDORS = ["Brightline Repair", "OakFix", "Helio Service Co.", "Tide Tech"]


def dispatch(claim_id, app_id, issue):
    return {
        "ok": True,
        "vendor": random.choice(VENDORS),
        "ticket": "RV-" + uuid.uuid4().hex[:10],
        "eta_days": random.choice([2, 3, 5, 7]),
    }
