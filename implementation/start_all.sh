#!/usr/bin/env bash
# Boot the full POC: OTel MCP + RAG MCP + BP MCP + SD MCP + Orchestrator REST.
#
# Each service runs as its own process; cross-service calls go over HTTP.
# Peer URLs are exported here so every service knows where to find the others
# (RAG_MCP_URL / BP_MCP_URL / SD_MCP_URL — see src/shared/peer_clients.py).
#
# Usage:
#   ./start_all.sh                       # boot all five services + portal in BG
#   ./start_all.sh --install-dependencies # also `pip install -r requirements.txt`
#   ./start_all.sh --dev                 # boot services in BG, run portal in FG;
#                                         # Ctrl+C in dev mode tears everything down
#   ./stop_all.sh                        # tear them down
#
# Logs land in implementation/logs/<svc>.log; PIDs in implementation/pids/.

set -euo pipefail

INSTALL=0
DEV_MODE=0
for arg in "$@"; do
  case "$arg" in
    --install-dependencies|--install-deps|-i) INSTALL=1 ;;
    --dev) DEV_MODE=1 ;;
    -h|--help)
      sed -n '2,18p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown flag: $arg" >&2
      exit 64
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "ERROR: venv not found at $SCRIPT_DIR/.venv" >&2
  echo "       create it with: python3.13 -m venv .venv" >&2
  exit 1
fi

if [ "$INSTALL" -eq 1 ]; then
  echo "==> Installing dependencies"
  "$PYTHON" -m pip install -r requirements.txt

  # Portal — Quasar dev server. Lives at implementation/portal/ and
  # needs Node + npm. `@quasar/cli` is a devDependency in package.json,
  # so npm install drops the `quasar` binary into node_modules/.bin/
  # automatically — no separate global install needed.
  if [ -d "$SCRIPT_DIR/portal" ] && command -v npm >/dev/null 2>&1; then
    echo "==> Installing portal dependencies (npm)"
    ( cd "$SCRIPT_DIR/portal" && npm install )
  elif [ -d "$SCRIPT_DIR/portal" ]; then
    echo "  (skipping portal: npm not on PATH — install Node 18+ to enable)" >&2
  fi
fi

# Source .env when present so users can pin paths/ports without editing this script.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# --------------------------------------------------------------------- ports
: "${OC_HOST:=127.0.0.1}"
: "${OC_PORT:=8000}"
: "${OTEL_MCP_HOST:=127.0.0.1}"
: "${OTEL_MCP_PORT:=8101}"
: "${RAG_MCP_HOST:=127.0.0.1}"
: "${RAG_MCP_PORT:=8102}"
: "${BP_MCP_HOST:=127.0.0.1}"
: "${BP_MCP_PORT:=8103}"
: "${SD_MCP_HOST:=127.0.0.1}"
: "${SD_MCP_PORT:=8104}"

# Peer URLs every service points at. The streamable-http MCP path is /mcp.
export OTEL_MCP_URL="http://${OTEL_MCP_HOST}:${OTEL_MCP_PORT}/mcp"
export RAG_MCP_URL="http://${RAG_MCP_HOST}:${RAG_MCP_PORT}/mcp"
export BP_MCP_URL="http://${BP_MCP_HOST}:${BP_MCP_PORT}/mcp"
export SD_MCP_URL="http://${SD_MCP_HOST}:${SD_MCP_PORT}/mcp"

# Force every MCP server onto streamable-http for this multi-process layout.
export OTEL_MCP_TRANSPORT=streamable-http
export RAG_MCP_TRANSPORT=streamable-http
export BP_MCP_TRANSPORT=streamable-http
export SD_MCP_TRANSPORT=streamable-http

# Default storage paths (every service derives its DB/Chroma path from env
# vars in src/.../store.py so all five processes see the same data dirs).
: "${OTEL_DB_PATH:=$SCRIPT_DIR/data/otel/spans.db}"
: "${AUDIT_DB_PATH:=$SCRIPT_DIR/data/audit/log.db}"
: "${RAG_CHROMA_PATH:=$SCRIPT_DIR/data/rag/chroma}"
: "${BP_INPUTS_ROOT:=$SCRIPT_DIR/data/bp/inputs}"
: "${BP_PAGES_ROOT:=$SCRIPT_DIR/data/bp/pages}"
: "${BP_DB_PATH:=$SCRIPT_DIR/data/bp/state.db}"
: "${SD_SOURCES_ROOT:=$SCRIPT_DIR/data/sd/sources}"
: "${SD_PAGES_ROOT:=$SCRIPT_DIR/data/sd/pages}"
: "${SD_DB_PATH:=$SCRIPT_DIR/data/sd/state.db}"
: "${OC_DB_PATH:=$SCRIPT_DIR/data/orchestrator/state.db}"

