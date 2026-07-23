# ProxSync — Handoff

State of the project as of the end of **M9**. Written for whoever picks this up next,
including a future session with no memory of building it.

---

## 1. Where things stand

| Milestone | State | Tests |
| --- | --- | --- |
| M0 foundations — architecture, schema, API, UI, roadmap | ✅ | — |
| M1 Backup Agent (host) | ✅ code-complete, **not host-validated** | 292 |
| M2 Dashboard core — persistence, auth, settings | ✅ code-complete, **not wire-validated** | (in 659) |
| M3 Backup engine — inventory, schedules, run executor, SSE | ✅ code-complete, **no real vzdump yet** | (in 659) |
| M4 Google Drive sync — transfers, retry queue, verification, browser | ✅ code-complete, **no real transfer yet** | (in 659) |
| M5 Retention & storage monitor | ✅ code-complete, **not live-validated** | (in 659) |
| M6 Restore — two-phase flow, executor, Drive source | ✅ code-complete, **no real restore yet** | (in 659) |
| M7 Telegram & logs — outbox, delivery worker, log sink | ✅ code-complete, **no real message sent yet** | (in 659) |
| M8 Frontend — shell, all pages, real-time SSE, dashboard endpoints | ✅ code-complete, **not wire-validated** | 79 fe |
| M9 Packaging & release — LXC installer, nginx, systemd, DB backup/restore, upgrade, CI audit, docs | ✅ code-complete, **not host-validated** | (shell: bash -n + shellcheck) |

**Totals:** 1049 tests (672 backend + 298 agent + 79 frontend), `ruff` clean, `ruff format`
clean, `mypy --strict` clean over the backend and agent (pinned `mypy>=1.13,<2` to match CI),
`eslint` + `prettier` + `tsc --noEmit` clean over the frontend. 64 dashboard API operations.
Migrations apply and reverse on SQLite and PostgreSQL 17. M9 adds mostly deployment shell, CI
and docs — held to the same bar (`bash -n` + shellcheck, a frontend build that asserts its
standalone output, and a dependency audit) — plus two config/typing bug fixes with regression
tests (see §5, 11–12). All three components report version `0.1.0`; `make release` is the tag gate.

```bash
make check           # all components: lint, types, tests
make backend-check   # backend only
make agent-check     # agent only
make frontend-check  # frontend only
```

The backend needs **Python 3.13**; the agent targets **≥3.11** so it runs on PVE 8.
`BACKEND_PYTHON` in the Makefile points at the 3.13 interpreter. The frontend is **Next.js 16 /
React 19 / Node**; `make frontend-install` bootstraps it.

---

## 2. The one thing to know

**Nothing in this repository has ever spoken to a real Proxmox host.**

Every component is tested against fakes that replay captured command output and canned HTTP
responses. That is enough to pin argv construction, parsing, state machines and error
handling — and it caught several real bugs (§5) — but it cannot catch a wrong flag name, a PVE
version difference, or an rclone output format that changed.

The closure blockers, in the order they should be cleared:

1. **M1** — on a real node: one live `vzdump`, one restore to a new VMID, one cancel
   mid-backup.
2. **M2** — `/health/detail` reporting `agent: ok` over real mutual TLS. The signing contract
   is tested from both sides independently; the two processes have never exchanged a packet.
3. **M3** — a scheduled and a manual backup end to end, surviving a dashboard restart mid-run.
4. **M4** — one real upload to a real Drive remote, then `/browser/compare` showing `in_sync`,
   then a deliberately corrupted remote copy reported as `hash_mismatch`.
5. **M5** — execute local and remote retention deletion through a real Agent/rclone remote,
   then collect storage samples and Drive quota from the deployed host.
6. **M6** — a real `qmrestore` and a real `pct restore` to a new VMID through the two-phase
   flow, one restore over an existing running guest with `force_stop`, and one cancel
   mid-restore.
7. **M7** — one real message to a real chat via `POST /notifications/telegram/test`, then a
   deliberate outage (wrong token, then a blocked network) confirming the outbox retries and
   drains rather than losing anything.
8. **M8** — the built UI against a running backend: a real login through the refresh-rotation
   flow, the SSE stream carrying a real backup's progress into the mini-bar, and the restore
   wizard's confirmation countdown against a real pending token. Every page is tested against
   the typed API and fakes, but the browser has not yet spoken to a running dashboard.
