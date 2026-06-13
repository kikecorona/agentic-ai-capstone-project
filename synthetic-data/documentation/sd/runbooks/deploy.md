# Deploy runbook

Local-only — there is no production target.

## Boot order

`start_all.sh` boots in this order: catalog, account, cart, order,
payment, fulfillment, review, search, pearcare-plan, pearcare-claim,
frontend. The frontend is last because it makes a few startup probes.

There is no health-gating in `start_all.sh`; if a backend service fails
to bind, the frontend still starts and the user-facing pages will 502.
Tail the relevant `logs/<svc>.log` to see which one failed.

## Restart a single service

```
pkill -f "services/<name>/app.py"   # or services/pearcare/<name>/app.py
PYTHONPATH=. python services/<name>/app.py > logs/<name>.log 2>&1 &
```

## Stop everything

```
pkill -f "services/.*/app.py"
pkill -f "services/pearcare/.*/app.py"
pkill -f "frontend/app.py"
```

## State

All services are in-memory. Restarting catalog wipes all apps; the seed
re-runs at import. Restarting payment wipes payment history. The
frontend's session cookie survives backend restarts but its pointers
(user id, pending plan ids) may dangle.

## Smoke test

```
curl :15001/health
curl :15002/health
curl :15101/health
curl :15102/health
curl :15000/   # the storefront home, should 200
```
