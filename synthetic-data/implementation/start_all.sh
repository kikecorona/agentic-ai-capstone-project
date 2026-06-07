#!/usr/bin/env bash
# Boot every Pear Store service. Logs land under ./logs/<service>.log.
#
# Override the interpreter with $PYTHON if you want a specific venv:
#   PYTHON=/path/to/.venv/bin/python ./start_all.sh
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p logs

# Pick a Python: $PYTHON, then python, then python3.
PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
    if   command -v python  >/dev/null 2>&1; then PY=python
    elif command -v python3 >/dev/null 2>&1; then PY=python3
    else
        echo "no python interpreter on PATH (set \$PYTHON to override)" >&2
        exit 1
    fi
fi
echo "using interpreter: $PY ($($PY --version 2>&1))"

start() {
    local name=$1
    local script=$2
    echo "starting $name..."
    PYTHONPATH="$(pwd)" "$PY" "$script" > "logs/$name.log" 2>&1 &
    echo "  pid=$! log=logs/$name.log"
}

start catalog        services/catalog/app.py
start account        services/account/app.py
start cart           services/cart/app.py
start order          services/order/app.py
start payment        services/payment/app.py
start fulfillment    services/fulfillment/app.py
start review         services/review/app.py
start search         services/search/app.py
start pearcare-plan  services/pearcare/plan/app.py
start pearcare-claim services/pearcare/claim/app.py
start frontend       frontend/app.py

echo
echo "Pear Store booting. Open http://localhost:15000"
echo "Tail any log with: tail -f logs/<service>.log"
