#!/usr/bin/env bash
# Optional root/Linux integration test. All networking stays inside three temporary namespaces.
set -Eeuo pipefail

if [[ "$(uname -s)" != Linux || $EUID -ne 0 ]]; then
    echo "SKIP: firewall integration test requires root on Linux"
    exit 77
fi
for command in ip nft nc timeout; do
    command -v "$command" >/dev/null || {
        echo "SKIP: missing $command"
        exit 77
    }
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The test is relocatable, so the source path is necessarily computed from this file.
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/install-agent.sh"

suffix="$$"
server_ns="proxsync-server-${suffix}"
dashboard_ns="proxsync-dashboard-${suffix}"
other_ns="proxsync-other-${suffix}"
work_dir="$(mktemp -d)"

cleanup_integration() {
    ip netns delete "$server_ns" >/dev/null 2>&1 || true
    ip netns delete "$dashboard_ns" >/dev/null 2>&1 || true
    ip netns delete "$other_ns" >/dev/null 2>&1 || true
    rm -rf -- "$work_dir"
}
trap cleanup_integration EXIT

ip netns add "$server_ns"
ip netns add "$dashboard_ns"
ip netns add "$other_ns"
ip link add "psd-${suffix}" type veth peer name "dps-${suffix}"
ip link add "pso-${suffix}" type veth peer name "ops-${suffix}"
ip link set "psd-${suffix}" netns "$server_ns"
ip link set "dps-${suffix}" netns "$dashboard_ns"
ip link set "pso-${suffix}" netns "$server_ns"
ip link set "ops-${suffix}" netns "$other_ns"

ip -n "$server_ns" address add 10.210.1.1/24 dev "psd-${suffix}"
ip -n "$dashboard_ns" address add 10.210.1.2/24 dev "dps-${suffix}"
ip -n "$server_ns" address add 10.210.2.1/24 dev "pso-${suffix}"
ip -n "$other_ns" address add 10.210.2.2/24 dev "ops-${suffix}"
for ns in "$server_ns" "$dashboard_ns" "$other_ns"; do
    ip -n "$ns" link set lo up
done
ip -n "$server_ns" link set "psd-${suffix}" up
ip -n "$dashboard_ns" link set "dps-${suffix}" up
ip -n "$server_ns" link set "pso-${suffix}" up
ip -n "$other_ns" link set "ops-${suffix}" up

rules="${work_dir}/proxsync.nft"
render_firewall "$rules" 10.210.1.2 18765
ip netns exec "$server_ns" nft --check --file "$rules"
ip netns exec "$server_ns" nft --file "$rules"

# The configured dashboard source reaches the agent port.
ip netns exec "$server_ns" timeout 3 nc -l 18765 >/dev/null &
listener_pid=$!
sleep 0.1
printf dashboard | ip netns exec "$dashboard_ns" timeout 2 nc -N 10.210.1.1 18765
wait "$listener_pid"

# Another source is dropped.
ip netns exec "$server_ns" timeout 3 nc -l 18765 >/dev/null &
listener_pid=$!
sleep 0.1
if printf other | ip netns exec "$other_ns" timeout 1 nc -N 10.210.2.1 18765; then
    echo "FAIL: unapproved source reached the agent port" >&2
    exit 1
fi
wait "$listener_pid" || true

# There is no output hook: the agent namespace can still initiate an rclone-like connection.
ip netns exec "$dashboard_ns" timeout 3 nc -l 18766 >/dev/null &
listener_pid=$!
sleep 0.1
printf outbound | ip netns exec "$server_ns" timeout 2 nc -N 10.210.1.2 18766
wait "$listener_pid"

echo "PASS: dashboard accepted, other source dropped, outbound connection allowed"
