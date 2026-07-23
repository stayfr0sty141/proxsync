# Changelog

All notable changes to ProxSync are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Repaired the frontend npm lockfile and patched vulnerable transitive PostCSS/Sharp versions.
- Pinned maintained GitHub Actions releases and made Python dependency audits resolve local
  project metadata without treating private editable packages as missing from PyPI.
- Made the host installer validate options before mutation, compare complete TLS SAN sets,
  recover partial PKI safely, verify real rclone remotes without logging credentials, and
  restore the previous service/configuration/PKI/firewall state after a failed installation.
- Replaced the reboot-volatile firewall apply with an idempotent, transactional, boot-persistent
  loader that owns only `table inet proxsync`; added explicit unchanged/removal modes and
  namespace integration coverage.

## [0.1.0] — 2026-07-23

The first tagged release. Every component is code-complete and tested against fakes that
replay captured command output and canned HTTP responses; live on-host validation is tracked
in docs/HANDOFF.md.

### Added

- **Backup Agent (M1)** — FastAPI service for the Proxmox host: mTLS + HMAC request signing,
  a closed command vocabulary (`vzdump`, `qmrestore`, `pct restore`, `pvesm`, checksum,
  rclone), argv-only executors, a validator layer as the security boundary, and a task
  registry with restart reconciliation.
- **Dashboard core (M2)** — SQLAlchemy 2.0 models for all 15 tables, Alembic migrations,
  Argon2id auth with rotating refresh tokens and family revocation, Fernet-encrypted settings
  secrets, and the mTLS/HMAC `AgentClient`.
- **Backup engine (M3)** — inventory sync, database-as-queue run executor, APScheduler-based
  schedules with correct crontab→APScheduler weekday translation, and an SSE event stream.
- **Google Drive sync (M4)** — rclone transfers on the host, a retry queue with jittered
  backoff, MD5-based verification, and local/remote browsing and comparison.
- **Retention & storage monitor (M5)** — per-guest retention that runs only after backup *and*
  upload success, a storage sampler, and a 30-day OLS forecast.
- **Restore (M6)** — a two-phase preflight→confirm flow with a single-use, TTL-bound token,
  nine preflight guards re-run against the live host on confirm, and a database-as-queue
  restore worker.
- **Telegram & logs (M7)** — a transactional outbox with at-least-once delivery, duplicate-storm
  suppression, a send-only Telegram client, and non-blocking log persistence behind `/logs`.
- **Frontend (M8)** — Next.js 16 / React 19 dashboard: every page from docs/UI.md, real-time
  SSE wiring, four explicit data states (loading/empty/error/partial), and AA-accessible
  status indicators.
- **Packaging & release (M9)** — `deploy/lxc/install.sh` container provisioning, nginx reverse
  proxy, hardened systemd units for the API and Web UI, database self-backup/restore and
  upgrade scripts with a daily backup timer, dependency-audit and frontend CI plus Dependabot,
  and the install / upgrade / security / troubleshooting / contributing guides.

### Fixed

Ten subtle bugs found while building, each invisible in normal operation and each with a
regression test — see docs/HANDOFF.md §5 for the full accounting. Highlights: `BigInteger`
primary keys never autoincrementing on SQLite; a weekly schedule firing on the wrong day; a
run queue that was accidentally a stack; `rstrip("/s")` destroying transfer sizes; and a lost
agent response being misread as a definitive failure.

[Unreleased]: https://github.com/stayfr0sty141/proxsync/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/stayfr0sty141/proxsync/releases/tag/v0.1.0
