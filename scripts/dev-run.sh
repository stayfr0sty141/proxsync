#!/usr/bin/env bash
#
# Run ProxSync locally for development: the backend API, the frontend dev server, and
# (optionally) the agent, all in one terminal. A single Ctrl-C stops every child cleanly.
#
#   ./scripts/dev-run.sh            # backend + frontend
#   ./scripts/dev-run.sh --agent    # backend + frontend + agent (no TLS)
#   ./scripts/dev-run.sh --no-frontend
#
# Assumes scripts/dev-setup.sh has been run (the .env files must exist). Logs from every
# process are interleaved in this terminal, each line prefixed with its source.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WITH_AGENT=0
WITH_FRONTEND=1
WITH_BACKEND=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent)        WITH_AGENT=1; shift ;;
        --no-frontend)  WITH_FRONTEND=0; shift ;;
        --no-backend)   WITH_BACKEND=0; shift ;;
        -h|--help)      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1 (try --help)" >&2; exit 1 ;;
    esac
done

log() { printf '\033[0;36m[dev-run]\033[0m %s\n' "$*"; }
die() { printf '\033[0;31m[dev-run]\033[0m %s\n' "$*" >&2; exit 1; }

[[ -f backend/.env ]] || die "backend/.env missing. Run: ./scripts/dev-setup.sh"

PIDS=()

# Stop every child process on exit, so no orphaned uvicorn/next lingers on a port.
cleanup() {
    log "Shutting down..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    log "Stopped."
}
trap cleanup INT TERM EXIT

# Prefix each line of a stream with a coloured tag so interleaved logs stay readable.
prefix() {
    local tag="$1" colour="$2"
    while IFS= read -r line; do
        printf '\033[0;%sm[%s]\033[0m %s\n' "$colour" "$tag" "$line"
    done
}

if [[ $WITH_BACKEND -eq 1 ]]; then
    log "Starting backend API on http://127.0.0.1:8000"
    ( cd backend && set -a && . .env && set +a && \
      exec .venv/bin/python -m app ) 2>&1 | prefix api 32 &
    PIDS+=($!)
fi

if [[ $WITH_AGENT -eq 1 ]]; then
    [[ -f agent/.env ]] || die "agent/.env missing. Run: ./scripts/dev-setup.sh --force"
    log "Starting agent on http://127.0.0.1:8765 (no TLS)"
    ( cd agent && set -a && . .env && set +a && \
      exec .venv/bin/python -m app ) 2>&1 | prefix agent 35 &
    PIDS+=($!)
fi

if [[ $WITH_FRONTEND -eq 1 ]]; then
    log "Starting frontend on http://localhost:3000"
    ( cd frontend && exec npm run dev ) 2>&1 | prefix web 34 &
    PIDS+=($!)
fi

log "All processes started. Press Ctrl-C to stop."
log "  dashboard  http://localhost:3000   (login admin / admin123)"

# Wait for any child to exit; the trap then tears the rest down.
wait -n 2>/dev/null || wait
