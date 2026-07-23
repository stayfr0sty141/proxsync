# ProxSync — API Design

Two separate APIs: the **Dashboard API** (consumed by the browser) and the **Agent API**
(consumed only by the dashboard). They share nothing but a correlation id.

---

# Part 1 — Dashboard API

Base path `/api/v1`. JSON only. OpenAPI served at `/api/v1/openapi.json`
(behind auth in production).

## Conventions

**Errors** — RFC 9457 problem details:

```json
{
  "type": "https://proxsync.dev/errors/validation-failed",
  "title": "Validation failed",
  "status": 422,
  "detail": "vmid 999 is not in the backup allow-list",
  "instance": "/api/v1/backups/run",
  "correlation_id": "3f1c…",
  "errors": [{ "field": "targets.0.vmid", "message": "not allow-listed" }]
}
```

**Pagination** — `?page=1&page_size=25&sort=-started_at`, response envelope:

```json
{ "items": [...], "page": 1, "page_size": 25, "total": 143, "pages": 6 }
```

**Auth** — `Authorization: Bearer <access_token>` (15 min). Refresh token lives in an
`HttpOnly; Secure; SameSite=Strict` cookie and is rotated on every use; reuse of a rotated
token revokes the family. Cookie-authenticated mutations additionally require the
`X-CSRF-Token` header matching the `csrf_token` cookie.

**Roles** — `A` admin, `O` operator, `V` viewer. Every endpoint below is annotated.

**Idempotency** — mutating endpoints that spawn work (`/backups/run`, `/restores`,
`/sync/upload`) accept `Idempotency-Key`; a repeat within 24 h returns the original response.

## 1. Auth

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/auth/login` | — | Rate-limited 5/15 min per IP+username, exponential lockout. Returns access token + sets refresh & CSRF cookies |
| POST | `/auth/refresh` | — | Rotates the refresh token |
| POST | `/auth/logout` | V | Revokes the current token family |
| GET | `/auth/me` | V | Current user + role + permissions |
| POST | `/auth/change-password` | V | Requires current password; revokes all other sessions |
| GET | `/auth/sessions` · DELETE `/auth/sessions/{id}` | V | Active refresh-token families |

## 2. Dashboard

`GET /dashboard/summary` (V) — single call backing the whole landing page:

```json
{
  "guests": { "vm_total": 6, "vm_running": 5, "lxc_total": 11, "lxc_running": 10 },
  "last_backup": { "id": 812, "finished_at": "2026-07-19T18:03:11Z", "status": "success",
                   "guest_count": 17, "duration_seconds": 4127, "size_bytes": 96833011712 },
  "next_backup": { "job_id": 1, "name": "Weekly Full", "next_run_at": "2026-07-26T18:00:00Z",
                   "cron": "0 1 * * 0", "timezone": "Asia/Jakarta" },
  "storage": { "local": { "total_bytes": 500107862016, "used_bytes": 318221926400,
                          "free_bytes": 181885935616, "used_percent": 63.6 },
               "gdrive": { "used_bytes": 214748364800, "quota_bytes": 2199023255552,
                           "used_percent": 9.8 } },
  "jobs": { "running": 1, "queued": 0, "failed_24h": 0, "failed_7d": 2 },
  "agent": { "reachable": true, "version": "0.1.0", "latency_ms": 12 }
}
```

`GET /dashboard/activity?limit=20` (V) — merged recent backup/restore/sync events.

## 3. Guests

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/guests` | V | `?type=vm|lxc&status=&search=&backup_enabled=` |
| POST | `/guests/refresh` | O | Force a PVE inventory sync |
| PATCH | `/guests/{id}` | A | Toggle `backup_enabled` (the allow-list) |

