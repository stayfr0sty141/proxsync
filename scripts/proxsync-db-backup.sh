#!/usr/bin/env bash
#
# Back up ProxSync's own database — the dashboard's state, not the Proxmox guests.
#
# ProxSync backs up VMs; nothing backs up ProxSync. This does: users, schedules, history,
# settings (secrets stay Fernet-encrypted in the row, so the dump is only as sensitive as the
# root secret that is *not* in it). Reads the driver from PROXSYNC_DATABASE_URL and dispatches:
#
#   sqlite   -> sqlite3 '.backup'   an online, WAL-consistent copy; safe while the API runs
#   postgres -> pg_dump -Fc         a custom-format dump restorable with pg_restore
#
#   ./proxsync-db-backup.sh [--out-dir /var/backups/proxsync] [--keep 14] [--env-file PATH]
#
# Exit status is non-zero if the backup could not be written or verified, so a cron wrapper or
# the systemd timer treats a failed backup as a failure rather than a quiet no-op.
set -euo pipefail

OUT_DIR=/var/backups/proxsync
KEEP=14
ENV_FILE=/etc/proxsync/api.env

log()  { printf '\033[0;36m[proxsync-db]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[proxsync-db]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31m[proxsync-db]\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out-dir)  OUT_DIR="$2"; shift 2 ;;
        --keep)     KEEP="$2"; shift 2 ;;
        --env-file) ENV_FILE="$2"; shift 2 ;;
        -h|--help)  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
done

[[ "$KEEP" =~ ^[0-9]+$ ]] || die "--keep must be a non-negative integer."
[[ -f "$ENV_FILE" ]] || die "Env file ${ENV_FILE} not found (pass --env-file)."

# Read only the one variable we need, without sourcing the whole env into this shell.
DATABASE_URL="$(grep -E '^PROXSYNC_DATABASE_URL=' "$ENV_FILE" | head -n1 | cut -d= -f2-)"
[[ -n "$DATABASE_URL" ]] || die "PROXSYNC_DATABASE_URL is not set in ${ENV_FILE}."

install -d -m 0700 "$OUT_DIR"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

case "$DATABASE_URL" in
    sqlite*)
        command -v sqlite3 >/dev/null || die "sqlite3 is required to back up a SQLite database."
        # Strip the driver and the leading ///, leaving an absolute path.
        DB_PATH="/$(printf '%s' "$DATABASE_URL" | sed -E 's#^sqlite(\+[a-z]+)?://+##')"
        [[ -f "$DB_PATH" ]] || die "SQLite database ${DB_PATH} does not exist yet."
        DEST="${OUT_DIR}/proxsync-${TIMESTAMP}.sqlite"
        log "Backing up ${DB_PATH} -> ${DEST}"
        # '.backup' uses the online backup API: it copies a consistent snapshot even with the
        # API mid-write, unlike 'cp' which can catch a torn WAL.
        sqlite3 "$DB_PATH" ".backup '${DEST}'"
        # Prove the copy opens and its schema is intact before we count it as a backup.
        sqlite3 "$DEST" 'PRAGMA integrity_check;' | grep -qx 'ok' \
            || die "Integrity check failed on ${DEST} — backup discarded."
        gzip -f "$DEST"
        ARTIFACT="${DEST}.gz"
        ;;
    postgresql*|postgres*)
        command -v pg_dump >/dev/null || die "pg_dump is required to back up a PostgreSQL database."
        DEST="${OUT_DIR}/proxsync-${TIMESTAMP}.dump"
        log "Backing up PostgreSQL -> ${DEST}"
        # -Fc is the custom format: compressed, and restorable selectively with pg_restore.
        # libpq reads the URL directly; the +asyncpg suffix is stripped for the sync tool.
        PG_URL="$(printf '%s' "$DATABASE_URL" | sed -E 's#\+[a-z]+://#://#')"
        pg_dump --format=custom --no-owner --no-privileges --dbname="$PG_URL" --file="$DEST"
        ARTIFACT="$DEST"
        ;;
    *)
        die "Unrecognised database driver in PROXSYNC_DATABASE_URL: ${DATABASE_URL%%:*}"
        ;;
esac

chmod 0600 "$ARTIFACT"
SIZE="$(du -h "$ARTIFACT" | cut -f1)"
log "Wrote ${ARTIFACT} (${SIZE})"

# --- Retention -------------------------------------------------------------
# Prune by count, newest kept. keep=0 disables pruning (an external system owns retention).
if [[ "$KEEP" -gt 0 ]]; then
    mapfile -t OLD < <(ls -1t "${OUT_DIR}"/proxsync-*.sqlite.gz "${OUT_DIR}"/proxsync-*.dump 2>/dev/null | tail -n +$((KEEP + 1)))
    if [[ ${#OLD[@]} -gt 0 ]]; then
        log "Pruning ${#OLD[@]} backup(s) beyond --keep ${KEEP}"
        rm -f -- "${OLD[@]}"
    fi
fi

log "Done."
