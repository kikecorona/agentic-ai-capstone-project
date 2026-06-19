#!/usr/bin/env bash
# Tear down every service start_all.sh launched.
#
# Three layers of defense, in order:
#   1. SIGTERM the PID stored in pids/<name>.pid (the real process —
#      start_all.sh's start_bg uses `exec` so the saved PID isn't a
#      subshell wrapper)
#   2. SIGTERM any descendant the service forked (uvicorn workers,
#      vite child processes, etc.)
#   3. SIGKILL anything still listening on a known service port
#
# Step 3 catches the case where a previous run crashed before
# stop_all.sh could pidfile-track its work.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDS_DIR="$SCRIPT_DIR/pids"

# Known service ports — must stay in sync with start_all.sh defaults.
PORTS=(
  "8000"   # orchestrator
  "8101"   # otel_mcp
  "8102"   # rag_mcp
  "8103"   # bp_mcp
  "8104"   # sd_mcp
  "9000"   # portal
)

descendants_of() {
  # Walk the process tree breadth-first under $1 and echo every PID.
  local root="$1"
  local frontier="$root"
  local all="$root"
  while [ -n "$frontier" ]; do
    local next=""
    for p in $frontier; do
      local kids
      kids="$(pgrep -P "$p" 2>/dev/null || true)"
      next="$next $kids"
      all="$all $kids"
    done
    frontier="$(echo "$next" | xargs 2>/dev/null || true)"
  done
  echo "$all" | tr ' ' '\n' | sort -u | grep -E '^[0-9]+$' || true
}

stop_one() {
  local name="$1" pid="$2"
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "  [$name] not running (pid $pid)"
    return 0
  fi
  # Snapshot the tree NOW, before SIGTERM, so we don't lose track of
  # children if the parent reaps them quickly.
  local tree
  tree="$(descendants_of "$pid")"
  echo "  stopping $name (pid $pid; tree: $(echo $tree | tr '\n' ' '))…"
  for p in $tree; do
    kill "$p" 2>/dev/null || true
  done
  for _ in 1 2 3 4 5 6; do
    local alive=0
    for p in $tree; do
      if kill -0 "$p" 2>/dev/null; then alive=1; break; fi
    done
    [ "$alive" = "0" ] && return 0
    sleep 0.5
  done
  echo "  [$name] still alive — sending SIGKILL"
  for p in $tree; do
    kill -9 "$p" 2>/dev/null || true
  done
}

# ---------------------------------------------------- step 1 + 2: pidfiles
if [ -d "$PIDS_DIR" ]; then
  # Stop in reverse-of-start order so consumers shut down before producers.
  for name in portal orchestrator sd_mcp bp_mcp rag_mcp otel_mcp; do
    pidfile="$PIDS_DIR/$name.pid"
    [ -f "$pidfile" ] || continue
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [ -n "$pid" ]; then
      stop_one "$name" "$pid"
    fi
    rm -f "$pidfile"
  done
else
  echo "(no pids dir at $PIDS_DIR — skipping pidfile sweep)"
fi

# --------------------------------------------- step 3: port-based safety net
echo "==> Port sweep — anything still bound to known service ports?"
held=0
for port in "${PORTS[@]}"; do
  pids=""
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
  elif command -v fuser >/dev/null 2>&1; then
    pids="$(fuser -n tcp "$port" 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' || true)"
  fi
  [ -z "$pids" ] && continue
  held=1
  echo "  port $port still held by pid(s): $pids — killing"
  for p in $pids; do
    kill "$p" 2>/dev/null || true
  done
  sleep 0.3
  for p in $pids; do
    if kill -0 "$p" 2>/dev/null; then
      kill -9 "$p" 2>/dev/null || true
    fi
  done
done
[ "$held" = "0" ] && echo "  (all ports clean)"

echo "All services stopped."
