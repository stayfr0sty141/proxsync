# ProxSync — Architecture

## 1. Topology

```
┌──────────────────────────── Proxmox VE Host (root) ────────────────────────────┐
│                                                                                │
│   /mnt/backup-hdd/dump          ← PVE backup storage (vzdump target)           │
│   /mnt/backup-hdd/tmp           ← vzdump scratch space                         │
│                                                                                │
│   ┌────────────────────────────────────────────────────────────────────────┐   │
│   │  proxsync-agent.service      FastAPI · 127.0.0.1 + vmbr0 · mTLS         │   │
│   │  ────────────────────────────────────────────────────────────────────  │   │
│   │  Closed command vocabulary, argv lists only, never shell=True:         │   │
│   │    vzdump · qmrestore · pct restore · pvesm · rclone · stat/du          │   │
│   │  In-process task registry + append-only per-task log files             │   │
│   └────────────────────────────────────────────────────────────────────────┘   │
│                    ▲                                    ▲                      │
│                    │ HTTPS (mTLS + HMAC)                │                      │
│  ══════════════════╪═══════ TRUST BOUNDARY ═════════════╪═══════════════════   │
│                    │                                    │                      │
│   ┌────────────────┴───────────────── LXC (unprivileged) ──────────────────┐   │
│   │  proxsync-api.service        FastAPI · uvicorn · 127.0.0.1:8000        │   │
│   │    APScheduler · SQLAlchemy · repositories · services                  │   │
│   │  proxsync-web.service        Next.js · 127.0.0.1:3000                  │   │
│   │  nginx                       :443 — TLS, static, /api reverse proxy    │   │
│   │  /var/lib/proxsync/proxsync.db      SQLite (WAL)                       │   │
│   └───────────────────────────────────────────────────────────────────────┘   │
│                    │ HTTPS (read-only PVEAuditor API token)                    │
│                    └──────────────► Proxmox VE API :8006                       │
└────────────────────────────────────────────────────────────────────────────────┘
                     │
                     └──────────────► Google Drive (rclone, executed on host)
                     └──────────────► Telegram Bot API (from LXC, egress only)
```

## 2. Design principles

1. **One privileged surface.** Only the agent runs privileged, and it exposes a fixed set of
   operations, not a command runner. Adding a capability means adding a typed endpoint and an
   executor with its own validator — never a new argument passed through from the dashboard.
2. **The dashboard owns policy, the agent owns execution.** Retention rules, schedules,
   history and notifications live in the dashboard. The agent has no database and no opinion;
   it starts processes, reports task state, and streams logs.
3. **Everything is a task.** `vzdump`, `qmrestore`, `pct restore`, and every rclone transfer
   produce an agent task id. The dashboard polls task state and mirrors it into its own
   history rows, so a dashboard restart never loses track of in-flight work.
4. **Fail closed.** Unknown VMID, unknown storage, unknown mode, expired signature, missing
   confirmation → HTTP 4xx, no process spawned.
5. **Portable persistence.** Repository interfaces sit between services and SQLAlchemy; no raw
   SQL, no SQLite-only types. Swapping to PostgreSQL is a settings change plus a migration run.

## 3. Component responsibilities

### 3.1 Backup Agent (Proxmox host)

Single-purpose FastAPI service. No database, no ORM, no UI, ~15 files.

| Layer | Responsibility |
|---|---|
| `api/routes/` | Thin HTTP handlers; Pydantic request models only |
| `validators/` | VMID allow-list, storage allow-list, mode/compression enums, path canonicalisation |
| `executors/` | `argv` construction and `asyncio.create_subprocess_exec` invocation |
| `tasks/` | Task registry: id, state, pid, exit code, log path, timestamps; survives via on-disk journal |
| `core/` | Config, mTLS/HMAC auth, structured logging, concurrency limits |

**Execution rules, enforced by design and by CI grep:**

- `subprocess` is invoked with an argument **list**; `shell=True` is banned repo-wide.
- Every path is resolved with `Path.resolve()` and must be a descendant of a configured root
  (`/mnt/backup-hdd/dump`); symlink escapes are rejected after resolution.
- VMIDs are integers in `100..999999999` **and** must appear in the live PVE inventory **and**
  in the configured allow-list.
- Storage identifiers are matched against `pvesm status` output, not accepted as free text.
- One backup task at a time per node (configurable); restores are serialised globally.