9. **M9** — a first real install: `deploy/lxc/install.sh` on a fresh unprivileged container and
   `install-agent.sh` on the host, the two introduced over real mTLS, then an upgrade and a
   database restore exercised in place. The shell is `bash -n`- and shellcheck-clean and the
   frontend build is asserted in CI, but the installers have been run only in review. The first
   real install *is* the acceptance test for M9 — and it is the same run that clears (1)–(8).

Until (1) and (2) are done, everything downstream rests on an unverified assumption.

---

## 3. Architecture in one page

Two processes, one privileged surface:

```text
┌─────────────────────────── Proxmox host (root) ───────────────────────────┐
│  proxsync-agent  ── vzdump · qmrestore · pct restore · pvesm · rclone      │
│  FastAPI, mTLS + HMAC, address allow-list, closed command vocabulary       │
└───────────────────────────────▲───────────────────────────────────────────┘
                                │ HTTPS, signed, allow-listed
┌───────────────────────────────┴─── LXC (unprivileged) ────────────────────┐
│  backend/  FastAPI · SQLAlchemy · Alembic · APScheduler                    │
│  frontend/ Next.js (M8)                          ──► Telegram (egress)     │
└───────────────────────────────────────────────────────────────────────────┘
```

The dashboard executes no shell command, ever. CI greps for `shell=True`, `os.system`,
`os.popen` and blocking `subprocess` calls and fails the build.

### Rules that hold everywhere

| Rule | Why |
| --- | --- |
| **The database is the queue** — for backup runs, transfers, restores and notifications | A crash loses nothing; restart recovery is the same code path as normal operation; the API and the scheduler submit work without knowing about each other |
| In-memory doorbells only remove latency | Rung from a SQLAlchemy `after_commit` hook, so a worker can never see a row before it is committed. Backup, sync, restore and outbox idle polls are the backstop |
| Retention is event-driven and fail-closed | It reconciles at startup, then uses post-commit doorbells and bounded failure retries. It has no healthy-state poll that would duplicate decision events |
| Where the outcome is unknown, say so | `interrupted` exists because a lost *response* is indistinguishable from a lost *request*, and `vzdump` is not idempotent |
| Retries are only automatic where they are safe | An upload overwrites the same object, so it retries. A backup does not, so it never does. A restore *destroys* its target, so it is never retried and never re-issued. A **message** destroys nothing, so it is at-least-once |
| A partial answer must never read as success | `/browser/compare` returns zero counts plus a `detail` when the remote cannot be listed; `/logs` reports `persisted` and `dropped` so an empty page cannot be misread as a quiet night |
| Telling the operator is never on the critical path | Outbox rows and log lines are written outside the work they describe — one in the same transaction, one in a bounded buffer — so neither can slow or fail a backup |

---

## 4. Component map

### `agent/` — runs on the Proxmox host

| Path | What it is |
| --- | --- |
| `app/core/security.py` | HMAC verification, nonce cache, clock skew, address allow-list |
| `app/executors/base.py` | The **only** place a child process is created. `create_subprocess_exec`, argv lists, absolute binaries, process-group cancellation |
| `app/executors/{vzdump,restore,pvesm,checksum,rclone}.py` | argv construction and output parsing, one module per tool |
| `app/validators/{paths,artifacts,identifiers,remotes}.py` | The security boundary. Everything from the network passes through here before reaching an argv list |
| `app/tasks/registry.py` | Task journal — atomic writes, restart reconciliation to `interrupted` |
| `app/services/` | Orchestration: backup, restore, sync, artifacts, storage |
| `deploy/host/` | Hardened systemd unit + installer (PKI, venv, config) |

### `backend/` — runs in the LXC

