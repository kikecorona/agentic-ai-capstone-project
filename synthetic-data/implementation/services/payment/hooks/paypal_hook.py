import random, string

# paypal stub
# returns success most of the time

def authorize(amount_cents, token):
    if random.random() < 0.07:
        return {"ok": False, "error": "payer_cannot_pay"}
    txn = "PP-" + "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(14))
    return {"ok": True, "provider": "paypal", "charge_id": txn}

def refund(charge_id):
    return {"ok": True, "provider": "paypal", "refund_id": "REF-" + charge_id[3:]}
