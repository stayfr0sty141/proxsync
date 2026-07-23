# ProxSync Backup Agent

The privileged half of ProxSync. Runs **on the Proxmox VE host**, executes a closed set of
backup and restore operations, and reports task state. It has no database, no UI, and no
generic command endpoint.

## Security model

| Layer | Control |
| --- | --- |
| Address | `PROXSYNC_AGENT_ALLOWED_CLIENT_NETWORKS` — checked before authentication, on every route including `/health`. Mirrored by the dedicated `table inet proxsync` input chain |
| Transport | Mutual TLS. uvicorn runs with `ssl_cert_reqs=CERT_REQUIRED` against the dashboard CA, so a handshake without a valid client certificate never reaches the application |
| Request | HMAC-SHA256 over `METHOD\|path?query\|timestamp\|nonce\|sha256(body)`. ±60 s window, single-use nonces, constant-time comparison |
| Arguments | Closed enums, VMID allow-list checked against real guest configs, storage checked against `pvesm status`, filenames matched against the vzdump pattern, paths canonicalised and containment-checked after symlink resolution |
| Execution | `asyncio.create_subprocess_exec` with an argv **list**. No shell exists in the process tree, so quoting, globbing, redirection and chaining are not possible. Absolute executable paths only — `PATH` is never consulted |

What the agent will refuse, with no process spawned: an unknown VMID, a VMID whose type does
not match the request, a storage outside the allow-list, an unparseable backup mode, a
filename containing a separator or a leading `-`, a symlink pointing outside the dump root,
a file that is not a vzdump artifact, a stale or replayed signature, and a request from an
unlisted address.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/backup/start` | Run `vzdump` for one guest → task id |
| GET | `/backup/list` | List artifacts (`?vmid=`, `?guest_type=`) |
| DELETE | `/backup/{id}` | Delete an artifact and its `.log` / `.notes` / `.sha256` sidecars |
| POST | `/restore/vm` | `qmrestore` |
| POST | `/restore/lxc` | `pct restore` |
| GET | `/task/{id}` | Task state and progress |
| GET | `/task/{id}/log` | Task log tail |
| POST | `/task/{id}/cancel` | SIGTERM → SIGKILL after the grace period |
| GET | `/task` | List tasks |
| GET | `/storage/status` | `statvfs` on the dump root + `pvesm status` |
| GET | `/health` | Version, uptime, slots, binary availability — the only unsigned route |

Full request and response shapes: [../docs/API.md](../docs/API.md) part 2.

## Task lifecycle

`queued → running → success | failed | cancelled | interrupted`

Every transition is written to `/var/lib/proxsync-agent/tasks/<id>.json` via temp-file +
`os.replace`, so the dashboard gets a truthful answer even if the agent restarted mid-poll.

`interrupted` means the agent restarted while the task was running. systemd kills the whole
control group, so the child did not survive — the agent says the outcome is unknown rather
than guessing. The dashboard surfaces that state instead of recording a backup that may not
exist.

## Install

```bash
cd /tmp && git clone https://github.com/stayfr0sty141/proxsync.git
cd proxsync/deploy/host
./install-agent.sh --agent-ip 10.0.0.10 --dashboard-ip 10.0.0.20
```

The script builds the PKI, writes `/etc/proxsync-agent/agent.env`, creates a virtualenv in
`/opt/proxsync-agent`, installs the hardened systemd unit and persistent
`table inet proxsync`, and reports the three files the dashboard needs. It does not print the
HMAC secret. Re-running preserves the CA, dashboard credentials, and HMAC secret; DNS/IP SAN
changes rotate only the server leaf. `--regenerate-all-secrets` is the explicit trust-reset
operation.

Configuration reference: [.env.example](.env.example).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                # 180 tests, no Proxmox host required
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy app tests        # strict
```

The test suite replaces `ProcessRunner` with a fake that records argv and replays captured
vzdump/qmrestore/pvesm output. What is verified is exactly what matters: which requests are
accepted, which are refused, and what argv a request would have produced.

Running without TLS (development only) logs a loud warning and still requires a signed
request; the agent refuses to start at all if `PROXSYNC_AGENT_HMAC_SECRET` is unset.

## Notes on Proxmox behaviour

- **Compression level does not exist.** `vzdump` takes `--compress {0,gzip,lzo,zstd}` and,
  for zstd, `--zstd <thread count>` (0 = half the host's cores). The agent exposes
  `zstd_threads`; it does not invent a level PVE would ignore.
- **Backup notes** are written by the agent as a `<archive>.notes` sidecar rather than passed
  via `--notes-template`, whose spelling and template semantics vary across PVE releases.
- **Argument order differs between restore tools**: `qmrestore <archive> <vmid>` but
  `pct restore <vmid> <archive>`. Each has its own builder and its own test.
- **LXC backups report no percentage** while running — tar emits none. The agent reports
  transferred bytes and leaves `percent` null rather than fabricating one.

## Google Drive sync (M4)

rclone runs **here**, not in the dashboard container: the artifacts are on this host, and
copying 40 GiB through the LXC to reach Drive would double the network cost for nothing.

| Endpoint | Command |
| --- | --- |
| `POST /sync/upload` · `/sync/download` | `rclone copyto` — never `copy`, which treats the destination as a directory |
| `POST /sync/delete` | `rclone deletefile` — never `delete`, which removes a directory's contents |
| `GET /sync/list` | `rclone lsjson --hash` |
| `GET /sync/about` | `rclone about --json` |
| `POST /sync/verify` | `lsjson` + a local MD5, compared |

**The remote allow-list is the important setting.** `PROXSYNC_AGENT_ALLOWED_REMOTES` empty
means any remote in `rclone.conf` may be addressed — including a *local* one, which turns an
upload into an arbitrary file read. Name the remotes you intend to use. The agent warns at
startup when the list is empty.

Remote paths are rejected for `..`, `:`, control characters and rclone's filter
metacharacters (`*?[]{}`), because a `*` reaching a filter would change which files a command
touches. Filenames must be well-formed vzdump artifacts inside the dump root — checked on
download as well as upload, so a tampered remote cannot write outside it.

`--retries 1` is passed deliberately: the dashboard counts attempts and applies the backoff.
`--low-level-retries` stays on — that retries one failed HTTP chunk, which is what lets a
40 GiB upload survive a dropped packet.