## 4. Backup jobs (schedules)

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/backup-jobs` | V | |
| POST | `/backup-jobs` | A | Cron validated server-side; `next_run_at` returned |
| GET/PUT/DELETE | `/backup-jobs/{id}` | V/A/A | |
| POST | `/backup-jobs/{id}/enable` · `/disable` | A | |
| POST | `/backup-jobs/{id}/run` | O | Run now, out of band |
| GET | `/backup-jobs/{id}/preview` | V | Next 5 fire times in the job's timezone + resolved target list, and `skipped` for named targets that would not run |

> **Cron is standard crontab, not APScheduler's dialect.** Day-of-week counts from **Sunday**
> (`0` and `7` both mean Sunday), matching `crontab(5)` and the `0 1 * * 0` in this document.
> APScheduler counts from Monday, so the field is translated before a trigger is built —
> without that, the specified weekly schedule would fire on Mondays.
>
> An expression restricting **both** day-of-month and day-of-week is **rejected** with a 400.
> Vixie cron treats that combination as "either"; APScheduler treats it as "both". Rather
> than silently choosing one, ProxSync refuses it and suggests two schedules.

Create payload:

```json
{
  "name": "Weekly Full",
  "cron_expression": "0 1 * * 0",
  "timezone": "Asia/Jakarta",
  "mode": "snapshot",
  "compression": "zstd",
  "zstd_threads": 4,
  "storage": "backup-hdd",
  "target_selector": "all",
  "targets": [],
  "keep_local": 2,
  "keep_remote": 2,
  "upload_enabled": true,
  "notify_on_start": true,
  "notify_on_success": true,
  "notify_on_failure": true,
  "bwlimit_kbps": 0
}
```

## 5. Manual backup & history

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/backups/run` | O | **Backup Now** — `{ targets: [{vmid, guest_type}], mode, compression, storage, upload }` |
| GET | `/backups` | V | Filters: `vmid, guest_type, status, upload_status, from, to, search`; sortable |
| GET | `/backups/{id}` | V | Full detail incl. checksum and remote state |
| DELETE | `/backups/{id}` | A | `?scope=local|remote|both`; refuses if a restore references it |
| GET | `/backups/{id}/log` | V | Agent task log, `?tail=500` or full |
| GET | `/backups/{id}/download` | O | Streamed via the agent, `Content-Disposition: attachment`; range requests supported |
| POST | `/backups/{id}/upload` | O | Manual upload / retry; `{ "force": false }`. Returns 202 and the queued `sync_task` |
| POST | `/backups/{id}/verify` | O | Compare local ↔ remote. **Writes**: a copy found missing or corrupt clears the row's `uploaded` state, because retention reads it |
| PATCH | `/backups/{id}/lock` | A | `{ "retention_locked": true }` |
| GET | `/runs` · `/runs/{id}` | V | Run-level grouping; `/runs/{id}` carries live `progress` while a backup is in flight |
| GET | `/runs/{id}/backups` | V | The artifacts one run produced |
| POST | `/runs/{id}/cancel` | O | |

### Retention preview

`POST /retention/preview` (A) evaluates retention against the current database without
changing it. `POST` is used because the settings screen can send proposed, unsaved values:

```json
{
  "keep_local": 3,
  "keep_remote": 1,
  "backup_id": 812
}
```

All three fields are optional. `keep_local` accepts `1..365`, `keep_remote` accepts
`0..365`, and `backup_id` scopes the preview to that backup's `(vmid, guest_type)`; `{}` uses
the currently stored policy and previews every eligible guest. The endpoint never calls the
agent, deletes an artifact, changes a soft-delete marker, or writes a `retention_events` row.
Its `policy.dry_run` is therefore always `true`.

The response contains the complete effective `policy`, one `items` decision per physical
local or remote copy, and this aggregate:

```json
{
  "summary": {
    "evaluated": 12,
    "local_delete_count": 2,
    "local_delete_bytes": 17179869184,
    "remote_delete_count": 1,
    "remote_delete_bytes": 8589934592,
    "deleted_local": 0,
    "deleted_remote": 0,
    "failed": 0
  }
}
```

Preview candidates have `would_delete: true` but `deleted: false`. Each item also carries
its location, proposed action, reason, size, and any blocker.

The retention worker starts automatically with the other background workers. It performs a
full database-derived sweep at startup. After that, successful local-only backups, upload or
verification state changes, pin/unpin, manual copy deletion, and retention/GDrive setting
changes ring an after-commit doorbell; edits/deletion of a still-referenced legacy job do the
same. A notification lost with a process restart is recovered by the next startup sweep.
There is no healthy-state idle sweep that would generate repeated
`retention_events`; failed passes receive a bounded delayed retry.

