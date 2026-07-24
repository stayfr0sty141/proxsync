# ProxSync — Installation Guide

ProxSync is two components that are installed separately and then introduced to each other:

| Component | Where it runs | Installer |
| --- | --- | --- |
| **Backup Agent** | The Proxmox VE host, as root | `deploy/host/install-agent.sh` |
| **Dashboard** (API + Web UI) | An unprivileged LXC container | `deploy/lxc/install.sh` |

Install the **agent first** — it generates the certificate and secret the dashboard needs.

> All commands assume a checkout of this repository is present on each machine. Clone it, or
> copy the tree across with `scp`/`rsync`.

---

## 0. Prerequisites

### On the Proxmox host

- Proxmox VE 8 (Debian 12) or 9 (Debian 13)
- A mounted backup storage (e.g. `backup-hdd`) and its dump directory
- `openssl`, `nftables`, `python3` ≥ 3.11 (all available on PVE)
- `rclone` configured with your Google Drive remote, if you want off-site sync (see §5)

### In the LXC container (Debian 12/13, unprivileged)

- Python **3.13** (the dashboard requires it)
- Node.js **20+** and npm (to build the frontend)
- `nginx`
- `sqlite3` (for the database self-backup), or PostgreSQL client tools if you use Postgres

```bash
# Inside the container, as root:
apt update
apt install -y python3 python3-venv nodejs npm nginx sqlite3 openssl
```

---

## 1. Install the Backup Agent (Proxmox host)

```bash
git clone https://github.com/stayfr0sty141/proxsync.git
cd proxsync/deploy/host
mkdir -m 0700 /root/proxsync-bundle
./install-agent.sh \
  --dashboard-ip 10.0.0.20 \
  --dump-root /mnt/backup-hdd/dump \
  --temp-dir /mnt/backup-hdd/tmp \
  --export-dashboard-bundle /root/proxsync-bundle
```

`--dashboard-ip` is the address of the LXC container that will run the dashboard; the agent
refuses connections from anywhere else at both the managed nftables firewall and application
layers. The installer produces a certificate and HMAC secret for the dashboard, which are safely
exported to the `--export-dashboard-bundle` directory. The private credential remains in this
root-only directory and is not printed to stdout to avoid shell history leaks.

Transfer the bundle to the dashboard container securely (e.g. via `scp`) and then delete the bundle directory on the host.

### Firewall lifecycle

The default `--configure-firewall` mode is fail-closed. The agent heavily depends on the `proxsync-firewall.service`; if the firewall fails to load, the agent will refuse to start. This guarantees that your network protections are active before the agent listens on its port.

The installer owns `table inet proxsync`. It creates `/etc/nftables.d/proxsync.nft` and a secure ownership marker file to ensure it never accidentally destroys an unrelated firewall table sharing the same name.

- `--configure-firewall` creates or deterministically updates runtime and boot-persistent rules, as well as the agent dependency drop-in.
- `--skip-firewall` changes nothing. Existing ProxSync rules remain active.
- `--remove-firewall` removes only the ProxSync table, loader, agent dependency drop-in, and unit, but strictly verifies ownership first.

You can check firewall state using:

```bash
systemctl status proxsync-firewall
systemctl show proxsync-agent -p Requires -p After
nft list table inet proxsync
```

Native nftables, Proxmox Firewall, and iptables compatibility may coexist. Do not create a
second `table inet proxsync`; a collision will cause the installer to abort to protect your existing table.

### Safe reruns, PKI repair, and rollback

Every rerun stages and validates its environment, systemd units, nftables transaction, and PKI
before mutation. It backs up the previous application, configuration, units, certificates, and
runtime firewall. A failed apply, restart, mTLS health check, certificate check, or firewall
reload restores those files and rules and restarts the previous service.