| Path | What it is |
| --- | --- |
| `app/api/deps.py` | Composition root. **Read the module docstring** — it documents the transaction boundary |
| `app/clients/agent_client.py` | The only component that may reach the agent. Signs byte-identically to its verifier |
| `app/clients/proxmox_client.py` | Read-only inventory. Exposes exactly one verb, `_get` — there is no code path that can write to the host |
| `app/clients/telegram_client.py` | Send-only. No `getUpdates`, no webhook; passes Telegram's own errors through and redacts the token from logs |
| `app/core/log_sink.py` | The structlog processor and its bounded buffer. **Never blocks, never raises, never opens a session** |
| `app/workers/backup_runner.py` | Run executor. Restart recovery lives here |
| `app/workers/sync_worker.py` | Upload queue, backoff, attempt counting |
| `app/workers/restore_worker.py` | Restore executor — claim, download-first, poll, adopt-on-restart |
| `app/workers/retention_worker.py` | Per-guest startup reconciliation, event coalescing, bounded retries |
| `app/workers/storage_sampler.py` | Immediate + 15-minute storage snapshots and threshold transitions |
| `app/workers/notification_worker.py` | Outbox delivery — claim, charge the attempt, send, classify the failure |
| `app/workers/log_writer.py` | Drains the sink into `logs`, prunes `logs` and `audit_events` |
| `app/workers/scheduler.py` | APScheduler wrapper. **No persistent job store** — see the docstring |
| `app/core/retention_guard.py` | Single-process linearization for retention decisions and copy/policy mutations |
| `app/core/cron.py` | crontab→APScheduler translation. **Read the docstring before touching it** |
| `app/services/restore_service.py` | Preflight, the two-phase confirmation, and every guard above the agent's floor |
| `app/services/notification_service.py` | Enqueue policy, de-duplication, resend, the synchronous test send. `NotificationGateway` is the seam every emitting worker holds |
| `app/services/notification_templates.py` | One template per event, every interpolated value HTML-escaped |
| `app/services/` | Use cases; depend on repository `Protocol`s, never on SQLAlchemy |
| `alembic/versions/` | `0001` baseline (15 tables), `0002` run plan + `cancelled`, `0003` `interrupted` restores + `restore_cancelled`, `0004` `notifications.dedupe_key` + `notify` log category |

---

## 5. Bugs found while building (all still fixed, all with regression tests)

These are recorded because each one was invisible in normal operation and would have
surfaced as data loss or silent wrong behaviour.

1. **`BigInteger` primary keys never autoincrement on SQLite** (M2). `BIGINT PRIMARY KEY` is
   not a rowid alias, so every insert failed. Fixed with
   `BigInteger().with_variant(Integer, "sqlite")`.
2. **`0 1 * * 0` fired on Monday** (M3). `CronTrigger.from_crontab` passes day-of-week
   through; APScheduler counts from Monday, crontab from Sunday. The project's specified
   weekly backup would have run on the wrong day, forever, with the UI saying "Sunday".
3. **The schedule preview showed one date five times** (M3). APScheduler computes from
   `min(now, previous_fire_time)`, so both arguments must advance.
4. **Missed schedules could never fire** (M3). `sync_jobs()` recomputed `next_run_at` before
   the catch-up pass read it. The entire misfire policy was decorative.
5. **The run queue was a stack** (M3). Claiming reused the newest-first listing built for the
   UI, so a run submitted during a backlog could starve.
6. **`rstrip("/s")` destroyed transfer sizes** (M4). It strips any trailing `/` or `s`, so
   the unit `GBytes` became `gbyte` and fell back to a factor of 1 — 1.2 GiB reported as
   1 byte. The same latent bug was in the vzdump parser.
7. **`$` matches before a trailing newline** (M4). The remote-name pattern now anchors with
   `\A`/`\Z`, so a name cannot smuggle a second line into an argv element.
8. **A state change written on the way out of a rejected request is silently discarded** (M6).
   `RetentionGuard.transaction()` rolls back on any exception, so marking a restore `expired`
   and *then* raising the 409 left the row `pending_confirmation` forever — and an active
   restore row blocks retention, so one abandoned confirmation dialog would have pinned a
   backup permanently. The refusal now writes nothing and the executor's sweep owns the
   transition, in its own transaction. This applies to **every** guarded route: inside
   `retention_guard.transaction()`, validate first and mutate second, without exception.
9. **A lost response is not a refusal** (M6). The first draft treated any failure to start a
   restore as `failed`. A transport error means the request may well have reached the host, so
   only an answer *from* the agent maps to `failed`; an unreachable agent maps to
   `interrupted`. `failed` would have told an operator nothing happened, inviting a second
   `qmrestore` over a guest already being rebuilt.
