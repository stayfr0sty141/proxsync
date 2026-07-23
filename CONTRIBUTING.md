# Contributing to ProxSync

Thanks for your interest. ProxSync is a security-sensitive project — it holds root on a
Proxmox host through the agent — so the bar for changes is deliberately high. This guide is
what keeps that bar consistent.

---

## Ground rules

1. **Production quality, no placeholders.** A change is complete and tested, or it is not
   started. There are no `TODO` bodies in the tree.
2. **Comments explain *why*, not *what*.** The code says what; a comment earns its place by
   explaining a decision, a constraint, or a trap. See the bug notes in
   [docs/HANDOFF.md](docs/HANDOFF.md) §5 for the house style.
3. **Documentation ships with the code.** If you change behaviour, update the relevant file in
   `docs/` in the *same* change.
4. **The security invariants are not negotiable.** No shell, ever (see below).

---

## Development setup

```bash
make agent-install       # agent virtualenv (Python >=3.11)
make backend-install     # backend virtualenv (Python 3.13)
make frontend-install    # frontend node_modules
```

Run everything before you push:

```bash
make check               # lint + types + tests for all three components
```

Or one component at a time: `make agent-check`, `make backend-check`, `make frontend-check`.

---

## The definition of done

Every change is held to the same bar as a module in the [roadmap](docs/ROADMAP.md):

- Fully typed — `mypy --strict` (backend, agent), `tsc --noEmit` (frontend).
- `ruff` + `ruff format` clean; `eslint` + `prettier` clean.
- Unit tests for services and validators; integration tests for routes against a temp SQLite.
- Structured logs with a correlation id on every code path.
- Errors mapped to RFC 9457 problem responses — no bare 500s.
- No `shell=True`, no string-built commands, no secrets stored in clear text.
- An Alembic revision when the schema changes — and it must reverse (CI runs `downgrade base`).
- `docs/` updated in the same change.

---

## The security invariants (enforced by CI)

The `security-invariants` job in `.github/workflows/agent.yml` fails the build on:

- `shell=True`, `os.system`, `os.popen` anywhere in the repository.
- Blocking `subprocess` APIs in `agent/app` — use the `ProcessRunner`
  (`create_subprocess_exec`, argv lists, absolute binaries).
- rclone invoked without an explicit `--retries`, or with a directory verb
  (`copy`/`delete`/`sync`/`purge`) instead of `copyto`/`deletefile`.
- Any shell script under `deploy/` or `scripts/` that fails `bash -n` or shellcheck.

If you are adding a new host operation, it goes through a validator in
`agent/app/validators/` first — that layer *is* the security boundary. Read
[docs/SECURITY.md](docs/SECURITY.md) §4 before you start.

---

## Commits and pull requests

- Keep each PR to one logical change. A green `make check` is the entry ticket.
- Describe *why* in the PR body; link the issue or the roadmap item.
- If you found and fixed a subtle bug, add it to `docs/HANDOFF.md` §5 with a one-paragraph
  explanation — those notes are one of the most valued parts of this project.

---

## Reporting security issues

Do **not** open a public issue for a vulnerability. Use a private GitHub security advisory — see
[docs/SECURITY.md](docs/SECURITY.md) §8.

---

## Working agreement on milestones

ProxSync is built one module at a time (M0–M9). A module is delivered, reported, and confirmed
before the next begins. If you are picking up a new area, read
[docs/HANDOFF.md](docs/HANDOFF.md) first — it is written for exactly that.
