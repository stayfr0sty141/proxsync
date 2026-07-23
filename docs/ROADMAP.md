# ProxSync — Development Roadmap

Modules are built and delivered **one at a time**, each ending in a reviewable, runnable
state. Nothing is scaffolded with `TODO` bodies: a module is either complete and tested, or
not started.

## Definition of done (every module)

- Fully typed (`mypy --strict` on backend and agent, `tsc --noEmit` on frontend)
- `ruff` + `ruff format` clean; `eslint` + `prettier` clean
- Unit tests for services and validators; integration tests for routes against a temp SQLite
- Structured logs with a correlation id on every code path
- Errors mapped to RFC 9457 problem responses — no bare 500s
- No `shell=True`, no string-built commands, no secrets in the database in clear text
- Alembic revision included when the schema changes
- `docs/` updated in the same change

---

## M0 — Foundations ✅

Folder structure · architecture · database schema · API contracts · UI design · roadmap.
Decisions **D1–D4** resolved in favour of the recommendations.

---

## M1 — Backup Agent (host) ✅

The privileged component first: nothing else can be tested end-to-end without it.

- ✅ FastAPI app, env-file config, structlog JSON logging, RFC 9457 problem responses
- ✅ mTLS (uvicorn `CERT_REQUIRED`) + HMAC request signing, TTL nonce cache, clock-skew
  rejection, address allow-list applied before authentication
- ✅ Validators: VMID allow-list checked against real guest configs, storage checked against
  `pvesm status`, closed mode/compression enums, vzdump filename pattern, path
  canonicalisation with post-symlink containment
- ✅ Executors: `vzdump`, `qmrestore`, `pct restore`, `qm`/`pct` lifecycle, `pvesm`,
  `statvfs`, SHA-256 with sidecar cache — argv lists only, absolute binaries only
- ✅ Task registry with atomic on-disk journal, per-task logs, progress parsing for both
  qemu and LXC output, cancel via SIGTERM→SIGKILL on the process group, `interrupted`
  reconciliation after a restart
- ✅ Endpoints: the six core + `/health`, `/storage/status`, `/task`, `/task/{id}/log`,
  `/task/{id}/cancel`
- ✅ Hardened systemd unit and `deploy/host/install-agent.sh` (PKI, venv, config, service)
- ✅ 180 tests — path-traversal and symlink-escape attempts, argv assertions, signature
  forgery/replay/skew, task lifecycle, full backup and restore flows. `ruff` clean,
  `mypy --strict` clean over 51 files
- ✅ CI: matrix on Python 3.11 (PVE 8) and 3.13 (PVE 9), plus a job that fails the build on
  `shell=True`, `os.system`, `os.popen` or blocking `subprocess` calls

**Remaining before M1 is closed:** on-host validation against a real Proxmox node — a live
`vzdump` end to end, a restore to a new VMID, and a cancel mid-backup. Everything above is
verified against captured command output, not a live host.

## M2 — Dashboard core: persistence, auth, settings ✅

- ✅ SQLAlchemy 2.0 models for all 15 tables in [DATABASE.md](DATABASE.md), constraint naming
  convention, `UTCDateTime` that rejects naive datetimes, JSON/JSONB dialect variant,
  SQLite pragmas applied per connection
- ✅ Alembic baseline (`0001`), async `env.py`, `render_as_batch`, plus a test that fails the
  build when models and migrations drift
- ✅ Repository `Protocol`s + SQLAlchemy implementations (users, refresh tokens, settings, audit)
- ✅ Argon2id with transparent re-hashing, JWT access tokens, opaque rotating refresh tokens
  with **family revocation on reuse**, CSRF double-submit, per-(IP, username) rate limiting
  and per-account exponential lockout, username-enumeration-resistant login
- ✅ Settings service: six sections as Pydantic models, merge-on-write, Fernet-encrypted
  secrets keyed by HKDF from one root secret, write-only secret handling with masked hints
- ✅ `AgentClient` — mTLS, HMAC signing byte-identical to the agent's verifier, GET-only
  retries with jittered backoff, circuit breaker, typed response models
