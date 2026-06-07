# pearcard hook
import random, string

def authorize(amount_cents, token):
    # internal pearcard, very high success
    if random.random() < 0.01:
        return {"ok": False, "error": "insufficient_funds"}
    return {"ok": True, "provider": "pearcard",
            "charge_id": "PC" + "".join(random.choices(string.digits, k=16))}

def refund(charge_id):
    return {"ok": True, "provider": "pearcard", "refund_id": "PCR" + charge_id[2:]}
