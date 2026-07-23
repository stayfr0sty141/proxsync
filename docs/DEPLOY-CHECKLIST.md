# ProxSync — Deployment Checklist

A print-and-tick checklist for a first real deployment. It condenses
[INSTALL.md](INSTALL.md) into ordered, verifiable steps. Tick each box only after its
**Verify** line passes.

> ⚠️ **This is ProxSync's first real install.** Everything is code-complete and tested against
> fakes, but no installer has touched a real Proxmox host yet (see [HANDOFF.md](HANDOFF.md) §2).
> Budget time for troubleshooting — this run *is* the M9 acceptance test.

---

## Topology recap

```text
┌─ Proxmox host (root) ───────────┐
│  proxsync-agent   (deploy/host/)  │  ← runs the vzdump/qmrestore/rclone commands
└──────────────▲──────────────┘
               │ HTTPS · mTLS + HMAC
┌──────────────┴─ unprivileged LXC ─┐
│  proxsync-api + proxsync-web       │  ← the dashboard + UI, no host privilege
│  + nginx (:443)  (deploy/lxc/)     │
└────────────────────────────┘
```

Two machines: the **agent** on the Proxmox host, the **dashboard** in an LXC.

---

## Phase 0 — Prerequisites

### 0.1 The LXC container

- [ ] Create an **unprivileged** LXC on Proxmox
      *(Datacenter → Create CT → uncheck "Privileged")*
- [ ] Use a **Debian 13 (Trixie)** template — it ships **Python 3.13**, which the backend
      requires. *(Ubuntu 24.04 ships 3.12 and the installer will refuse; if you must use it,
      install `python3.13` from the deadsnakes PPA first.)*
- [ ] Give it a static IP (note it — this is `<LXC_IP>`)
- [ ] Resources: ≥ 1 vCPU, ≥ 1 GB RAM, ≥ 8 GB disk
- **Verify:** `pct exec <ctid> -- python3 --version` → `Python 3.13.x`

### 0.2 Note your addresses

- [ ] `<LXC_IP>` — the dashboard container (e.g. `10.0.0.20`)
- [ ] `<HOST_IP>` — the Proxmox host (e.g. `10.0.0.10`)
- [ ] `<SERVER_NAME>` — hostname you'll open in the browser (e.g. `proxsync.lan`)

### 0.3 Get the code onto both machines

- [ ] Clone/copy the repo into the **LXC** (e.g. `/root/ProxSync`)
- [ ] Clone/copy the repo onto the **Proxmox host** (e.g. `/root/ProxSync`)
- **Verify:** `ls ProxSync/deploy/lxc/install.sh` exists on the LXC, and
      `ls ProxSync/deploy/host/install-agent.sh` exists on the host

---

## Phase 1 — Install the Backup Agent (on the Proxmox HOST)

> Do the agent **first** — it generates the certificates and HMAC secret the dashboard needs.

### 1.1 Preconditions

- [ ] Backup storage exists and is mounted (e.g. `backup-hdd`)
- [ ] Dump directory exists (e.g. `/mnt/backup-hdd/dump`)
- **Verify:** `pvesm status | grep backup-hdd` shows the storage

### 1.2 Run the installer

```bash
cd proxsync/deploy/host
./install-agent.sh --dashboard-ip <LXC_IP> --dump-root /mnt/backup-hdd/dump
```

- [ ] Installer finished without error
- [ ] Read the HMAC secret locally from `/etc/proxsync-agent/agent.env` as root; it is not
      printed by the installer
- [ ] Copied `ca.crt`, `dashboard.crt`, and `dashboard.key` to the dashboard
- **Verify:** `systemctl is-active proxsync-agent` → `active`
- **Verify:** `systemctl is-enabled proxsync-firewall.service` → `enabled`
- **Verify:** `nft list table inet proxsync` names only the intended dashboard IP and agent port
- **Verify:** `curl --cacert /etc/proxsync-agent/tls/ca.crt https://<HOST_IP>:8765/health`
      returns a health JSON

### 1.3 Create the read-only Proxmox token

```bash
pveum user add proxsync@pve
pveum acl modify / --user proxsync@pve --role PVEAuditor
pveum user token add proxsync@pve dashboard --privsep 0
```

- [ ] **Saved** the printed token id (`proxsync@pve!dashboard`) and secret
- **Verify:** `pveum user token list proxsync@pve` shows the `dashboard` token

---

## Phase 2 — Install the Dashboard (in the LXC)

### 2.1 Install OS prerequisites

```bash
apt update
apt install -y python3 python3-venv nodejs npm nginx sqlite3 openssl
```

- **Verify:** `node -v` ≥ v20, `nginx -v` works, `python3 --version` = 3.13.x

### 2.2 Run the installer

```bash
cd proxsync/deploy/lxc
./install.sh --server-name <SERVER_NAME> --agent-ip <HOST_IP>
```