export OTEL_DB_PATH AUDIT_DB_PATH RAG_CHROMA_PATH
export BP_INPUTS_ROOT BP_PAGES_ROOT BP_DB_PATH
export SD_SOURCES_ROOT SD_PAGES_ROOT SD_DB_PATH
export OC_DB_PATH OC_HOST OC_PORT

# When GITHUB_OWNER + GITHUB_REPO + GITHUB_PERSONAL_ACCESS_TOKEN are set,
# BP and SD switch to the GitHub-backed PageStore / SourceStore (every
# read AND write goes through the upstream GitHub MCP). Re-export so the
# child processes inherit them — no-op when they're absent.
export GITHUB_PERSONAL_ACCESS_TOKEN GITHUB_OWNER GITHUB_REPO GITHUB_BRANCH
export BP_INPUTS_GH_PATH BP_PAGES_GH_PATH SD_PAGES_GH_PATH SD_SOURCES_GH_PATH

if [ -n "${GITHUB_PERSONAL_ACCESS_TOKEN-}" ] && [ -n "${GITHUB_OWNER-}" ] && [ -n "${GITHUB_REPO-}" ]; then
  echo "==> GitHub MCP mode: ${GITHUB_OWNER}/${GITHUB_REPO}@${GITHUB_BRANCH:-main}"
  if ! command -v npx >/dev/null 2>&1; then
    echo "ERROR: npx not found on PATH — required to spawn @modelcontextprotocol/server-github" >&2
    exit 1
  fi
fi

# Make the storage parents exist (each service creates its own DB file lazily).
mkdir -p \
  "$(dirname "$OTEL_DB_PATH")" \
  "$(dirname "$AUDIT_DB_PATH")" \
  "$RAG_CHROMA_PATH" \
  "$BP_INPUTS_ROOT" "$BP_PAGES_ROOT" "$(dirname "$BP_DB_PATH")" \
  "$SD_SOURCES_ROOT" "$SD_PAGES_ROOT" "$(dirname "$SD_DB_PATH")" \
  "$(dirname "$OC_DB_PATH")"

# ---------------------------------------------------------------------- boot
LOGS_DIR="$SCRIPT_DIR/logs"
PIDS_DIR="$SCRIPT_DIR/pids"
mkdir -p "$LOGS_DIR" "$PIDS_DIR"

start_bg() {
  local name="$1"; shift
  local logfile="$LOGS_DIR/$name.log"
  local pidfile="$PIDS_DIR/$name.pid"
  # Per-call working directory override — the portal needs to run from
  # implementation/portal/ so Quasar can find quasar.config.js. Default
  # is implementation/ so every Python service still works unchanged.
  local wd="${START_BG_CWD:-$SCRIPT_DIR}"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "  [$name] already running (pid $(cat "$pidfile")) — skipping"
    return 0
  fi
  echo "  starting ${name}…"
  # `exec` replaces the subshell with the command so $! is the real
  # process PID, not the subshell wrapper. Without this, redirections
  # (>>logfile 2>&1) prevent bash's auto-exec optimization and SIGTERM
  # to $! goes to the wrapper instead of the actual service —
  # stop_all.sh then leaves orphans behind.
  ( cd "$wd" && exec "$@" >>"$logfile" 2>&1 ) &
  echo $! > "$pidfile"
}

wait_for_http() {
  local name="$1" url="$2" tries="${3:-30}"
  for _ in $(seq 1 "$tries"); do
    if curl -fsS -o /dev/null --max-time 1 "$url" 2>/dev/null; then return 0; fi
    sleep 0.3
  done
  echo "  WARNING: $name did not respond at $url after $tries probes" >&2
  return 1
}

