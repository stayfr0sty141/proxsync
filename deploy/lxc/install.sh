#!/usr/bin/env bash
#
# Install the ProxSync dashboard (API + Web UI) inside an unprivileged LXC container.
#
# Mirrors deploy/host/install-agent.sh: creates a service user, a virtualenv, the built
# frontend, the configuration, a bootstrap TLS certificate, the nginx site and the systemd
# units — then leaves you with a running dashboard on :443. Safe to re-run: an existing secret
# key and certificate are preserved unless --regenerate-secrets is passed.
#
#   ./install.sh --server-name proxsync.lan --agent-ip 10.0.0.10 [--admin-user admin]
#
# Run as root inside the container, from a checkout of the repository.
set -euo pipefail

INSTALL_ROOT=/opt/proxsync
CONFIG_DIR=/etc/proxsync
TLS_DIR="${CONFIG_DIR}/tls"
ENV_FILE="${CONFIG_DIR}/api.env"
STATE_DIR=/var/lib/proxsync
LOG_DIR=/var/log/proxsync
BACKUP_DIR=/var/backups/proxsync
SERVICE_USER=proxsync

SERVER_NAME=""
AGENT_IP=""
AGENT_PORT=8765
ADMIN_USER="admin"
REGENERATE=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

log()  { printf '\033[0;36m[proxsync]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[proxsync]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31m[proxsync]\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --server-name)         SERVER_NAME="$2"; shift 2 ;;
        --agent-ip)            AGENT_IP="$2"; shift 2 ;;
        --agent-port)          AGENT_PORT="$2"; shift 2 ;;
        --admin-user)          ADMIN_USER="$2"; shift 2 ;;
        --regenerate-secrets)  REGENERATE=1; shift ;;
        -h|--help)             usage ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
done

# ---- Preflight -------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "Run as root."
[[ -n "$SERVER_NAME" ]] || die "--server-name is required (the hostname the browser will use)."
[[ -d "${REPO_ROOT}/backend/app" ]] || die "Run from a checkout: ${REPO_ROOT}/backend/app not found."

command -v python3 >/dev/null || die "python3 is required."
command -v openssl >/dev/null || die "openssl is required."
command -v nginx   >/dev/null || die "nginx is required. Install it first (apt install nginx)."
command -v node    >/dev/null || die "node is required for the frontend. Install Node 20+ first."
command -v npm     >/dev/null || die "npm is required for the frontend."

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)'; then
    die "The dashboard requires Python 3.13 or newer (found $(python3 -V 2>&1))."
fi
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
[[ "$NODE_MAJOR" -ge 20 ]] || die "Node 20 or newer is required (found $(node -v))."

# ---- Service user ----------------------------------------------------------
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    log "Creating system user ${SERVICE_USER}"
    useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# ---- Directories -----------------------------------------------------------
log "Creating directories"
install -d -m 0755 "$INSTALL_ROOT"
install -d -m 0750 "$CONFIG_DIR" "$TLS_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$STATE_DIR" "$LOG_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$BACKUP_DIR"

# ---- Backend application ---------------------------------------------------
log "Installing backend into ${INSTALL_ROOT}/backend"
rm -rf "${INSTALL_ROOT}/backend/app"
install -d "${INSTALL_ROOT}/backend"
cp -a "${REPO_ROOT}/backend/app"        "${INSTALL_ROOT}/backend/app"
cp -a "${REPO_ROOT}/backend/alembic"    "${INSTALL_ROOT}/backend/alembic"
cp -a "${REPO_ROOT}/backend/alembic.ini" "${INSTALL_ROOT}/backend/alembic.ini"
cp -a "${REPO_ROOT}/backend/pyproject.toml" "${INSTALL_ROOT}/backend/pyproject.toml"
cp -a "${REPO_ROOT}/backend/README.md"  "${INSTALL_ROOT}/backend/README.md" 2>/dev/null || true

if [[ ! -x "${INSTALL_ROOT}/backend/venv/bin/python" ]]; then
    log "Creating backend virtualenv"
    python3 -m venv "${INSTALL_ROOT}/backend/venv"
fi
log "Installing backend dependencies"
"${INSTALL_ROOT}/backend/venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_ROOT}/backend/venv/bin/pip" install --quiet "${INSTALL_ROOT}/backend"

# ---- Frontend build --------------------------------------------------------
log "Building the frontend (this takes a minute)"
( cd "${REPO_ROOT}/frontend" && npm ci --no-audit --no-fund && npm run build )