The desired server SAN set is exactly the agent DNS name, agent IP, `127.0.0.1`, and `::1`.
Changing either DNS or IP rotates only the server leaf certificate. A missing server or
dashboard key is repaired with the existing CA. If `ca.crt` exists but `ca.key` is unavailable,
valid existing leaves may be reused, but any required repair or rotation stops with an error.
Use `--repair-pki` to explicitly audit partial state, `--rotate-server-cert` for a leaf-only
rotation, and `--regenerate-all-secrets` only for an intentional trust reset; the latter
replaces the CA, dashboard credentials, and HMAC secret.

`--rclone-config` is an import source. The installer validates it, copies it with mode `0600`
into `/var/lib/proxsync-agent/rclone.conf`, and points the service at that managed copy. This is
required because the hardened unit hides `/root`; the state directory remains writable when
rclone refreshes an OAuth token. A rerun stages the current managed copy first, so token updates
are preserved and a failed install restores the prior copy.

---

## 2. Create a read-only Proxmox token

The dashboard reads the guest inventory through a **PVEAuditor** token — read-only, so no
dashboard code path can change anything on the host (decision D2).

```bash
# On the Proxmox host:
pveum user add proxsync@pve
pveum acl modify / --user proxsync@pve --role PVEAuditor
pveum user token add proxsync@pve dashboard --privsep 0
```

The last command prints a **token id** (`proxsync@pve!dashboard`) and a **secret**. Keep both.

---

## 3. Install the Dashboard (LXC container)

```bash
cd proxsync/deploy/lxc
./install.sh --server-name proxsync.lan --agent-ip 10.0.0.10
```

`--server-name` is the hostname you will point your browser at; `--agent-ip` is the Proxmox
host. The installer creates the `proxsync` service user, a virtualenv, builds the frontend,
writes `/etc/proxsync/api.env`, generates a **self-signed** TLS certificate, installs the
nginx site and the systemd units, runs the database migrations, and starts everything.

It prints a generated **first-run administrator** password. Save it.

---

## 4. Introduce the two components

Copy the agent's client credentials bundle into the container, then fill in the secrets:

```bash
# From the dashboard container:
scp -r root@10.0.0.10:/root/proxsync-bundle /etc/proxsync/
mv /etc/proxsync/proxsync-bundle/ca.crt         /etc/proxsync/agent-ca.crt
mv /etc/proxsync/proxsync-bundle/dashboard.crt  /etc/proxsync/agent-client.crt
mv /etc/proxsync/proxsync-bundle/dashboard.key  /etc/proxsync/agent-client.key
chown root:proxsync /etc/proxsync/agent-*.crt /etc/proxsync/agent-client.key
chmod 0640 /etc/proxsync/agent-client.key
```

Edit `/etc/proxsync/api.env` and set:

```ini
PROXSYNC_AGENT_HMAC_SECRET=<the HMAC secret found in /etc/proxsync/proxsync-bundle/env.fragment>
PROXSYNC_PROXMOX_TOKEN_ID=proxsync@pve!dashboard
PROXSYNC_PROXMOX_TOKEN_SECRET=<the token secret>
```

After configuring, securely erase the bundle in both locations.

Restart the API:

```bash
systemctl restart proxsync-api
```

---

## 5. Verify

```bash
# The API is up and can reach the agent over mTLS:
curl -sk https://proxsync.lan/api/v1/health/detail | python3 -m json.tool
```

`agent` should report `ok`. Then open `https://proxsync.lan/` in a browser, sign in with the
bootstrap administrator, and change the password when prompted. Finally, clear the one-time
password from the env file:

```bash
sed -i 's/^PROXSYNC_BOOTSTRAP_ADMIN_PASSWORD=.*/PROXSYNC_BOOTSTRAP_ADMIN_PASSWORD=/' /etc/proxsync/api.env
```

---

## 6. Off-site sync (optional)

Google Drive replication runs on the host via rclone (decision D1).

### 6a. Install rclone

```bash
# On the Proxmox host:
apt update && apt install -y rclone
rclone version
```

### 6b. Authenticate with Google Drive

Run the interactive config wizard. This is a one-time OAuth login — rclone stores the token
so you never need to log in again:

