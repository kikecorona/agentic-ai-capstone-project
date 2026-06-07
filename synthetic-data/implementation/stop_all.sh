#!/usr/bin/env bash
# Stop every Pear Store service started by start_all.sh.
#
# Each service is matched by its script path (the same path start_all.sh
# uses to launch it), so we don't accidentally hit unrelated python
# processes.
set -euo pipefail

cd "$(dirname "$0")"

stop() {
    local name=$1
    local script=$2
    local pids
    pids=$(pgrep -f "$script" || true)
    if [[ -z "$pids" ]]; then
        echo "stopping $name... (not running)"
        return
    fi
    # shellcheck disable=SC2086
    echo "stopping $name... pids=$(echo $pids | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 0.3
    # SIGKILL anything still alive after the polite SIGTERM.
    pids=$(pgrep -f "$script" || true)
    if [[ -n "$pids" ]]; then
        # shellcheck disable=SC2086
        kill -9 $pids 2>/dev/null || true
    fi
}

stop frontend       frontend/app.py
stop pearcare-claim services/pearcare/claim/app.py
stop pearcare-plan  services/pearcare/plan/app.py
stop search         services/search/app.py
stop review         services/review/app.py
stop fulfillment    services/fulfillment/app.py
stop payment        services/payment/app.py
stop order          services/order/app.py
stop cart           services/cart/app.py
stop account        services/account/app.py
stop catalog        services/catalog/app.py

echo
echo "Pear Store stopped."
