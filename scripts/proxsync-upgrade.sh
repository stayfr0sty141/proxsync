#!/usr/bin/env bash
#
# Upgrade an installed ProxSync dashboard in place.
#
# The order is deliberate and safe-by-default: take a database backup *first*, so a bad
# migration is always recoverable; only then update code, dependencies and the built UI; run
# migrations with the services stopped; and restart. Any failure before the restart leaves the
# old services running, because they were never stopped until the last safe moment.
#
#   ./proxsync-upgrade.sh [--repo /opt/proxsync/src] [--ref main] [--no-backup]
#
# Run as root inside the dashboard LXC, from a checkout or with --repo pointing at one.
set -euo pipefail

INSTALL_ROOT=/opt/proxsync
REPO=/opt/proxsync/src
REF=""
DO_BACKUP=1
ENV_FILE=/etc/proxsync/api.env

log()  { printf '\033[0;36m[proxsync]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[proxsync]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31m[proxsync]\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)      REPO="$2"; shift 2 ;;
        --ref)       REF="$2"; shift 2 ;;
        --no-backup) DO_BACKUP=0; shift ;;
        --env-file)  ENV_FILE="$2"; shift 2 ;;
        -h|--help)   sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "Run as root."
[[ -d "${REPO}/backend/app" ]] || die "Cannot find ${REPO}/backend/app — pass --repo to a repository checkout."

# --- 1. Refresh the source ---------------------------------------------------
if [[ -n "$REF" ]] && command -v git >/dev/null && [[ -d "${REPO}/.git" ]]; then
    log "Fetching ${REF} in ${REPO}"
    git -C "$REPO" fetch --quiet --all
    git -C "$REPO" checkout --quiet "$REF"
    git -C "$REPO" pull --quiet --ff-only origin "$REF" || warn "Not fast-forwardable; using the checked-out tree as-is."
fi

OLD_VERSION="$(grep -E '^version' "${INSTALL_ROOT}/backend/pyproject.toml" 2>/dev/null | head -n1 | cut -d'"' -f2 || echo unknown)"
NEW_VERSION="$(grep -E '^version' "${REPO}/backend/pyproject.toml" | head -n1 | cut -d'"' -f2)"
log "Upgrading ProxSync ${OLD_VERSION} -> ${NEW_VERSION}"

# --- 2. Back up the database BEFORE any migration ---------------------------
if [[ $DO_BACKUP -eq 1 ]]; then
    log "Backing up the database before upgrading"
    "${REPO}/scripts/proxsync-db-backup.sh" --env-file "$ENV_FILE" \
        || die "Pre-upgrade backup failed — refusing to continue. (override with --no-backup)"
fi

# --- 3. Sync code into the install root -------------------------------------
log "Updating application code in ${INSTALL_ROOT}"
for component in backend agent frontend scripts; do
    [[ -d "${REPO}/${component}" ]] || continue
done
rsync -a --delete --exclude venv --exclude .venv --exclude node_modules \
    "${REPO}/backend/" "${INSTALL_ROOT}/backend/"
rsync -a "${REPO}/scripts/" "${INSTALL_ROOT}/scripts/"
chmod +x "${INSTALL_ROOT}/scripts/"*.sh 2>/dev/null || true

# --- 4. Dependencies --------------------------------------------------------
log "Updating backend dependencies"
"${INSTALL_ROOT}/backend/venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_ROOT}/backend/venv/bin/pip" install --quiet "${INSTALL_ROOT}/backend"

# --- 5. Rebuild the frontend ------------------------------------------------
if [[ -d "${REPO}/frontend" ]]; then
    log "Building the frontend"
    ( cd "${REPO}/frontend" && npm ci --no-audit --no-fund && npm run build )
    log "Installing the standalone build into ${INSTALL_ROOT}/frontend"
    rm -rf "${INSTALL_ROOT}/frontend"
    install -d "${INSTALL_ROOT}/frontend"
    cp -a "${REPO}/frontend/.next/standalone/." "${INSTALL_ROOT}/frontend/"
    install -d "${INSTALL_ROOT}/frontend/.next"
    cp -a "${REPO}/frontend/.next/static" "${INSTALL_ROOT}/frontend/.next/static"
    [[ -d "${REPO}/frontend/public" ]] && cp -a "${REPO}/frontend/public" "${INSTALL_ROOT}/frontend/public"
    chown -R proxsync:proxsync "${INSTALL_ROOT}/frontend"
fi

# --- 6. Migrate with services stopped ---------------------------------------
log "Stopping services for migration"
systemctl stop proxsync-web.service proxsync-api.service 2>/dev/null || warn "Could not stop services via systemctl."

log "Applying migrations"
( cd "${INSTALL_ROOT}/backend" && venv/bin/alembic upgrade head ) \
    || die "Migration failed. Services are stopped; restore with proxsync-db-restore.sh before retrying."

# --- 7. Reinstall units in case they changed, then restart ------------------
if [[ -d "${REPO}/deploy/systemd" ]]; then
    log "Refreshing systemd units"
    cp "${REPO}/deploy/systemd/"*.service "${REPO}/deploy/systemd/"*.target /etc/systemd/system/ 2>/dev/null || true
    cp "${REPO}/deploy/systemd/"*.timer /etc/systemd/system/ 2>/dev/null || true
    systemctl daemon-reload
fi

log "Starting services"
systemctl start proxsync-api.service proxsync-web.service

sleep 2
if systemctl is-active --quiet proxsync-api.service && systemctl is-active --quiet proxsync-web.service; then
    log "Upgrade to ${NEW_VERSION} complete."
else
    warn "One or more services did not come back up. Recent logs:"
    journalctl -u proxsync-api.service -u proxsync-web.service -n 40 --no-pager >&2
    die "Upgrade finished with services down — investigate before retrying."
fi