**Privilege model.** `vzdump`, `qmrestore` and `pct restore` require root and cannot be
meaningfully constrained by `sudoers` argument matching (the arguments are dynamic; a pattern
loose enough to be useful is loose enough to be abused). ProxSync instead runs the agent as
root under a hardened systemd unit — `NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectHome=yes`, `PrivateTmp=yes`, `ReadWritePaths=/mnt/backup-hdd /var/log/proxsync-agent`,
`RestrictAddressFamilies`, `SystemCallFilter=@system-service`, `MemoryMax`, `IPAddressAllow`
limited to the dashboard LXC address — and treats the request validator as the real security
boundary. This is documented in `docs/SECURITY.md` (M1) with the residual risk stated plainly.

### 3.2 Dashboard API (LXC)

Clean-architecture layering, dependencies point inward:

```
api/v1/routes  →  services  →  repositories  →  db/models
                     ↓
                  clients (agent, proxmox, telegram)
```

- **routes** — HTTP concerns only: auth dependency, request/response schemas, status codes.
- **services** — use cases: `BackupService`, `RestoreService`, `RetentionService`,
  `SyncService`, `NotificationService`, `StorageService`, `AuthService`. No ORM imports.
- **repositories** — one per aggregate, defined as a `Protocol` and implemented over
  SQLAlchemy. Services depend on the protocol, which is what makes them unit-testable without
  a database and what keeps PostgreSQL migration mechanical.
- **clients** — `AgentClient` (httpx, mTLS, retry with jitter, circuit breaker),
  `ProxmoxClient` (read-only inventory and storage), `TelegramClient`.
- **scheduler** — APScheduler uses an in-memory schedule rebuilt from the durable
  `backup_jobs` table on startup; timezone-aware (`Asia/Jakarta` default), with misfire grace
  and coalescing so a container reboot does not fire six missed weekly jobs at once.
- **workers** — long-running reconcilers: run executor (agent task → history row), upload
  queue, restore executor, retention sweeper, storage sampler, notification outbox, log writer.

Dependency injection is FastAPI `Depends` at the edge, constructor injection underneath.
Every service receives its repositories and clients; nothing reaches for a global.

### 3.3 Frontend (LXC)

Next.js App Router. Server components for shells and static chrome, client components for
anything live. TanStack Query owns server state, with an SSE subscription (`/api/v1/events/stream`)
invalidating query keys on task progress so the UI updates without polling loops.

## 4. Control flow: a scheduled backup

```
APScheduler fires (0 1 * * 0, Asia/Jakarta)
  └─ BackupService.run_job(job_id)
       ├─ resolve targets  (job targets ∩ live PVE inventory ∩ allow-list)
       ├─ create backup_run row  (status=running)
       ├─ queue BACKUP_STARTED in the same transaction              → outbox → Telegram
       └─ for each guest, sequentially:
            ├─ create backup_history row (status=running)
            ├─ AgentClient.start_backup(...) → agent_task_id
            ├─ TaskPoller mirrors agent task state every 5s → history row
            ├─ on success: record size, path, duration, sha256
            │    ├─ upload requested → enqueue upload  → SyncService
            │    │    ├─ AgentClient.sync_upload(...) → task id
            │    │    └─ on success: upload_status=uploaded, remote_path, verified checksum
            │    │         └─ after commit: notify RetentionWorker(backup_id)
            │    │    └─ retries spent → queue UPLOAD_FAILED
            │    └─ no upload required
            │         └─ after commit: notify RetentionWorker(backup_id)
            └─ on failure: status=failed, capture agent log tail
       └─ close backup_run, queue BACKUP_SUCCESS or BACKUP_FAILED in the same transaction
```

Notifications are **per run**, not per guest. A run where two of fifty guests failed sends one
`backup_failed` naming those two, rather than fifty messages — and a partial run reports as a
failure because at least one guest has no backup, which is what the operator has to act on.
Per-guest detail is in the history table and the log, where it can be read at leisure. A
cancelled run notifies nothing: the operator who stopped it already knows.

Retention starts with a full database-derived rescan, then waits for post-commit doorbells
from copy-state, lock, and policy changes. There is no healthy-state idle timer that would
duplicate audit rows; failed passes have bounded delayed retries. The startup pass recovers
work whose in-memory notification was lost with a restart, and each execution reselects the
newest eligible backup per `(vmid, guest_type)` immediately before resolving policy. A
job-backed run carries explicit provenance and frozen counts that survive job deletion;
manual/API backups use current global counts, while ambiguous legacy orphan runs fail closed.
The single API process shares a retention guard across final classification, physical delete,
copy-state changes, pins, and policy commits, closing the time-of-check/time-of-use window.
Running multiple API worker processes would require replacing it with a durable database claim
or advisory lock; the supported entry point intentionally starts one uvicorn process.

