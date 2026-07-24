#!/usr/bin/env bash
#
# Transactional installer for the ProxSync Backup Agent on a Proxmox VE host.
#
set -Eeuo pipefail

INSTALL_DIR=/opt/proxsync-agent
CONFIG_DIR=/etc/proxsync-agent
TLS_DIR="${CONFIG_DIR}/tls"
ENV_FILE="${CONFIG_DIR}/agent.env"
UNIT_FILE=/etc/systemd/system/proxsync-agent.service
SERVICE=proxsync-agent
FIREWALL_FILE=/etc/nftables.d/proxsync.nft
FIREWALL_LOADER=/usr/libexec/proxsync-firewall-apply
FIREWALL_UNIT=/etc/systemd/system/proxsync-firewall.service
FIREWALL_SERVICE=proxsync-firewall.service

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

WORK_DIR=""
BACKUP_DIR=""
TRANSACTION_ACTIVE=0
TRANSACTION_COMMITTED=0
OLD_SERVICE_ACTIVE=0
OLD_SERVICE_ENABLED=0
OLD_FIREWALL_ENABLED=0
OLD_FIREWALL_RUNTIME=0
OLD_FIREWALL_EXISTS=0
FIREWALL_MODE_SET=""

log()  { printf '\033[0;36m[proxsync]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[proxsync]\033[0m %s\n' "$*" >&2; }
die()  {
    printf '\033[0;31m[proxsync]\033[0m Error: %s\n' "$*" >&2
    if [[ "$TRANSACTION_ACTIVE" -eq 1 && "$TRANSACTION_COMMITTED" -eq 0 ]]; then
        rollback_on_error 1
    fi
    exit 1
}

init_defaults() {
    AGENT_IP=""
    DASHBOARD_IP=""
    AGENT_DNS=""
    DUMP_ROOT=/mnt/backup-hdd/dump
    TEMP_DIR=/mnt/backup-hdd/tmp
    BACKUP_STORAGE=backup-hdd
    PORT=8765
    MEMORY_HIGH=1G
    MEMORY_MAX=2G
    RCLONE_CONFIG=/var/lib/proxsync-agent/rclone.conf
    RCLONE_CONFIG_SOURCE=""
    RCLONE_CHECK_CONFIG=""
    RCLONE_REMOTE=""
    FORCE_IP=0
    FIREWALL_MODE=managed
    REPAIR_PKI=0
    ROTATE_SERVER_CERT=0
    REGENERATE_ALL_SECRETS=0
    REQUIRE_RCLONE=0
    REQUIRE_DRIVE_CONNECTIVITY=0
    REQUIRE_DUMP_MOUNT=0
    UNINSTALL_MODE=0
    FIREWALL_MODE_SET=""
    FORCE_REMOVE_FOREIGN_FIREWALL_TABLE=0
    EXPORT_DASHBOARD_BUNDLE=""
}

usage() {
    cat <<'EOF'
ProxSync Host Agent Installer

Usage:
  ./install-agent.sh --dashboard-ip <IP> [OPTIONS]

Required:
  --dashboard-ip <IP>          Dashboard LXC IPv4 or IPv6 address.

Agent:
  --agent-ip <IP>              Host interface address (auto-detected if unambiguous).
  --agent-port, --port <PORT>  Listening port (default: 8765).
  --agent-dns <NAME>           DNS SAN (default: host FQDN).
  --dump-root <PATH>           Dump directory (default: /mnt/backup-hdd/dump).
  --backup-path <PATH>         Alias for --dump-root.
  --temp-dir <PATH>            Temporary backup directory.
  --backup-storage <ID>        Proxmox storage ID (default: backup-hdd).
  --memory-high <LIMIT>        systemd MemoryHigh (default: 1G).
  --memory-max <LIMIT>         systemd MemoryMax (default: 2G).
  --force-ip                   Permit an agent IP not assigned to this host.

Firewall:
  --configure-firewall         Create/update persistent managed nftables rules (default).
  --skip-firewall              Leave all existing firewall state unchanged.
  --remove-firewall            Remove only ProxSync runtime and persistent firewall state.
  --force-remove-foreign-firewall-table Override safety check and remove table inet proxsync even if not owned.
  --export-dashboard-bundle <DIR> Securely export dashboard credentials to a root-owned directory.

PKI:
  --repair-pki                 Repair missing/invalid leaf certificate pairs with the current CA.
  --rotate-server-cert         Rotate only the server certificate/key with the current CA.
  --regenerate-all-secrets     Replace CA, leaf certificates, keys, and HMAC secret.
  --regenerate-secrets         Compatibility alias for --regenerate-all-secrets.

rclone:
  --rclone-config <PATH>       Config to import into the agent's protected state directory.
  --rclone-remote <NAME>       Remote to allow and optionally verify.
  --require-rclone             Require a usable binary and readable config.
  --require-drive-connectivity Require a successful authenticated operation on the remote.

Maintenance:
  --uninstall                  Remove the agent and its managed firewall state.
  -h, --help                   Show this help.
EOF
}

require_option_value() {
    local option="$1"
    local value="${2-}"
    if [[ -z "$value" || "$value" == --* ]]; then
        die "${option} requires a value."
    fi
}

set_firewall_mode() {
    local requested="$1"
    local option="$2"
    if [[ -n "$FIREWALL_MODE_SET" && "$FIREWALL_MODE" != "$requested" ]]; then
        die "${option} conflicts with ${FIREWALL_MODE_SET}."
    fi
    FIREWALL_MODE="$requested"
    FIREWALL_MODE_SET="$option"
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --agent-ip)
                require_option_value "$1" "${2-}" || return
                AGENT_IP="$2"; shift 2 ;;
            --dashboard-ip)
                require_option_value "$1" "${2-}" || return
                DASHBOARD_IP="$2"; shift 2 ;;
            --agent-port|--port)
                require_option_value "$1" "${2-}" || return
                PORT="$2"; shift 2 ;;
            --agent-dns)
                require_option_value "$1" "${2-}" || return
                AGENT_DNS="$2"; shift 2 ;;
            --dump-root|--backup-path)
                require_option_value "$1" "${2-}" || return
                DUMP_ROOT="$2"; shift 2 ;;
            --temp-dir)
                require_option_value "$1" "${2-}" || return
                TEMP_DIR="$2"; shift 2 ;;
            --backup-storage)
                require_option_value "$1" "${2-}" || return
                BACKUP_STORAGE="$2"; shift 2 ;;
            --memory-high)
                require_option_value "$1" "${2-}" || return
                MEMORY_HIGH="$2"; shift 2 ;;
            --memory-max)
                require_option_value "$1" "${2-}" || return
                MEMORY_MAX="$2"; shift 2 ;;
            --rclone-config)
                require_option_value "$1" "${2-}" || return
                RCLONE_CONFIG_SOURCE="$2"; shift 2 ;;
            --rclone-remote)
                require_option_value "$1" "${2-}" || return
                RCLONE_REMOTE="$2"; shift 2 ;;
            --force-ip) FORCE_IP=1; shift ;;
            --configure-firewall) set_firewall_mode managed "$1" || return; shift ;;
            --skip-firewall) set_firewall_mode unchanged "$1" || return; shift ;;
            --remove-firewall) set_firewall_mode removed "$1" || return; shift ;;
            --repair-pki) REPAIR_PKI=1; shift ;;
            --rotate-server-cert) ROTATE_SERVER_CERT=1; shift ;;
            --regenerate-all-secrets|--regenerate-secrets)
                REGENERATE_ALL_SECRETS=1; shift ;;
            --require-rclone) REQUIRE_RCLONE=1; shift ;;
            --require-drive-connectivity)
                REQUIRE_DRIVE_CONNECTIVITY=1
                REQUIRE_RCLONE=1
                shift
                ;;
            --require-dump-mount) REQUIRE_DUMP_MOUNT=1; shift ;;
            --force-remove-foreign-firewall-table) FORCE_REMOVE_FOREIGN_FIREWALL_TABLE=1; shift ;;
            --export-dashboard-bundle)
                require_option_value "$1" "${2-}" || return
                EXPORT_DASHBOARD_BUNDLE="$2"; shift 2 ;;
            --uninstall) UNINSTALL_MODE=1; shift ;;
            -h|--help) usage; return 2 ;;
            *) die "Unknown option: $1 (try --help)." ;;
        esac
    done
}