Retention ranks copies per `(vmid, guest_type)`. Pinned copies are excluded from the rank, so
a retention lock does not consume one of the configured keep slots. A local candidate and
the newest replacement set must each be either `not_required` or `uploaded` with remote
metadata before local deletion is allowed. `pending_confirmation`, `confirmed`, and
`running` restores block deletion at either location. Actual remote objects are ranked
separately; a `not_required` row never displaces a remote copy.

For a job-backed run, `keep_local` and `keep_remote` plus an explicit `retention_source` are
frozen when the run is requested; the policy therefore survives later job edits or deletion.
Manual/API runs use the current global policy, so a global retention-setting change
intentionally re-evaluates those guests. A startup rescan and every queued pass choose the
newest eligible backup for each guest immediately before execution. Pre-M5 rows still linked
to a job use that job's current counts; a source-less run whose provenance can no longer be
proven stops fail-closed and records skipped decisions instead of guessing a destructive
policy.

## 6. Restore (two-phase)

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/restores/preflight` | O | Runs every check and writes nothing. A blocking report is a **200**, not an error |
| POST | `/restores` | O | 201; creates `pending_confirmation` + returns the token once (TTL 5 min) |
| POST | `/restores/{id}/confirm` | O | 202; body must echo `confirmation_token` **and** `target_vmid` |
| POST | `/restores/{id}/cancel` | O | |
| GET | `/restores` | V | `?status=&backup_id=&target_vmid=&limit=&offset=` |
| GET | `/restores/{id}` · `/restores/{id}/log` | V | |

```jsonc
// POST /restores
{
  "backup_id": 812,
  "restore_mode": "new_id",          // original_id | new_id
  "target_vmid": 151,                 // required when new_id
  "target_storage": "local-lvm",      // defaults to the configured default storage
  "target_node": "pve",               // defaults to the node the agent runs on
  "overwrite_existing": false,
  "force_stop": false,
  "start_after_restore": false
}
// 201 →
{
  "id": 44, "status": "pending_confirmation", "source": "local",
  "confirmation_token": "rst_9f2c…", "expires_at": "2026-07-22T09:05:00Z",
  "preflight": {
    "checks": [
      { "name": "backup_restorable",      "ok": true,  "detail": "vzdump-qemu-101-… (vmid 101, vm)" },
      { "name": "backup_present_locally", "ok": true },
      { "name": "checksum_matches",       "ok": true,  "detail": "sha256 4f3a…c1b2 matches" },
      { "name": "target_vmid_free",       "ok": true,  "detail": "VMID 151 is free" },
      { "name": "target_type_matches",    "ok": true },
      { "name": "target_guest_stopped",   "ok": true },
      { "name": "target_node_supported",  "ok": true },
      { "name": "storage_free_space",     "ok": true,  "detail": "412.0 GiB free, 71.0 GiB required on 'local-lvm'" },
      { "name": "no_restore_in_flight",   "ok": true }
    ],
    "blocking": false,
    "warnings": ["This backup was taken on node 'pve2' and will be restored on 'pve'."],
    "source": "local", "backup_id": 812, "size_bytes": 9126805504,
    "target_vmid": 151, "target_type": "vm",
    "target_storage": "local-lvm", "target_node": "pve"
  }
}
```

**A check that could not be evaluated fails.** An unreachable agent, a storage the host does
not report, an inactive storage — each is a blocking `ok: false` with the reason in `detail`.
A restore that cannot be shown to be safe is not one that may proceed, and a report where
"could not check" looked like "checked, fine" would be worse than no report.

A missing checksum or an unrecorded size is a **warning**, not a refusal: a `vzdump` whose
digest could not be computed still produced a real backup, and refusing to restore it would
leave an operator holding a usable archive they are not allowed to use. Where a digest *is*
recorded it is passed to the agent, which verifies it before spawning anything.

`storage_free_space` requires `size_bytes × 1.15` — the archive is compressed, and the
restored guest needs both the difference and room to write.

**Confirmation re-runs every check.** The report in the dialog can be five minutes old: the
archive may have been deleted, the target started, another restore authorised. If anything now
blocks, confirmation returns **409** with the fresh report and the restore stays pending. The
token is stored only as a SHA-256 and is cleared on use, so it cannot be replayed.

`no_restore_in_flight` fails on a `confirmed` or `running` restore, not on another
`pending_confirmation` one — a proposal blocks retention from deleting its source, but it does
not stop a second operator from preflighting. Only one restore can ever be authorised at a
time, and the agent holds a single restore slot besides.

**Source selection.** The archive is looked for on the host, not in ProxSync's history: the
two can differ in both directions. If it is absent locally but a verified Drive copy exists,
`source` is `gdrive` and the executor downloads it before restoring, in the same task. If it
is on neither, the restore is refused.

**States.** `pending_confirmation → confirmed → running → success | failed | cancelled |
interrupted`, plus `expired` for a window nobody confirmed. `interrupted` means the outcome is
unknown — the dashboard restarted after the agent was asked, or the agent stopped answering
mid-restore. A restore is **never** retried or re-issued automatically; a second `qmrestore`
over a running one is the worst outcome available. Cancelling a `running` restore asks the
agent to stop it; the row becomes terminal when the agent reports back, because only the agent
knows whether the process died before or after it began rewriting the guest.

## 7. Google Drive sync

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/sync/status` | V | Queue depth, the transfer in flight, and how many backups await one |
| GET | `/sync/tasks` | V | Recent transfers with attempt counts and `next_retry_at` |
| GET | `/sync/quota` | V | Remote quota. Never 5xx: an unreachable remote returns `configured: true` with a `detail` |
| GET | `/sync/status` | V | Queue depth, in-flight transfers, last success/failure, remote quota |
| GET | `/sync/tasks` | V | Paginated transfer history |
| POST | `/sync/upload` | O | `{ backup_ids: [...] }` or `{ all_pending: true }` |
| POST | `/sync/download` | O | `{ backup_id }` — pulls a Drive-only artifact back to local |
| POST | `/sync/verify` | O | `{ backup_ids }` — size + checksum comparison |
| POST | `/sync/tasks/{id}/cancel` · `/retry` | O | |
| POST | `/sync/test-connection` | A | Validates remote name and credentials via the agent |

