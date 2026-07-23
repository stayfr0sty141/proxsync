#!/usr/bin/env bash
#
# Install the ProxSync Backup Agent on a Proxmox VE host.
#
# Creates the PKI, the configuration, a virtualenv and the systemd unit, then prints the
# credentials the dashboard needs. Safe to re-run: existing keys and secrets are preserved
# unless --regenerate-secrets is passed.
#
#   ./install-agent.sh --dashboard-ip 10.0.0.20 [--dump-root /mnt/backup-hdd/dump]
#
set -euo pipefail

INSTALL_DIR=/opt/proxsync-agent
CONFIG_DIR=/etc/proxsync-agent
TLS_DIR="${CONFIG_DIR}/tls"
ENV_FILE="${CONFIG_DIR}/agent.env"
UNIT_FILE=/etc/systemd/system/proxsync-agent.service
SERVICE=proxsync-agent

DASHBOARD_IP=""
DUMP_ROOT=/mnt/backup-hdd/dump
TEMP_DIR=/mnt/backup-hdd/tmp
BACKUP_STORAGE=backup-hdd
PORT=8765
REGENERATE=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

log()  { printf '\033[0;36m[proxsync]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[proxsync]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31m[proxsync]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dashboard-ip)        DASHBOARD_IP="$2"; shift 2 ;;
        --dump-root)           DUMP_ROOT="$2"; shift 2 ;;
        --temp-dir)            TEMP_DIR="$2"; shift 2 ;;
        --backup-storage)      BACKUP_STORAGE="$2"; shift 2 ;;
        --port)                PORT="$2"; shift 2 ;;
        --regenerate-secrets)  REGENERATE=1; shift ;;
        -h|--help)             usage ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
done

