#!/usr/bin/env bash
#
# Restore ProxSync's own database from a backup written by proxsync-db-backup.sh.
#
# This is destructive: it replaces the live database. The API is stopped first so nothing
# writes during the swap, and — for SQLite — the current database is moved aside rather than
# deleted, so a mistaken restore is itself recoverable.
#
#   ./proxsync-db-restore.sh --from /var/backups/proxsync/proxsync-20260723-020000.sqlite.gz
#                            [--env-file PATH] [--yes]
#
# Requires the literal --yes (or an interactive confirmation) because there is no undo beyond
# the safety copy this script makes.
set -euo pipefail

FROM=""
ENV_FILE=/etc/proxsync/api.env
ASSUME_YES=0
API_SERVICE=proxsync-api

log()  { printf '\033[0;36m[proxsync-db]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[proxsync-db]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31m[proxsync-db]\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --from)     FROM="$2"; shift 2 ;;
        --env-file) ENV_FILE="$2"; shift 2 ;;
        --service)  API_SERVICE="$2"; shift 2 ;;
        --yes)      ASSUME_YES=1; shift ;;
        -h|--help)  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
done

[[ -n "$FROM" ]] || die "--from BACKUP_FILE is required."
[[ -f "$FROM" ]] || die "Backup file ${FROM} not found."
[[ -f "$ENV_FILE" ]] || die "Env file ${ENV_FILE} not found (pass --env-file)."

DATABASE_URL="$(grep -E '^PROXSYNC_DATABASE_URL=' "$ENV_FILE" | head -n1 | cut -d= -f2-)"
[[ -n "$DATABASE_URL" ]] || die "PROXSYNC_DATABASE_URL is not set in ${ENV_FILE}."

if [[ $ASSUME_YES -eq 0 ]]; then
    warn "This will REPLACE the live ProxSync database from:"
    warn "  ${FROM}"
    read -r -p "Type 'restore' to proceed: " reply
    [[ "$reply" == "restore" ]] || die "Aborted."
fi

# Stop the API so nothing writes mid-restore. systemctl is absent in some minimal
# containers; fall back to a warning rather than refusing to restore.
stop_api()  { systemctl stop "$API_SERVICE" 2>/dev/null || warn "Could not stop ${API_SERVICE}; ensure it is not running."; }
start_api() { systemctl start "$API_SERVICE" 2>/dev/null || warn "Could not start ${API_SERVICE}; start it manually."; }

case "$DATABASE_URL" in
    sqlite*)
        command -v sqlite3 >/dev/null || die "sqlite3 is required to restore a SQLite database."
        DB_PATH="/$(printf '%s' "$DATABASE_URL" | sed -E 's#^sqlite(\+[a-z]+)?://+##')"

        # Decompress into a staging file first, and integrity-check it *before* touching the
        # live database: a corrupt backup must never cost us the working copy.
        STAGING="$(mktemp "${DB_PATH}.restore.XXXXXX")"
        trap 'rm -f "$STAGING"' EXIT
        log "Staging restore into ${STAGING}"
        case "$FROM" in
            *.gz) gzip -dc "$FROM" > "$STAGING" ;;
            *)    cp "$FROM" "$STAGING" ;;
        esac
        sqlite3 "$STAGING" 'PRAGMA integrity_check;' | grep -qx 'ok' \
            || die "Backup failed its integrity check — live database left untouched."

        stop_api
        if [[ -f "$DB_PATH" ]]; then
            SAFETY="${DB_PATH}.pre-restore-$(date +%Y%m%d-%H%M%S)"
            log "Moving current database aside to ${SAFETY}"
            mv "$DB_PATH" "$SAFETY"
            # WAL/SHM sidecars belong to the old database; leaving them would corrupt the new one.
            rm -f "${DB_PATH}-wal" "${DB_PATH}-shm"
        fi
        log "Installing restored database at ${DB_PATH}"
        mv "$STAGING" "$DB_PATH"
        trap - EXIT
        chmod 0640 "$DB_PATH"
        ;;
    postgresql*|postgres*)
        command -v pg_restore >/dev/null || die "pg_restore is required to restore a PostgreSQL database."
        PG_URL="$(printf '%s' "$DATABASE_URL" | sed -E 's#\+[a-z]+://#://#')"
        stop_api
        log "Restoring PostgreSQL from ${FROM} (existing objects are dropped and recreated)"
        # --clean --if-exists makes the restore idempotent; a partial prior restore does not block it.
        pg_restore --clean --if-exists --no-owner --no-privileges \
            --dbname="$PG_URL" "$FROM"
        ;;
    *)
        die "Unrecognised database driver in PROXSYNC_DATABASE_URL: ${DATABASE_URL%%:*}"
        ;;
esac

# Bring the schema to head in case the backup predates the current code (an older dump
# restored under a newer release). Alembic is a no-op when the schema is already current.
log "Applying any pending migrations"
if [[ -x /opt/proxsync/backend/venv/bin/alembic ]]; then
    (cd /opt/proxsync/backend && venv/bin/alembic upgrade head) || warn "alembic upgrade failed; check the schema before starting the API."
else
    warn "alembic not found at /opt/proxsync/backend; skipping migration step."
fi

start_api
log "Restore complete."
