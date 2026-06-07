# stripe hook
# call this with amount and tok, it returns a charge id
# does not really call stripe lol
import random, string

def _id():
    return "ch_" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(18))

def authorize(amount_cents, token):
    # 95% success rate
    if random.random() < 0.05:
        return {"ok": False, "error": "card_declined"}
    return {"ok": True, "provider": "stripe", "charge_id": _id()}

def refund(charge_id):
    return {"ok": True, "provider": "stripe", "refund_id": "re_" + charge_id[3:]}