10. **A truthy check dropped the identifier from an alert** (M7). `guest_list` rendered
    `name (type/vmid)` only `if vmid` — so an id of 0 produced a message naming a guest the
    operator could not act on. Now `is not None`. Proxmox will not issue VMID 0, which is
    exactly why this would have survived every real run and shown up in the one that mattered.
11. **List settings crashed the process on first boot from a `.env` file** (M9). pydantic-settings
    treats a `list[...]` field as "complex" and `json.loads()`es it at the *source* level — for
    both environment variables and the `.env` file — before any `mode="before"` validator runs. A
    bare CSV value such as `PROXSYNC_AGENT_ALLOWED_CLIENT_NETWORKS=10.0.0.20/32` or
    `PROXSYNC_CORS_ORIGINS=https://ui.lan` is not valid JSON, so it raised `JSONDecodeError` at
    startup. Both `install-agent.sh` and `.env.example` write exactly that CSV shape, so the agent
    (and the dashboard) would have crashed on the very first real install. Every one of the 292
    agent and 667 backend tests missed it, because they build `Settings` by passing Python lists
    straight to the constructor and never exercise the environment/`.env` path a deployment uses.
    Fixed with `enable_decoding=False` on both models so `_split_csv` owns parsing, and pinned by a
    new `test_config.py` in each component that loads a list field from a real `.env` file (and was
    confirmed to fail when the fix is reverted).
12. **A `Protocol.create(**fields: object)` is not a supertype of its concrete implementation** (M9).
    `RestoreRepository.create`, `BackupHistoryRepository.create` and `.count` were declared on the
    `Protocol` with a loose `**fields: object` signature, while the SQLAlchemy classes implement a
    fixed keyword signature. A concrete method that accepts *fewer* calls than the protocol promises
    is not a structural subtype, so assigning the implementation to a protocol-typed parameter is a
    type error — `mypy --strict` was right to reject it. It stayed hidden until M8's `DashboardService`
    became the first consumer to pass a concrete repository into a protocol-typed argument. Fixed by
    giving each protocol method the same concrete signature as its implementation, matching every
    other repository in the tree. (mypy is also pinned `<2` so local matches CI, but the pin alone did
    not fix this — the error reproduces on mypy 1.x.)

---

## 6. Decisions that are settled (do not re-litigate without reading why)

| Decision | Where the reasoning lives |
| --- | --- |
| rclone runs on the host, not in the LXC (D1) | `agent/app/executors/rclone.py` docstring |
| Inventory via a read-only PVEAuditor token (D2) | `backend/app/clients/proxmox_client.py` docstring |
| Agent runs as root under a hardened unit, not sudoers (D3) | `docs/ARCHITECTURE.md` §3.1 |
| The scheduler has no persistent job store | `backend/app/workers/scheduler.py` docstring |
| The session commits on 4xx | `backend/app/api/deps.py` docstring |
| Connection credentials live in the environment, not the settings table | `backend/app/schemas/settings.py` docstring |
| Verification uses MD5, not SHA-256 | `agent/app/executors/checksum.py` docstring |
| Retention is per-guest and only after upload success | `docs/DATABASE.md` §4 |
| A restore confirmation re-runs preflight and can refuse | `backend/app/services/restore_service.py` docstring |
| An unevaluable preflight check fails rather than passing | same docstring |
| Confirm refuses an expired window without writing the transition; the executor's sweep owns that write | `RestoreService.confirm`, and the transaction rule in `app/api/deps.py` |
| Notification delivery is at-least-once, unlike every other retry in the system | `backend/app/workers/notification_worker.py` docstring |
| Notifications are per **run**, not per guest; a partial run reports as failed | `docs/ARCHITECTURE.md` §4, `BackupRunner._notify_run_finished` |
| A cancelled backup or restore notifies nothing | `BackupRunner._notify_run_finished`, `RestoreWorker._notify_finished` |
| The log sink drops the oldest entry rather than blocking a log call | `backend/app/core/log_sink.py` docstring |
| The bot token and chat id are operator settings in the database; only delivery mechanics are environment | `backend/.env.example`, Notifications section |

---