## 8. Backup browser

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/browser/local` | V | Artifacts on the HDD, read from the agent — including ones ProxSync did not create, because they occupy real space |
| GET | `/browser/remote` | V | Artifacts on the remote |
| GET | `/browser/compare` | V | `in_sync` / `local_only` / `remote_only` / `size_mismatch`. When the remote cannot be listed the counts are zero and `detail` says why — a partial answer must never read as "everything is in sync" |
| GET | `/browser/local?path=` | V | Path is validated against the storage root; returns name, size, mtime, checksum (if known), matching `backup_id` |
| GET | `/browser/remote?path=` | V | Same shape from the rclone remote |
| GET | `/browser/compare` | V | Local-only / remote-only / both / mismatched checksum |

## 9. Storage

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/storage` | V | Live local + remote usage, per-storage breakdown |
| GET | `/storage/history?days=30` | V | `storage_snapshots` series for the chart |
| GET | `/storage/forecast` | V | Estimated days until full, based on the trailing 30-day slope |
| GET | `/storage/by-guest` | V | Space consumed per guest — drives the "biggest offenders" table |

`GET /storage` queries local usage and remote quota independently. If either upstream is
unavailable, that half is returned as `null`/nullable fields plus a diagnostic `detail`; an
unavailable measurement is never presented as zero and never erases the valid half.

The sampler runs once at worker startup and then every 15 minutes by default
(`PROXSYNC_STORAGE_SAMPLE_INTERVAL_SECONDS`). If local sampling fails it writes no snapshot.
If only remote quota fails it persists the local sample with nullable remote fields. Each
committed sample emits `storage.update`.

Local utilisation is the effective writable-space percentage
`clamp((total_bytes - free_bytes) / total_bytes × 100, 0, 100)`, rather than the agent's
reported `used / total`; this accounts for filesystem-reserved blocks. The current retention
settings classify it as `healthy`, `warning`, or `critical` (defaults 85% and 95%;
unmeasurable is `unknown`). The event's alert flag is set only on an upward transition into
warning or critical, so a disk that remains critical does not alert every 15 minutes.

