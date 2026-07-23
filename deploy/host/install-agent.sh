#!/usr/bin/env bash
#
# Install the ProxSync Backup Agent on a Proxmox VE host.
#
# Creates the PKI, configuration, virtualenv, firewall rules, and systemd unit,
# then prints the credentials required by the dashboard.
#
# Safe to re-run: existing keys and HMAC secrets are preserved unless explicit
# regeneration flags are passed. Non-secret options (bind IP, port, storage, firewall)
# are updated idempotently on rerun.
#
#   ./install-agent.sh --agent-ip 10.10.10.2 --dashboard-ip 10.10.10.104 [--agent-port 8765]
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

FORCE_IP=0
CONFIGURE_FIREWALL=1
ROTATE_SERVER_CERT=0
REGENERATE_ALL_SECRETS=0
REQUIRE_RCLONE=0
REQUIRE_DRIVE_CONNECTIVITY=0
SKIP_OUTBOUND_CHECK=0
UNINSTALL_MODE=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

log()  { printf '\033[0;36m[proxsync]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[proxsync]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31m[proxsync]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
ProxSync Host Agent Installer

Usage:
  ./install-agent.sh --dashboard-ip <IP> [OPTIONS]

Required Parameters:
  --dashboard-ip <IP>         IPv4/IPv6 address of the ProxSync Dashboard LXC.

Options:
  --agent-ip <IP>             Explicit IP address of this agent host interface.
  --agent-port <PORT>         Port for agent service (default: 8765).
  --port <PORT>               Alias for --agent-port.
  --agent-dns <NAME>          Host FQDN/DNS name for TLS certificate SAN (default: hostname).
  --dump-root <PATH>          Directory path for backup dumps (default: /mnt/backup-hdd/dump).
  --temp-dir <PATH>           Directory path for temporary files (default: /mnt/backup-hdd/tmp).
  --backup-storage <NAME>     PVE storage identifier (default: backup-hdd).
  --memory-high <LIMIT>       Systemd cgroup MemoryHigh limit (default: 1G).
  --memory-max <LIMIT>        Systemd cgroup MemoryMax limit (default: 2G).
  --force-ip                  Skip checking whether --agent-ip exists on host interfaces.

Firewall & Security:
  --configure-firewall        Enable host nftables firewall (default).
  --skip-firewall             Do not configure host firewall rules.
  --rotate-server-cert        Force regeneration of server TLS certificate using existing CA.
  --regenerate-all-secrets    Regenerate all PKI certificates, keys, and HMAC secret.
  --regenerate-secrets        Alias for --regenerate-all-secrets.

Health Checks:
  --require-rclone            Require rclone binary to be installed (fatal if missing).
  --require-drive-connectivity Require DNS & TCP 443 connectivity to Google Drive (fatal if missing).
  --skip-outbound-check       Skip outbound connectivity checks.

Maintenance:
  --uninstall                 Remove ProxSync agent service, firewall table, and files.
  -h, --help                  Show this help message.
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent-ip)                    AGENT_IP="$2"; shift 2 ;;
        --dashboard-ip)                DASHBOARD_IP="$2"; shift 2 ;;
        --agent-port|--port)           PORT="$2"; shift 2 ;;
        --agent-dns)                   AGENT_DNS="$2"; shift 2 ;;
        --dump-root)                   DUMP_ROOT="$2"; shift 2 ;;
        --temp-dir)                    TEMP_DIR="$2"; shift 2 ;;
        --backup-storage)              BACKUP_STORAGE="$2"; shift 2 ;;
        --memory-high)                 MEMORY_HIGH="$2"; shift 2 ;;
        --memory-max)                  MEMORY_MAX="$2"; shift 2 ;;
        --force-ip)                    FORCE_IP=1; shift ;;
        --configure-firewall)          CONFIGURE_FIREWALL=1; shift ;;
        --skip-firewall)               CONFIGURE_FIREWALL=0; shift ;;
        --rotate-server-cert)          ROTATE_SERVER_CERT=1; shift ;;
        --regenerate-all-secrets|--regenerate-secrets) REGENERATE_ALL_SECRETS=1; shift ;;
        --require-rclone)              REQUIRE_RCLONE=1; shift ;;
        --require-drive-connectivity)  REQUIRE_DRIVE_CONNECTIVITY=1; shift ;;
        --skip-outbound-check)         SKIP_OUTBOUND_CHECK=1; shift ;;
        --uninstall)                   UNINSTALL_MODE=1; shift ;;
        -h|--help)                     usage ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
