# ProxSync

![Version](https://img.shields.io/badge/version-0.1.0-blue?style=for-the-badge)
![License](https://img.shields.io/badge/license-MIT-green?style=for-the-badge)
![Python](https://img.shields.io/badge/python-3.13%20%7C%203.11+-yellow?style=for-the-badge&logo=python)
![Status](https://img.shields.io/badge/status-M0--M9%20complete-success?style=for-the-badge)

![ProxSync Logo](https://raw.githubusercontent.com/stayfr0sty141/proxsync/main/docs/assets/logo-dark.svg)

**The batteries-included backup dashboard your Proxmox homelab deserves.**
Schedule, replicate, retain, restore — all from a clean web UI, without compromising your hypervisor.

---

## ✨ Why ProxSync?

Proxmox VE is a fantastic hypervisor, but its built-in backup tooling leaves gaps: no
out-of-the-box cloud replication, no granular retention policies per guest, no friendly
dashboard for the family admin who just wants to know their VMs are safe.

**ProxSync fills every gap:**

- 🗓️ **Flexible scheduling** — cron-driven or one-click manual backups per guest
- ☁️ **Cloud replication** — automatic rclone push to Google Drive (more backends planned)
- 🗂️ **Per-guest retention** — keep N daily, M weekly, K monthly snapshots — per VM/CT
- 🔒 **Guarded restores** — restore to a different VM ID by default; override with intent
- 📊 **Storage dashboard** — at-a-glance datastore usage, backup sizes, and trends
- 📱 **Telegram notifications** — success, failure, or both; per-job configurable
- 🛡️ **Security-first** — the dashboard never runs a shell command. Zero `shell=True`. CI enforces it.

---

## 🧱 Architecture at a Glance

Proxmox backup commands (`vzdump`, `qmrestore`, `pct restore`) require **root on the host**.
Running a web dashboard there? That's an unacceptable attack surface.

ProxSync splits into **two hardened halves** that talk over mutual TLS:

```text
┌─────────────────────────────────┐      mTLS       ┌──────────────────────────────┐
│        Dashboard (LXC)          │ ◄──────────────► │     Backup Agent (PVE Host)  │
│  ┌───────────┐  ┌────────────┐  │                  │  ┌──────────────────────────┐ │
│  │ Next.js UI│  │FastAPI+SQL │  │   fixed command  │  │  FastAPI (root)          │ │
│  │ shadcn/ui │  │APScheduler │  │   vocabulary     │  │  allow-listed subprocess │ │
│  └───────────┘  └────────────┘  │                  │  └──────────────────────────┘ │
│  Unprivileged user              │                  │  root, hardened              │
└─────────────────────────────────┘                  └──────────────────────────────┘
```

| Component | Runs on | Privilege | Role |
| --- | --- | --- | --- |
| **Dashboard** `backend/` + `frontend/` | Unprivileged LXC | Ordinary user | UI, auth, scheduling, retention policies, history |
| **Backup Agent** `agent/` | Proxmox host | `root`, hardened | Execute a **fixed, closed set** of backup/restore/storage ops |

> 🛡️ The dashboard **never** shells out. The agent accepts only a small, Pydantic-validated
> command vocabulary. No `shell=True`. No arbitrary commands. CI enforces this invariant.

---

## 🚀 Quickstart

Two halves, two installers. Run the agent on your Proxmox host first — it prints the mTLS
credentials the dashboard needs:

```bash
# 1️⃣ On the Proxmox host:
cd proxsync/deploy/host && ./install-agent.sh --agent-ip 10.0.0.10 --dashboard-ip 10.0.0.20

# 2️⃣ In the unprivileged LXC:
cd proxsync/deploy/lxc && ./install.sh --server-name proxsync.lan --agent-ip 10.0.0.10
```

Full walkthrough — including mTLS credential exchange, PVEAuditor token setup, and
**rclone Google Drive OAuth login** — lives in **[docs/INSTALL.md](docs/INSTALL.md)**.

---

## 📦 What's Inside

```text
proxsync/
├── agent/            FastAPI backup agent — runs on PVE host as root
├── backend/          Dashboard API — FastAPI + SQLAlchemy + APScheduler
├── frontend/         Next.js App Router + TypeScript + Tailwind + shadcn/ui
├── deploy/           systemd units, nginx config, LXC & host installers
├── docs/             Architecture, DB schema, API reference, UI spec, roadmap
├── scripts/          Dev helpers, DB backup/restore, upgrade tooling
└── .github/          CI workflows (lint, type-check, test, security audit)
```

---

## 📚 Documentation

| Doc | What you'll find |
| --- | --- |
| **[📖 HANDOFF](docs/HANDOFF.md)** | 👈 **Start here.** Current state, verified vs pending, settled decisions |
| **[🏗️ Architecture](docs/ARCHITECTURE.md)** | Trust boundaries, data flow, threat model, component topology |
| **[🗄️ Database](docs/DATABASE.md)** | Schema, ERD, indexes, portability (SQLite ↔ PostgreSQL) |
| **[🔌 API Reference](docs/API.md)** | Dashboard REST API & Agent REST API contracts |
| **[🎨 UI Spec](docs/UI.md)** | Design tokens, route map, page-by-page wireframes |
| **[🗺️ Roadmap](docs/ROADMAP.md)** | Milestones M0–M9 with acceptance criteria |
| **[📥 Install](docs/INSTALL.md)** | Step-by-step agent + dashboard install with mTLS |
| **[⬆️ Upgrade](docs/UPGRADE.md)** | In-place upgrades, DB self-backup/restore, secret rotation |
| **[🔐 Security](docs/SECURITY.md)** | Threat model, no-shell guarantee, OS hardening, disclosure policy |
| **[🔧 Troubleshooting](docs/TROUBLESHOOTING.md)** | Symptom-first fixes — start from `/health/detail` |
| **[🤖 Agent](agent/README.md)** | Agent security model, endpoints, PVE behaviour notes |
| **[⚙️ Backend](backend/README.md)** | Dashboard layering, auth model, DB portability, first-run |

---

## 🛠️ Development

```bash
make check     # Lint + type-check + test across all three components
make release   # Version-gated tag check — use before pushing a release
```

See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the full developer workflow.

---

## 🧰 Tech Stack

| Layer | Choices |
| --- | --- |
| **Backend** | Python 3.13 · FastAPI · SQLAlchemy 2.0 · Alembic · APScheduler · Pydantic v2 · httpx · structlog |
| **Agent** | Python ≥3.11 (PVE 8 compat) · FastAPI · stdlib `subprocess` (list-args only) |
| **Database** | SQLite by default, PostgreSQL-ready — zero dialect-specific SQL |
| **Frontend** | Next.js (App Router) · TypeScript · TailwindCSS · shadcn/ui · TanStack Query · Recharts |
| **Auth** | JWT access + rotating refresh tokens · Argon2id password hashing |
| **Deploy** | systemd · nginx reverse proxy · mTLS between agent & dashboard |

---

## 📄 License

MIT © ProxSync contributors. See **[LICENSE](LICENSE)**.
Contributions welcome under the same terms — read **[CONTRIBUTING.md](CONTRIBUTING.md)**.

---

*Built with ❤️ for the homelab community. v0.1.0 — M0–M9 complete.*