- ✅ Endpoints: `/auth/*`, `/settings/*`, `/health`, `/health/detail`; correlation-id
  propagation and security headers
- ✅ 157 tests, `ruff` clean, `mypy --strict` clean over 54 files
- ✅ CI: lint, types, tests, plus a job that applies the migration to **PostgreSQL 17**, so
  the portability claim is exercised rather than asserted

**Bug found and fixed during implementation:** a `BigInteger` primary key is not a rowid alias
in SQLite, so it never autoincrements and every insert failed. Primary and foreign keys now
use `BigInteger().with_variant(Integer, "sqlite")`, with a regression test.

**Remaining before M2 is closed:** end-to-end verification against a running M1 agent
(`/health/detail` reporting `agent: ok` over real mutual TLS). The signing contract is covered
by tests on both sides, but the two processes have not yet spoken to each other.

## M3 — Backup engine ✅

- ✅ `ProxmoxClient` — read-only PVEAuditor token, one HTTP verb (`_get`) so no dashboard code
  path can write to the host, PVE payload quirks handled (delimited tag strings, integer
  booleans, hostname-vs-name)
- ✅ Inventory sync: never deletes a row, never touches `backup_enabled`, new guests off by
  default so adding a VM cannot silently widen what is backed up
- ✅ `BackupService` — target resolution for `all`/`include`/`exclude`, the allow-list enforced
  for manual backups too, run options frozen at request time
- ✅ Run executor: **the database is the queue**, guests backed up one at a time, progress
  streamed and never persisted, cancel via the agent, restart recovery that adopts a live
  agent task, resumes a partial run, and records an unconfirmed backup as `interrupted`
  rather than retrying it
- ✅ APScheduler on an in-memory job store with `backup_jobs` as the single source of truth,
  `next_run_at` reconciled at startup for missed firings, per-job cron validation
- ✅ `cancelled` added to the backup vocabulary (migration `0002`) so a deliberately stopped
  backup is not counted as a failure
- ✅ Event bus + SSE `/events/stream`, bounded per-subscriber queues that drop the oldest
  event rather than applying backpressure to a running backup
- ✅ 36 endpoint operations; 340 backend tests, `ruff` clean, `mypy --strict` clean over 88 files

**Bugs found and fixed during implementation** — all four would have been invisible in
production until they mattered:

1. **`0 1 * * 0` fired on Monday.** `CronTrigger.from_crontab` passes the day-of-week field
   through unchanged, but APScheduler numbers weekdays from Monday while crontab numbers them
   from Sunday. The project's specified weekly schedule would have run on the wrong day. The
   field is now translated to APScheduler's weekday *names*.
2. **The schedule preview showed the same date five times.** APScheduler computes from
   `min(now, previous_fire_time)`, so the reference has to advance along with the cursor.
3. **Missed schedules could never run.** `sync_jobs()` recomputed `next_run_at` before the
   catch-up pass read it, erasing the evidence that a firing had been missed.
4. **The run queue was a stack.** Claiming used the newest-first listing built for the UI, so
   a run submitted during a backlog could be starved indefinitely.

**Remaining before M3 is closed:** a real `vzdump` through the whole path. The agent is
stubbed by a transport that mints a task per request and walks it through the state sequence
the real agent produces, which exercises every branch of the executor — but it has still
never spoken to a Proxmox host.

## M4 — Google Drive sync ✅

**Agent** (rclone runs where the artifacts are — decision D1):

- ✅ `/sync/upload`, `/download`, `/verify`, `/list`, `/about`, `/delete`, all argv-only
- ✅ Remote **name** checked against an allow-list, remote **path** rejected for traversal,
  `:`, control characters and rclone's filter metacharacters (`*?[]{}`)
- ✅ `copyto` not `copy`; `deletefile` not `delete`; `--retries 1` so the dashboard owns the
  retry policy, `--low-level-retries` left on so one transfer survives a dropped packet
- ✅ Progress parsed from `--stats-one-line-date`; `lsjson`/`about` JSON parsed into typed
  results, with a missing quota reported as `null` rather than 0