`GET /storage/history` accepts `days=1..365`. `GET /storage/forecast` always uses the
trailing 30 days and ordinary least squares over local used bytes versus elapsed days; it
projects from the latest free-space measurement and returns a truthful `detail` with no
full-date estimate when samples are insufficient, timestamps are not distinct, or usage is
stable/declining. `GET /storage/by-guest` sums successful, undeleted local backups with known
sizes by `(vmid, guest_type)` and uses the newest eligible backup's name, so VM/LXC identity
and guest renames are handled deterministically.

## 10. Notifications

| Method | Path | Role | Description |
|---|---|---|---|
| GET/PUT | `/settings/telegram` | A | Bot token write-only; reads return `"configured": true` and a masked hint |
| POST | `/notifications/telegram/test` | A | Sends a test message now and returns the Telegram API result verbatim |
| GET | `/notifications` | V | Outbox; `?status=&event_type=&channel=&from=&to=&limit=&offset=` |
| POST | `/notifications/{id}/resend` | A | 202; requeues a `failed`, `suppressed` or already-`sent` message |

### The outbox

Every notification row is written **in the same transaction as the state change it
describes**, then delivered by a worker. A Telegram outage delays messages and never loses
one, and a backup never waits on a message. `GET /notifications` returns the rendered text as
it was frozen at enqueue time, alongside `attempts`, `next_attempt_at`, `error_message`, and a
top-level `pending` count — a number that stays high is an outage, not a backlog.

Delivery is **at-least-once**. The attempt is charged before the request is sent, so a process
that dies mid-send leaves a message with a scheduled retry rather than one stuck claiming an
attempt that never happened. A message received twice is an annoyance; a failure nobody is
told about is the thing this feature exists to prevent.

