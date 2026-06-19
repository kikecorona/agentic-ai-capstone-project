#!/usr/bin/env bash
# Reset every piece of mutable runtime state so the agent starts from a
# clean slate. Four concerns, each independently selectable:
#
#   1. Embeddings store   — Chroma persistent dir at $RAG_CHROMA_PATH
#   2. Audit logs         — $AUDIT_DB_PATH (LLM call + service log) and
#                           $OTEL_DB_PATH (OTel spans)
#   3. Per-service state  — BP doc index + sources inventory ($BP_DB_PATH),
#                           SD doc index + sources inventory ($SD_DB_PATH),
#                           Orchestrator queue + tasks ($OC_DB_PATH).
#                           These are tied to the repo state — wipe them
#                           when --repo runs or doc indexes will point at
#                           pages that no longer exist.
#   4. Docs repo          — pear-store force-reset to the `starting-point`
#                           tag and pushed to the configured remote.
#
# Run with no flags to reset everything; pass any subset of
# --embeddings / --logs / --state / --repo to do only those.
#
# Destructive operations:
#   * `git push --force-with-lease` against the configured remote.
#   * `rm -rf` against $RAG_CHROMA_PATH.
#   * `rm -f` against $AUDIT_DB_PATH, $OTEL_DB_PATH, $BP_DB_PATH,
#     $SD_DB_PATH, $OC_DB_PATH (and their -journal/-shm/-wal siblings).
#
# Pass --yes / -y to skip the confirmation prompt.

set -euo pipefail

DO_EMBEDDINGS=0
DO_REPO=0
DO_LOGS=0
DO_STATE=0
ASSUME_YES=0

if [ "$#" -eq 0 ]; then
  DO_EMBEDDINGS=1
  DO_REPO=1
  DO_LOGS=1
  DO_STATE=1
fi

for arg in "$@"; do
  case "$arg" in
    --embeddings|--rag) DO_EMBEDDINGS=1 ;;
    --repo|--pear-store) DO_REPO=1 ;;
    --logs|--audit) DO_LOGS=1 ;;
    --state|--service-state|--db|--sqlite) DO_STATE=1 ;;
    --all) DO_EMBEDDINGS=1; DO_REPO=1; DO_LOGS=1; DO_STATE=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help)
      sed -n '2,28p' "$0"
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

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# Defaults match start_all.sh + .env.example.
: "${RAG_CHROMA_PATH:=$SCRIPT_DIR/data/rag/chroma}"
: "${AUDIT_DB_PATH:=$SCRIPT_DIR/data/audit/log.db}"
: "${OTEL_DB_PATH:=$SCRIPT_DIR/data/otel/spans.db}"
: "${BP_DB_PATH:=$SCRIPT_DIR/data/bp/state.db}"
: "${SD_DB_PATH:=$SCRIPT_DIR/data/sd/state.db}"
: "${OC_DB_PATH:=$SCRIPT_DIR/data/orchestrator/state.db}"
: "${PEARSTORE_REPO_PATH:=$SCRIPT_DIR/../../pear-store}"
: "${PEARSTORE_RESET_TAG:=starting-point}"
: "${PEARSTORE_REMOTE:=origin}"
: "${PEARSTORE_BRANCH:=main}"

# Helper: rm a SQLite file plus its -journal / -shm / -wal siblings.
_rm_sqlite() {
  local label="$1" path="$2"
  for p in "$path" "$path-journal" "$path-shm" "$path-wal"; do
    if [ -f "$p" ]; then
      rm -f "$p"
      echo "    [$label] removed $p"
    fi
  done
}

# Confirmation prompt — destructive operations don't run silently by default.
echo "==> reset_state.sh — about to:"
[ "$DO_EMBEDDINGS" -eq 1 ] && echo "    [embeddings] rm -rf $RAG_CHROMA_PATH"
[ "$DO_LOGS" -eq 1 ] && echo "    [logs] rm -f $AUDIT_DB_PATH (+journals) and $OTEL_DB_PATH (+journals)"
[ "$DO_STATE" -eq 1 ] && echo "    [state] rm -f $BP_DB_PATH, $SD_DB_PATH, $OC_DB_PATH (+journals)"
[ "$DO_REPO" -eq 1 ] && echo "    [repo] force-reset $PEARSTORE_REPO_PATH to tag $PEARSTORE_RESET_TAG and 'git push --force-with-lease $PEARSTORE_REMOTE $PEARSTORE_BRANCH'"
echo