- ✅ 112 new tests (292 total)

**Dashboard:**

- ✅ `SyncService` — queueing, verification, and the local/remote comparison
- ✅ `SyncWorker` — the database is the queue, exponential backoff with full jitter, attempt
  counting that survives a restart, and re-queueing of transfers interrupted mid-flight
- ✅ Endpoints: `/backups/{id}/upload|verify`, `/sync/status|tasks|quota`,
  `/browser/local|remote|compare`
- ✅ 48 new tests (388 total)

**Verification compares what the remote actually holds.** Google Drive stores an MD5 and
returns it without a download; asking for SHA-256 would make rclone fetch the whole artifact
back to hash it. Where a remote publishes no hash, the result is `hash_unavailable` — not
"verified" — because a truncated file of exactly the right length would otherwise pass.

**Bugs found and fixed during implementation:**

1. **`rstrip("/s")` silently destroyed transfer sizes.** It strips any trailing `/` or `s`,
   so the unit `GBytes` became `gbyte`, matched nothing, and fell back to a factor of 1 — a
   1.2 GiB transfer reported as 1 byte. The same latent bug was in the vzdump parser.
2. **`$` in the remote-name pattern matches before a trailing newline.** Anchors are now
   `\A`/`\Z`, so a name cannot smuggle a second line into an argv element.

**Remaining before M4 is closed:** a real transfer to a real Drive remote. rclone is stubbed
by a process runner that records argv and replays captured output, which pins the command
vocabulary and the parsers — but no byte has yet left the host.

## M5 — Retention & storage monitor ✅

