# ProxSync — Security Model

This document is the standalone security reference for ProxSync. The threat model and the
trust boundaries are also summarised in [ARCHITECTURE.md](ARCHITECTURE.md) §8; this expands on
them and is the place to look before changing anything that touches the boundary.

---

## 1. The central problem

Proxmox backup and restore commands (`vzdump`, `qmrestore`, `pct restore`) must run as **root
on the Proxmox host**. A web dashboard is a large attack surface. Running the dashboard on the
host would mean one web vulnerability equals host root.

ProxSync separates the two:

```text
  Browser ──TLS──▶ Dashboard (unprivileged LXC) ──mTLS + HMAC──▶ Agent (host, root)
```

The dashboard has the UI, the database and the policy, and **no host privilege**. The agent
has the privilege and **a fixed, closed command vocabulary** — it cannot be asked to run an
arbitrary command, only to perform one of a small set of validated operations.

---

## 2. Trust boundaries

| Boundary | Control |
| --- | --- |
| Browser → Dashboard | TLS (nginx). Session is a short-lived JWT access token + a rotating refresh token; CSRF double-submit on state-changing requests. |
| Dashboard → Agent | Mutual TLS **and** an HMAC signature over each request, with a TTL nonce cache and clock-skew rejection. Both are required; neither alone suffices. |
| Agent → Proxmox tools | A closed set of argv-only executors. No shell, ever. |
| Dashboard → Proxmox API | A **read-only** PVEAuditor token. There is no code path that can write to the host through this channel. |

The two secrets on the dashboard→agent path defend different failures: mTLS authenticates the
*channel* (and stops anyone without the client certificate from connecting at all), while the
HMAC signature authenticates each *request* and makes a replay detectable even if TLS were
terminated by a proxy in between.

---

## 3. No shell, and how that is enforced

`shell=True` appears nowhere in the repository, and neither do `os.system`, `os.popen`, or the
blocking `subprocess.run/call/check_output/Popen` APIs in agent code. Every child process is
created in **one place** (`agent/app/executors/base.py`) with `create_subprocess_exec` and an
argv **list** of arguments to an **absolute** binary path — there is no string a caller can
inject a second command into.

CI enforces this on every push: the `security-invariants` job in
`.github/workflows/agent.yml` greps for each banned primitive and fails the build if one
appears. It also asserts rclone is invoked with the safe verbs (`copyto`/`deletefile`, never
`copy`/`delete`/`sync`/`purge`) and an explicit `--retries`.

---

## 4. Input validation is the security boundary

Everything arriving from the network passes through `agent/app/validators/` before it can reach
an argv list:

- **VMIDs** are checked against the real guest configuration on the host, and optionally against
  an operator allow-list.
- **Storages** are checked against `pvesm status`; an unknown storage is refused.
- **Paths** are canonicalised and then checked for containment *after* symlink resolution, so a
  symlink cannot point an operation outside the dump root.
- **Remote names** are matched against an allow-list anchored with `\A`/`\Z` (not `^`/`$`, which
  match around a newline), and remote paths reject traversal, `:`, control characters and
  rclone's own filter metacharacters.
- **Filenames** must match the vzdump artifact pattern.

An allow-list left empty is a deliberate widening and is documented as such at each site — most
dangerously `PROXSYNC_AGENT_ALLOWED_REMOTES`, where an empty list would permit a *local* rclone
remote and turn an “upload” into an arbitrary file read. Name the remotes you use.

---

## 5. Secrets

| Secret | Where it lives | Protection |
| --- | --- | --- |
| Root secret (`PROXSYNC_SECRET_KEY`) | Environment only, never the database | Derives the JWT key and the settings-encryption key via HKDF |
| Settings secrets (e.g. Telegram bot token) | `settings` table | Fernet-encrypted with a key derived from the root secret; write-only in the API, returned only as a masked hint |
| Agent HMAC secret | Environment on both sides | Never logged; used only to sign/verify |
| Proxmox token | Environment | Read-only role (PVEAuditor) so exposure cannot escalate to a write |
| User passwords | `users` table | Argon2id, with transparent re-hashing on parameter changes |
| Agent bot token in logs | — | Redacted from every log line by the Telegram client |

Rotating the root secret invalidates all sessions and makes stored settings secrets unreadable
(re-enter them). This is expected — see [UPGRADE.md](UPGRADE.md#rotating-the-root-secret).

---

## 6. Hardening at the OS layer

Both the agent and the dashboard run under systemd units with aggressive sandboxing:
`NoNewPrivileges`, `ProtectSystem` (strict for the dashboard), `ProtectHome`, a restricted
`SystemCallFilter`, `RestrictAddressFamilies`, and a `MemoryMax`. The agent unit is
deliberately *looser* in a few specific, commented places — it keeps `RestrictNamespaces=no`
(pct restore creates namespaces), a shared `/tmp` and device access (vzdump snapshots need
them). The dashboard unit has no such exceptions because it needs none.

The agent's dedicated `table inet proxsync` accepts loopback and the configured dashboard source
to the agent port, then drops other sources to that port. Its persistent loader replaces only
that table in a syntax-checked transaction and never flushes the global ruleset. It has no
output hook, so rclone egress remains unaffected. This is defence in depth behind the
application's own `ALLOWED_CLIENT_NETWORKS` check and coexists with independently managed
Proxmox Firewall, native nftables, and iptables-compatibility chains.

---

## 7. What ProxSync deliberately does *not* do

- The agent has **no `getUpdates` and no webhook** for Telegram: the bot can send, but cannot be
  commanded. A compromised chat cannot drive a backup or restore.
- A restore is **never retried or re-issued automatically** — it destroys its target, so an
  ambiguous outcome is recorded as `interrupted` and left for a human, never re-run.
- Nothing widens what is backed up on its own: a newly discovered guest is added with backups
  **off**, so appearing in inventory can never silently enlarge the backup set.

---

## 8. Reporting a vulnerability

ProxSync is a homelab-scale project. If you find a security issue, please open a **private**
security advisory on the repository (GitHub → Security → Report a vulnerability) rather than a
public issue, and allow reasonable time for a fix before disclosure. Include the version, the
component (agent/dashboard/frontend), and the smallest reproduction you can.
