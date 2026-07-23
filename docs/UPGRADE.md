# ProxSync — Upgrade Guide

ProxSync upgrades in place, and the order is chosen so that a bad upgrade is always
recoverable: the database is backed up **before** any migration runs, and the running services
are not stopped until the last safe moment.

---

## The easy path

From a fresh checkout (or after `git pull`) of the new version, inside the LXC container:

```bash
cd ProxSync
./scripts/proxsync-upgrade.sh --repo "$(pwd)"
```

The script:

1. Reads the old and new versions and prints the transition.
2. **Backs up the database** with `proxsync-db-backup.sh` — and refuses to continue if that
   fails (override only with `--no-backup`, which you should not).
3. Syncs backend code and scripts into `/opt/proxsync`, preserving the virtualenv.
4. Reinstalls backend dependencies and **rebuilds the frontend** into its standalone form.
5. Stops `proxsync-web` and `proxsync-api`, runs `alembic upgrade head`, refreshes the systemd
   units, and starts the services again.
6. Confirms both services came back up, dumping recent logs and failing loudly if not.

If step 5's migration fails, the services are left stopped and the pre-upgrade backup is on
disk — restore it (see below) before retrying.

---

## Upgrading against a tracked git checkout

If `/opt/proxsync/src` is a git checkout, the script can fetch and check out a ref for you:

```bash
./scripts/proxsync-upgrade.sh --repo /opt/proxsync/src --ref v0.2.0
```

---

## Database backups

A daily self-backup runs via `proxsync-db-backup.timer` (02:15, catch-up on boot). To take one
by hand, or before a risky change:

```bash
/opt/proxsync/scripts/proxsync-db-backup.sh --out-dir /var/backups/proxsync --keep 14
```

Backups are integrity-checked before they count: a SQLite copy that fails
`PRAGMA integrity_check` is discarded rather than kept as a false comfort.

### Restoring

Restoring is destructive and asks for confirmation. For SQLite the current database is moved
aside (not deleted) first, so a mistaken restore is itself reversible.

```bash
/opt/proxsync/scripts/proxsync-db-restore.sh \
    --from /var/backups/proxsync/proxsync-20260723-021500.sqlite.gz
```

The restore stops the API, swaps the database in, runs `alembic upgrade head` (so an older dump
is brought to the current schema), and restarts the API.

---

## Rotating the root secret

`PROXSYNC_SECRET_KEY` derives both the JWT signing key and the settings-encryption key. Rotating
it is deliberately consequential:

- **Every session is invalidated** — all users must log in again.
- **Encrypted settings become unreadable.** Connection secrets stored in the settings table
  (the Telegram bot token, for example) are encrypted with a key derived from this value. After
  rotating, re-enter those secrets from the Settings page.

The agent HMAC secret and the Proxmox token are stored in the environment, **not** encrypted
with the root secret, so they survive a rotation untouched.

---

## Downgrading

Alembic migrations are reversible and tested to reverse (CI runs `downgrade base` on SQLite and
PostgreSQL). To move back a version:

```bash
cd /opt/proxsync/backend
venv/bin/alembic downgrade <target-revision>
# then deploy the older code
```

But the honest path for a homelab is usually simpler: restore the pre-upgrade backup, which
captures the schema and the data together at a known-good point.

---

## After any upgrade

```bash
curl -sk https://<server-name>/api/v1/health/detail | python3 -m json.tool
```

Confirm `agent` and `database` both report `ok`, and that the version shown matches what you
installed. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) if anything is degraded.