done

# ---- Helpers & Validation --------------------------------------------------
is_valid_ip() {
    local ip="$1"
    python3 -c "import ipaddress, sys; sys.exit(0 if ipaddress.ip_address(sys.argv[1]) else 1)" "$ip" 2>/dev/null
}

is_ipv6() {
    local ip="$1"
    python3 -c "import ipaddress, sys; sys.exit(0 if ipaddress.ip_address(sys.argv[1]).version == 6 else 1)" "$ip" 2>/dev/null
}

format_url_host() {
    local host="$1"
    if is_ipv6 "$host"; then
        printf '[%s]' "$host"
    else
        printf '%s' "$host"
    fi
}

get_cidr() {
    local ip="$1"
    if is_ipv6 "$ip"; then
        printf '%s/128' "$ip"
    else
        printf '%s/32' "$ip"
    fi
}

get_host_ips() {
    python3 -c '
import subprocess
ips = []
try:
    out = subprocess.check_output(["ip", "-o", "addr", "show", "up", "scope", "global"], text=True)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            ip = parts[3].split("/")[0]
            if ip not in ips and not ip.startswith("127.") and ip != "::1":
                ips.append(ip)
except Exception:
    pass
if not ips:
    try:
        out = subprocess.check_output(["hostname", "-I"], text=True)
        for ip in out.split():
            if ip not in ips and not ip.startswith("127.") and ip != "::1":
                ips.append(ip)
    except Exception:
        pass
print(" ".join(ips))
'
}

# ---- Uninstall Mode ---------------------------------------------------------
if [[ $UNINSTALL_MODE -eq 1 ]]; then
    [[ $EUID -eq 0 ]] || die "Uninstall must run as root."
    log "Uninstalling ProxSync Backup Agent..."
    if systemctl is-active --quiet "$SERVICE" 2>/dev/null || systemctl is-enabled --quiet "$SERVICE" 2>/dev/null; then
        log "Stopping and disabling systemd service '$SERVICE'..."
        systemctl stop "$SERVICE" 2>/dev/null || true
        systemctl disable "$SERVICE" 2>/dev/null || true
    fi
    if [[ -f "$UNIT_FILE" ]]; then
        rm -f "$UNIT_FILE"
        systemctl daemon-reload
    fi
    if command -v nft >/dev/null 2>&1; then
        log "Cleaning up ProxSync nftables firewall table (table inet proxsync)..."
        nft delete table inet proxsync 2>/dev/null || true
    fi
    log "Removing installation directories..."
    rm -rf "$INSTALL_DIR" "$CONFIG_DIR" /var/lib/proxsync-agent /var/log/proxsync-agent
    log "Uninstall complete."
    exit 0
fi

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

# ---- PKI & Certificate Management -----------------------------------------
generate_server_cert() {
    log "Generating server certificate for ${AGENT_DNS} (${AGENT_IP})"
    local san_ext="subjectAltName=DNS:${AGENT_DNS},IP:${AGENT_IP},IP:127.0.0.1,IP:::1"
    openssl req -newkey rsa:2048 -sha256 -nodes \
        -keyout "${TLS_DIR}/server.key.new" -out "${TLS_DIR}/server.csr" \
        -subj "/CN=${AGENT_DNS}/O=ProxSync" 2>/dev/null
    openssl x509 -req -in "${TLS_DIR}/server.csr" -days 3650 -sha256 \
        -CA "${TLS_DIR}/ca.crt" -CAkey "${TLS_DIR}/ca.key" -CAcreateserial \
        -out "${TLS_DIR}/server.crt.new" \
        -extfile <(printf '%s\nextendedKeyUsage=serverAuth\n' "$san_ext") 2>/dev/null
    rm -f "${TLS_DIR}/server.csr"

    mv "${TLS_DIR}/server.key.new" "${TLS_DIR}/server.key"
    mv "${TLS_DIR}/server.crt.new" "${TLS_DIR}/server.crt"
    chmod 0640 "${TLS_DIR}/server.key"
    chmod 0644 "${TLS_DIR}/server.crt"
}

