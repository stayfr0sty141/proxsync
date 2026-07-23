# ProxSync Dashboard API

Runs inside the LXC container. Owns the database, the schedule, the policy and the history.
Holds **no privileges on the Proxmox host** — every operation that touches a guest is
delegated to the [Backup Agent](../agent/README.md) over mutual TLS.

## Layering

```text
api/v1/routes  →  services  →  repositories  →  db/models
                     ↓
                  clients (agent, proxmox, telegram)
```

Dependencies point inward. Services depend on repository `Protocol`s, not on SQLAlchemy,
which is what makes them testable without a database and what keeps the PostgreSQL migration
mechanical. FastAPI `Depends` wires the edge; constructor injection does the rest.

## Security model

| Concern | Decision |
| --- | --- |
| Passwords | Argon2id (`t=3, m=64 MiB, p=4`), transparently re-hashed on login when parameters are raised |
| Access tokens | JWT, 15 min, browser memory only — never written to a cookie, so a CSRF cannot replay one |
| Refresh tokens | Opaque random strings stored as SHA-256 digests. Rotated on every use; presenting an already-rotated token revokes the whole **family**, turning theft into a detectable, self-limiting event |
| Cookies | Refresh: `HttpOnly; Secure; SameSite=Strict`, scoped to `/api/v1/auth`. CSRF: readable mirror cookie echoed in `X-CSRF-Token` |
| CSRF | Double-submit, enforced on the cookie-authenticated endpoints only. Bearer-authenticated routes need no CSRF: an `Authorization` header is not ambient |
| Login abuse | Sliding window per (IP, username) **and** per-account exponential lockout, so rotating source addresses gains nothing |
| Enumeration | Unknown username and wrong password return the same message; an unknown user still pays for one Argon2 verification so timing does not differ |
| Secrets at rest | Fernet, keyed by HKDF from `PROXSYNC_SECRET_KEY`. The API returns `configured` + a masked hint, never the value |
| Root secret | One env var; JWT and encryption keys are derived, never stored |

Connection credentials — agent URL/HMAC/certificates, Proxmox token, database URL — live in
the environment, not the settings table. Storing them in a database the app needs those
credentials to reach is circular, and it would put the keys to the host in a database dump.

## Database

15 tables, defined in [app/db/models/](app/db/models/) and documented in
[../docs/DATABASE.md](../docs/DATABASE.md). SQLite by default, PostgreSQL-ready:

- Constraint naming convention (SQLite's batch-mode `ALTER` can only rebuild *named* constraints)
- `UTCDateTime` rejects naive datetimes on write and returns tz-aware UTC on read
- Enumerations as `VARCHAR` + `CHECK`, mirrored by a Python `StrEnum` — no native `ENUM` types
- `JSON` on SQLite, `JSONB` on PostgreSQL, via a dialect variant
- Primary keys are `BigInteger().with_variant(Integer, "sqlite")`. **This matters:** a plain
  `BIGINT PRIMARY KEY` is not a rowid alias in SQLite, so it never autoincrements and every
  insert fails. There is a regression test for it.

Alembic owns the schema. The service never calls `create_all` — a service that silently
created tables would mask a failed migration.

```bash
alembic upgrade head          # apply
alembic revision --autogenerate -m "..."   # after changing a model; review the result
```

`tests/test_migrations.py` fails the build if the models and the migration drift apart.

## Endpoints (M2–M7)

| Method | Path | Role |
| --- | --- | --- |
| POST | `/api/v1/auth/login` | — |
| POST | `/api/v1/auth/refresh` · `/logout` | cookie + CSRF |
| GET | `/api/v1/auth/me` | any |
| POST | `/api/v1/auth/change-password` | any |
| GET/DELETE | `/api/v1/auth/sessions[/{id}]` | any |
| GET/PUT | `/api/v1/settings[/{section}]` | admin |
| GET | `/api/v1/guests` · `/guests/{id}` | viewer |
| POST | `/api/v1/guests/refresh` | operator |
| PATCH | `/api/v1/guests/{id}` | **admin** — the backup allow-list |
| GET/POST | `/api/v1/backup-jobs` | viewer / admin |
| GET/PUT/DELETE | `/api/v1/backup-jobs/{id}` | viewer / admin / admin |
| POST | `/api/v1/backup-jobs/{id}/enable` · `/disable` | admin |
| GET | `/api/v1/backup-jobs/{id}/preview` | viewer |
| POST | `/api/v1/backup-jobs/{id}/run` | operator |
| POST | `/api/v1/backups/run` | operator — **Backup Now**, returns 202 |
| GET | `/api/v1/backups` · `/backups/{id}` · `/backups/{id}/log` | viewer |
| PATCH | `/api/v1/backups/{id}/lock` | admin |
| DELETE | `/api/v1/backups/{id}` | admin |
| GET | `/api/v1/runs` · `/runs/{id}` · `/runs/{id}/backups` | viewer |
| POST | `/api/v1/runs/{id}/cancel` | operator |
| POST | `/api/v1/backups/{id}/upload` · `/verify` | operator |
| GET | `/api/v1/sync/status` · `/sync/tasks` · `/sync/quota` | viewer |
| GET | `/api/v1/browser/local` · `/remote` · `/compare` | viewer |
| POST | `/api/v1/retention/preview` | admin — side-effect-free policy preview |
| GET | `/api/v1/storage` · `/storage/history` · `/storage/forecast` · `/storage/by-guest` | viewer |
| POST | `/api/v1/restores/preflight` | operator — every check, no writes |
| POST | `/api/v1/restores` | operator — 201, returns the confirmation token once |
| POST | `/api/v1/restores/{id}/confirm` · `/cancel` | operator |
| GET | `/api/v1/restores` · `/restores/{id}` · `/restores/{id}/log` | viewer |
| GET | `/api/v1/notifications` | viewer — the outbox |
| POST | `/api/v1/notifications/{id}/resend` | admin — 202 |
| POST | `/api/v1/notifications/telegram/test` | admin — sends now, returns Telegram's own error verbatim |
| GET | `/api/v1/logs` | viewer |
| GET | `/api/v1/logs/export` | admin — streamed NDJSON or CSV |
| GET | `/api/v1/audit` | admin |
| GET | `/api/v1/events/stream` | viewer (SSE) |
| GET | `/api/v1/health` · `/health/detail` | — / admin |

62 operations in total; see [../docs/API.md](../docs/API.md) for the full contract.

## Backup orchestration

**The database is the queue.** A run is durable in `backup_runs` before anything starts; the
in-memory doorbell only removes latency. A crash therefore loses nothing, restart recovery is
the same code path as normal operation, and the scheduler and the API can both submit work
without knowing about each other.

| Concern | Decision |
| --- | --- |
| Concurrency | One run, one guest at a time. The agent holds a single backup slot, so overlapping would only earn a 409 from it |
| Restart mid-run | The run is re-queued and resumes: guests already finished are skipped, and a backup still running on the agent is re-adopted rather than restarted |
| Unknown outcome | Recorded as `interrupted`, never retried. `vzdump` is not idempotent, and a lost *response* is indistinguishable from a lost *request* |
| Cancellation | A distinct `cancelled` status, so the dashboard's failed-backup count excludes backups an operator stopped on purpose |
| Progress | Streamed over SSE, never written to the database — a percentage is worthless a minute later |
| Run options | Frozen when the run is requested, so editing a schedule cannot change a run already queued |

**The scheduler has no persistent job store, deliberately.** Pointing APScheduler's
`SQLAlchemyJobStore` at the same database would create two sources of truth for one fact, and
they drift silently. `backup_jobs` is authoritative and the in-memory schedule is rebuilt from
it; `next_run_at` is persisted and reconciled at startup so a firing missed while the service
was down is either caught up inside the grace window or skipped and logged loudly.

**Cron is crontab, not APScheduler's dialect.** `CronTrigger.from_crontab` numbers weekdays
from Monday; crontab numbers them from Sunday, so `0 1 * * 0` — "every Sunday at 01:00" —
fires on Monday if passed through. ProxSync translates the day-of-week field to weekday names
first, and refuses an expression that restricts both day-of-month and day-of-week, because
cron ORs those and APScheduler ANDs them.

## First run

```bash
export PROXSYNC_SECRET_KEY=$(openssl rand -hex 32)
export PROXSYNC_BOOTSTRAP_ADMIN_PASSWORD='choose-something-long'
alembic upgrade head
python -m app
```

The first start seeds default settings and creates the administrator with
`must_change_password` set — every endpoint except the password change is refused until it is
rotated. Remove `PROXSYNC_BOOTSTRAP_ADMIN_PASSWORD` afterwards.

Configuration reference: [.env.example](.env.example).

## Development

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev]" httpx2
.venv/bin/pytest                # 659 tests, no Proxmox host required
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy app tests        # strict
```

The agent is stubbed with an `httpx` transport that records requests; the signing test
re-implements the agent's verification, so a change on either side of that contract fails the
build rather than failing in production.

## Transaction boundary

The session dependency commits on success **and** on expected domain errors (`AppError` with
a 4xx status). That is deliberate: a failed login must still persist its audit row and its
lockout counter. Unexpected errors always roll back. The rule this places on services:
validate first, mutate second — never leave partial state before raising a 4xx.

The rule is stricter inside `RetentionGuard.transaction()`, which every guarded route uses:
it rolls back on *any* exception, so a mutation written on the way out of a 4xx is discarded
silently. Confirming an expired restore is the example — the refusal writes nothing, and the
restore executor's sweep performs the `expired` transition in its own transaction.

## Google Drive replication

The upload queue is `sync_tasks`, and it is a queue rather than a log: `attempt`,
`max_attempts` and `next_retry_at` *are* the retry state, so a restart resumes the backoff
schedule instead of starting it over.

| Concern | Decision |
| --- | --- |
| Who queues | The sync worker scans for successful backups marked `pending` each cycle. The run executor does not call it, so a backup that finished while the worker was stopped is still uploaded, and neither worker knows about the other |
| Retry | Exponential backoff with **full jitter**. The common failure is a Drive quota shared by every pending transfer; retrying in lockstep reproduces the burst that caused it |
| Is a retry safe? | Yes, unlike a backup. `rclone copyto` overwrites the same object; it cannot produce a second artifact the way a second `vzdump` would. That is what makes an automatic retry policy defensible here at all |
| Restart mid-transfer | Re-queued **without** burning an attempt — the process died, which is not the transfer's fault |
| While retrying | The backup row stays `pending`, not `failed`: the tile should show work outstanding, not a failure that is about to be attempted again |
| Verification | Compares size and MD5 — the digest Drive actually stores. A failed verification fails the *upload*; matching sizes with no available hash is `hash_unavailable`, never "verified" |

**Retention depends on all of this being honest.** It deletes a local artifact only after
both the candidate and its newest replacement set are either `not_required` or confirmed
uploaded with remote metadata. An upload reported as successful when it is not could
otherwise delete the only copy of a backup. That is why `/backups/{id}/verify` *writes*: a
remote copy found missing or corrupt clears the row's `uploaded` state.

## Retention and storage monitoring

Retention is ranked independently per `(vmid, guest_type)` and per location. Pinned backups
sit outside the rank, so a lock never consumes a keep slot; `not_required` rows do not
consume remote slots. A restore in `pending_confirmation`, `confirmed`, or `running` blocks
deletion. Removing one location keeps the history row successful while the other usable copy
exists, and only removing both changes it to `deleted`.

The worker performs a full rescan automatically at startup and then waits for post-commit
doorbells from copy-state, lock, retention/GDrive policy, and relevant legacy-job changes. It
has no healthy-state idle sweep; failed passes receive bounded delayed retries. A job-backed
run carries explicit provenance and frozen keep counts that remain valid if its job is later
edited or deleted;
manual/API backups use current global counts. Every pass resolves its guest from the newest
eligible backup immediately before execution, while ambiguous legacy orphan runs fail closed.
Applying passes record every decision in `retention_events`; the admin-only
`POST /api/v1/retention/preview` accepts optional `keep_local`, `keep_remote`, and `backup_id`
values but makes no agent calls, deletions, soft-delete changes, or event writes.

The storage sampler runs immediately at worker startup and then every 15 minutes by default.
Local failure writes no snapshot; remote failure preserves the local sample with nullable
remote fields. Effective usage is
`clamp((total_bytes - free_bytes) / total_bytes * 100, 0, 100)`, classified against the
configured warning/critical thresholds. Each committed sample publishes `storage.update`;
its alert flag is true only when severity moves upward into warning or critical.

## Restore

A restore is the only operation ProxSync performs that destroys data, and everything about the
flow follows from that.

| Concern | Decision |
| --- | --- |
| Authorisation | Two phases. `POST /restores` records a `pending_confirmation` row and a five-minute token; the restore starts only when a second request echoes that token **and** the literal target VMID. The token is stored as a SHA-256 and cleared on use, so it cannot be replayed |
| Staleness | Confirmation re-runs every preflight check against the live host. The archive may have been deleted or the target started while the dialog was open; a new blocker returns 409 with the fresh report and the restore stays pending |
| Unevaluable checks | Fail. An unreachable agent or an unlisted storage blocks the restore — a report where "could not check" read as "checked, fine" would be worse than no report |
| Missing digest or size | A warning, not a refusal. The backup is real; refusing it would be a policy the artifact does not justify. A digest that *is* recorded is enforced by the agent before anything is spawned |
| Concurrency | One at a time, globally. The agent holds a single restore slot, the executor claims one row, and a second restore cannot be confirmed while one is authorised |
| Drive-only source | Downloaded to the host inside the same task, before the restore starts, then `local_deleted_at` is cleared under the retention guard |
| Unknown outcome | `interrupted`, never `failed` and never re-issued. Telling an operator nothing happened invites a second `qmrestore` over a guest already being rebuilt |
| Restart mid-restore | A live agent task is adopted and followed to its real end; a row with no task id becomes `interrupted` |
| Cancellation | A `running` restore is cancelled at the agent and reaches its terminal state when the agent reports back — only the agent knows whether the process died before or after it began rewriting the guest |

Creation and confirmation both commit inside the shared retention guard, so an active restore
is visible to retention's blocker from the moment preflight approved the archive. A manual
`DELETE /backups/{id}` refuses the same source, for the same reason.

The four viewer endpoints expose independent live local/remote truth, historical snapshots,
a trailing-30-day ordinary least-squares forecast, and successful undeleted local bytes
grouped by `(vmid, guest_type)`. Unavailable upstream values stay null with diagnostic detail
instead of being rendered as zero.

## Notifications

Telling the operator is never on the critical path of the work being reported.

| Concern | Decision |
| --- | --- |
| Durability | The outbox row is written **in the same transaction as the state change it describes**. A message cannot describe something that was rolled back, and a state change cannot happen unannounced |
| Delivery | At-least-once. The attempt is charged *before* the send, so a process that dies mid-request leaves a scheduled retry rather than a row claiming an attempt that never happened. Sending twice destroys nothing — the opposite of the trade `vzdump` and `qmrestore` force |
| Retry policy | A transport error, a 429 or a 5xx is requeued with exponential backoff and full jitter, honouring Telegram's own `retry_after`. `400 chat not found`, `401 Unauthorized` and missing configuration are terminal on the first attempt: retrying them only delays a failure the operator has to see |
| Exactly once | Run- and restore-scoped events carry a unique `dedupe_key`, so a second enqueue writes nothing whatever the clock says. The guarantee is a row, not a timer, which is why a resumed run does not announce a second start |
| Duplicate storms | Genuinely recurring events (storage thresholds, retention passes) are suppressed for a window, and the repeat is **recorded** as `suppressed` with `suppressed_by`. "We decided not to tell you at 03:14" is itself information |
| Granularity | Per run, not per guest. Fifty failing guests send one message naming them. A partial run reports as `backup_failed`, because at least one guest has no backup |
| Cancellation | Silent. An alert for something the operator did themselves teaches them to ignore alerts |
| Message text | Rendered and frozen at enqueue time, with every interpolated value HTML-escaped. A message that sat through an outage says what was true when it was written |
| Credentials | The bot token is an operator setting, Fernet-encrypted in the database and write-only over the API. It is a URL path segment at Telegram, so the client redacts it from every log line |

`POST /notifications/telegram/test` sends synchronously — "it will be attempted shortly" is not
an answer to "is this token right?" — works while notifications are still disabled, and returns
a 200 carrying Telegram's own `error_code` and `description` when the message is rejected.

## Logs

A log call must never block, never raise, and never open a database session: call sites are
routinely inside a transaction, and a synchronous write there would deadlock on SQLite's single
writer or recurse through SQLAlchemy's own logging.

So the structlog processor appends to a bounded deque and returns; `LogWriter` drains it.

| Situation | Behaviour |
| --- | --- |
| Buffer full | The **oldest** entry is dropped and counted. `/logs` and `/health/detail` report the count, so an incomplete page says so rather than reading as a quiet night |
| Database unavailable | The batch goes back to the front of the queue. A hiccup costs latency, not log lines |
| Foreign key no longer resolves | The batch is rewritten with the links cleared and the ids left in `context`. The line is the point, not the join, and one rolled-back reference must not wedge every batch behind it |
| The writer's own logging | Capture is paused for the whole write, so a persistence failure cannot generate the line describing it and refill the queue it just drained |
| Retention | `general.log_retention_days` (90) and `general.audit_retention_days` (730). Different on purpose: logs are diagnostic, the audit trail is evidence |

Only structlog calls are captured. Standard-library loggers do not pass through this chain,
which is what stops the writer's own queries from generating the lines that describe them.