Keep slots are per guest and per location. Pinned rows are outside the ranking rather than
occupying a slot. Before deleting a local candidate, both it and the newest replacement set
must be `not_required` or safely `uploaded` with remote metadata. Active restores in
`pending_confirmation`, `confirmed`, or `running` block deletion. Deleting one physical copy
keeps the history row successful while the other copy exists; only loss of both locations
sets it to `deleted`. Every applying decision is written to `retention_events`, while
`POST /retention/preview` runs the same classifier without agent calls, deletes, or writes.

## 5. Control flow: storage monitoring

```
Application worker startup
  └─ StorageSampler.sample_once() immediately, then every 15 minutes by default
       ├─ read local filesystem + pvesm status from the agent
       ├─ independently read remote quota when Drive sync is enabled
       ├─ local unavailable  → no snapshot (never substitute zero)
       ├─ remote unavailable → persist valid local values + null remote values
       └─ commit storage_snapshots row
            ├─ emit storage.update
            └─ alert=true only when severity moves upward to warning/critical
```

Effective utilisation is based on writable space:
`clamp((total - free) / total × 100, 0, 100)`. This is intentionally not `used / total`,
because filesystem-reserved blocks may make `used + free != total`. Warning and critical
thresholds come from retention settings (85%/95% by default).

The read API preserves partial truth: `/storage` calls local and remote independently and
returns nullable unavailable values with diagnostics. `/storage/history` reads committed
samples, `/storage/forecast` performs an ordinary least-squares fit over the trailing 30
days and reports insufficient or stable data instead of inventing a full date, and
`/storage/by-guest` groups successful undeleted local bytes by `(vmid, guest_type)` using the
newest eligible name.

## 6. Control flow: a restore (two-phase, never immediate)

```
POST /api/v1/restores/preflight  → runs every check, writes nothing, returns the report
POST /api/v1/restores            → creates restore_history row, status=pending_confirmation
                                    returns preflight report + confirmation_token (TTL 5 min)
   preflight checks:
     · the backup succeeded and has an artifact
     · the archive is on the host (or a verified Drive copy exists to download first)
     · the archive matches the recorded digest
     · target VMID free, or explicitly marked for overwrite
     · the target's guest type matches the archive's
     · the target is stopped, or force_stop was set
     · the target node is the node this agent serves
     · target storage exists, is active, and has free space ≥ backup size × 1.15
     · no other restore is confirmed or running
POST /api/v1/restores/{id}/confirm
     body must echo the confirmation_token AND the literal target VMID
                                 → every check is re-run against the live host; a new blocker
                                   returns 409 and the restore stays pending
                                 → status=confirmed, token hash cleared (single use)
RestoreWorker claims the confirmed row (the database is the queue)
                                 → gdrive source? download to the host first, same task
                                 → AgentClient.start_restore → status=running → polled
                                 → success | failed | cancelled | interrupted
```

A check that could not be *evaluated* fails. An unreachable agent, an unlisted storage, an
inactive one: each blocks. A report where "could not check" read as "checked, fine" would be
worse than no report at all. A missing checksum or unrecorded size is a warning instead — the
backup is real, and refusing to restore it would be a policy the artifact does not justify.

The UI additionally requires a typed-VMID confirmation dialog when the target is an existing
guest. Overwriting a running guest is refused unless `force_stop` is explicitly set.

**A restore is never retried or re-issued.** `qmrestore` and `pct restore` destroy the target
and rebuild it, so where the outcome is unknown the row records `interrupted` and waits for a
human — the same rule the run executor applies to `vzdump`, for a strictly worse failure mode.
A row left `running` by a crash is adopted if the agent still knows the task, and recorded as
`interrupted` if it does not. Restores are serialised globally: the agent holds one restore
slot, the executor claims one row at a time, and a second restore cannot be confirmed while
one is authorised.

Creation and confirmation both run inside the shared retention guard, so an active restore row
is visible to retention's blocker from the moment preflight approved the archive rather than
shortly afterwards. Retention and a manual `DELETE /backups/{id}` refuse the same source.

## 7. Control flow: notifications and log persistence

Two things happen alongside every state change above, and both are deliberately *out of the
path* of the work they describe.

```
Any worker, inside the transaction that records the state change
  └─ NotificationGateway.emit(event, variables, dedupe_key, unique)
       ├─ channel off, or this event's toggle off → nothing written
       ├─ unique key already present               → nothing written (survives a restart)
       ├─ identical report inside the window       → row written as `suppressed`
       └─ otherwise → row written `pending`, message rendered and frozen now
            └─ after commit: ring the outbox doorbell

NotificationWorker (the database is the queue, oldest first)
  └─ claim → charge the attempt → send (no session held across the network)
       ├─ ok                       → sent
       ├─ retryable (429/5xx/net)  → pending + backoff, honouring Telegram's retry_after
       └─ refusal or unconfigured  → failed immediately; `resend` is the way back
```