# Free up a TCP port before binding. Used for the portal — vite's
# `strictPort: true` will refuse to start if 9000 is held by an orphan
# from a previous run, so we sweep first. Uses lsof when available
# (covers macOS) and falls back to fuser/ss.
kill_port() {
  local port="$1"
  local pids=""
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
  elif command -v fuser >/dev/null 2>&1; then
    pids="$(fuser -n tcp "$port" 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' || true)"
  fi
  [ -z "$pids" ] && return 0
  echo "  freeing port $port (killing pid(s): $pids)"
  kill $pids 2>/dev/null || true
  sleep 0.4
  for p in $pids; do
    if kill -0 "$p" 2>/dev/null; then
      kill -9 "$p" 2>/dev/null || true
    fi
  done
  # Also drop any stale pidfile that referenced one of these.
  if [ -d "$PIDS_DIR" ]; then
    for f in "$PIDS_DIR"/*.pid; do
      [ -f "$f" ] || continue
      local stale
      stale="$(cat "$f" 2>/dev/null || true)"
      for p in $pids; do
        [ "$stale" = "$p" ] && rm -f "$f"
      done
    done
  fi
}

echo "==> Starting POC services"
echo "  data dir:   $SCRIPT_DIR/data"
echo "  logs dir:   $LOGS_DIR"
echo "  pids dir:   $PIDS_DIR"
echo

# 1. OTel MCP first — services emit spans into the shared SQLite store from
#    boot, but the MCP front needs to be up for any external dashboard.
start_bg otel_mcp \
  "$PYTHON" -m src.otel_mcp.server \
    --transport streamable-http \
    --host "$OTEL_MCP_HOST" --port "$OTEL_MCP_PORT" \
    --db "$OTEL_DB_PATH"

# 2. RAG MCP — every specialist depends on it.
start_bg rag_mcp \
  "$PYTHON" -m src.rag_service.server \
    --transport streamable-http \
    --host "$RAG_MCP_HOST" --port "$RAG_MCP_PORT" \
    --chroma "$RAG_CHROMA_PATH" --otel-db "$OTEL_DB_PATH"

# Give the RAG server a moment to bind so BP/SD's MCPHttpClient can connect
# on first use.
sleep 1

# 3. BP and SD specialists — both reach RAG over HTTP via $RAG_MCP_URL.
start_bg bp_mcp \
  "$PYTHON" -m src.bp_service.server \
    --transport streamable-http \
    --host "$BP_MCP_HOST" --port "$BP_MCP_PORT"

start_bg sd_mcp \
  "$PYTHON" -m src.sd_service.server \
    --transport streamable-http \
    --host "$SD_MCP_HOST" --port "$SD_MCP_PORT"

sleep 1

# 4. Orchestrator REST — uses BP_MCP_URL + SD_MCP_URL to reach the specialists.
start_bg orchestrator \
  "$PYTHON" -m src.orchestrator.server \
    --host "$OC_HOST" --port "$OC_PORT"

# 5. Documentation Portal (§9.8) — picks `quasar` only when the local
#    @quasar/app-vite package is installed (the global `quasar` binary
#    alone exposes a stripped-down command set that doesn't include
#    `dev`). Otherwise falls back to `vite`. In normal mode, runs in BG;
#    with --dev we defer to a foreground run further down so HMR output
#    is interactive and Ctrl+C can tear the whole stack down.
: "${PORTAL_HOST:=127.0.0.1}"
: "${PORTAL_PORT:=9000}"
export PORTAL_HOST PORTAL_PORT
PORTAL_URL=""
PORTAL_BIN=""
PORTAL_ARGS=()
have_quasar_app_local() {
  # `quasar dev` only works when a local @quasar/app-vite (or
  # app-webpack) is present alongside the CLI.
  [ -d "$SCRIPT_DIR/portal/node_modules/@quasar/app-vite" ] \
    || [ -d "$SCRIPT_DIR/portal/node_modules/@quasar/app-webpack" ]
}
if [ -d "$SCRIPT_DIR/portal/node_modules" ]; then
  if [ -x "$SCRIPT_DIR/portal/node_modules/.bin/quasar" ] && have_quasar_app_local; then
    PORTAL_BIN="$SCRIPT_DIR/portal/node_modules/.bin/quasar"
    PORTAL_ARGS=(dev)
  elif [ -x "$SCRIPT_DIR/portal/node_modules/.bin/vite" ]; then
    PORTAL_BIN="$SCRIPT_DIR/portal/node_modules/.bin/vite"
  elif command -v vite >/dev/null 2>&1; then
    PORTAL_BIN="$(command -v vite)"
  fi
  if [ -n "$PORTAL_BIN" ]; then
    # Expose the orchestrator base URL to the SPA — both quasar dev and
    # vite read VITE_OC_BASE_URL via import.meta.env at bundle time.
    export VITE_OC_BASE_URL="http://${OC_HOST}:${OC_PORT}"
    PORTAL_URL="http://${PORTAL_HOST}:${PORTAL_PORT}/"
    # Always sweep the port first so an orphan vite/quasar from a
    # crashed previous run doesn't push us onto 9001/9002. With
    # `strictPort: true` in vite.config.js this is now a hard
    # requirement — without it, vite would refuse to start.
    kill_port "$PORTAL_PORT"
    if [ "$DEV_MODE" -eq 0 ]; then
      sleep 1
      START_BG_CWD="$SCRIPT_DIR/portal" start_bg portal "$PORTAL_BIN" ${PORTAL_ARGS[@]+"${PORTAL_ARGS[@]}"}
    fi
  else
    echo "  (skipping portal: no vite binary found — re-run with --install-dependencies)" >&2
  fi
elif [ -d "$SCRIPT_DIR/portal" ]; then
  echo "  (skipping portal: portal/node_modules missing — re-run with --install-dependencies)" >&2
fi

# ---------------------------------------------------------------- summary
echo
echo "==> All services launched. URLs:"
printf "  %-14s %s\n" "OTel MCP:"     "$OTEL_MCP_URL"
printf "  %-14s %s\n" "RAG MCP:"      "$RAG_MCP_URL"
printf "  %-14s %s\n" "BP MCP:"       "$BP_MCP_URL"
printf "  %-14s %s\n" "SD MCP:"       "$SD_MCP_URL"
printf "  %-14s http://%s:%s/v1/\n"   "Orchestrator:" "$OC_HOST" "$OC_PORT"
if [ -n "$PORTAL_URL" ]; then
  printf "  %-14s %s\n" "Portal:" "$PORTAL_URL"
fi
echo
echo "Tail logs with: tail -F $LOGS_DIR/*.log"
echo "Tear down with: $SCRIPT_DIR/stop_all.sh"
echo

# Best-effort liveness probe on the orchestrator REST so an obvious boot
# failure surfaces here rather than at first traffic.
wait_for_http "orchestrator" "http://${OC_HOST}:${OC_PORT}/v1/health" 30 || true

# And on the portal, when we tried to launch one. Quasar's first cold
# build is slow (vite warms up + transpiles), so allow more retries.
# Skip in --dev: there's no portal in BG, we run it in FG below.
if [ -n "$PORTAL_URL" ] && [ "$DEV_MODE" -eq 0 ]; then
  wait_for_http "portal" "$PORTAL_URL" 90 || \
    echo "  (portal didn't respond; check $LOGS_DIR/portal.log)" >&2
fi

# --------------------------------------------------------- --dev FG path
# Run the portal in the foreground via `quasar dev` (or vite fallback)
# so HMR output is interactive. On Ctrl+C / SIGTERM, tear down every
# backend service via stop_all.sh so they don't leak.
if [ "$DEV_MODE" -eq 1 ]; then
  if [ -z "$PORTAL_BIN" ]; then
    echo "ERROR: --dev requested but no portal binary found." >&2
    echo "       Run: ./start_all.sh --install-dependencies" >&2
    "$SCRIPT_DIR/stop_all.sh" || true
    exit 1
  fi

  cleanup_dev() {
    local rc=$?
    trap - INT TERM EXIT
    echo
    echo "==> Tearing down POC services (dev mode exit)"
    "$SCRIPT_DIR/stop_all.sh" || true
    exit "$rc"
  }
  trap cleanup_dev INT TERM EXIT

  echo
  echo "==> dev: running portal in foreground (Ctrl+C to stop everything)"
  echo "    bin: $PORTAL_BIN ${PORTAL_ARGS[*]:-}"
  echo "    cwd: $SCRIPT_DIR/portal"
  echo
  ( cd "$SCRIPT_DIR/portal" && exec "$PORTAL_BIN" ${PORTAL_ARGS[@]+"${PORTAL_ARGS[@]}"} )
  # `exec` replaces the subshell so we never reach here on success;
  # if the subshell returns, the EXIT trap cleans up.
fi