**Not every failure is retried.** The client classifies Telegram's answer: a transport error,
a 429 or a 5xx goes back on the queue with exponential backoff and full jitter (and honours
Telegram's own `parameters.retry_after` when it sends one), while `400 chat not found` or
`401 Unauthorized` is terminal on the first attempt. Missing configuration is terminal too —
retrying cannot save a message when no bot token is stored, and `resend` is the way back once
one is.

### Events and de-duplication

| Event | Fires | Key |
|---|---|---|
| `backup_started` | once per run, when it is claimed | `run:{id}:started` |
| `backup_success` | once per run, all guests succeeded | `run:{id}:backup_success` |
| `backup_failed` | once per run, any guest failed — a *partial* run reports here, naming the guests that failed | `run:{id}:backup_failed` |
| `restore_started` | once per restore, when it becomes `running` | `restore:{id}:started` |
| `restore_finished` | once per restore, on success; carries any agent warning | `restore:{id}:restore_finished` |
| `restore_failed` | once per restore, on `failed` or `interrupted` — the message says which | `restore:{id}:restore_failed` |
| `upload_failed` | once per transfer, only after its retries are spent | `sync_task:{id}` |
| `retention_deleted` | per applying pass that removed something | `retention:{type}:{vmid}` |
| `storage_threshold` | on an upward transition into warning or critical | `storage:{severity}` |
| `test` | on request | — |

The first seven are **unique**: a second enqueue for the same key writes nothing, whatever the
clock says, so "exactly once" survives a restart because the guarantee is a row rather than a
timer. The last two genuinely recur and are instead suppressed for
`notification_suppress_window_seconds` (default 300); the repeat is *recorded* as `suppressed`
with `suppressed_by` naming the message it repeats, because "we decided not to tell you at
03:14" is itself information an operator may need. A predecessor that `failed` does not
suppress anything — it told nobody anything.

A **cancelled** backup or restore produces no notification. The operator who stopped it
already knows, and an alert for something they did themselves is the fastest way to teach
them to ignore alerts.

Each event has a per-event toggle in `/settings/telegram` (`notify_backup_started`, …). A
disabled event writes no row at all: an outbox entry for something the operator asked never to
hear about would be a queue that never drains.

### `POST /notifications/telegram/test`

```json
{ "chat_id": "-1001234567890" }
```

`chat_id` is optional and overrides the configured one, so a new chat can be verified before
it replaces a working one. The message is sent **synchronously** — "it will be attempted
shortly" is not an answer to "is this token right?" — and works even while `enabled` is false,
because you configure a token, test it, then switch the feature on.

A rejected message is a **200** carrying Telegram's own words:

```json
{
  "ok": false,
  "detail": "Telegram returned 401 (401): Unauthorized",
  "error_code": 401,
  "description": "Unauthorized",
  "notification_id": 44
}
```

The HTTP request succeeded; the answer is "no, and here is exactly why". Turning that into a
problem response would hide the one field worth reading. A genuine request error — no token
saved, no chat id — is still a 400. Either way the attempt is recorded in the outbox as a
`test` row with its real outcome.

## 11. Settings

`GET /settings` (A) returns all sections; `GET/PUT /settings/{section}` for
`general | gdrive | telegram | retention | agent | proxmox`. Secrets are write-only.
`POST /settings/agent/test` and `POST /settings/proxmox/test` validate connectivity before save.

## 12. Logs

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/logs` | V | `?category=&level=&from=&to=&search=&correlation_id=&backup_id=&restore_id=&limit=&offset=` |
| GET | `/logs/export` | A | `?format=ndjson\|csv` plus the same filters; streamed as an attachment |
| GET | `/audit` | A | `?action=&result=&user_id=&from=&to=&search=&correlation_id=&limit=&offset=` |

Categories are `api backup restore upload retention scheduler auth agent notify system`;
levels are `debug info warning error critical`. `search` matches the log **message** only —
`context` is JSON on SQLite and JSONB on PostgreSQL, and a LIKE over serialised JSON matches
key *names* as readily as values, so a search for "error" would return every row that merely
has an error field. On `/audit`, `search` matches the username, action, and resource.

Filtering by `correlation_id` is the point of the page: one id ties the browser request, the
dashboard's decisions and the agent's task together, so it is the shortest path from "the
Sunday job failed" to the exact `vzdump` stderr line.

`GET /logs` also returns `persisted` and `dropped`. `persisted: false` means log persistence
is switched off, so an empty list reads as "not recorded" rather than "nothing happened";
`dropped` is non-zero when the capture buffer overflowed and the page is therefore incomplete.

**How logging reaches the table.** A structlog call appends to a bounded in-memory buffer and
returns — it never opens a database session, because the call site is frequently already
inside a transaction, and a synchronous write there would deadlock on SQLite's single writer
or recurse through SQLAlchemy's own logging. A worker drains the buffer in batches. When the
buffer is full the *oldest* entry is discarded and counted: losing the start of an incident is
bad, but wedging a backup because logging is behind is worse. A batch the database rejects is
put back at the front rather than dropped, and a line naming a row that no longer exists is
written with its foreign keys cleared (the ids stay readable in `context`) rather than
poisoning every batch behind it.

Rows are pruned against `general.log_retention_days` (default 90) and
`general.audit_retention_days` (default 730). The two ages differ on purpose: application logs
are diagnostic, the audit trail is evidence.

`/logs/export` streams the whole filtered set newest-first, keyset-paginated rather than by
offset — an export runs while the sink is still writing, and an offset would skip or repeat
rows as the set shifts underneath it. CSV always carries its header row, including when the
result is empty, so the file opens as a table rather than as nothing at all.

## 13. System & live updates

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/health` | — | Liveness; no auth, no detail |
| GET | `/health/detail` | A | DB, agent, PVE, and runner/scheduler/sync/retention/storage/notification/log-writer state; sampler errors, stale samples, failing delivery and dropped log entries degrade health |
| GET | `/events/stream` | V | **SSE**: `stream.open`, `backup.progress`, `backup.state`, `run.state`, `job.state`, `inventory.updated`, sync events, `storage.update`, `restore.state`, `restore.progress` and `notification.state` |

`EventSource` cannot set an `Authorization` header, so the access token is passed as
`?token=`. It is the same short-lived JWT used everywhere else, and the request middleware
logs paths rather than query strings. Each subscriber gets a bounded queue: a client that
stops reading loses its oldest events rather than stalling the backup that produced them.

SSE frame:

```
event: backup.progress
data: {"backup_id":812,"vmid":101,"percent":42,"bytes":12884901888,"eta_seconds":310,"status":"running"}
```

---

# Part 2 — Agent API

Runs on the Proxmox host. Reachable only from the dashboard LXC address.
Auth on **every** request: mTLS client certificate **plus** headers
`X-ProxSync-Key`, `X-ProxSync-Timestamp`, `X-ProxSync-Nonce`, `X-ProxSync-Signature`
where the signature is `HMAC-SHA256(secret, "{method}|{path}|{timestamp}|{nonce}|{sha256(body)}")`.
Timestamps outside ±60 s are rejected; nonces are cached for the window.

## Core endpoints (as specified in the brief)

### `POST /backup/start`

```jsonc
{ "vmid": 101, "guest_type": "vm", "mode": "snapshot", "compression": "zstd",
  "zstd_threads": 4, "storage": "backup-hdd", "notes": "proxsync run 812",
  "bwlimit_kbps": 0, "correlation_id": "3f1c…" }
// 202 → { "task_id": "9a7e…", "kind": "backup", "state": "queued",
//         "created_at": "…", "correlation_id": "3f1c…" }
```

> **`zstd_threads`, not `compression_level`.** vzdump exposes `--compress {0,gzip,lzo,zstd}`
> and, for zstd, `--zstd <thread count>` (0 = half the host's cores). There is no
> compression-level knob in PVE, so the agent does not offer one.

Validation: `vmid` ∈ live PVE inventory ∩ allow-list · `guest_type` matches the actual guest ·
`mode`/`compression` ∈ enum · `storage` ∈ `pvesm status` · concurrency slot free.
Executes `["vzdump", "101", "--mode", "snapshot", "--compress", "zstd", "--storage",
"backup-hdd", ...]` — an argv list, never a string.

### `POST /restore/vm`

```jsonc
{ "archive": "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst",
  "target_vmid": 151, "storage": "local-lvm",
  "overwrite": false, "force_stop": false, "start_after": false,
  "expected_sha256": "4f3a…c1b2", "bwlimit_kbps": 0, "correlation_id": "…" }
// 202 → { "task_id": "…", "kind": "restore_vm", "state": "queued", "created_at": "…" }
```

`expected_sha256` is optional; when present the agent verifies the artifact's digest and
refuses the restore on a mismatch, before any process is spawned.

`archive` is a **basename only** — no separators, resolved against the dump root and
re-checked for containment after `realpath`. Refuses when the target exists and `overwrite`
is false, or when the target is running and `force_stop` is false.
Executes `qmrestore`.

### `POST /restore/lxc`

Same shape; executes `pct restore <vmid> <archive> --storage <storage>`.
Additional guard: LXC restore to an existing unprivileged container requires `overwrite`.

### `GET /backup/list`

`?storage=backup-hdd&vmid=&guest_type=` →

```json
[{ "filename": "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst",
   "path": "/mnt/backup-hdd/dump/vzdump-qemu-101-2026_07_19-01_00_04.vma.zst",
   "vmid": 101, "guest_type": "vm", "size_bytes": 8589934592,
   "created_at": "2026-07-19T01:00:04+07:00", "checksum_sha256": "…",
   "notes": "proxsync run 812" }]
```

Checksums are computed lazily and cached in a sidecar `.sha256` file next to the artifact.

### `GET /task/{id}`

```json
{ "task_id": "9a7e…", "kind": "backup", "state": "running",
  "exit_code": null, "started_at": "…", "finished_at": null, "duration_seconds": 312.4,
  "progress": { "percent": 42, "bytes_done": 12884901888, "bytes_total": 30601641984,
                "rate_bps": 41943040, "eta_seconds": 310, "message": "writing archive" },
  "result": {}, "meta": { "vmid": 101, "guest_type": "vm", "storage": "backup-hdd" },
  "log_path": "/var/log/proxsync-agent/tasks/9a7e….log",
  "correlation_id": "3f1c…", "error": null }
```

States: `queued → running → success | failed | cancelled | interrupted`. **`interrupted`**
means the agent restarted while the task was running; systemd kills the whole control group,
so the child did not survive and the outcome is unknown. The dashboard must surface that
rather than record a completed backup.

On success, `result` carries what the dashboard records: `filename`, `path`, `size_bytes`,
`checksum_sha256`, `duration_seconds`, and a `warnings` array when something was odd but not
fatal (e.g. the archive landed outside the configured dump root).

There is **no `upid`**: the agent invokes `vzdump` as a CLI process, and PVE only mints a UPID
for API-initiated tasks. The agent's own `task_id` is the correlation handle.

`GET /task/{id}/log?tail=500&follow=false` streams the task log (SSE when `follow=true`).
`POST /task/{id}/cancel` sends SIGTERM, then SIGKILL after a grace period.

### `DELETE /backup/{id}`

`{id}` is the artifact basename. Deletes the artifact plus its `.notes`, `.log` and `.sha256`
sidecars. The filename must match the vzdump pattern
`vzdump-(qemu|lxc)-\d+-\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2}\.(vma|tar)(\.(zst|gz|lzo))?` and
resolve inside the dump root. Anything else is a 400 — never a deletion.

## Extension endpoints (decision D1/D2)

Required by features 5, 9 and 12; each is typed and validated exactly like the core set.

| Method | Path | Purpose |
|---|---|---|
| POST | `/sync/upload` | `{ filename, remote, remote_path, bwlimit_kbps, transfers, retries }` → task id. Executes `rclone copy` with argv list |
| POST | `/sync/download` | Pull an artifact back from the remote |
| POST | `/sync/verify` | `rclone check` / `hashsum` → per-file match report |
| GET | `/sync/list` | Remote listing via `rclone lsjson` |
| GET | `/sync/about` | Remote quota via `rclone about --json` |
| GET | `/storage/status` | `pvesm status` + `statvfs` on the backup root |
| GET | `/backup/{id}/stream` | Byte-range artifact stream backing the dashboard download |
| GET | `/health` | Version, uptime, concurrency, rclone/vzdump availability |
| GET | `/inventory` | Guest list — **only if D2 chooses the agent over the PVE API** |

## Agent status codes

| Code | Meaning |
|---|---|
| 202 | Task accepted, `task_id` returned |
| 400 | Validation failure (bad vmid, mode, path, filename pattern) |
| 401 | Signature/certificate/clock failure |
| 404 | Unknown task or artifact |
| 409 | Concurrency conflict — a backup or restore already holds the slot |
| 423 | Target guest locked by another PVE task |
| 507 | Insufficient storage detected in preflight |

---

## Agent sync endpoints (M4)

`POST /sync/upload` · `/sync/download` · `/sync/verify` · `/sync/delete`,
`GET /sync/list` · `/sync/about`.

```jsonc
// POST /sync/upload
{ "filename": "vzdump-qemu-101-2026_07_26-01_00_04.vma.zst",
  "remote": "gdrive", "remote_path": "proxsync/dump",
  "bwlimit_kbps": 0, "transfers": 4, "verify_after": true, "correlation_id": "…" }
// 202 → { "task_id": "…", "kind": "upload", "state": "queued", "created_at": "…" }
```

Validation, in order: the **remote name** must match `\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z`
*and* appear in the agent's `allowed_remotes`; the **remote path** must contain no `..`, no
`:`, no control characters and none of rclone's filter metacharacters `*?[]{}`; the
**filename** must be a well-formed vzdump artifact resolving inside the dump root — on the way
in as well as out, so a tampered remote cannot write outside it.

`POST /sync/verify` is synchronous and returns an `outcome`:

| Outcome | Meaning |
|---|---|
| `match` | Size and MD5 both agree. The only value that counts as verified |
| `size_mismatch` | Different lengths |
| `hash_mismatch` | Same length, different MD5 — the remote copy is corrupt |
| `missing_remote` | Not present at the remote path |
| `hash_unavailable` | Sizes agree but the remote publishes no hash. **Not** verified: a truncated file of exactly the right length would otherwise pass |

MD5 rather than SHA-256 because that is the digest Google Drive stores and will return without
a download; `rclone hashsum sha256` against Drive would fetch the whole artifact back to hash
it locally. The local digest is cached in a `<filename>.md5` sidecar.

`rclone` is always invoked with `--retries 1`. The dashboard owns the retry policy — attempt
counts, jittered backoff and a record of each failure live in `sync_tasks`, where an operator
can see them — and a silent retry loop underneath would make that record a lie.