generate_all_pki() {
    log "Generating certificate authority"
    openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
        -keyout "${TLS_DIR}/ca.key" -out "${TLS_DIR}/ca.crt" \
        -subj "/CN=ProxSync Agent CA/O=ProxSync" 2>/dev/null

    generate_server_cert

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

# Determine PKI state
if [[ ! -f "${TLS_DIR}/ca.crt" ]] || [[ ! -f "${TLS_DIR}/server.crt" ]] || [[ $REGENERATE_ALL_SECRETS -eq 1 ]]; then
    generate_all_pki
else
    # Check if existing server cert SAN contains current AGENT_IP
    SERVER_SAN_OK=1
    if ! openssl x509 -in "${TLS_DIR}/server.crt" -noout -text 2>/dev/null | grep -q "IP Address:${AGENT_IP}"; then
        SERVER_SAN_OK=0
    fi

    if [[ $ROTATE_SERVER_CERT -eq 1 ]] || [[ $SERVER_SAN_OK -eq 0 ]]; then
        log "Agent IP or DNS updated (or --rotate-server-cert requested). Rotating server certificate using existing CA..."
        generate_server_cert
    else
        log "Reusing existing PKI certificates in ${TLS_DIR}"
    fi
fi

# ---- Configuration & Rerun Preservation ------------------------------------
DASHBOARD_CIDR="$(get_cidr "$DASHBOARD_IP")"

# Preserve existing secret if available
HMAC_SECRET=""
if [[ -f "$ENV_FILE" ]] && [[ $REGENERATE_ALL_SECRETS -eq 0 ]]; then
    HMAC_SECRET="$(grep -E '^PROXSYNC_AGENT_HMAC_SECRET=' "$ENV_FILE" | cut -d= -f2- || true)"
fi

if [[ -z "$HMAC_SECRET" ]]; then
    HMAC_SECRET="$(openssl rand -hex 32)"
fi

log "Writing agent configuration to ${ENV_FILE}"
cat > "$ENV_FILE" <<EOF
# Generated by install-agent.sh on $(date -Is)
PROXSYNC_AGENT_BIND_HOST=${AGENT_IP}
PROXSYNC_AGENT_BIND_PORT=${PORT}
PROXSYNC_AGENT_LOG_LEVEL=INFO
PROXSYNC_AGENT_LOG_JSON=true

PROXSYNC_AGENT_TLS_CERTFILE=${TLS_DIR}/server.crt
PROXSYNC_AGENT_TLS_KEYFILE=${TLS_DIR}/server.key
PROXSYNC_AGENT_TLS_CLIENT_CA=${TLS_DIR}/ca.crt
PROXSYNC_AGENT_ALLOWED_CLIENT_NETWORKS=${DASHBOARD_CIDR}

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
chmod 0640 "$ENV_FILE"

# ---- Host Firewall (nftables) ----------------------------------------------
apply_firewall() {
    log "Configuring nftables host firewall (table inet proxsync)..."
    if ! command -v nft >/dev/null 2>&1; then
        die "nftables ('nft' command) is not installed on host. Install nftables or pass --skip-firewall."
    fi

    local ip_family="ip"
    if is_ipv6 "$DASHBOARD_IP"; then
        ip_family="ip6"
    fi

    local tmp_rules
    tmp_rules="$(mktemp)"
    cat > "$tmp_rules" <<EOF
table inet proxsync {
    chain agent_input {
        type filter hook input priority filter; policy accept;
        iif "lo" tcp dport ${PORT} accept
        ${ip_family} saddr ${DASHBOARD_IP} tcp dport ${PORT} accept
        tcp dport ${PORT} drop
    }
}
EOF
    if nft -f "$tmp_rules"; then
        log "  [PASS] nftables rule applied cleanly. Allowed dashboard ${DASHBOARD_IP} to port ${PORT}."
        rm -f "$tmp_rules"
    else
        rm -f "$tmp_rules"
        die "Failed to apply nftables rules."
    fi
}

if [[ $CONFIGURE_FIREWALL -eq 1 ]]; then
    apply_firewall
else
    log "Skipping host firewall configuration (--skip-firewall specified)."
fi

# ---- systemd service -------------------------------------------------------
log "Installing systemd unit"
sed -e "s|MemoryHigh=.*|MemoryHigh=${MEMORY_HIGH}|" \
    -e "s|MemoryMax=.*|MemoryMax=${MEMORY_MAX}|" \
    "${SCRIPT_DIR}/proxsync-agent.service" > "$UNIT_FILE"
chmod 0644 "$UNIT_FILE"

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

sleep 2

# ---- Post-Install Health Checks & Verification -----------------------------
log "Running post-install health checks..."
WARN_COUNT=0
FATAL_COUNT=0
FAILED_CHECKS=()

record_fatal() {
    local msg="$1"
    FATAL_COUNT=$((FATAL_COUNT + 1))
    FAILED_CHECKS+=("$msg")
    warn "  [FAIL] $msg"
}

record_warn() {
    local msg="$1"
    WARN_COUNT=$((WARN_COUNT + 1))
    warn "  [WARN] $msg"
}

record_pass() {
    local msg="$1"
    log "  [PASS] $msg"
}

# Check 1: Systemd Service Active
if systemctl is-active --quiet "$SERVICE"; then
    record_pass "Service '$SERVICE' is active and running."
else
    record_fatal "Service '$SERVICE' failed to start."
    journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
fi

# Check 2: Mandatory mTLS Certificate & Key Files Exist
for f in "${TLS_DIR}/ca.crt" "${TLS_DIR}/server.crt" "${TLS_DIR}/server.key" "${TLS_DIR}/dashboard.crt" "${TLS_DIR}/dashboard.key"; do
    if [[ ! -r "$f" ]]; then
        record_fatal "Required mTLS file '$f' is missing or unreadable."
    fi
done

# Check 3: Certificate SAN Validation
if openssl x509 -in "${TLS_DIR}/server.crt" -noout -text 2>/dev/null | grep -q "IP Address:${AGENT_IP}"; then
    record_pass "Certificate SAN contains agent IP ${AGENT_IP}."
else
    record_fatal "Certificate SAN is missing agent IP ${AGENT_IP}."
fi

# Check 4: Agent Config File Readability
if [[ -r "$ENV_FILE" ]]; then
    record_pass "Agent environment configuration file is readable."
else
    record_fatal "Configuration file '$ENV_FILE' is unreadable."
fi

# Check 5: Firewall Verification
if [[ $CONFIGURE_FIREWALL -eq 1 ]]; then
    if nft list table inet proxsync >/dev/null 2>&1; then
        record_pass "nftables table 'inet proxsync' is active."
    else
        record_fatal "Firewall table 'inet proxsync' is not active."
    fi
fi

# Check 6: Rclone Binary Presence
if command -v rclone >/dev/null 2>&1; then
    record_pass "rclone binary is installed."
else
    if [[ $REQUIRE_RCLONE -eq 1 ]]; then
        record_fatal "rclone binary is missing (--require-rclone specified)."
    else
        record_warn "rclone binary is not installed on host. Offsite Google Drive syncs require rclone."
    fi
fi

# Check 7: Outbound DNS & HTTPS for Google Drive
if [[ $SKIP_OUTBOUND_CHECK -eq 0 ]]; then
    if python3 -c "import socket; socket.getaddrinfo('drive.googleapis.com', 443)" >/dev/null 2>&1; then
        record_pass "DNS resolution for drive.googleapis.com succeeded."
    else
        if [[ $REQUIRE_DRIVE_CONNECTIVITY -eq 1 ]]; then
            record_fatal "DNS resolution for drive.googleapis.com failed."
        else
            record_warn "DNS resolution for drive.googleapis.com failed. Check host DNS settings."
        fi
    fi

    if python3 -c "import socket; socket.create_connection(('drive.googleapis.com', 443), timeout=3).close()" >/dev/null 2>&1; then
        record_pass "Outbound TCP 443 to drive.googleapis.com succeeded."
    else
        if [[ $REQUIRE_DRIVE_CONNECTIVITY -eq 1 ]]; then
            record_fatal "Outbound TCP 443 to drive.googleapis.com failed/timed out."
        else
            record_warn "Outbound TCP 443 to drive.googleapis.com failed/timed out. Check host egress firewall."
        fi
    fi
fi

# Check 8: Local Agent /health Endpoint
URL_HOST="$(format_url_host "$AGENT_IP")"
HEALTH_URL="https://${URL_HOST}:${PORT}/health"

if python3 -c '
import urllib.request, ssl, sys
ctx = ssl.create_default_context(cafile="'"${TLS_DIR}/ca.crt"'")
ctx.load_cert_chain(certfile="'"${TLS_DIR}/dashboard.crt"'", keyfile="'"${TLS_DIR}/dashboard.key"'")
req = urllib.request.Request("'"${HEALTH_URL}"'")
try:
    with urllib.request.urlopen(req, context=ctx, timeout=5) as res:
        sys.exit(0 if res.status == 200 else 1)
except Exception:
    sys.exit(1)
' >/dev/null 2>&1; then
    record_pass "Agent health endpoint (${HEALTH_URL}) answered HTTP 200 OK."
else
    record_fatal "Local health check query to ${HEALTH_URL} did not return HTTP 200 OK."
fi

# ---- Final Output Report ----------------------------------------------------
echo ""
log "========================================================================="
if [[ $FATAL_COUNT -eq 0 ]]; then
    log "Installation status: SUCCESS"
    log "Warnings: ${WARN_COUNT}"
    log "Failed checks: 0"
    log "========================================================================="
    echo ""
    log "Agent successfully installed and running at https://${URL_HOST}:${PORT}"
    echo ""
    echo "Configure the ProxSync dashboard with:"
    echo ""
    echo "  Agent URL          https://${URL_HOST}:${PORT}"
    echo "  API key id         proxsync-dashboard"
    echo "  HMAC secret        [SECURELY STORED IN ${ENV_FILE}]"
    echo ""
    echo "To view the HMAC secret for dashboard configuration, run:"
    echo "  grep PROXSYNC_AGENT_HMAC_SECRET ${ENV_FILE}"
    echo ""
    echo "Copy these three client authentication files to your ProxSync dashboard LXC:"
    echo ""
    echo "  ${TLS_DIR}/ca.crt          -> /etc/proxsync/agent-ca.crt"
    echo "  ${TLS_DIR}/dashboard.crt   -> /etc/proxsync/agent-client.crt"
    echo "  ${TLS_DIR}/dashboard.key   -> /etc/proxsync/agent-client.key   (mode 0640)"
    echo ""
    echo "Example transfer command from the dashboard LXC:"
    echo "  scp root@${URL_HOST}:${TLS_DIR}/{ca.crt,dashboard.crt,dashboard.key} /etc/proxsync/"
    echo ""
    echo "Verification:  curl --cacert ${TLS_DIR}/ca.crt https://${URL_HOST}:${PORT}/health"
    echo "Logs:          journalctl -u ${SERVICE} -f"
    echo ""
    exit 0
else
    warn "Installation status: FAILED"
    warn "Warnings: ${WARN_COUNT}"
    warn "Failed checks: ${FATAL_COUNT}"
    for failed in "${FAILED_CHECKS[@]}"; do
        warn "  - ${failed}"
    done
    warn "========================================================================="
    die "Agent installation failed post-install health checks."
fi
