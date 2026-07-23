#!/usr/bin/env bash
#
# Install the ProxSync Backup Agent on a Proxmox VE host.
#
# Creates the PKI, the configuration, a virtualenv and the systemd unit, then prints the
# credentials the dashboard needs. Safe to re-run: existing keys and secrets are preserved
# unless --regenerate-secrets is passed.
#
#   ./install-agent.sh --agent-ip 10.10.10.2 --dashboard-ip 10.10.10.104 [--agent-port 8443]
#
set -euo pipefail

INSTALL_DIR=/opt/proxsync-agent
CONFIG_DIR=/etc/proxsync-agent
TLS_DIR="${CONFIG_DIR}/tls"
ENV_FILE="${CONFIG_DIR}/agent.env"
UNIT_FILE=/etc/systemd/system/proxsync-agent.service
SERVICE=proxsync-agent

AGENT_IP=""
DASHBOARD_IP=""
AGENT_DNS=""
DUMP_ROOT=/mnt/backup-hdd/dump
TEMP_DIR=/mnt/backup-hdd/tmp
BACKUP_STORAGE=backup-hdd
PORT=8765
MEMORY_HIGH=1G
MEMORY_MAX=2G
REGENERATE=0
FORCE_IP=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

log()  { printf '\033[0;36m[proxsync]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[proxsync]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31m[proxsync]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
ProxSync Host Agent Installer

Usage:
  ./install-agent.sh --agent-ip <IP> --dashboard-ip <IP> [OPTIONS]

Required Parameters:
  --dashboard-ip <IP>    IPv4/IPv6 address of the ProxSync Dashboard LXC.

Options:
  --agent-ip <IP>        Explicit IP address of this agent host interface.
  --agent-port <PORT>    Port for agent service (default: 8765).
  --port <PORT>          Alias for --agent-port.
  --agent-dns <NAME>     Host FQDN/DNS name for TLS certificate SAN (default: hostname).
  --dump-root <PATH>     Directory path for backup dumps (default: /mnt/backup-hdd/dump).
  --temp-dir <PATH>      Directory path for temporary files (default: /mnt/backup-hdd/tmp).
  --backup-storage <NAME> PVE storage identifier (default: backup-hdd).
  --memory-high <LIMIT>  Systemd cgroup MemoryHigh limit (default: 1G).
  --memory-max <LIMIT>   Systemd cgroup MemoryMax limit (default: 2G).
  --force-ip             Skip checking whether --agent-ip exists on host interfaces.
  --regenerate-secrets   Regenerate PKI certificates and HMAC secret.
  -h, --help             Show this help message.
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent-ip)            AGENT_IP="$2"; shift 2 ;;
        --dashboard-ip)        DASHBOARD_IP="$2"; shift 2 ;;
        --agent-port|--port)   PORT="$2"; shift 2 ;;
        --agent-dns)           AGENT_DNS="$2"; shift 2 ;;
        --dump-root)           DUMP_ROOT="$2"; shift 2 ;;
        --temp-dir)            TEMP_DIR="$2"; shift 2 ;;
        --backup-storage)      BACKUP_STORAGE="$2"; shift 2 ;;
        --memory-high)         MEMORY_HIGH="$2"; shift 2 ;;
        --memory-max)          MEMORY_MAX="$2"; shift 2 ;;
        --force-ip)            FORCE_IP=1; shift ;;
        --regenerate-secrets)  REGENERATE=1; shift ;;
        -h|--help)             usage ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
done

# ---- Helpers & Validation --------------------------------------------------
is_valid_ip() {
    local ip="$1"
    python3 -c "import ipaddress, sys; sys.exit(0 if ipaddress.ip_address(sys.argv[1]) else 1)" "$ip" 2>/dev/null
}