- ✅ `RetentionService` implements [DATABASE.md §4](DATABASE.md#4-retention-semantics) exactly:
  per-guest, only after backup **and** upload success, with `retention_events` for every decision
- ✅ Dry-run mode + side-effect-free preview endpoint for the settings page
- ✅ Storage sampler worker, four `/storage/*` endpoints, and 30-day OLS forecast
- ✅ Warning/critical threshold transitions published with committed storage samples
- ✅ 55 new backend tests (443 total); Ruff and strict mypy clean over 115 files

**Acceptance:** with 5 backups of one guest and `keep=2`, exactly 3 are removed, none of
another guest's, and nothing is removed while an upload is pending. This is covered by the
retention regression suite, including stable ordering, locks, active restores, stale triggers,
policy provenance, dry-run, retries, and crash recovery.

**Remaining live validation:** exercise local and remote retention deletion against a real
Backup Agent/rclone remote, and collect storage samples from the real host and Drive quota.

## M6 — Restore ✅

- ✅ Two-phase flow: preflight → `pending_confirmation` + token (TTL 5 min) → confirm → execute.
  The token is stored as a SHA-256 and cleared on use, so it cannot be replayed; confirmation
  requires the literal target VMID as well
- ✅ Nine preflight guards — artifact restorable, present on the host (or downloadable from
  Drive), digest matches, VMID free, guest type matches, target stopped, node supported, free
  space ≥ size × 1.15, no restore in flight. A check that could not be *evaluated* fails
- ✅ Confirmation re-runs every check against the live host and returns 409 with the fresh
  report if anything now blocks; the report an operator agreed to is never the one relied on
- ✅ `RestoreWorker` — the database is the queue, oldest first, one restore at a time, restart
  recovery that adopts a live agent task and records an unconfirmed one as `interrupted`
- ✅ Drive-sourced restore: the archive is downloaded to the host in the same task before the
  restore starts, and the history row stops claiming the local copy is gone
- ✅ `interrupted` added to the restore vocabulary (migration `0003`), plus `restore_cancelled`
  in the audit trail. A restore is never retried or re-issued automatically
- ✅ Create and confirm run inside the shared retention guard; a manual `DELETE /backups/{id}`
  now refuses an artifact an active restore names, as retention already did
- ✅ Endpoints: `/restores/preflight|list|detail|confirm|cancel|log`; `restore.state` and
  `restore.progress` on the SSE stream
- ✅ 63 new backend tests (506 total); 56 endpoint operations, Ruff and strict mypy clean over
  123 files

**Acceptance:** a VM and an LXC each restore to a new VMID; an unconfirmed or expired restore
never executes; overwriting a running guest is refused without `force_stop`. Covered by the
restore service, API and executor suites, including token replay, a changed host between
preflight and confirmation, digest mismatch, Drive-sourced restore ordering, and both
restart-recovery paths.

**Remaining before M6 is closed:** a real `qmrestore` and a real `pct restore` through the
whole path, plus one cancel mid-restore. The agent is stubbed by a transport that mints a task
per request and walks it through the state sequence the real agent produces.

## M7 — Telegram notifications & logs ✅

- ✅ `TelegramClient` — send-only (no `getUpdates`, no webhook, so the bot cannot be told to do
  anything), Telegram's own `error_code`/`description` passed through verbatim, retryable and
  terminal failures separated at the source, bot token redacted from every log line
- ✅ Outbox: rows written **in the same transaction as the state change they describe**, ten
  templated messages, HTML-escaped values, text frozen at enqueue time
- ✅ `NotificationWorker` — the database is the queue, oldest first, at-least-once delivery
  (the attempt is charged before the send), exponential backoff with full jitter, Telegram's
  `retry_after` honoured over the computed delay, and no retry at all for a refusal that would
  repeat forever
- ✅ Duplicate-storm suppression in two forms: one-per-occurrence events keyed uniquely, so
  "exactly once" survives a restart as a row rather than a timer; genuinely recurring events
  windowed, with the repeat **recorded** as `suppressed` rather than dropped
- ✅ `POST /notifications/telegram/test` — sends synchronously, works while notifications are
  disabled, can target a different chat, returns the real Telegram error verbatim with a 200
- ✅ Log persistence: a structlog processor that appends to a bounded buffer and never blocks
  or raises, a writer that batches it into `logs` with capture paused, a poisoned batch written
  with its foreign keys cleared rather than wedging the queue, and retention over both `logs`
  and `audit_events`
- ✅ Endpoints: `/notifications`, `/notifications/{id}/resend`, `/notifications/telegram/test`,
  `/logs`, `/logs/export` (streamed NDJSON/CSV, keyset-paginated), `/audit`
- ✅ `notify` added to the log category vocabulary and `notifications.dedupe_key` added
  (migration `0004`); `notification.state` on the SSE stream
- ✅ 153 new backend tests (659 total); 62 endpoint operations, Ruff and strict mypy clean over
  145 files

**Acceptance:** every event fires exactly once with correct content — asserted by driving the
real workers and reading the outbox, including a resumed run that must not announce a second
start — and a Telegram outage delays but never loses a message.

**Design notes worth knowing before changing this:**

- Notifications are **per run**, not per guest. Fifty failing guests send one message naming
  them, not fifty messages. A partial run reports as `backup_failed`, because at least one
  guest has no backup.
- A **cancelled** backup or restore notifies nothing. An alert for something the operator did
  themselves is the fastest way to teach them to ignore alerts.
- Delivery is at-least-once, the opposite of the choice `vzdump` and `qmrestore` force. Sending
  a message twice destroys nothing; a failure nobody hears about is what this module prevents.
- A full log buffer drops its **oldest** entries and counts them. Losing the start of an
  incident is bad; wedging a backup because logging is behind is worse — and `/logs` and
  `/health/detail` both report the count, so the loss is never silent.

**Remaining before M7 is closed:** one real message to a real Telegram chat, and a deliberate
outage (wrong token, then blocked network) confirming the outbox drains afterwards. The client
is stubbed by a transport that replays captured API responses, which pins the request shape and
the error classification — but no message has yet left the LXC.

## M8 — Frontend ✅

Built page by page against the finished API, Next.js 15 (App Router) · React 19 · TypeScript ·
Tailwind v4 · TanStack Query v5 · Recharts · Vitest.

- ✅ **Foundation** — design tokens (dark-first, layered surfaces, light theme behind the same
  tokens, `prefers-reduced-motion`), the fetch client (Bearer + CSRF double-submit,
  single-flight 401 refresh-and-replay, RFC 9457 → typed `ApiError`/`NetworkError`), an
  in-memory token store (never a cookie — matches ARCHITECTURE.md), and the SSE `EventSource`
  hook (owns its own reconnect/backoff so a rotated token cannot pin a 401 loop)
- ✅ **Real-time** — a declarative event→domain invalidation table drives TanStack Query
  refreshes; the two high-frequency progress events feed a `useSyncExternalStore` progress
  store instead of the cache, so a running transfer never triggers a refetch storm
- ✅ **Shell** — role-filtered sidebar (collapses to an icon rail 768–1280px, an overlay sheet
  below 768px), header with the agent-connectivity pill, notification bell, theme toggle and
  sign-out, and the live mini-progress bar; `AuthGuard` gates every route and mounts the stream
- ✅ **Pages** — login · dashboard · backup history + detail (log + upload/verify actions) ·
  manual-backup dialog · schedules + cron builder + next-fire preview · the two-phase restore
  wizard (preflight → one-time token → typed-VMID confirm with a live countdown; a 409 on
  confirm shows the fresh report) · browser · sync · storage (usage, 30-day Recharts trend,
  OLS forecast, top consumers) · logs + audit trail · notifications · settings (six sections,
  write-only secrets)
- ✅ **Four states everywhere** — a single `DataState` renders loading (skeletons), empty (with
  a CTA), error (typed, with retry) and **partial** (a stale/degraded banner, e.g. the remote
  couldn't be listed) so a zero count is never misread as "all clear"
- ✅ **Accessibility** — status is never colour-only (every badge pairs a glyph with its label
  and an `aria-label`), a single accent focus ring, `prefers-reduced-motion` respected globally
- ✅ **Backend** — added the two aggregate endpoints the dashboard needs but M2–M7 never built:
  `GET /dashboard/summary` and `/dashboard/activity` (schema + `DashboardService` + deps +
  route + router), composed from existing repositories with no new persisted state
- ✅ 79 frontend tests (Vitest + Testing Library) + 8 new backend tests (**667 backend total**);
  `eslint`, `prettier` and `tsc --noEmit` clean; `make frontend-check` wired into `make check`

**Acceptance:** each page matches [UI.md](UI.md), handles all four states, is keyboard
operable, and passes AA contrast.

**Bugs found and fixed during implementation:**

1. **The dashboard crashed when the agent was unreachable.** `_agent_summary` awaited the live
   health probe directly, so an agent that was down or returned an unexpected body raised
   through the whole `/dashboard/summary` handler — a 500 for the one screen an operator opens
   *because* something looks wrong. The probe is now fail-closed: any error reports the agent as
   unreachable and degrades a single tile, honouring "telling the operator is never on the
   critical path."
2. **A Unicode escape as bare JSX text renders literally.** `\u2026`/`\u2192` written as JSX
   children (or inside an attribute string) shows the literal backslash sequence, not the
   glyph. Fixed by using real characters or `{"\u2026"}` string expressions.

**Remaining before M8 is closed:** run against a live backend end to end — a real login through
the refresh-rotation flow, the SSE stream carrying a real backup's progress into the
mini-bar, and the restore wizard's confirmation countdown against a real pending token. Every
page is built and tested against the typed API and the fake transports, but the browser has not
yet spoken to a running dashboard.

## M9 — Packaging & release ✅

Everything needed to install, run, upgrade and maintain ProxSync as a deployed system, built to
the same bar as the code it ships.

- ✅ `deploy/lxc/install.sh` — full dashboard provisioning: service user, backend venv, a real
  `next build` into standalone form, `/etc/proxsync/api.env`, a bootstrap TLS certificate, the
  nginx site, the systemd units, `alembic upgrade head`, and a generated first-run admin. Safe
  to re-run; mirrors `install-agent.sh` (secrets preserved unless `--regenerate-secrets`)
- ✅ `deploy/host/install-agent.sh` — already shipped in M1; the install guide now drives the
  agent↔dashboard certificate exchange and the PVEAuditor token end to end
- ✅ `deploy/nginx/proxsync.conf` — one origin on :443, `/api/*` → backend, `/*` → the Next.js
  server, **SSE left unbuffered** for `/events/stream`, edge rate-limit on login, security
  headers, immutable caching for `/_next/static`
- ✅ `deploy/systemd/` — hardened `proxsync-api` and `proxsync-web` units (stricter than the
  agent's: `ProtectSystem=strict`, a `SystemCallFilter`, no namespace/device exceptions), a
  `proxsync.target` grouping them, and a `proxsync-db-backup` service + daily timer
- ✅ Backup/restore of ProxSync's own database — `scripts/proxsync-db-backup.sh` (SQLite online
  `.backup` with an integrity check, or `pg_dump -Fc`) and `proxsync-db-restore.sh` (staged and
  integrity-checked **before** the live database is touched; the old SQLite file is moved aside,
  not deleted). `proxsync-upgrade.sh` backs up first, then updates code, deps and the built UI,
  migrates with services stopped, and refuses to proceed if the pre-upgrade backup fails
- ✅ CI: a **frontend** workflow (lint, types, tests, and a real build that asserts
  `.next/standalone/server.js` exists), a **dependency-audit** workflow (`pip-audit` for backend
  and agent, `npm audit` for the frontend, weekly on a schedule), `dependabot.yml`, and the
  existing `shell=True`/shellcheck guard widened to cover the new installer and scripts
- ✅ Docs: [INSTALL.md](INSTALL.md), [UPGRADE.md](UPGRADE.md), [SECURITY.md](SECURITY.md) (the
  standalone model `ARCHITECTURE.md` referenced but never had), [TROUBLESHOOTING.md](TROUBLESHOOTING.md),
  root `CONTRIBUTING.md`, and a Keep-a-Changelog `CHANGELOG.md`
- ✅ `make version`, `version-check`, `audit`, and `release` (the gate: versions agree, then
  every check runs) — all three components verified at `0.1.0`

**v0.1.0** is ready to tag: `make release` passes the gate, and `make version-check` confirms
backend, agent and frontend all report `0.1.0`.

**Bugs found while wiring the dev environment** — both invisible until code ran outside the test
harness, both with new regression tests (full detail in [HANDOFF.md](HANDOFF.md) §5):

1. **List settings crashed on first boot from a `.env` file.** pydantic-settings `json.loads()`es a
   `list[...]` field at the source level before the CSV validator runs, so a bare value like
   `ALLOWED_CLIENT_NETWORKS=10.0.0.20/32` — exactly what `install-agent.sh` and `.env.example`
   write — raised `JSONDecodeError` at startup. The agent and dashboard would have crashed on the
   very first real install. Every test missed it by passing lists straight to the constructor.
   Fixed with `enable_decoding=False` and a `test_config.py` that loads from a real `.env`.
2. **Three repository `Protocol` methods were not supertypes of their implementations.** A loose
   `create(**fields: object)`/`count(**filters: object)` on the protocol is not satisfied by a
   concrete method with a fixed keyword signature, so `mypy --strict` rejected passing the
   implementation to a protocol-typed parameter — surfaced first by M8's `DashboardService`. Fixed
   by matching each protocol signature to its implementation, as every other repository already did.

**Remaining before M9 — and v1 — is closed:** the same live validation the whole project still
owes (see [HANDOFF.md](HANDOFF.md) §2). Packaging is complete and its shell is syntax- and
shellcheck-clean, but the two installers have been run only in review, not yet against a real
Proxmox host and a fresh LXC. The first real install is the acceptance test for M9.

---

## Sequencing rationale

The agent comes first because it is the only component that cannot be faked convincingly —
everything downstream depends on the shape of its task lifecycle. Retention (M5) lands after
sync (M4) because the rule "delete only after upload success" cannot be honestly tested
before uploads exist. The frontend is last so it is written against a stable, real API rather
than a moving one.

## Post-v1 candidates

Multi-node clusters · Proxmox Backup Server as an alternative target · S3/B2 remotes ·
per-guest schedules · backup encryption at rest · Prometheus `/metrics` · webhook and
email notifications · 2FA · read-only public status page.
