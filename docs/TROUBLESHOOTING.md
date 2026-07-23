# ProxSync — Troubleshooting

Start here when something is degraded. The single most useful command is the detailed health
probe, which reports each dependency independently:

```bash
curl -sk https://<server-name>/api/v1/health/detail | python3 -m json.tool
```

and the two service journals:

```bash
journalctl -u proxsync-api -u proxsync-web -f      # dashboard (LXC)
journalctl -u proxsync-agent -f                     # agent (Proxmox host)
```

---

## The dashboard won't start

**`Configuration error: ...` in the API journal.** The API refuses to start with an invalid
config rather than running half-configured. Common causes:

- `PROXSYNC_SECRET_KEY` shorter than 32 characters — generate one with `openssl rand -hex 32`.
- `PROXSYNC_DATABASE_URL` naming a sync driver — it must be `sqlite+aiosqlite://` or
  `postgresql+asyncpg://`, not `sqlite://`/`postgresql://`.

**The service starts then exits.** Check the journal for a traceback. A migration that has not
been applied (after a manual code update) shows as a schema error — run
`cd /opt/proxsync/backend && venv/bin/alembic upgrade head`.

---

## `agent` reports not `ok` in /health/detail

The dashboard talks to the agent over mutual TLS **and** an HMAC signature; either failing
shows the agent as unreachable. Work through:

1. **Network / firewall.** Check `systemctl status proxsync-firewall.service` and
   `nft list table inet proxsync`, then test from the container. If its address changed, rerun
   `install-agent.sh --dashboard-ip <new-address>`; do not hand-edit only `agent.env`, because
   the application allow-list and managed nftables rule must change together.
2. **Client certificate.** The three files must be present in the container and referenced by
   `PROXSYNC_AGENT_CA_CERT` / `_CLIENT_CERT` / `_CLIENT_KEY`, with the key readable by the
   `proxsync` user (mode 0640, group `proxsync`).
3. **HMAC secret mismatch.** `PROXSYNC_AGENT_HMAC_SECRET` in the container must equal
   `PROXSYNC_AGENT_HMAC_SECRET` on the host **exactly**. A signature failure is logged on the
   agent as a rejected request.
4. **Clock skew.** The agent rejects requests outside its signature window (default 60s). If
   the host and container clocks differ by more than that, sync them (`timedatectl`,
   or an NTP client).

---

## Backups

**A backup is stuck in `interrupted`.** This is by design when the agent's response was lost:
the `vzdump` may have succeeded or failed, and ProxSync will not guess. Check the agent's task
log (`/var/log/proxsync-agent/tasks/<id>.log` on the host), confirm the artifact, and either
keep it or delete and re-run. `interrupted` is **never** retried automatically, precisely
because `vzdump` is not idempotent.

**A backup fails immediately with a storage or VMID error.** The agent validated the request
against the live host and refused it: the storage is not in `pvesm status`, or the VMID is not
an existing guest / not on the allow-list. The message names which.

**A scheduled backup ran on the wrong day — it didn't.** Weekly schedules were a real bug once
(crontab counts weekdays from Sunday, APScheduler from Monday) and are now translated
correctly. If a schedule looks wrong, check its `next_run_at` on the Schedules page; the cron
translation lives in `backend/app/core/cron.py`.

---

## Google Drive sync

**Uploads fail with a remote error.** Confirm the remote name is in
`PROXSYNC_AGENT_ALLOWED_REMOTES` and exists in the host's `rclone.conf`
(`rclone listremotes`). A remote that is not allow-listed is refused before rclone runs.

**`/browser/compare` shows zero counts.** A zero here can mean *the remote couldn't be listed*,
not *nothing is there* — the response carries a `detail` explaining which. Check the agent can
reach Drive (`rclone about gdrive:`).

**Verification says `hash_unavailable`.** Google Drive publishes an MD5, not SHA-256; where a
remote publishes no hash at all, ProxSync reports `hash_unavailable` rather than claiming the
file is verified. This is not an error.

---

## Restores

**“Confirmation expired.”** The restore token has a five-minute TTL by design — it turns “I
clicked restore” into “I meant this guest, now.” Start the restore again; the preflight re-runs
against the live host.

**Confirm returns 409 with a fresh report.** The host changed between preflight and confirm
(the VMID is now taken, free space dropped, the guest is running). The report you agreed to is
never the one acted on; read the new one and resolve what it flags.

---

## Notifications

**No Telegram messages.** Use the built-in test: `POST /api/v1/notifications/telegram/test`
(there is a button on the Notifications settings page). It sends synchronously and returns
Telegram's own error verbatim. `400 chat not found` or a missing bot token are terminal and
won't be retried; a 429/5xx/network error is retried with backoff.

**A message says a run “started” twice — it won't.** One-per-occurrence events are keyed
uniquely, so a resumed run after a restart does not announce a second start.

---

## Logs

**`/logs` looks empty for a busy period.** Check the `dropped` count reported by `/logs` and
`/health/detail`. Under extreme load the bounded log buffer drops its oldest entries (a backup
must never block on logging) and counts them — an empty page with a non-zero drop count is a
load signal, not a quiet night.

---

## Database self-backup

**The daily backup didn't run.** `systemctl status proxsync-db-backup.timer` shows the next
firing; `journalctl -u proxsync-db-backup.service` shows the last run. A non-zero exit means
the dump failed its integrity check and was discarded — investigate disk space and permissions
on `/var/backups/proxsync`.

---

## When all else fails

Collect the version and the health detail, and the relevant journal window, and open an issue:

```bash
curl -sk https://<server-name>/api/v1/health/detail
journalctl -u proxsync-api --since '10 min ago' --no-pager
```