get_host_ips() {
    python3 -c '
import subprocess, re
ips = []
try:
    out = subprocess.check_output(["ip", "-o", "addr", "show", "up", "scope", "global"], text=True)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            ip = parts[3].split("/")[0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
except Exception:
    pass
if not ips:
    try:
        out = subprocess.check_output(["hostname", "-I"], text=True)
        for ip in out.split():
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
print(" ".join(ips))
'
}

# ---- Preflight Checks -------------------------------------------------------
[[ $EUID -eq 0 ]] || die "Run as root."
[[ -n "$DASHBOARD_IP" ]] || die "--dashboard-ip is required (the ProxSync LXC address)."
is_valid_ip "$DASHBOARD_IP" || die "--dashboard-ip must be a valid IPv4 or IPv6 address."

command -v vzdump  >/dev/null || die "vzdump not found. This script must run on a Proxmox VE host."
command -v openssl >/dev/null || die "openssl is required."
command -v python3 >/dev/null || die "python3 is required."

PYTHON_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    die "Python ${PYTHON_VERSION} found; the agent requires 3.11 or newer."
fi
log "Python ${PYTHON_VERSION} detected."

# IP Selection Logic
HOST_IPS=($(get_host_ips))

if [[ -n "$AGENT_IP" ]]; then
    is_valid_ip "$AGENT_IP" || die "--agent-ip '$AGENT_IP' is not a valid IPv4/IPv6 address."
    if [[ $FORCE_IP -eq 0 ]]; then
        FOUND=0
        for host_ip in "${HOST_IPS[@]}"; do
            if [[ "$host_ip" == "$AGENT_IP" ]]; then
                FOUND=1
                break
            fi
        done
        if [[ $FOUND -eq 0 ]]; then
            warn "Available host IPs: ${HOST_IPS[*]:-none}"
            die "Agent IP '$AGENT_IP' was not found on host interfaces. Use --force-ip to override if intentional."
        fi
    fi
else
    if [[ ${#HOST_IPS[@]} -eq 1 ]]; then
        AGENT_IP="${HOST_IPS[0]}"
        log "Auto-detected agent IP: ${AGENT_IP}"
    elif [[ ${#HOST_IPS[@]} -gt 1 ]]; then
        warn "Multiple network interfaces / IP addresses detected on host:"
        for candidate in "${HOST_IPS[@]}"; do
            warn "  - ${candidate}"
        done
        die "Multiple candidate IPs found. You must explicitly pass --agent-ip <IP> to select the correct interface."
    else
        die "No active non-loopback network interface detected. Specify --agent-ip <IP> explicitly."
    fi
fi

if [[ -z "$AGENT_DNS" ]]; then
    AGENT_DNS="$(hostname -f 2>/dev/null || hostname)"
fi

log "Agent IP: ${AGENT_IP}"
log "Agent DNS: ${AGENT_DNS}"
log "Dashboard IP: ${DASHBOARD_IP}"

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
    log "Generating certificate authority"
    openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
        -keyout "${TLS_DIR}/ca.key" -out "${TLS_DIR}/ca.crt" \
        -subj "/CN=ProxSync Agent CA/O=ProxSync" 2>/dev/null

    log "Generating server certificate for ${AGENT_DNS} (${AGENT_IP})"
    openssl req -newkey rsa:2048 -sha256 -nodes \
        -keyout "${TLS_DIR}/server.key" -out "${TLS_DIR}/server.csr" \
        -subj "/CN=${AGENT_DNS}/O=ProxSync" 2>/dev/null
    openssl x509 -req -in "${TLS_DIR}/server.csr" -days 3650 -sha256 \
        -CA "${TLS_DIR}/ca.crt" -CAkey "${TLS_DIR}/ca.key" -CAcreateserial \
        -out "${TLS_DIR}/server.crt" \
        -extfile <(printf 'subjectAltName=DNS:%s,IP:%s,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n' \
                   "$AGENT_DNS" "$AGENT_IP") 2>/dev/null

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
else
    HMAC_SECRET="$(openssl rand -hex 32)"
    log "Writing ${ENV_FILE}"
    cat > "$ENV_FILE" <<EOF
# Generated by install-agent.sh on $(date -Is)
PROXSYNC_AGENT_BIND_HOST=${AGENT_IP}
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
sed -e "s|MemoryHigh=.*|MemoryHigh=${MEMORY_HIGH}|" \
    -e "s|MemoryMax=.*|MemoryMax=${MEMORY_MAX}|" \
    "${SCRIPT_DIR}/proxsync-agent.service" > "$UNIT_FILE"
chmod 0644 "$UNIT_FILE"

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

sleep 2

# ---- Post-Install Verification & Health Checks -----------------------------
log "Running post-install health checks..."
HEALTH_PASSED=1

# Check 1: Systemd Service Active
if systemctl is-active --quiet "$SERVICE"; then
    log "  [PASS] Service '$SERVICE' is active and running."
else
    warn "  [FAIL] Service '$SERVICE' failed to start."
    journalctl -u "$SERVICE" -n 30 --no-pager >&2
    HEALTH_PASSED=0
fi

# Check 2: Certificate SAN Validation
if openssl x509 -in "${TLS_DIR}/server.crt" -noout -text 2>/dev/null | grep -q "IP Address:${AGENT_IP}"; then
    log "  [PASS] Certificate SAN contains agent IP ${AGENT_IP}."
else
    warn "  [FAIL] Certificate SAN missing agent IP ${AGENT_IP}."
    HEALTH_PASSED=0
fi

# Check 3: Rclone Binary Presence
if command -v rclone >/dev/null 2>&1; then
    log "  [PASS] rclone binary is installed."
else
    warn "  [WARN] rclone binary is not installed on host. Offsite Google Drive syncs require rclone."
fi

# Check 4: Outbound DNS & HTTPS for Google Drive
if python3 -c "import socket; socket.gethostbyname('drive.googleapis.com')" >/dev/null 2>&1; then
    log "  [PASS] DNS resolution for drive.googleapis.com succeeded."
else
    warn "  [WARN] DNS resolution for drive.googleapis.com failed. Check host DNS settings."
fi

if python3 -c "import socket; socket.create_connection(('drive.googleapis.com', 443), timeout=3).close()" >/dev/null 2>&1; then
    log "  [PASS] Outbound TCP 443 to drive.googleapis.com succeeded."
else
    warn "  [WARN] Outbound TCP 443 to drive.googleapis.com failed/timed out. Check host egress firewall."
fi

# Check 5: Local Agent /health HTTP endpoint check over TLS
if python3 -c '
import urllib.request, ssl, sys
ctx = ssl.create_default_context(cafile="'"${TLS_DIR}/ca.crt"'")
ctx.load_cert_chain(certfile="'"${TLS_DIR}/dashboard.crt"'", keyfile="'"${TLS_DIR}/dashboard.key"'")
req = urllib.request.Request("https://'${AGENT_IP}':'${PORT}'/health")
try:
    with urllib.request.urlopen(req, context=ctx, timeout=5) as res:
        sys.exit(0 if res.status == 200 else 1)
except Exception as e:
    sys.exit(1)
' >/dev/null 2>&1; then
    log "  [PASS] Agent health endpoint (https://${AGENT_IP}:${PORT}/health) answered 200 OK."
else
    warn "  [WARN] Local health check query to https://${AGENT_IP}:${PORT}/health did not return 200 OK."
fi

if [[ $HEALTH_PASSED -eq 0 ]]; then
    die "Post-install verification failed. Review warnings/errors above."
fi

# ---- Summary Output (Secrets redacted) -------------------------------------
cat <<EOF

$(log "Agent successfully installed and running on https://${AGENT_IP}:${PORT}")

Configure the ProxSync dashboard with:

  Agent URL          https://${AGENT_IP}:${PORT}
  API key id         proxsync-dashboard
  HMAC secret        [SECURELY STORED IN ${ENV_FILE}]

To view the HMAC secret for dashboard configuration, run:
  grep PROXSYNC_AGENT_HMAC_SECRET ${ENV_FILE}

Copy these three client authentication files to your ProxSync dashboard LXC:

  ${TLS_DIR}/ca.crt          -> /etc/proxsync/agent-ca.crt
  ${TLS_DIR}/dashboard.crt   -> /etc/proxsync/agent-client.crt
  ${TLS_DIR}/dashboard.key   -> /etc/proxsync/agent-client.key   (mode 0640)

Example transfer command from the dashboard LXC:

  scp root@${AGENT_IP}:${TLS_DIR}/{ca.crt,dashboard.crt,dashboard.key} /etc/proxsync/

Verification:  curl --cacert ${TLS_DIR}/ca.crt https://${AGENT_IP}:${PORT}/health
Logs:          journalctl -u ${SERVICE} -f

EOF