## 7. M9 result — Packaging & release (final module)

M7 is notifications and log persistence, both deliberately kept off the critical path of the
work they describe.

**The outbox.** `NotificationGateway.emit()` is called from inside the transaction that
records the state change, so a message can never describe something that was rolled back, and
a state change can never happen unannounced. `NotificationWorker` claims rows oldest-first,
charges the attempt *before* sending — a process that dies mid-request leaves a scheduled
retry rather than a row claiming an attempt that never happened — and classifies the failure:
a 429, a 5xx or a dropped connection goes back on the queue with jittered backoff (honouring
Telegram's own `retry_after`), while `400 chat not found` and a missing bot token are terminal
on the first attempt, because spending five attempts on them only delays the failure an
operator needs to see.

**Two kinds of duplicate, two answers.** An event that happens once per occurrence — a run
finishing, a restore failing — is keyed uniquely, and a second enqueue writes nothing at all.
That is what makes "exactly once" survive a restart: the guarantee is a row, not a timer, and
a resumed run does not announce a second start. An event that genuinely recurs — a storage
threshold, a retention pass — is suppressed for a window instead, and the repeat is *recorded*
as `suppressed` with `suppressed_by` naming what it repeats, because "we decided not to tell
you at 03:14" is itself something an operator may need to see.

**Log persistence.** A structlog call appends to a bounded deque and returns. It cannot open a
session: log calls happen inside transactions, and a synchronous write there would deadlock on
SQLite's single writer or recurse through SQLAlchemy's own logging. `LogWriter` drains it in
batches with capture paused, puts a failed batch back at the front, and rewrites a batch whose
foreign keys no longer resolve with the links cleared rather than letting one poisoned row
wedge every log line behind it. A full buffer drops its oldest entries and counts them, and
both `/logs` and `/health/detail` report that count.

M8 built the frontend page by page against the now-complete API; M9 packages the whole system
for deployment. **M9 is now complete** — the last module in the roadmap.

**What M9 added (no application code, deployment surface only):**

- `deploy/lxc/install.sh` — provisions the dashboard container end to end (service user, backend
  venv, a real `next build`, config, bootstrap TLS, nginx, systemd units, migrations, first-run
  admin). Mirrors `install-agent.sh`; safe to re-run.
- `deploy/nginx/proxsync.conf` — one :443 origin, `/api/*` → backend, `/*` → the Next.js server,
  **SSE deliberately unbuffered** so `/events/stream` is not held or timed out.
- `deploy/systemd/` — hardened `proxsync-api` and `proxsync-web` units (stricter than the agent:
  `ProtectSystem=strict`, a syscall filter, no namespace/device exceptions), a `proxsync.target`,
  and a `proxsync-db-backup` service + daily timer.
- `scripts/` — `proxsync-db-backup.sh`, `proxsync-db-restore.sh` and `proxsync-upgrade.sh`. The
  invariant worth knowing: **a backup is verified before it counts, and the live database is
  never touched until a restore's source has passed its own integrity check.** The upgrade backs
  up first and refuses to continue if that fails.
- CI — a frontend workflow (asserts `.next/standalone/server.js` exists), a dependency-audit
  workflow (`pip-audit` × 2, `npm audit`, weekly), `dependabot.yml`, and the `shell=True`/
  shellcheck guard widened to the new scripts.
- Docs — INSTALL, UPGRADE, SECURITY (the standalone model ARCHITECTURE.md referenced),
  TROUBLESHOOTING, root CONTRIBUTING, and a CHANGELOG. `make release` is the tag gate for v0.1.0.

**The one thing to carry forward:** M9 is code-complete but, like every module before it, not
yet host-validated. The first real install (§2, blocker 9) is its acceptance test — and it is
the same run that finally clears blockers (1)–(8) for the whole project.

---

## 8. Working agreements with the user

- **One module at a time.** Deliver M*n*, report, then wait for confirmation before starting
  M*n+1*. The user says "lanjut m*n*" to proceed.
- **Production quality, no placeholders.** No `TODO` bodies; a module is complete and tested
  or not started.
- **This will be open-sourced.** Comments explain *why*, not *what*. Documentation is updated
  in the same change as the code.
- The user writes in a mix of Indonesian and English; replies have been in English.