```bash
rclone config
```

| Prompt | Answer |
| --- | --- |
| `n/s/q>` | `n` (new remote) |
| `name>` | **`gdrive`** (the default remote name ProxSync expects) |
| `Storage>` | Pick the number for **Google Drive** |
| `client_id>` | Press Enter (use rclone's built-in OAuth client) |
| `client_secret>` | Press Enter |
| `scope>` | `1` — `drive.file` (ProxSync needs write + list + delete) |
| `root_folder_id>` | Press Enter (use "My Drive" root) |
| `service_account_file>` | Press Enter (skip) |
| `Edit advanced config?` | `n` (no) |
| `Use web browser to authenticate?` | `y` if the host has a browser; `n` if headless (copy the URL to another machine) |
| `y/e/d>` | `y` (yes, keep the remote) |
| `e/n/d/r/c/s/q>` | `q` (quit) |

After completing the OAuth flow in your browser, verify the remote works without printing its
configuration or token:

```bash
rclone about gdrive:
# Expected output: Used / Total / Free + quota info

rclone ls gdrive:
# Lists files at the root of your Drive (may be empty)
```

For an installer-gated authenticated check, use:

```bash
./install-agent.sh --dashboard-ip 10.0.0.20 --rclone-remote gdrive \
  --require-rclone --require-drive-connectivity
```

Despite the compatibility option name, the check is backend-neutral: it verifies the named
entry from `rclone listremotes` with a bounded `rclone lsd remote:` call. Raw DNS/TCP reachability
is not treated as proof that OAuth credentials, backend permissions, or the remote work. Command
output is suppressed and failures are reported without config, token, or provider response text.

> **Troubleshooting**: If the host is headless (no desktop), choose `n` at the browser
> prompt. rclone prints a URL — open it on your laptop, sign in to Google, copy the
> verification code, and paste it back into the terminal. Alternatively, run
> `rclone authorize "drive"` on a machine *with* a browser and paste the resulting token.

### 6c. Allow-list the remote & enable sync

Set the allow-list in `/etc/proxsync-agent/agent.env`:

```ini
PROXSYNC_AGENT_ALLOWED_REMOTES=gdrive
```

Then restart the agent:

```bash
systemctl restart proxsync-agent
```

### 6d. Enable in the dashboard

Open the ProxSync web UI, go to **Settings → Google Drive**, and set:

| Setting | Value |
| --- | --- |
| `enabled` | `true` |
| `remote_name` | `gdrive` |
| `folder` | `proxsync/dump` (or your preferred path) |

From this point on, any backup policy with sync enabled will automatically push archives
to Google Drive after the backup completes.

---

## 7. A real TLS certificate

The installer's self-signed certificate makes browsers warn. For a trusted certificate, either
drop your own PEM files at `/etc/proxsync/tls/web.crt` and `web.key` and `systemctl reload
nginx`, or use Let's Encrypt (the nginx site already serves the ACME http-01 challenge from
`/var/www/certbot`):

```bash
apt install -y certbot
certbot certonly --webroot -w /var/www/certbot -d proxsync.lan
# Point ssl_certificate[_key] in /etc/nginx/sites-available/proxsync.conf at the issued files.
```

---

## What the installer configured

| Path | Contents |
| --- | --- |
| `/opt/proxsync/backend` | API code + virtualenv |
| `/opt/proxsync/frontend` | Next.js standalone build |
| `/opt/proxsync/scripts` | Maintenance scripts (DB backup/restore, upgrade) |
| `/etc/proxsync/api.env` | Configuration and secrets (`root:proxsync`, mode 0640) |
| `/etc/proxsync/tls/` | Browser-facing TLS certificate |
| `/var/lib/proxsync/` | SQLite database and state |
| `/var/log/proxsync/` | Logs |
| `/var/backups/proxsync/` | Database self-backups (daily timer) |

Continue with [UPGRADE.md](UPGRADE.md) for updates, and
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) if `/health/detail` is unhappy.