- [ ] Installer finished without error (it builds the frontend — takes a minute)
- [ ] **Saved** the printed first-run **admin password**
- **Verify:** `systemctl is-active proxsync-api proxsync-web` → both `active`
- **Verify:** `nginx -t` → syntax OK

---

## Phase 3 — Introduce the two components

### 3.1 Copy the agent's client certificates into the LXC

- [ ] Use `scp` to copy the `--export-dashboard-bundle` output from the host to the dashboard container (`/etc/proxsync/proxsync-bundle/`).
- [ ] Move `ca.crt`, `dashboard.crt`, and `dashboard.key` from the bundle to `/etc/proxsync/`, renaming them to `agent-ca.crt`, `agent-client.crt`, and `agent-client.key` respectively.
- [ ] Set permissions: `chown root:proxsync /etc/proxsync/agent-*.crt /etc/proxsync/agent-client.key` and `chmod 0640 /etc/proxsync/agent-client.key`.
- [ ] Get the HMAC secret from `/etc/proxsync/proxsync-bundle/env.fragment`; set `PROXSYNC_AGENT_HMAC_SECRET` in `/etc/proxsync/api.env`.
- [ ] Set `PROXSYNC_PROXMOX_TOKEN_ID` and `PROXSYNC_PROXMOX_TOKEN_SECRET` in `/etc/proxsync/api.env`.
- [ ] Delete the bundle directory from both the host and the container.

- [ ] Set the three values above
- [ ] Restart: `systemctl restart proxsync-api`

---

## Phase 4 — Verify end to end

### 4.1 Health probe

```bash
curl -sk https://<SERVER_NAME>/api/v1/health/detail | python3 -m json.tool
```

- [ ] `agent` reports **`ok`** (mTLS + HMAC working)
- [ ] `database` reports **`ok`**
- [ ] `proxmox` reports **`ok`** (PVEAuditor token working)

### 4.2 Log in

- [ ] Open `https://<SERVER_NAME>/` (accept the self-signed cert warning for now)
- [ ] Log in with `admin` + the printed password
- [ ] Change the password when prompted
- [ ] Clear the bootstrap password:
      `sed -i 's/^PROXSYNC_BOOTSTRAP_ADMIN_PASSWORD=.*/PROXSYNC_BOOTSTRAP_ADMIN_PASSWORD=/' /etc/proxsync/api.env`

### 4.3 Inventory

- [ ] Guests from the host appear in the dashboard (read via the PVEAuditor token)
- [ ] New guests are **off for backup by default** (expected — you enable them explicitly)

---

## Phase 5 — First real operations (the M9 acceptance test)

Do these deliberately, one at a time, watching the logs
(`journalctl -u proxsync-api -f` and `journalctl -u proxsync-agent -f`):

- [ ] **One manual backup** of a small guest → completes, artifact appears in history
- [ ] **One restore** to a *new* VMID through the two-phase wizard → completes
- [ ] **One cancel** mid-backup → recorded as `cancelled`, not `failed`
- [ ] *(if Drive configured)* **One upload** → `/browser/compare` shows `in_sync`
- [ ] **One Telegram test** (Settings → Notifications → Test) → message arrives

---

## Phase 6 — Hardening & housekeeping

- [ ] Replace the self-signed cert with a real one
      (drop PEMs at `/etc/proxsync/tls/web.{crt,key}` and `systemctl reload nginx`,
      or use certbot — see [INSTALL.md](INSTALL.md) §7)
- [ ] Confirm the daily DB self-backup timer is enabled:
      `systemctl status proxsync-db-backup.timer`
- [ ] Take one manual DB backup and confirm it verifies:
      `/opt/proxsync/scripts/proxsync-db-backup.sh`
- [ ] *(optional)* Configure rclone on the host for Google Drive
      (`rclone config`, then set `PROXSYNC_AGENT_ALLOWED_REMOTES=gdrive` and restart the agent)
- [ ] Note the upgrade path for later: `/opt/proxsync/scripts/proxsync-upgrade.sh`
      (see [UPGRADE.md](UPGRADE.md))

---

## If something breaks

Start from [TROUBLESHOOTING.md](TROUBLESHOOTING.md). The commands that answer most questions:

```bash
curl -sk https://<SERVER_NAME>/api/v1/health/detail | python3 -m json.tool
journalctl -u proxsync-api -u proxsync-web -f     # in the LXC
journalctl -u proxsync-agent -f                    # on the host
```

Most first-install failures are one of:

- **`agent` not `ok`** → managed firewall/application allow-list, client cert path/perms, HMAC mismatch, or
  clock skew between host and LXC (see TROUBLESHOOTING.md).
- **API won't start** → a missing/invalid value in `/etc/proxsync/api.env`
  (`PROXSYNC_SECRET_KEY` too short, wrong `DATABASE_URL` driver).
- **Frontend build failed** → Node < 20, or the LXC ran out of RAM mid-build.