contains_control_character() {
    local value="$1"
    [[ "$value" =~ [[:cntrl:]] ]]
}

is_valid_ip() {
    local ip="$1"
    ! contains_control_character "$ip" &&
        python3 - "$ip" <<'PY' 2>/dev/null
import ipaddress
import sys
try:
    ipaddress.ip_address(sys.argv[1])
except ValueError:
    raise SystemExit(1)
PY
}

normalize_ip() {
    python3 -c 'import ipaddress,sys; print(ipaddress.ip_address(sys.argv[1]))' "$1"
}

is_ipv6() {
    local ip="$1"
    python3 -c 'import ipaddress,sys; raise SystemExit(ipaddress.ip_address(sys.argv[1]).version != 6)' \
        "$ip" 2>/dev/null
}

is_valid_dns() {
    local name="$1"
    ! contains_control_character "$name" &&
        python3 - "$name" <<'PY' 2>/dev/null
import re
import sys
name = sys.argv[1]
if name.endswith("."):
    name = name[:-1]
valid = (
    1 <= len(name) <= 253
    and all(
        1 <= len(label) <= 63
        and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
        for label in name.split(".")
    )
)
raise SystemExit(0 if valid else 1)
PY
}

is_valid_port() {
    local port="$1"
    [[ "$port" =~ ^[0-9]+$ ]] && ((10#$port >= 1 && 10#$port <= 65535))
}

is_valid_memory_limit() {
    local value="$1"
    [[ "$value" == "infinity" || "$value" =~ ^[0-9]+([KMGTPE])?$ ]]
}

is_valid_identifier() {
    local value="$1"
    [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]
}

canonicalize_path() {
    local option="$1"
    local path="$2"
    local canonical=""
    [[ "$path" == /* ]] || die "${option} must be an absolute path."
    contains_control_character "$path" && die "${option} must not contain control characters."
    if [[ -e "$path" || -L "$path" ]]; then
        [[ ! -L "$path" ]] || die "${option} must not be a symbolic link."
        canonical="$(readlink -f -- "$path")"
    else
        canonical="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$path")"
    fi
    printf '%s\n' "$canonical"
}

format_url_host() {
    local host="$1"
    if is_ipv6 "$host"; then printf '[%s]' "$host"; else printf '%s' "$host"; fi
}

get_cidr() {
    local ip="$1"
    if is_ipv6 "$ip"; then printf '%s/128' "$ip"; else printf '%s/32' "$ip"; fi
}

get_host_ips() {
    python3 <<'PY'
import ipaddress
import subprocess

addresses = []
try:
    output = subprocess.check_output(
        ["ip", "-o", "addr", "show", "up", "scope", "global"],
        text=True,
        stderr=subprocess.DEVNULL,
    )
    candidates = [line.split()[3].split("/", 1)[0] for line in output.splitlines()]
except (OSError, subprocess.CalledProcessError, IndexError):
    try:
        candidates = subprocess.check_output(
            ["hostname", "-I"], text=True, stderr=subprocess.DEVNULL
        ).split()
    except (OSError, subprocess.CalledProcessError):
        candidates = []
for candidate in candidates:
    try:
        address = ipaddress.ip_address(candidate.split("%", 1)[0])
    except ValueError:
        continue
    if not address.is_loopback and str(address) not in addresses:
        addresses.append(str(address))
print("\n".join(addresses))
PY
}

select_agent_ip() {
    local requested="$1"
    local force="$2"
    shift 2
    local host_ips=("$@")
    local candidate
    if [[ -n "$requested" ]]; then
        is_valid_ip "$requested" || die "--agent-ip must be a valid single IPv4 or IPv6 address."
        requested="$(normalize_ip "$requested")"
        if [[ "$force" -eq 0 ]]; then
            for candidate in "${host_ips[@]}"; do
                [[ "$candidate" == "$requested" ]] && {
                    printf '%s\n' "$requested"
                    return 0
                }
            done
            die "Agent IP '${requested}' is not assigned to this host; use --force-ip only when intentional."
        fi
        printf '%s\n' "$requested"
    elif [[ ${#host_ips[@]} -eq 1 ]]; then
        printf '%s\n' "${host_ips[0]}"
    elif [[ ${#host_ips[@]} -gt 1 ]]; then
        warn "Multiple host addresses found: ${host_ips[*]}"
        die "Multiple candidate IPs found; pass --agent-ip explicitly."
    else
        die "No active non-loopback address found; pass --agent-ip explicitly."
    fi
}

desired_sans() {
    local dns="$1"
    local ip="$2"
    python3 - "$dns" "$ip" <<'PY'
import ipaddress
import sys
values = {
    "DNS:" + sys.argv[1].rstrip(".").lower(),
    "IP:" + str(ipaddress.ip_address(sys.argv[2])),
    "IP:127.0.0.1",
    "IP:::1",
}
print("\n".join(sorted(values)))
PY
}

certificate_sans() {
    local certificate="$1"
    local output
    output="$(openssl x509 -in "$certificate" -noout -ext subjectAltName 2>/dev/null)" || return 1
    python3 - "$output" <<'PY'
import ipaddress
import re
import sys
values = []
for item in re.split(r",\s*", " ".join(sys.argv[1].splitlines()[1:]).strip()):
    item = item.strip()
    if item.startswith("DNS:"):
        values.append("DNS:" + item[4:].rstrip(".").lower())
    elif item.startswith("IP Address:"):
        values.append("IP:" + str(ipaddress.ip_address(item[11:])))
    elif item:
        values.append("OTHER:" + item)
print("\n".join(sorted(values)))
PY
}

certificate_has_desired_sans() {
    local certificate="$1"
    local dns="$2"
    local ip="$3"
    [[ "$(certificate_sans "$certificate")" == "$(desired_sans "$dns" "$ip")" ]]
}

valid_certificate() {
    openssl x509 -in "$1" -noout -checkend 0 >/dev/null 2>&1
}

valid_private_key() {
    openssl pkey -in "$1" -noout >/dev/null 2>&1
}

certificate_key_match() {
    local certificate="$1"
    local key="$2"
    local cert_digest key_digest
    cert_digest="$(
        openssl x509 -in "$certificate" -pubkey -noout 2>/dev/null |
            openssl pkey -pubin -outform DER 2>/dev/null |
            sha256sum | awk '{print $1}'
    )" || return 1
    key_digest="$(
        openssl pkey -in "$key" -pubout -outform DER 2>/dev/null |
            sha256sum | awk '{print $1}'
    )" || return 1
    [[ -n "$cert_digest" && "$cert_digest" == "$key_digest" ]]
}

certificate_signed_by_ca() {
    openssl verify -CAfile "$1" "$2" >/dev/null 2>&1
}

leaf_pair_valid() {
    local ca="$1"
    local certificate="$2"
    local key="$3"
    [[ -f "$certificate" && -f "$key" ]] &&
        valid_certificate "$certificate" &&
        valid_private_key "$key" &&
        certificate_key_match "$certificate" "$key" &&
        certificate_signed_by_ca "$ca" "$certificate"
}

generate_ca() {
    local tls_dir="$1"
    openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
        -keyout "${tls_dir}/ca.key" -out "${tls_dir}/ca.crt" \
        -subj "/CN=ProxSync Agent CA/O=ProxSync" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" >/dev/null 2>&1
    chmod 0640 "${tls_dir}/ca.key"
    chmod 0644 "${tls_dir}/ca.crt"
}

generate_server_certificate() {
    local tls_dir="$1"
    local dns="$2"
    local ip="$3"
    local san
    san="DNS:${dns},IP:${ip},IP:127.0.0.1,IP:::1"
    openssl req -newkey rsa:2048 -sha256 -nodes \
        -keyout "${tls_dir}/server.key.new" -out "${tls_dir}/server.csr" \
        -subj "/CN=${dns}/O=ProxSync" >/dev/null 2>&1
    openssl x509 -req -in "${tls_dir}/server.csr" -days 3650 -sha256 \
        -CA "${tls_dir}/ca.crt" -CAkey "${tls_dir}/ca.key" -CAcreateserial \
        -out "${tls_dir}/server.crt.new" \
        -extfile <(printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\n' "$san") \
        >/dev/null 2>&1
    mv "${tls_dir}/server.key.new" "${tls_dir}/server.key"
    mv "${tls_dir}/server.crt.new" "${tls_dir}/server.crt"
    rm -f "${tls_dir}/server.csr"
    chmod 0640 "${tls_dir}/server.key"
    chmod 0644 "${tls_dir}/server.crt"
}

generate_dashboard_certificate() {
    local tls_dir="$1"
    openssl req -newkey rsa:2048 -sha256 -nodes \
        -keyout "${tls_dir}/dashboard.key.new" -out "${tls_dir}/dashboard.csr" \
        -subj "/CN=proxsync-dashboard/O=ProxSync" >/dev/null 2>&1
    openssl x509 -req -in "${tls_dir}/dashboard.csr" -days 3650 -sha256 \
        -CA "${tls_dir}/ca.crt" -CAkey "${tls_dir}/ca.key" -CAcreateserial \
        -out "${tls_dir}/dashboard.crt.new" \
        -extfile <(printf 'extendedKeyUsage=clientAuth\n') >/dev/null 2>&1
    mv "${tls_dir}/dashboard.key.new" "${tls_dir}/dashboard.key"
    mv "${tls_dir}/dashboard.crt.new" "${tls_dir}/dashboard.crt"
    rm -f "${tls_dir}/dashboard.csr"
    chmod 0640 "${tls_dir}/dashboard.key"
    chmod 0644 "${tls_dir}/dashboard.crt"
}

prepare_pki() {
    local tls_dir="$1"
    local dns="$2"
    local ip="$3"
    local regenerate="$4"
    local repair="$5"
    local rotate="$6"
    local ca_cert="${tls_dir}/ca.crt"
    local ca_key="${tls_dir}/ca.key"

    if [[ "$regenerate" -eq 1 ]]; then
        warn "DANGER: --regenerate-all-secrets replaces trust and authentication material."
        rm -f "${tls_dir}"/*
        generate_ca "$tls_dir"
        generate_server_certificate "$tls_dir" "$dns" "$ip"
        generate_dashboard_certificate "$tls_dir"
        return
    fi
    [[ "$repair" -eq 0 ]] || log "Inspecting and repairing partial PKI state."

    if [[ ! -e "$ca_cert" && ! -e "$ca_key" ]]; then
        generate_ca "$tls_dir"
    elif [[ ! -f "$ca_cert" || ! -f "$ca_key" ]]; then
        if [[ -f "$ca_cert" ]] && valid_certificate "$ca_cert"; then
            if leaf_pair_valid "$ca_cert" "${tls_dir}/server.crt" "${tls_dir}/server.key" &&
                certificate_has_desired_sans "${tls_dir}/server.crt" "$dns" "$ip" &&
                leaf_pair_valid "$ca_cert" "${tls_dir}/dashboard.crt" "${tls_dir}/dashboard.key" &&
                [[ "$rotate" -eq 0 ]]; then
                log "CA key is absent; valid existing leaf certificates can be reused."
                return
            fi
            die "CA certificate exists but CA key is missing; required certificate repair/rotation is impossible. Restore ca.key or use --regenerate-all-secrets."
        fi
        die "Partial CA state is unusable; restore the CA pair or use --regenerate-all-secrets."
    elif ! valid_certificate "$ca_cert" || ! valid_private_key "$ca_key" ||
        ! certificate_key_match "$ca_cert" "$ca_key"; then
        die "CA certificate/key is corrupt or mismatched; restore it or use --regenerate-all-secrets."
    fi

    if [[ "$rotate" -eq 1 ]] ||
        ! leaf_pair_valid "$ca_cert" "${tls_dir}/server.crt" "${tls_dir}/server.key" ||
        ! certificate_has_desired_sans "${tls_dir}/server.crt" "$dns" "$ip"; then
        log "Generating server certificate with the complete desired SAN set."
        generate_server_certificate "$tls_dir" "$dns" "$ip"
    fi

    if ! leaf_pair_valid "$ca_cert" "${tls_dir}/dashboard.crt" "${tls_dir}/dashboard.key"; then
        log "Repairing dashboard client certificate pair with the existing CA."
        generate_dashboard_certificate "$tls_dir"
    fi
}

env_value() {
    local value="$1"
    contains_control_character "$value" && die "Environment values must not contain control characters."
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    printf '"%s"' "$value"
}

render_environment() {
    local destination="$1"
    local hmac_secret="$2"
    local dashboard_cidr="$3"
    {
        printf '# Generated transactionally by install-agent.sh\n'
        printf 'PROXSYNC_AGENT_BIND_HOST=%s\n' "$(env_value "$AGENT_IP")"
        printf 'PROXSYNC_AGENT_BIND_PORT=%s\n' "$(env_value "$PORT")"
        printf 'PROXSYNC_AGENT_LOG_LEVEL=INFO\nPROXSYNC_AGENT_LOG_JSON=true\n\n'
        printf 'PROXSYNC_AGENT_TLS_CERTFILE=%s\n' "$(env_value "${TLS_DIR}/server.crt")"
        printf 'PROXSYNC_AGENT_TLS_KEYFILE=%s\n' "$(env_value "${TLS_DIR}/server.key")"
        printf 'PROXSYNC_AGENT_TLS_CLIENT_CA=%s\n' "$(env_value "${TLS_DIR}/ca.crt")"
        printf 'PROXSYNC_AGENT_ALLOWED_CLIENT_NETWORKS=%s\n\n' "$(env_value "$dashboard_cidr")"
        printf 'PROXSYNC_AGENT_API_KEY_ID=proxsync-dashboard\n'
        printf 'PROXSYNC_AGENT_HMAC_SECRET=%s\n' "$(env_value "$hmac_secret")"
        printf 'PROXSYNC_AGENT_SIGNATURE_WINDOW_SECONDS=60\n\n'
        printf 'PROXSYNC_AGENT_DUMP_ROOT=%s\n' "$(env_value "$DUMP_ROOT")"
        printf 'PROXSYNC_AGENT_TEMP_DIR=%s\n' "$(env_value "$TEMP_DIR")"
        printf 'PROXSYNC_AGENT_STATE_DIR=/var/lib/proxsync-agent\n'
        printf 'PROXSYNC_AGENT_LOG_DIR=/var/log/proxsync-agent\n\n'
        printf 'PROXSYNC_AGENT_ALLOWED_VMIDS=\n'
        printf 'PROXSYNC_AGENT_ALLOWED_BACKUP_STORAGES=%s\n' "$(env_value "$BACKUP_STORAGE")"
        printf 'PROXSYNC_AGENT_ALLOWED_RESTORE_STORAGES=\n'
        printf 'PROXSYNC_AGENT_VERIFY_STORAGE_WITH_PVESM=true\n\n'
        printf 'PROXSYNC_AGENT_MAX_CONCURRENT_BACKUPS=1\n'
        printf 'PROXSYNC_AGENT_MAX_CONCURRENT_RESTORES=1\n'
        printf 'PROXSYNC_AGENT_CHECKSUM_AFTER_BACKUP=true\n\n'
        printf 'PROXSYNC_AGENT_SYNC_ENABLED=true\n'
        printf 'PROXSYNC_AGENT_RCLONE_BIN=/usr/bin/rclone\n'
        printf 'PROXSYNC_AGENT_RCLONE_CONFIG=%s\n' "$(env_value "$RCLONE_CONFIG")"
        printf 'PROXSYNC_AGENT_ALLOWED_REMOTES=%s\n' "$(env_value "$RCLONE_REMOTE")"
    } >"$destination"
    chmod 0640 "$destination"
}

render_firewall() {
    local destination="$1"
    local dashboard_ip="$2"
    local port="$3"
    local family=ip
    is_ipv6 "$dashboard_ip" && family=ip6
    {
        printf 'table inet proxsync {\n'
        printf '    comment "managed-by=proxsync;schema=1"\n'
        printf '    chain agent_input {\n'
        printf '        type filter hook input priority filter; policy accept;\n'
        printf '        iifname "lo" tcp dport %s counter accept\n' "$port"
        printf '        %s saddr %s tcp dport %s counter accept\n' "$family" "$dashboard_ip" "$port"
        printf '        tcp dport %s counter drop\n' "$port"
        printf '    }\n}\n'
    } >"$destination"
}

render_firewall_loader() {
    local destination="$1"
    cat >"$destination" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
RULES=/etc/nftables.d/proxsync.nft
tmp="$(mktemp /run/proxsync-firewall.XXXXXX)"
cleanup() { rm -f "$tmp"; }
trap cleanup EXIT
if nft list table inet proxsync >/dev/null 2>&1; then
    printf 'delete table inet proxsync\n' >"$tmp"
fi
cat "$RULES" >>"$tmp"
nft --check --file "$tmp"
nft --file "$tmp"
EOF
    chmod 0755 "$destination"
}

render_firewall_verifier() {
    local destination="$1"
    cat >"$destination" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
MARKER=/etc/proxsync-agent/firewall-managed.json
if [[ ! -f "$MARKER" ]]; then
    printf 'Firewall marker %s not found.\n' "$MARKER" >&2
    exit 1
fi
if [[ "$(stat -c "%U:%a" "$MARKER" 2>/dev/null)" != "root:600" ]]; then
    printf 'Firewall marker %s has incorrect permissions.\n' "$MARKER" >&2
    exit 1
fi
RULES="$(nft list table inet proxsync 2>/dev/null)" || {
    printf 'Table inet proxsync is not loaded.\n' >&2
    exit 1
}
grep -q 'comment "managed-by=proxsync;schema=1"' <<<"$RULES" || {
    printf 'Table inet proxsync is missing managed-by comment.\n' >&2
    exit 1
}
grep -q 'chain agent_input' <<<"$RULES" || {
    printf 'Table inet proxsync is missing agent_input chain.\n' >&2
    exit 1
}
grep -Eq 'iif(name)? "lo" tcp dport .* accept' <<<"$RULES" || {
    printf 'Table inet proxsync is missing localhost allow rule.\n' >&2
    exit 1
}
grep -Eq 'saddr .* tcp dport .* accept' <<<"$RULES" || {
    printf 'Table inet proxsync is missing dashboard allow rule.\n' >&2
    exit 1
}
grep -Eq 'tcp dport .* drop' <<<"$RULES" || {
    printf 'Table inet proxsync is missing drop rule.\n' >&2
    exit 1
}
exit 0
EOF
    chmod 0755 "$destination"
}

render_firewall_unit() {
    local destination="$1"
    cat >"$destination" <<'EOF'
[Unit]
Description=ProxSync managed nftables firewall
Documentation=https://github.com/stayfr0sty141/proxsync/blob/main/docs/INSTALL.md
DefaultDependencies=no
RequiresMountsFor=/etc/nftables.d /usr/libexec
Wants=network-pre.target
After=local-fs.target
Before=network-pre.target proxsync-agent.service
ConditionPathExists=/etc/nftables.d/proxsync.nft

[Service]
Type=oneshot
ExecStart=/usr/libexec/proxsync-firewall-apply
ExecReload=/usr/libexec/proxsync-firewall-apply
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    chmod 0644 "$destination"
}

generate_firewall_marker() {
    local destination="$1"
    local rules_file="$2"
    local dashboard_ip="$3"
    local port="$4"
    local hash
    hash="$(sha256sum "$rules_file" | awk '{print $1}')"
    {
        printf '{\n'
        printf '  "schema_version": 1,\n'
        printf '  "family": "inet",\n'
        printf '  "table": "proxsync",\n'
        printf '  "dashboard_ip": "%s",\n' "$dashboard_ip"
        printf '  "agent_port": %s,\n' "$port"
        printf '  "rules_sha256": "%s"\n' "$hash"
        printf '}\n'
    } >"$destination"
}

build_firewall_transaction() {
    local rules="$1"
    local transaction="$2"
    : >"$transaction"
    if nft list table inet proxsync >/dev/null 2>&1; then
        local marker_file="/etc/proxsync-agent/firewall-managed.json"
        if [[ "$FORCE_REMOVE_FOREIGN_FIREWALL_TABLE" -eq 0 ]]; then
            if [[ ! -f "$marker_file" ]]; then
                die "Refusing to modify table inet proxsync because it is not verified as ProxSync-managed (missing marker)."
            fi
            local marker_stat
            marker_stat="$(stat -c "%U:%a" "$marker_file" 2>/dev/null || true)"
            if [[ "$marker_stat" != "root:600" ]]; then
                die "Refusing to modify table inet proxsync because marker file permissions are invalid."
            fi
            if ! nft list table inet proxsync | grep -q 'comment "managed-by=proxsync;schema=1"'; then
                die "Refusing to modify table inet proxsync because it lacks the managed-by comment."
            fi
        fi
        printf 'delete table inet proxsync\n' >>"$transaction"
    fi
    cat "$rules" >>"$transaction"
}

validate_firewall_rules() {
    local rules="$1"
    local transaction="$2"
    build_firewall_transaction "$rules" "$transaction"
    nft --check --file "$transaction"
}

apply_firewall_rules() {
    local rules="$1"
    local transaction
    transaction="$(mktemp "${WORK_DIR}/nft-apply.XXXXXX")"
    validate_firewall_rules "$rules" "$transaction"
    nft --file "$transaction"
}

firewall_rules_valid() {
    local expected_ip="$1"
    local expected_port="$2"
    local rules
    rules="$(nft list table inet proxsync 2>/dev/null)" || return 1
    grep -Fq 'chain agent_input {' <<<"$rules" &&
        grep -Eq 'type filter hook input priority (filter|0); policy accept;' <<<"$rules" &&
        grep -Eq "iif(name)? \"lo\" tcp dport ${expected_port}.* accept" <<<"$rules" &&
        grep -Fq "saddr ${expected_ip} tcp dport ${expected_port}" <<<"$rules" &&
        grep -Eq "tcp dport ${expected_port}.* drop" <<<"$rules" &&
        [[ "$(grep -Ec 'saddr .* tcp dport' <<<"$rules")" -eq 1 ]] &&
        [[ "$(grep -Ec 'tcp dport [0-9]+' <<<"$rules")" -eq 3 ]]
}

atomic_install() {
    local source="$1"
    local destination="$2"
    local mode="$3"
    local temporary
    install -d -m 0755 "$(dirname "$destination")"
    temporary="$(mktemp "$(dirname "$destination")/.proxsync.XXXXXX")"
    install -m "$mode" "$source" "$temporary"
    mv -f "$temporary" "$destination"
}

snapshot_path() {
    local path="$1"
    local label="$2"
    if [[ -e "$path" || -L "$path" ]]; then
        cp -a "$path" "${BACKUP_DIR}/${label}"
        touch "${BACKUP_DIR}/${label}.present"
    fi
}

restore_path() {
    local path="$1"
    local label="$2"
    rm -rf -- "$path"
    if [[ -f "${BACKUP_DIR}/${label}.present" ]]; then
        install -d -m 0755 "$(dirname "$path")"
        cp -a "${BACKUP_DIR}/${label}" "$path"
    fi
}

begin_transaction() {
    BACKUP_DIR="${WORK_DIR}/backup"
    install -d -m 0700 "$BACKUP_DIR"
    if systemctl is-active --quiet "$SERVICE"; then OLD_SERVICE_ACTIVE=1; fi
    if systemctl is-enabled --quiet "$SERVICE"; then OLD_SERVICE_ENABLED=1; fi
    if [[ -f "$FIREWALL_UNIT" ]]; then OLD_FIREWALL_EXISTS=1; fi
    if systemctl is-enabled --quiet "$FIREWALL_SERVICE"; then OLD_FIREWALL_ENABLED=1; fi
    if systemctl is-active --quiet "$FIREWALL_SERVICE"; then OLD_FIREWALL_ACTIVE=1; fi
    if systemctl is-failed --quiet "$FIREWALL_SERVICE"; then OLD_FIREWALL_FAILED=1; fi
    snapshot_path "$INSTALL_DIR" install
    snapshot_path "$CONFIG_DIR" config
    snapshot_path "$UNIT_FILE" agent-unit
    snapshot_path "$FIREWALL_FILE" firewall-file
    snapshot_path "$FIREWALL_LOADER" firewall-loader
    snapshot_path "/usr/libexec/proxsync-verify-firewall" firewall-verifier
    snapshot_path "$FIREWALL_UNIT" firewall-unit
    snapshot_path "/etc/proxsync-agent/firewall-managed.json" firewall-marker
    snapshot_path "/etc/systemd/system/proxsync-agent.service.d" agent-dropin
    snapshot_path "$RCLONE_CONFIG" rclone-config
    if command -v nft >/dev/null 2>&1 &&
        nft list table inet proxsync >"${BACKUP_DIR}/firewall-runtime.nft" 2>/dev/null; then
        OLD_FIREWALL_RUNTIME=1
    fi
    TRANSACTION_ACTIVE=1
}

restore_runtime_firewall() {
    command -v nft >/dev/null 2>&1 || return 0
    nft delete table inet proxsync >/dev/null 2>&1 || true
    if [[ "$OLD_FIREWALL_RUNTIME" -eq 1 ]]; then
        nft --check --file "${BACKUP_DIR}/firewall-runtime.nft"
        nft --file "${BACKUP_DIR}/firewall-runtime.nft"
    fi
}

rollback_on_error() {
    local status="${1:-1}"
    trap - ERR
    set +e
    if [[ "$TRANSACTION_ACTIVE" -eq 1 && "$TRANSACTION_COMMITTED" -eq 0 ]]; then
        warn "Installation failed; restoring the previous ProxSync state."
        systemctl stop "$SERVICE" >/dev/null 2>&1
        restore_path "$INSTALL_DIR" install
        restore_path "$CONFIG_DIR" config
        restore_path "$UNIT_FILE" agent-unit
        restore_path "$FIREWALL_FILE" firewall-file
        restore_path "$FIREWALL_LOADER" firewall-loader
        restore_path "/usr/libexec/proxsync-verify-firewall" firewall-verifier
        restore_path "$FIREWALL_UNIT" firewall-unit
        restore_path "/etc/proxsync-agent/firewall-managed.json" firewall-marker
        restore_path "/etc/systemd/system/proxsync-agent.service.d" agent-dropin
        restore_path "$RCLONE_CONFIG" rclone-config
        restore_runtime_firewall
        systemctl daemon-reload
        if [[ "${OLD_FIREWALL_FAILED:-0}" -eq 0 ]]; then
            systemctl reset-failed "$FIREWALL_SERVICE" >/dev/null 2>&1 || true
        fi
        if [[ "$OLD_FIREWALL_ENABLED" -eq 1 ]]; then
            systemctl enable "$FIREWALL_SERVICE" >/dev/null 2>&1
        elif [[ "${OLD_FIREWALL_EXISTS:-0}" -eq 1 ]]; then
            systemctl disable "$FIREWALL_SERVICE" >/dev/null 2>&1
        else
            rm -f "$FIREWALL_FILE" "$FIREWALL_LOADER" "/usr/libexec/proxsync-verify-firewall" "$FIREWALL_UNIT"
            systemctl daemon-reload
        fi
        if [[ "${OLD_FIREWALL_ACTIVE:-0}" -eq 1 ]]; then
            systemctl start "$FIREWALL_SERVICE" >/dev/null 2>&1 || true
        else
            systemctl stop "$FIREWALL_SERVICE" >/dev/null 2>&1 || true
        fi
        if [[ "${OLD_SERVICE_FAILED:-0}" -eq 0 ]]; then
            systemctl reset-failed "$SERVICE" >/dev/null 2>&1 || true
        fi
        if [[ "$OLD_SERVICE_ENABLED" -eq 1 ]]; then
            systemctl enable "$SERVICE" >/dev/null 2>&1
        else
            systemctl disable "$SERVICE" >/dev/null 2>&1
        fi
        if [[ "$OLD_SERVICE_ACTIVE" -eq 1 ]]; then
            if systemctl restart "$SERVICE" && systemctl is-active --quiet "$SERVICE"; then
                warn "Rollback completed and the previous service is active."
            else
                warn "Rollback restored files, but the previous service could not be verified."
            fi
        fi
    fi
    exit "$status"
}

cleanup() {
    local status=$?
    [[ -z "$WORK_DIR" ]] || rm -rf -- "$WORK_DIR"
    exit "$status"
}

port_is_available() {
    local port="$1"
    local old_port=""
    if [[ -f "$ENV_FILE" ]]; then
        old_port="$(sed -E -n 's/^PROXSYNC_AGENT_BIND_PORT=["'\'']*([^"'\'']*).*/\1/p' "$ENV_FILE" | head -1)"
    fi
    if [[ "$old_port" == "$port" ]] && systemctl is-active --quiet "$SERVICE"; then
        return 0
    fi
    ! ss -H -ltn 2>/dev/null | awk '{print $4}' |
        grep -Eq "(^|\\]|:)${port}$"
}

validate_dependencies() {
    local command
    for command in python3 openssl sha256sum readlink timeout vzdump pvesm ss systemctl systemd-analyze; do
        command -v "$command" >/dev/null 2>&1 || die "${command} is required."
    done
    python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' ||
        die "Python 3.11 or newer is required."
    if [[ "$FIREWALL_MODE" == managed ]]; then
        command -v nft >/dev/null 2>&1 ||
            die "nftables is required for firewall mode '${FIREWALL_MODE}'."
    fi
}

validate_inputs() {
    [[ -n "$DASHBOARD_IP" ]] || die "--dashboard-ip is required."
    is_valid_ip "$DASHBOARD_IP" ||
        die "--dashboard-ip must be a valid single IPv4 or IPv6 address."
    DASHBOARD_IP="$(normalize_ip "$DASHBOARD_IP")"
    is_valid_port "$PORT" || die "--agent-port must be an integer from 1 to 65535."
    is_valid_memory_limit "$MEMORY_HIGH" ||
        die "--memory-high must be a systemd size such as 512M, 1G, or infinity."
    is_valid_memory_limit "$MEMORY_MAX" ||
        die "--memory-max must be a systemd size such as 512M, 1G, or infinity."
    is_valid_identifier "$BACKUP_STORAGE" ||
        die "--backup-storage contains unsupported characters."
    [[ -z "$RCLONE_REMOTE" ]] || is_valid_identifier "$RCLONE_REMOTE" ||
        die "--rclone-remote contains unsupported characters."
    
    DUMP_ROOT="$(canonicalize_path --dump-root "$DUMP_ROOT")"
    TEMP_DIR="$(canonicalize_path --temp-dir "$TEMP_DIR")"

    python3 - "$DUMP_ROOT" "$TEMP_DIR" <<'PY' || die "Directory validation failed."
import os, sys
def validate_dir(path, name, allow_create=False, safe_paths_only=False):
    if safe_paths_only:
        sensitive = ["/", "/etc", "/usr", "/var/lib/vz"]
        if path in sensitive or any(path.startswith(p + "/") for p in sensitive):
            print(f"Error: {name} '{path}' is in a sensitive system path.", file=sys.stderr)
            sys.exit(1)
    if not os.path.exists(path):
        if allow_create:
            return
        print(f"Error: {name} '{path}' does not exist.", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(path):
        print(f"Error: {name} '{path}' is not a directory.", file=sys.stderr)
        sys.exit(1)
    if os.path.islink(path):
        print(f"Error: {name} '{path}' must not be a symbolic link.", file=sys.stderr)
        sys.exit(1)
    if not os.access(path, os.W_OK):
        print(f"Error: {name} '{path}' is not writable or is on a read-only filesystem.", file=sys.stderr)
        sys.exit(1)
    st = os.stat(path)
    if allow_create and os.path.exists(path):
        if (st.st_mode & 0o777) != 0o700 or st.st_uid != 0:
            print(f"Error: {name} '{path}' must have 0700 permissions and be owned by root.", file=sys.stderr)
            sys.exit(1)
    if (st.st_mode & 0o002) and not (st.st_mode & 0o1000):
        print(f"Error: {name} '{path}' is world-writable without sticky bit.", file=sys.stderr)
        sys.exit(1)

validate_dir(sys.argv[1], "Dump root")
validate_dir(sys.argv[2], "Temporary directory", allow_create=True, safe_paths_only=True)
PY

    if [[ "$REQUIRE_DUMP_MOUNT" -eq 1 ]]; then
        if ! findmnt -M "$DUMP_ROOT" >/dev/null 2>&1 && ! mountpoint -q "$DUMP_ROOT" 2>/dev/null; then
            die "Dump root '${DUMP_ROOT}' is not an active mountpoint (--require-dump-mount)."
        fi
    fi

    if [[ ! -d "$TEMP_DIR" ]]; then
        log "Temporary directory '${TEMP_DIR}' does not exist; creating it with 0700 root:root."
        install -d -m 0700 "$TEMP_DIR" || die "Failed to create temporary directory '${TEMP_DIR}'."
    fi

    if [[ -n "$EXPORT_DASHBOARD_BUNDLE" ]]; then
        EXPORT_DASHBOARD_BUNDLE="$(canonicalize_path --export-dashboard-bundle "$EXPORT_DASHBOARD_BUNDLE")"
        [[ -d "$EXPORT_DASHBOARD_BUNDLE" ]] || die "Export bundle directory '${EXPORT_DASHBOARD_BUNDLE}' does not exist."
        local bundle_stat
        bundle_stat="$(stat -c "%U:%a" "$EXPORT_DASHBOARD_BUNDLE")"
        [[ "$bundle_stat" == "root:700" ]] || die "Export bundle directory must be owned by root with 0700 permissions."
    fi

    if [[ -z "$RCLONE_CONFIG_SOURCE" ]]; then
        if [[ -f "$RCLONE_CONFIG" ]]; then
            RCLONE_CONFIG_SOURCE="$RCLONE_CONFIG"
        else
            RCLONE_CONFIG_SOURCE=/root/.config/rclone/rclone.conf
        fi
    fi
    RCLONE_CONFIG_SOURCE="$(canonicalize_path --rclone-config "$RCLONE_CONFIG_SOURCE")"
}

check_rclone() {
    local require="$1"
    local connectivity="$2"
    if [[ "$require" -eq 0 ]]; then
        command -v rclone >/dev/null 2>&1 ||
            warn "rclone is not installed; off-site sync will be unavailable."
        return 0
    fi
    command -v rclone >/dev/null 2>&1 || die "rclone is required but not installed."
    rclone version >/dev/null 2>&1 || die "rclone is installed but cannot run."
    [[ -f "$RCLONE_CHECK_CONFIG" && ! -L "$RCLONE_CHECK_CONFIG" && -r "$RCLONE_CHECK_CONFIG" ]] ||
        die "rclone config is missing, unreadable, or a symbolic link."

    if [[ "$connectivity" -eq 0 && -z "$RCLONE_REMOTE" ]]; then
        return 0
    fi

    local remotes=()
    mapfile -t remotes < <(rclone --config "$RCLONE_CHECK_CONFIG" listremotes 2>/dev/null |
        sed 's/:$//' | sed '/^$/d')
    if [[ -z "$RCLONE_REMOTE" ]]; then
        if [[ ${#remotes[@]} -eq 1 ]]; then
            RCLONE_REMOTE="${remotes[0]}"
        else
            die "--rclone-remote is required when the config does not contain exactly one remote."
        fi
    fi
    printf '%s\n' "${remotes[@]}" | grep -Fxq "$RCLONE_REMOTE" ||
        die "Configured rclone remote '${RCLONE_REMOTE}' was not found."

    if [[ "$connectivity" -eq 1 ]]; then
        local error_log="${WORK_DIR}/rclone-connectivity.log"
        if ! timeout 25s rclone --config "$RCLONE_CHECK_CONFIG" lsd "${RCLONE_REMOTE}:" \
            --max-depth 1 --timeout 10s --contimeout 5s --retries 1 \
            --low-level-retries 1 >/dev/null 2>"$error_log"; then
            : >"$error_log"
            die "Authenticated rclone operation failed for remote '${RCLONE_REMOTE}' (details redacted)."
        fi
        : >"$error_log"
        log "Authenticated rclone operation succeeded for remote '${RCLONE_REMOTE}'."
    fi
}

uninstall() {
    [[ $EUID -eq 0 ]] || die "Uninstall must run as root."
    systemctl stop "$SERVICE" >/dev/null 2>&1 || true
    systemctl disable "$SERVICE" >/dev/null 2>&1 || true
    systemctl stop "$FIREWALL_SERVICE" >/dev/null 2>&1 || true
    systemctl disable "$FIREWALL_SERVICE" >/dev/null 2>&1 || true
    if command -v nft >/dev/null 2>&1; then
        if [[ "$FORCE_REMOVE_FOREIGN_FIREWALL_TABLE" -eq 1 ]] || [[ -f /etc/proxsync-agent/firewall-managed.json ]]; then
            nft delete table inet proxsync >/dev/null 2>&1 || true
        else
            warn "Skipping removal of table inet proxsync because ownership could not be verified."
        fi
    fi
    rm -f "$UNIT_FILE" "$FIREWALL_FILE" "$FIREWALL_LOADER" "/usr/libexec/proxsync-verify-firewall" "$FIREWALL_UNIT"
    rm -rf "$INSTALL_DIR" "$CONFIG_DIR" /var/lib/proxsync-agent /var/log/proxsync-agent /etc/systemd/system/proxsync-agent.service.d
    systemctl daemon-reload
    log "Uninstall complete; only ProxSync-managed files and firewall state were removed."
}

verify_firewall() {
    case "$FIREWALL_MODE" in
        managed)
            [[ -f "$FIREWALL_FILE" && -x "$FIREWALL_LOADER" && -f "$FIREWALL_UNIT" ]] ||
                die "Persistent firewall files are incomplete."
            systemctl is-enabled --quiet "$FIREWALL_SERVICE" ||
                die "Persistent firewall loader is not enabled."
            firewall_rules_valid "$DASHBOARD_IP" "$PORT" ||
                die "Runtime firewall does not exactly match the desired dashboard IP and port."
            systemctl reload "$FIREWALL_SERVICE"
            firewall_rules_valid "$DASHBOARD_IP" "$PORT" ||
                die "Firewall reload changed or lost the desired rules."
            ;;
        unchanged)
            if nft list table inet proxsync >/dev/null 2>&1 || [[ -e "$FIREWALL_FILE" ]]; then
                warn "--skip-firewall left existing ProxSync-managed firewall state active."
            fi
            ;;
        removed)
            ! nft list table inet proxsync >/dev/null 2>&1 ||
                die "ProxSync runtime firewall table still exists."
            [[ ! -e "$FIREWALL_FILE" && ! -e "$FIREWALL_LOADER" && ! -e "$FIREWALL_UNIT" ]] ||
                die "ProxSync persistent firewall state still exists."
            ;;
    esac
}

verify_installation() {
    systemctl is-active --quiet "$SERVICE" || die "Service '${SERVICE}' is not active."
    leaf_pair_valid "${TLS_DIR}/ca.crt" "${TLS_DIR}/server.crt" "${TLS_DIR}/server.key" ||
        die "Server certificate pair failed verification."
    leaf_pair_valid "${TLS_DIR}/ca.crt" "${TLS_DIR}/dashboard.crt" "${TLS_DIR}/dashboard.key" ||
        die "Dashboard certificate pair failed verification."
    certificate_has_desired_sans "${TLS_DIR}/server.crt" "$AGENT_DNS" "$AGENT_IP" ||
        die "Server certificate SAN set does not exactly match the desired set."
    [[ -r "$ENV_FILE" ]] || die "Agent environment file is unreadable."
    verify_firewall

    local url_host health_url
    url_host="$(format_url_host "$AGENT_IP")"
    health_url="https://${url_host}:${PORT}/health"
    if ! python3 - "$health_url" "${TLS_DIR}/ca.crt" "${TLS_DIR}/dashboard.crt" \
        "${TLS_DIR}/dashboard.key" <<'PY' >/dev/null
import ssl
import sys
import time
import urllib.request

url, cafile, certfile, keyfile = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
context = ssl.create_default_context(cafile=cafile)
context.load_cert_chain(certfile=certfile, keyfile=keyfile)

last_err = None
for _ in range(15):
    try:
        with urllib.request.urlopen(url, context=context, timeout=3) as response:
            if response.status == 200:
                sys.exit(0)
            else:
                last_err = f"HTTP {response.status}"
    except Exception as e:
        last_err = e
        time.sleep(1)

print(f"Health check error: {last_err}", file=sys.stderr)
sys.exit(1)
PY
    then
        die "mTLS health endpoint '${health_url}' did not return HTTP 200."
    fi
    log "Service, full PKI SAN set, firewall persistence/reload, and mTLS health endpoint verified."
}

install_agent() {
    [[ $EUID -eq 0 ]] || die "Run as root."
    validate_dependencies
    validate_inputs

    WORK_DIR="$(mktemp -d /tmp/proxsync-install.XXXXXX)"
    chmod 0700 "$WORK_DIR"
    trap cleanup EXIT
    local staged_rclone="${WORK_DIR}/rclone.conf"
    if [[ -f "$RCLONE_CONFIG_SOURCE" ]]; then
        install -m 0600 "$RCLONE_CONFIG_SOURCE" "$staged_rclone"
        RCLONE_CHECK_CONFIG="$staged_rclone"
    else
        RCLONE_CHECK_CONFIG="$RCLONE_CONFIG_SOURCE"
    fi

    local host_ips=()
    mapfile -t host_ips < <(get_host_ips)
    AGENT_IP="$(select_agent_ip "$AGENT_IP" "$FORCE_IP" "${host_ips[@]}")"
    if [[ -z "$AGENT_DNS" ]]; then
        AGENT_DNS="$(hostname -f 2>/dev/null || hostname)"
    fi
    AGENT_DNS="${AGENT_DNS%.}"
    is_valid_dns "$AGENT_DNS" ||
        die "--agent-dns must be a valid hostname or FQDN."
    [[ -d "$DUMP_ROOT" ]] || die "Dump root '${DUMP_ROOT}' does not exist."
    port_is_available "$PORT" || die "TCP port ${PORT} is already in use."
    if ! pvesm status 2>/dev/null | awk 'NR > 1 {print $1}' | grep -Fxq "$BACKUP_STORAGE"; then
        warn "Storage '${BACKUP_STORAGE}' is not currently reported by pvesm."
    fi
    check_rclone "$REQUIRE_RCLONE" "$REQUIRE_DRIVE_CONNECTIVITY"

    trap 'rollback_on_error $?' ERR

    local staged_tls="${WORK_DIR}/tls"
    local staged_env="${WORK_DIR}/agent.env"
    local staged_unit="${WORK_DIR}/proxsync-agent.service"
    local verify_unit="${WORK_DIR}/verify-agent.service"
    local staged_firewall="${WORK_DIR}/proxsync.nft"
    local staged_loader="${WORK_DIR}/proxsync-firewall-apply"
    local staged_firewall_unit="${WORK_DIR}/proxsync-firewall.service"
    local verify_firewall_unit="${WORK_DIR}/verify-firewall.service"
    local staged_firewall_marker="${WORK_DIR}/firewall-managed.json"
    local staged_verifier="${WORK_DIR}/proxsync-verify-firewall"
    local staged_agent_dropin_dir="${WORK_DIR}/proxsync-agent.service.d"
    local nft_transaction="${WORK_DIR}/nft-check.nft"
    local old_hmac="" hmac_secret dashboard_cidr

    install -d -m 0700 "$staged_tls"
    if [[ -d "$TLS_DIR" ]]; then cp -a "${TLS_DIR}/." "$staged_tls/"; fi
    prepare_pki "$staged_tls" "$AGENT_DNS" "$AGENT_IP" \
        "$REGENERATE_ALL_SECRETS" "$REPAIR_PKI" "$ROTATE_SERVER_CERT"

    if [[ -f "$ENV_FILE" && "$REGENERATE_ALL_SECRETS" -eq 0 ]]; then
        old_hmac="$(sed -E -n 's/^PROXSYNC_AGENT_HMAC_SECRET=["'\'']*([^"'\'']*).*/\1/p' "$ENV_FILE" | head -1)"
    fi
    hmac_secret="$old_hmac"
    [[ -n "$hmac_secret" ]] || hmac_secret="$(openssl rand -hex 32)"
    dashboard_cidr="$(get_cidr "$DASHBOARD_IP")"
    agent_cidr="$(get_cidr "$AGENT_IP")"
    allowed_networks="$dashboard_cidr"
    if [[ "$allowed_networks" != *"$agent_cidr"* ]]; then
        allowed_networks="${allowed_networks},${agent_cidr}"
    fi
    if [[ "$allowed_networks" != *"127.0.0.1/32"* ]]; then
        allowed_networks="${allowed_networks},127.0.0.1/32"
    fi
    render_environment "$staged_env" "$hmac_secret" "$allowed_networks"

    sed -e "s|MemoryHigh=.*|MemoryHigh=${MEMORY_HIGH}|" \
        -e "s|MemoryMax=.*|MemoryMax=${MEMORY_MAX}|" \
        "${SCRIPT_DIR}/proxsync-agent.service" >"$staged_unit"
    sed 's|^ExecStart=.*|ExecStart=/bin/true|' "$staged_unit" >"$verify_unit"
    systemd-analyze verify "$verify_unit"

    if [[ "$FIREWALL_MODE" == managed ]]; then
        render_firewall "$staged_firewall" "$DASHBOARD_IP" "$PORT"
        render_firewall_loader "$staged_loader"
        render_firewall_verifier "$staged_verifier"
        render_firewall_unit "$staged_firewall_unit"
        generate_firewall_marker "$staged_firewall_marker" "$staged_firewall" "$DASHBOARD_IP" "$PORT"
        validate_firewall_rules "$staged_firewall" "$nft_transaction"
        sed -e 's|^ExecStart=.*|ExecStart=/bin/true|' \
            -e 's|^ExecReload=.*|ExecReload=/bin/true|' \
            "$staged_firewall_unit" >"$verify_firewall_unit"
        systemd-analyze verify "$verify_firewall_unit"
        install -d -m 0755 "$staged_agent_dropin_dir"
        {
            printf '[Unit]\n'
            printf 'Requires=proxsync-firewall.service\n'
            printf 'After=proxsync-firewall.service\n'
            printf '\n[Service]\n'
            printf 'ExecStartPre=/usr/libexec/proxsync-verify-firewall\n'
        } >"${staged_agent_dropin_dir}/10-firewall.conf"
    fi

    [[ -d "${REPO_ROOT}/agent/app" && -f "${REPO_ROOT}/agent/pyproject.toml" ]] ||
        die "Agent source is missing from this checkout."
    begin_transaction

    install -d -m 0755 "$INSTALL_DIR"
    rm -rf "${INSTALL_DIR}/app"
    cp -a "${REPO_ROOT}/agent/app" "${INSTALL_DIR}/app"
    atomic_install "${REPO_ROOT}/agent/pyproject.toml" "${INSTALL_DIR}/pyproject.toml" 0644
    if [[ ! -x "${INSTALL_DIR}/venv/bin/python" ]]; then python3 -m venv "${INSTALL_DIR}/venv"; fi
    "${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
    "${INSTALL_DIR}/venv/bin/pip" install --quiet "$INSTALL_DIR"

    install -d -m 0750 "$CONFIG_DIR" "$TLS_DIR"
    local file mode
    for file in ca.crt ca.key server.crt server.key dashboard.crt dashboard.key; do
        if [[ -f "${staged_tls}/${file}" ]]; then
            mode=0644
            if [[ "$file" == *.key ]]; then mode=0640; fi
            atomic_install "${staged_tls}/${file}" "${TLS_DIR}/${file}" "$mode"
        fi
    done
    atomic_install "$staged_env" "$ENV_FILE" 0640
    atomic_install "$staged_unit" "$UNIT_FILE" 0644
    install -d -m 0750 /var/lib/proxsync-agent /var/log/proxsync-agent
    if [[ -f "$staged_rclone" ]]; then
        atomic_install "$staged_rclone" "$RCLONE_CONFIG" 0600
    fi
    install -d -m 0700 "$TEMP_DIR"

    case "$FIREWALL_MODE" in
        managed)
            atomic_install "$staged_firewall" "$FIREWALL_FILE" 0644
            atomic_install "$staged_loader" "$FIREWALL_LOADER" 0755
            atomic_install "$staged_verifier" "/usr/libexec/proxsync-verify-firewall" 0755
            atomic_install "$staged_firewall_unit" "$FIREWALL_UNIT" 0644
            atomic_install "$staged_firewall_marker" "/etc/proxsync-agent/firewall-managed.json" 0600
            install -d -m 0755 /etc/systemd/system/proxsync-agent.service.d
            atomic_install "${staged_agent_dropin_dir}/10-firewall.conf" "/etc/systemd/system/proxsync-agent.service.d/10-firewall.conf" 0644
            apply_firewall_rules "$staged_firewall"
            ;;
        unchanged)
            log "Firewall mode: unchanged"
            ;;
        removed)
            systemctl stop "$FIREWALL_SERVICE" >/dev/null 2>&1 || true
            systemctl disable "$FIREWALL_SERVICE" >/dev/null 2>&1 || true
            if [[ "$FORCE_REMOVE_FOREIGN_FIREWALL_TABLE" -eq 1 ]] || [[ -f /etc/proxsync-agent/firewall-managed.json ]]; then
                nft delete table inet proxsync >/dev/null 2>&1 || true
            else
                warn "Skipping removal of table inet proxsync because ownership could not be verified."
            fi
            rm -f "$FIREWALL_FILE" "$FIREWALL_LOADER" "/usr/libexec/proxsync-verify-firewall" "$FIREWALL_UNIT" /etc/proxsync-agent/firewall-managed.json /etc/systemd/system/proxsync-agent.service.d/10-firewall.conf
            ;;
    esac

    systemctl daemon-reload
    if [[ "$FIREWALL_MODE" == managed ]]; then
        systemctl enable "$FIREWALL_SERVICE" >/dev/null
        systemctl restart "$FIREWALL_SERVICE"
    fi
    systemctl enable "$SERVICE" >/dev/null
    systemctl restart "$SERVICE"
    verify_installation

    if [[ -n "$old_hmac" && "$REGENERATE_ALL_SECRETS" -eq 0 ]]; then
        [[ "$old_hmac" == "$hmac_secret" ]] || die "HMAC preservation invariant failed."
    fi
    TRANSACTION_COMMITTED=1
    TRANSACTION_ACTIVE=0

    local url_host
    url_host="$(format_url_host "$AGENT_IP")"
    log "Installation status: SUCCESS"
    log "Firewall mode: ${FIREWALL_MODE}"
    printf '\nAgent URL: https://%s:%s\n' "$url_host" "$PORT"
    printf 'API key id: proxsync-dashboard\n'
    if [[ -n "$EXPORT_DASHBOARD_BUNDLE" ]]; then
        install -m 0644 "${TLS_DIR}/ca.crt" "${EXPORT_DASHBOARD_BUNDLE}/"
        install -m 0644 "${TLS_DIR}/dashboard.crt" "${EXPORT_DASHBOARD_BUNDLE}/"
        install -m 0600 "${TLS_DIR}/dashboard.key" "${EXPORT_DASHBOARD_BUNDLE}/"
        {
            printf 'PROXSYNC_SERVER_URL=https://%s:%s\n' "$url_host" "$PORT"
            printf 'PROXSYNC_DASHBOARD_API_KEY_ID=proxsync-dashboard\n'
            printf 'PROXSYNC_DASHBOARD_HMAC_SECRET="%s"\n' "$hmac_secret"
        } >"${EXPORT_DASHBOARD_BUNDLE}/env.fragment"
        chmod 0600 "${EXPORT_DASHBOARD_BUNDLE}/env.fragment"
        log "Dashboard credentials exported to: ${EXPORT_DASHBOARD_BUNDLE}"
        printf 'Dashboard credentials: %s (exported)\n' "$EXPORT_DASHBOARD_BUNDLE"
    else
        printf 'HMAC secret: [stored in %s; not printed]\n' "$ENV_FILE"
        printf 'Dashboard credentials: %s/{ca.crt,dashboard.crt,dashboard.key}\n' "$TLS_DIR"
    fi
}

main() {
    init_defaults
    local parse_status=0
    parse_args "$@" || parse_status=$?
    [[ "$parse_status" -eq 0 ]] || {
        [[ "$parse_status" -eq 2 ]] && return 0
        return "$parse_status"
    }
    if [[ "$UNINSTALL_MODE" -eq 1 ]]; then
        uninstall
    else
        install_agent
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