log "Installing the standalone frontend into ${INSTALL_ROOT}/frontend"
rm -rf "${INSTALL_ROOT}/frontend"
install -d "${INSTALL_ROOT}/frontend"
cp -a "${REPO_ROOT}/frontend/.next/standalone/." "${INSTALL_ROOT}/frontend/"
install -d "${INSTALL_ROOT}/frontend/.next"
cp -a "${REPO_ROOT}/frontend/.next/static" "${INSTALL_ROOT}/frontend/.next/static"
[[ -d "${REPO_ROOT}/frontend/public" ]] && cp -a "${REPO_ROOT}/frontend/public" "${INSTALL_ROOT}/frontend/public"

# ---- Maintenance scripts ---------------------------------------------------
log "Installing maintenance scripts"
install -d "${INSTALL_ROOT}/scripts"
cp -a "${REPO_ROOT}/scripts/"*.sh "${INSTALL_ROOT}/scripts/" 2>/dev/null || true
chmod +x "${INSTALL_ROOT}/scripts/"*.sh 2>/dev/null || true

chown -R "$SERVICE_USER:$SERVICE_USER" "${INSTALL_ROOT}/frontend"

# ---- Configuration ---------------------------------------------------------
if [[ -f "$ENV_FILE" ]] && [[ $REGENERATE -eq 0 ]]; then
    log "Reusing existing ${ENV_FILE}"
    SECRET_KEY="$(grep -E '^PROXSYNC_SECRET_KEY=' "$ENV_FILE" | cut -d= -f2-)"
else
    SECRET_KEY="$(openssl rand -hex 32)"
    log "Writing ${ENV_FILE}"
    AGENT_URL="https://127.0.0.1:${AGENT_PORT}"
    [[ -n "$AGENT_IP" ]] && AGENT_URL="https://${AGENT_IP}:${AGENT_PORT}"
    cat > "$ENV_FILE" <<EOF
# Generated by deploy/lxc/install.sh on $(date -Is)
PROXSYNC_ENVIRONMENT=production
PROXSYNC_BIND_HOST=127.0.0.1
PROXSYNC_BIND_PORT=8000
PROXSYNC_LOG_LEVEL=INFO
PROXSYNC_LOG_JSON=true

# Root secret: the JWT signing key and the settings-encryption key are derived from this.
# Rotating it invalidates every session and makes stored secrets unreadable — see docs/UPGRADE.md.
PROXSYNC_SECRET_KEY=${SECRET_KEY}

PROXSYNC_DATABASE_URL=sqlite+aiosqlite:///${STATE_DIR}/proxsync.db

# The dashboard is served same-origin behind nginx, so CORS stays empty.
PROXSYNC_CORS_ORIGINS=
PROXSYNC_COOKIE_SECURE=true

# ---- Backup Agent (fill in after running install-agent.sh on the host) ----
PROXSYNC_AGENT_BASE_URL=${AGENT_URL}
PROXSYNC_AGENT_API_KEY_ID=proxsync-dashboard
PROXSYNC_AGENT_HMAC_SECRET=
PROXSYNC_AGENT_CA_CERT=${CONFIG_DIR}/agent-ca.crt
PROXSYNC_AGENT_CLIENT_CERT=${CONFIG_DIR}/agent-client.crt
PROXSYNC_AGENT_CLIENT_KEY=${CONFIG_DIR}/agent-client.key

# ---- Proxmox read-only inventory token (PVEAuditor) -----------------------
PROXSYNC_PROXMOX_BASE_URL=https://${AGENT_IP:-127.0.0.1}:8006
PROXSYNC_PROXMOX_TOKEN_ID=
PROXSYNC_PROXMOX_TOKEN_SECRET=
PROXSYNC_PROXMOX_NODE=pve

# ---- First-run administrator ----------------------------------------------
# Set a password here for the very first start, then remove it. The account is created
# with must_change_password, so it is replaced at first login.
PROXSYNC_BOOTSTRAP_ADMIN_USERNAME=${ADMIN_USER}
PROXSYNC_BOOTSTRAP_ADMIN_PASSWORD=
EOF
fi
chmod 0640 "$ENV_FILE"
chown root:"$SERVICE_USER" "$ENV_FILE"