Delivery is at-least-once, which is the opposite of the choice `vzdump` and `qmrestore` force:
sending a message twice destroys nothing, so the attempt is charged before the request and a
lost response is retried rather than left ambiguous.

```
Any structlog call anywhere
  └─ processor appends to a bounded in-memory deque and returns   (never blocks, never raises)
       │   full? drop the OLDEST and count it — logging must not stall a backup
LogWriter
  └─ drain a batch → insert with capture paused → prune on a long timer
       ├─ database error   → batch restored to the front of the queue
       └─ foreign key gone → rewritten with links cleared; the ids stay in `context`
```

The processor never opens a session: log calls happen inside transactions, and a synchronous
write there would deadlock on SQLite's single writer or recurse through SQLAlchemy's own
logging. Only structlog calls are captured — standard-library loggers do not pass through this
chain, which is what stops the writer's own queries from generating the lines describing them.

## 8. Trust boundaries and threat model

| Boundary | Control |
|---|---|
| Browser → Dashboard | TLS; JWT access token (15 min, memory only); refresh token in `HttpOnly; Secure; SameSite=Strict` cookie; CSRF double-submit token on all cookie-authenticated mutations; login rate-limited per IP+username with exponential lockout |
| Dashboard → Agent | mTLS with a pinned client certificate, **plus** HMAC-SHA256 request signature over `method|path|timestamp|nonce|sha256(body)`; ±60 s clock window; nonce cache rejects replay; agent firewalled to the LXC address |
| Dashboard → PVE API | Dedicated API **token** with `PVEAuditor` role only — read-only inventory and storage stats. It cannot start, stop or delete anything |
| Dashboard → Telegram | Egress only; bot token encrypted at rest (Fernet, key from `PROXSYNC_SECRET_KEY` env, never in the database) |
| Agent → filesystem | Path canonicalisation + root-containment check; deletes restricted to files matching the vzdump artifact pattern under the dump root |

**Explicitly out of scope for v1:** multi-node clusters (single host assumed), PBS integration,
and multi-tenant RBAC beyond `admin`/`operator`/`viewer`.

## 9. Failure handling

| Failure | Behaviour |
|---|---|
| Agent unreachable | Circuit breaker opens after 3 consecutive failures; dashboard shows a persistent banner; scheduled jobs are marked `skipped_agent_unavailable`, never silently dropped |
| Dashboard restarts mid-backup | Task poller reconciles on boot: any `running` history row with an `agent_task_id` is re-queried; the agent's on-disk task journal survives its own restart too |
| Upload fails | Exponential backoff, configurable retry count; backup stays `upload_status=failed`; retention refuses to delete anything that depends on it |
| Disk full | Storage sampling classifies effective writable-space usage and emits one upward-transition alert; failed reads remain unavailable rather than becoming fake zeroes |
| Clock skew | HMAC window rejects requests; surfaced as a distinct diagnostic, not a generic 401 |
| Telegram unreachable | Messages queue in the outbox and retry with jittered backoff; nothing is lost and nothing blocks. A refusal Telegram would repeat forever (bad token, unknown chat) fails immediately instead of spending five attempts, and `/health/detail` reports the delivery error |
| Log writer behind | The capture buffer drops its **oldest** entries and counts them; `/logs` and `/health/detail` report the count, so an incomplete page says so rather than reading as a quiet period |

## 10. Open decisions

These record the initial design decisions: D1-D3 are settled in the implemented
architecture, while D4 remains open before the first release.

- **D1 — rclone placement.** Backups live on the host; the dashboard must not run shell
  commands. Recommendation: **rclone runs on the host, driven by new agent endpoints**
  (`POST /sync/upload|download|verify`, `GET /sync/list`). This extends the agent's endpoint
  list beyond the six in the brief, all of them typed and validated. The alternative — a
  read-only bind mount into the LXC plus rclone there — would put a shell-executing component
  in the dashboard, which the brief forbids.
- **D2 — guest inventory source.** Recommendation: **query the PVE API directly from the
  dashboard using a read-only `PVEAuditor` token**. It is an HTTPS call, not a shell command,
  and it keeps the privileged agent smaller. Alternative: a `GET /inventory` agent endpoint.
- **D3 — agent privilege.** Recommendation: **root + hardened systemd unit** (rationale in
  §3.1). Alternative: dedicated user + `sudoers`, which is weaker than it looks.
- **D4 — licence.** MIT is scaffolded; switch before first release if a patent grant or
  copyleft is wanted.