if [ "$ASSUME_YES" -ne 1 ]; then
  read -r -p "Proceed? [y/N] " confirm
  case "$confirm" in
    y|Y|yes|YES) ;;
    *) echo "aborted."; exit 1 ;;
  esac
fi

# ----------------------------------------------------------- 1. embeddings
if [ "$DO_EMBEDDINGS" -eq 1 ]; then
  echo
  echo "==> [embeddings] purging Chroma store at $RAG_CHROMA_PATH"
  if [ -d "$RAG_CHROMA_PATH" ]; then
    rm -rf "$RAG_CHROMA_PATH"
    echo "    removed."
  else
    echo "    (not present)"
  fi
  # Recreate the empty directory so a follow-on `start_all.sh` doesn't
  # have to mkdir it lazily and race with first-write.
  mkdir -p "$RAG_CHROMA_PATH"
fi

# --------------------------------------------------------------- 2. logs
if [ "$DO_LOGS" -eq 1 ]; then
  echo
  echo "==> [logs] removing SQLite audit + OTel stores"
  _rm_sqlite "audit" "$AUDIT_DB_PATH"
  _rm_sqlite "otel"  "$OTEL_DB_PATH"
  mkdir -p "$(dirname "$AUDIT_DB_PATH")" "$(dirname "$OTEL_DB_PATH")"
fi

# --------------------------------------------------------- 3. service state
if [ "$DO_STATE" -eq 1 ]; then
  echo
  echo "==> [state] removing per-service SQLite stores"
  _rm_sqlite "bp" "$BP_DB_PATH"
  _rm_sqlite "sd" "$SD_DB_PATH"
  _rm_sqlite "oc" "$OC_DB_PATH"
  mkdir -p \
    "$(dirname "$BP_DB_PATH")" \
    "$(dirname "$SD_DB_PATH")" \
    "$(dirname "$OC_DB_PATH")"
fi

# ----------------------------------------------------------------- 4. repo
if [ "$DO_REPO" -eq 1 ]; then
  echo
  echo "==> [repo] resetting $PEARSTORE_REPO_PATH to tag $PEARSTORE_RESET_TAG"
  if [ ! -d "$PEARSTORE_REPO_PATH/.git" ]; then
    echo "    ERROR: $PEARSTORE_REPO_PATH is not a git checkout" >&2
    echo "    Set PEARSTORE_REPO_PATH in .env or clone the docs repo first" >&2
    exit 1
  fi
  (
    cd "$PEARSTORE_REPO_PATH"
    echo "    fetching tags from $PEARSTORE_REMOTE"
    git fetch "$PEARSTORE_REMOTE" --tags --prune
    if ! git rev-parse --verify --quiet "refs/tags/$PEARSTORE_RESET_TAG^{commit}" >/dev/null; then
      echo "    ERROR: tag $PEARSTORE_RESET_TAG not found in $(pwd)" >&2
      echo "    Available tags:" >&2
      git tag --list | sed 's/^/      /' >&2
      exit 1
    fi
    target_sha="$(git rev-parse "refs/tags/$PEARSTORE_RESET_TAG^{commit}")"
    echo "    target sha: $target_sha"

    # Make sure we're on the configured branch. Create it if missing.
    if git show-ref --verify --quiet "refs/heads/$PEARSTORE_BRANCH"; then
      git checkout "$PEARSTORE_BRANCH"
    else
      git checkout -B "$PEARSTORE_BRANCH" "$target_sha"
    fi

    echo "    git reset --hard $PEARSTORE_RESET_TAG"
    git reset --hard "$target_sha"

    echo "    git push --force-with-lease $PEARSTORE_REMOTE $PEARSTORE_BRANCH"
    # --force-with-lease is the safer cousin of --force: if someone else
    # pushed in between fetch and push, the operation aborts instead of
    # clobbering their commit.
    git push --force-with-lease "$PEARSTORE_REMOTE" "$PEARSTORE_BRANCH"
  )
fi

echo
done_parts=()
[ "$DO_EMBEDDINGS" -eq 1 ] && done_parts+=("embeddings")
[ "$DO_LOGS"       -eq 1 ] && done_parts+=("logs")
[ "$DO_STATE"      -eq 1 ] && done_parts+=("state")
[ "$DO_REPO"       -eq 1 ] && done_parts+=("repo")
IFS=' + '
echo "==> done. ${done_parts[*]} reset."
unset IFS