# ---- Bootstrap admin password ----------------------------------------------
# Only generate one if none is set and no users exist yet, so a re-run never resets it.
if ! grep -qE '^PROXSYNC_BOOTSTRAP_ADMIN_PASSWORD=.+' "$ENV_FILE"; then
    BOOTSTRAP_PW="$(openssl rand -base64 18)"
    sed -i "s#^PROXSYNC_BOOTSTRAP_ADMIN_PASSWORD=.*#PROXSYNC_BOOTSTRAP_ADMIN_PASSWORD=${BOOTSTRAP_PW}#" "$ENV_FILE"
    PRINT_BOOTSTRAP=1
fi

# ---- Database migration ----------------------------------------------------
log "Applying database migrations"
(
  cd "${INSTALL_ROOT}/backend"
  set -a
  # shellcheck source=/dev/null
  . "$ENV_FILE"
  set +a
  venv/bin/alembic upgrade head
)
chown -R "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR"

# ---- Bootstrap TLS certificate ---------------------------------------------
if [[ ! -f "${TLS_DIR}/web.crt" ]] || [[ $REGENERATE -eq 1 ]]; then
    log "Generating a self-signed TLS certificate for ${SERVER_NAME}"
    log "(replace it with a CA-signed or Let's Encrypt certificate for production — see docs/INSTALL.md)"
    openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
        -keyout "${TLS_DIR}/web.key" -out "${TLS_DIR}/web.crt" \
        -subj "/CN=${SERVER_NAME}/O=ProxSync" \
        -addext "subjectAltName=DNS:${SERVER_NAME}" 2>/dev/null
    chmod 0640 "${TLS_DIR}/web.key"
    chmod 0644 "${TLS_DIR}/web.crt"
fi

# ---- nginx -----------------------------------------------------------------
log "Installing nginx site"
NGINX_SITE=/etc/nginx/sites-available/proxsync.conf
sed "s/__SERVER_NAME__/${SERVER_NAME}/g" "${REPO_ROOT}/deploy/nginx/proxsync.conf" > "$NGINX_SITE"
ln -sf "$NGINX_SITE" /etc/nginx/sites-enabled/proxsync.conf
# Debian's default site would otherwise shadow ours on :80.
rm -f /etc/nginx/sites-enabled/default
install -d /var/www/certbot
nginx -t || die "nginx configuration test failed — not reloading."

# ---- systemd ---------------------------------------------------------------
log "Installing systemd units"
cp "${REPO_ROOT}/deploy/systemd/proxsync-api.service" /etc/systemd/system/
cp "${REPO_ROOT}/deploy/systemd/proxsync-web.service" /etc/systemd/system/
cp "${REPO_ROOT}/deploy/systemd/proxsync.target"      /etc/systemd/system/
cp "${REPO_ROOT}/deploy/systemd/proxsync-db-backup.service" /etc/systemd/system/
cp "${REPO_ROOT}/deploy/systemd/proxsync-db-backup.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable proxsync-api.service proxsync-web.service >/dev/null
systemctl enable proxsync-db-backup.timer >/dev/null
systemctl restart proxsync-api.service proxsync-web.service
systemctl restart proxsync-db-backup.timer
systemctl reload nginx || systemctl restart nginx

sleep 2
if ! systemctl is-active --quiet proxsync-api.service; then
    warn "proxsync-api did not start. Recent log output:"
    journalctl -u proxsync-api.service -n 30 --no-pager >&2
    die "Installation failed."
fi

# ---- Summary ---------------------------------------------------------------
cat <<EOF

$(log "ProxSync dashboard is running at https://${SERVER_NAME}/")

Next steps:

  1. Install the Backup Agent on the Proxmox host (deploy/host/install-agent.sh),
     then copy its three client files into ${CONFIG_DIR}/:
         agent-ca.crt, agent-client.crt, agent-client.key
     and set PROXSYNC_AGENT_HMAC_SECRET in ${ENV_FILE} to the value it printed.

  2. Create a read-only PVEAuditor API token on the host and set
     PROXSYNC_PROXMOX_TOKEN_ID / _SECRET in ${ENV_FILE}.

  3. Restart:  systemctl restart proxsync-api
EOF

if [[ "${PRINT_BOOTSTRAP:-0}" -eq 1 ]]; then
    cat <<EOF

  First-run administrator:
      username  ${ADMIN_USER}
      password  ${BOOTSTRAP_PW}
  You will be required to change it at first login. Then clear
  PROXSYNC_BOOTSTRAP_ADMIN_PASSWORD from ${ENV_FILE}.
EOF
fi

cat <<EOF

Logs:  journalctl -u proxsync-api -u proxsync-web -f

EOF