# ---- Preflight -------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "Run as root."
[[ -n "$DASHBOARD_IP" ]] || die "--dashboard-ip is required (the ProxSync LXC address)."
[[ "$DASHBOARD_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "--dashboard-ip must be an IPv4 address."

command -v vzdump  >/dev/null || die "vzdump not found. This script must run on a Proxmox VE host."
command -v openssl >/dev/null || die "openssl is required."
command -v python3 >/dev/null || die "python3 is required."

PYTHON_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    die "Python ${PYTHON_VERSION} found; the agent requires 3.11 or newer."
fi
log "Python ${PYTHON_VERSION} detected."

[[ -d "$DUMP_ROOT" ]] || die "Dump root ${DUMP_ROOT} does not exist. Create and mount it first."

if ! pvesm status 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$BACKUP_STORAGE"; then
    warn "Storage '${BACKUP_STORAGE}' was not reported by pvesm. Backups will be refused until it exists."
fi

# ---- Directories -----------------------------------------------------------
log "Creating directories"
install -d -m 0755 "$INSTALL_DIR"
install -d -m 0750 "$CONFIG_DIR" "$TLS_DIR"
install -d -m 0750 /var/lib/proxsync-agent /var/log/proxsync-agent
install -d -m 0755 "$TEMP_DIR"

# ---- Application -----------------------------------------------------------
log "Installing application into ${INSTALL_DIR}"
if [[ ! -d "${REPO_ROOT}/agent/app" ]]; then
    die "Cannot find ${REPO_ROOT}/agent/app — run this script from a checkout of the repository."
fi
rm -rf "${INSTALL_DIR}/app"
cp -a "${REPO_ROOT}/agent/app" "${INSTALL_DIR}/app"
cp -a "${REPO_ROOT}/agent/pyproject.toml" "${INSTALL_DIR}/pyproject.toml"

if [[ ! -x "${INSTALL_DIR}/venv/bin/python" ]]; then
    log "Creating virtualenv"
    python3 -m venv "${INSTALL_DIR}/venv"
fi
log "Installing dependencies"
"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install --quiet "${INSTALL_DIR}"

# ---- PKI -------------------------------------------------------------------
generate_pki() {
    local hostname_fqdn host_ip
    hostname_fqdn="$(hostname -f 2>/dev/null || hostname)"
    host_ip="$(hostname -I | awk '{print $1}')"

    log "Generating certificate authority"
    openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
        -keyout "${TLS_DIR}/ca.key" -out "${TLS_DIR}/ca.crt" \
        -subj "/CN=ProxSync Agent CA/O=ProxSync" 2>/dev/null

    log "Generating server certificate for ${hostname_fqdn} (${host_ip})"
    openssl req -newkey rsa:2048 -sha256 -nodes \
        -keyout "${TLS_DIR}/server.key" -out "${TLS_DIR}/server.csr" \
        -subj "/CN=${hostname_fqdn}/O=ProxSync" 2>/dev/null
    openssl x509 -req -in "${TLS_DIR}/server.csr" -days 3650 -sha256 \
        -CA "${TLS_DIR}/ca.crt" -CAkey "${TLS_DIR}/ca.key" -CAcreateserial \
        -out "${TLS_DIR}/server.crt" \
        -extfile <(printf 'subjectAltName=DNS:%s,IP:%s,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n' \
                   "$hostname_fqdn" "$host_ip") 2>/dev/null

    log "Generating client certificate for the dashboard"
    openssl req -newkey rsa:2048 -sha256 -nodes \
        -keyout "${TLS_DIR}/dashboard.key" -out "${TLS_DIR}/dashboard.csr" \
        -subj "/CN=proxsync-dashboard/O=ProxSync" 2>/dev/null
    openssl x509 -req -in "${TLS_DIR}/dashboard.csr" -days 3650 -sha256 \
        -CA "${TLS_DIR}/ca.crt" -CAkey "${TLS_DIR}/ca.key" -CAcreateserial \
        -out "${TLS_DIR}/dashboard.crt" \
        -extfile <(printf 'extendedKeyUsage=clientAuth\n') 2>/dev/null

    rm -f "${TLS_DIR}"/*.csr
    chmod 0640 "${TLS_DIR}"/*.key
    chmod 0644 "${TLS_DIR}"/*.crt
}

if [[ ! -f "${TLS_DIR}/ca.crt" ]] || [[ $REGENERATE -eq 1 ]]; then
    generate_pki
else
    log "Reusing existing certificates in ${TLS_DIR} (pass --regenerate-secrets to replace)"
fi

# ---- Configuration ---------------------------------------------------------
if [[ -f "$ENV_FILE" ]] && [[ $REGENERATE -eq 0 ]]; then
    log "Reusing existing ${ENV_FILE}"
    HMAC_SECRET="$(grep -E '^PROXSYNC_AGENT_HMAC_SECRET=' "$ENV_FILE" | cut -d= -f2-)"
else
    HMAC_SECRET="$(openssl rand -hex 32)"
    log "Writing ${ENV_FILE}"
    cat > "$ENV_FILE" <<EOF
# Generated by install-agent.sh on $(date -Is)
PROXSYNC_AGENT_BIND_HOST=0.0.0.0
PROXSYNC_AGENT_BIND_PORT=${PORT}
PROXSYNC_AGENT_LOG_LEVEL=INFO
PROXSYNC_AGENT_LOG_JSON=true

PROXSYNC_AGENT_TLS_CERTFILE=${TLS_DIR}/server.crt
PROXSYNC_AGENT_TLS_KEYFILE=${TLS_DIR}/server.key
PROXSYNC_AGENT_TLS_CLIENT_CA=${TLS_DIR}/ca.crt
PROXSYNC_AGENT_ALLOWED_CLIENT_NETWORKS=${DASHBOARD_IP}/32

PROXSYNC_AGENT_API_KEY_ID=proxsync-dashboard
PROXSYNC_AGENT_HMAC_SECRET=${HMAC_SECRET}
PROXSYNC_AGENT_SIGNATURE_WINDOW_SECONDS=60

PROXSYNC_AGENT_DUMP_ROOT=${DUMP_ROOT}
PROXSYNC_AGENT_TEMP_DIR=${TEMP_DIR}
PROXSYNC_AGENT_STATE_DIR=/var/lib/proxsync-agent
PROXSYNC_AGENT_LOG_DIR=/var/log/proxsync-agent

PROXSYNC_AGENT_ALLOWED_VMIDS=
PROXSYNC_AGENT_ALLOWED_BACKUP_STORAGES=${BACKUP_STORAGE}
PROXSYNC_AGENT_ALLOWED_RESTORE_STORAGES=
PROXSYNC_AGENT_VERIFY_STORAGE_WITH_PVESM=true

PROXSYNC_AGENT_MAX_CONCURRENT_BACKUPS=1
PROXSYNC_AGENT_MAX_CONCURRENT_RESTORES=1
PROXSYNC_AGENT_CHECKSUM_AFTER_BACKUP=true
EOF
fi
chmod 0640 "$ENV_FILE"

# ---- systemd ---------------------------------------------------------------
log "Installing systemd unit"
sed "s|IPAddressAllow=10\.0\.0\.20/32|IPAddressAllow=${DASHBOARD_IP}/32|" \
    "${SCRIPT_DIR}/proxsync-agent.service" > "$UNIT_FILE"
chmod 0644 "$UNIT_FILE"

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

sleep 2
if ! systemctl is-active --quiet "$SERVICE"; then
    warn "The service did not start. Recent log output:"
    journalctl -u "$SERVICE" -n 30 --no-pager >&2
    die "Installation failed."
fi

# ---- Summary ---------------------------------------------------------------
HOST_IP="$(hostname -I | awk '{print $1}')"
cat <<EOF

$(log "Agent is running on https://${HOST_IP}:${PORT}")

Configure the ProxSync dashboard with:

  Agent URL          https://${HOST_IP}:${PORT}
  API key id         proxsync-dashboard
  HMAC secret        ${HMAC_SECRET}

Copy these three files to the dashboard LXC (they authenticate it to this agent):

  ${TLS_DIR}/ca.crt          -> /etc/proxsync/agent-ca.crt
  ${TLS_DIR}/dashboard.crt   -> /etc/proxsync/agent-client.crt
  ${TLS_DIR}/dashboard.key   -> /etc/proxsync/agent-client.key   (mode 0640)

For example, from the dashboard container:

  scp root@${HOST_IP}:${TLS_DIR}/{ca.crt,dashboard.crt,dashboard.key} /etc/proxsync/

Verify:  curl --cacert ${TLS_DIR}/ca.crt https://${HOST_IP}:${PORT}/health
Logs:    journalctl -u ${SERVICE} -f

EOF
