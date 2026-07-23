# ProxSync — UI Design

Dark-first, information-dense, built for a storage operator who wants to read state in one
glance. Reference points: Proxmox VE's density, Synology DSM's card hierarchy, TrueNAS
Scale's calm palette. Explicitly *not* a consumer SaaS dashboard — no gradients, no oversized
hero numbers, no decorative illustration.

## 1. Design tokens

```css
/* Surfaces — layered, never pure black */
--bg-base:        #0d1117;   /* app background            */
--bg-surface:     #161b22;   /* cards, panels             */
--bg-elevated:    #1c2128;   /* modals, dropdowns, hover  */
--bg-inset:       #0a0d12;   /* code/log blocks, wells    */
--border:         #30363d;
--border-muted:   #21262d;

/* Text */
--fg-primary:     #e6edf3;
--fg-secondary:   #9ba7b4;
--fg-muted:       #6e7781;

/* Brand — Proxmox orange, used sparingly: primary actions and the active nav rail only */
--accent:         #e57000;
--accent-hover:   #ff8c1a;
--accent-subtle:  rgba(229,112,0,0.12);

/* Status — the only other saturated colours in the app */
--success:        #3fb950;   --success-subtle: rgba(63,185,80,0.14);
--warning:        #d29922;   --warning-subtle: rgba(210,153,34,0.14);
--danger:         #f85149;   --danger-subtle:  rgba(248,81,73,0.14);
--info:           #58a6ff;   --info-subtle:    rgba(88,166,255,0.14);
--running:        #58a6ff;   /* + 2s pulse animation */

/* Type */
--font-sans: "Inter", ui-sans-serif, system-ui, sans-serif;
--font-mono: "JetBrains Mono", ui-monospace, "SF Mono", monospace;  /* sizes, IDs, paths, logs */
/* scale: 11 / 12 / 13 / 14 / 16 / 20 / 24 / 30 px — 13px is the table/body default */

/* Geometry */
--radius-sm: 4px;  --radius-md: 6px;  --radius-lg: 8px;
--row-height: 40px;                      /* dense tables */
--sidebar-width: 232px;  --sidebar-collapsed: 56px;
--shadow-overlay: 0 8px 24px rgba(1,4,9,0.6);
```

A light theme ships behind the same token names (M8) — every component reads tokens, never
literals, so the switch is a class on `<html>`.

**Numbers are monospace and right-aligned** in tables (sizes, durations, VMIDs). Byte values
use binary units with one decimal (`8.5 GiB`). Durations render as `1h 08m`. Absolute
timestamps in the configured timezone, with a relative hint (`2 days ago`) as a tooltip.

## 2. Route map

```
/login
/                          Dashboard
/backups                   History (default landing for operators)
/backups/[id]              Detail: metadata, log viewer, actions
/schedules                 Job list
/schedules/[id]            Job editor + next-fire preview
/restore                   Restore wizard + restore history
/browser                   Local ⇄ Google Drive file browser
/sync                      Transfer queue and remote status
/storage                   Usage, trend, forecast, per-guest breakdown
/logs                      Filterable log console
/logs/audit                Security audit trail (admin)
/notifications             Notification outbox: what was sent, withheld, or is still queued
/settings/[section]        general | gdrive | telegram | retention | agent | users
```

## 3. Shell

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│ ▌ProxSync          pve · agent ●online          🔍 ⌘K    🔔 2    ◐    admin ▾       │  56px
├──────────────┬─────────────────────────────────────────────────────────────────────┤
│              │                                                                     │
│ ▣ Dashboard  │                                                                     │
│ ⛁ Backups  3 │                          page content                               │
│ ⏱ Schedules  │                                                                     │
│ ⟲ Restore    │                                                                     │
│ ⌸ Browser    │                                                                     │
│ ☁ Sync     1 │                                                                     │
│ ▤ Storage    │                                                                     │
│ ≡ Logs       │                                                                     │
│ ⚙ Settings   │                                                                     │
│              │                                                                     │
│──────────────│                                                                     │
│ ● Job running│  ← live mini-progress, always visible while work is in flight        │
│   lxc/104 42%│                                                                     │
└──────────────┴─────────────────────────────────────────────────────────────────────┘
```

- Sidebar collapses to icons below 1280 px, becomes a sheet below 768 px.
- The agent connectivity pill in the header is permanent: `●online 12ms` / `●offline` in
  `--danger` with a retry action. If the agent is down, the whole app must say so immediately —
  every write path depends on it.
- `⌘K` command palette: jump to a guest, start a backup, open a job.

## 4. Dashboard

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│  Overview                                              [ ⟲ Refresh ]  [ ⛁ Backup Now ]│
├──────────────────┬──────────────────┬──────────────────┬───────────────────────────┤
│ VIRTUAL MACHINES │ CONTAINERS       │ LAST BACKUP      │ NEXT BACKUP               │
│  6               │  11              │  ✔ Success       │  Sun 26 Jul · 01:00       │
│  5 running       │  10 running      │  3 days ago      │  in 4d 6h                 │
│  ────────────    │  ────────────    │  17 guests·90 GiB│  Weekly Full · 17 guests  │
├──────────────────┴──────────────────┴──────────────────┴───────────────────────────┤
│ LOCAL STORAGE  /mnt/backup-hdd                    │ GOOGLE DRIVE  gdrive:proxsync   │
│ ███████████████████████████░░░░░░░░░░  63.6 %     │ ████░░░░░░░░░░░░░░░░░  9.8 %    │
│ 296.4 GiB used · 169.4 GiB free · 465.7 GiB total │ 200 GiB used of 2 TiB           │
│ ⚠ ~41 days until full at current growth           │ last sync 3 days ago · ✔ 17/17  │
├───────────────────────────────────────────────────┴────────────────────────────────┤
│ RUNNING JOBS                                                                        │
│ ● lxc/104  homeassistant   snapshot   ███████████░░░░░░░░  42%   12.0/28.5 GiB  5m12s│
│ ○ vm/101   docker-host     queued                                                   │
├─────────────────────────────────────────────────────────────────────────────────────┤
│ RECENT ACTIVITY                                          FAILED (7 DAYS)        2   │
│ ✔ 19 Jul 01:00  Weekly Full   17 guests  90.1 GiB  1h08m  │ ✖ vm/103 timeout       │
│ ☁ 19 Jul 02:14  Upload        17 files   90.1 GiB  42m    │ ✖ upload lxc/108 quota │
│ ⟲ 14 Jul 09:31  Restore       vm/101→151 8.5 GiB   11m    │                        │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

Stat cards are 4-up ≥1440 px, 2-up ≥768 px, stacked below. Progress bars animate from SSE,
not polling. "Failed" counts are clickable filters into `/backups`.

## 5. Backup history

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│ Backup History                                                 [ ⛁ Backup Now ]      │
│ ┌────────┐┌────────┐┌──────────┐┌─────────┐┌──────────────┐            ┌──────────┐ │
│ │Type ▾  ││Status ▾││Upload ▾  ││Guest ▾  ││Last 30 days ▾│            │🔍 search │ │
│ └────────┘└────────┘└──────────┘└─────────┘└──────────────┘            └──────────┘ │
├──┬──────┬───────────────┬──────┬─────────┬──────────────┬────────┬────────┬────────┤
│☐ │ ID   │ GUEST         │ TYPE │    SIZE │ CREATED      │ DURA…  │ UPLOAD │ STATUS │
├──┼──────┼───────────────┼──────┼─────────┼──────────────┼────────┼────────┼────────┤
│☐ │ 101  │ docker-host   │ VM   │ 8.5 GiB │ 19 Jul 01:00 │  6m12s │ ☁ ✔    │ ✔ OK   │⋮│
│☐ │ 104  │ homeassistant │ LXC  │ 2.1 GiB │ 19 Jul 01:07 │  1m48s │ ☁ ✔    │ ✔ OK   │⋮│
│☐ │ 108  │ nextcloud     │ LXC  │ 41.2 GiB│ 19 Jul 01:09 │ 22m03s │ ☁ ✖ ↻  │ ✔ OK   │⋮│
│☐ │ 103  │ win-srv       │ VM   │       — │ 19 Jul 01:31 │ 30m00s │ —      │ ✖ FAIL │⋮│
│☐ │ 101  │ docker-host   │ VM   │ 8.4 GiB │ 12 Jul 01:00 │  6m40s │ ☁ ✔    │ ✔ OK 🔒│⋮│
├──┴──────┴───────────────┴──────┴─────────┴──────────────┴────────┴────────┴────────┤
│ 2 selected · [Upload] [Verify] [Delete]          ◀ 1 2 3 … 6 ▶   25/page · 143 total│
└─────────────────────────────────────────────────────────────────────────────────────┘
```

Row menu `⋮`: **Restore** · **Download** · **View log** · **Upload now** · **Verify** ·
**Lock (exempt from retention)** · **Delete**. 🔒 marks a retention-locked backup.
Destructive items are separated and rendered in `--danger`.

Failed rows show the error's first line inline on hover; the full log opens in a drawer.

## 6. Manual backup dialog

```
┌──── Backup Now ─────────────────────────────────────────────┐
│                                                             │
│ SELECT GUESTS                          17 available         │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ 🔍 filter                       [ All ] [ VMs ] [ LXCs ] │ │
│ │ ☑ 101  docker-host      VM   ● running     8.5 GiB last │ │
│ │ ☑ 104  homeassistant    LXC  ● running     2.1 GiB last │ │
│ │ ☐ 103  win-srv          VM   ○ stopped        — never   │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ MODE       ( ● ) snapshot   (   ) suspend   (   ) stop      │
│            Live backup, no downtime. Requires storage       │
│            snapshot support.                                │
│                                                             │
│ COMPRESSION  [ zstd ▾ ]  level [ 3 ]      STORAGE [backup-hdd ▾]│
│ ☑ Upload to Google Drive when finished                      │
│                                                             │
│ ─────────────────────────────────────────────────────────── │
│ 2 guests · ~10.6 GiB estimated · ~8 min                     │
│                              [ Cancel ]  [ Start Backup ]   │
└─────────────────────────────────────────────────────────────┘
```

Mode selection shows its consequence in plain language — `stop` warns that the guest will be
shut down and names the guests affected.

## 7. Restore wizard — deliberately slow

```
Step 1 Select backup   Step 2 Target   Step 3 ► Confirm
┌─────────────────────────────────────────────────────────────┐
│  ⚠  RESTORE WILL OVERWRITE DATA                             │
│                                                             │
│  SOURCE   vzdump-qemu-101-2026_07_19-01_00_04.vma.zst       │
│           VM 101 · docker-host · 8.5 GiB · 19 Jul 01:00     │
│           sha256 4f3a…c1b2  ✔ verified                      │
│  TARGET   VM 151  (new)   storage local-lvm   node pve      │
│                                                             │
│  PREFLIGHT                                                  │
│   ✔ Archive present locally      ✔ Checksum matches         │
│   ✔ VMID 151 is free            ✔ 412 GiB free (71 required)│
│   ⚠ Source node pve2 ≠ target node pve                      │
│                                                             │
│  Type the target VMID to confirm:   [ 151        ]          │
│  ☐ Start the guest after restore                            │
│                                                             │
│              [ Cancel ]   [ Restore VM 151 ]  ← danger btn  │
│  Confirmation expires in 4:51                               │
└─────────────────────────────────────────────────────────────┘
```

The action button stays disabled until the typed VMID matches. Restoring **over** an existing
guest adds a second checkbox ("I understand VM 101 will be destroyed") and turns the header
banner solid red.

## 8. Backup browser

Two-pane, local left, Drive right, with a middle status gutter.

```
┌──── LOCAL /mnt/backup-hdd/dump ────────┬─┬──── gdrive:proxsync/dump ────────────────┐
│ NAME                     SIZE   DATE   │ │ NAME                     SIZE   DATE     │
│ vzdump-qemu-101-…zst   8.5 GiB 19 Jul  │=│ vzdump-qemu-101-…zst   8.5 GiB 19 Jul    │
│ vzdump-lxc-104-…zst    2.1 GiB 19 Jul  │=│ vzdump-lxc-104-…zst    2.1 GiB 19 Jul    │
│ vzdump-lxc-108-…zst   41.2 GiB 19 Jul  │↑│ —                                        │
│ —                                      │↓│ vzdump-qemu-101-…zst   8.4 GiB 05 Jul    │
│ vzdump-qemu-103-…zst   6.0 GiB 12 Jul  │≠│ vzdump-qemu-103-…zst   6.0 GiB 12 Jul    │
├────────────────────────────────────────┴─┴──────────────────────────────────────────┤
│ = in sync (12)  ↑ local only (1)  ↓ remote only (1)  ≠ checksum mismatch (1)         │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

Gutter glyphs are also filters. Mismatched checksums are `--danger` and offer **Re-upload**.

## 9. Storage monitor

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│ LOCAL HDD  /mnt/backup-hdd                                                          │
│ ████████████████████████████░░░░░░░░░░░░░  296.4 / 465.7 GiB   63.6 %               │
│ ▏backups 291.0  ▏temp 4.2  ▏other 1.2  ▏free 169.4                                  │
│                                                                                     │
│ 30-DAY TREND                                          ⚠ ESTIMATED FULL: 41 days     │
│  400G ┤                                        ╭──                                  │
│  300G ┤                          ╭─────────────╯      growth  +4.1 GiB/day          │
│  200G ┤        ╭─────────────────╯                    projection from 30-day slope  │
│  100G ┼────────╯                                                                    │
│       └─────┬─────────┬─────────┬─────────┬────────                                 │
│           22 Jun    29 Jun     6 Jul    13 Jul                                      │
├─────────────────────────────────────────────────────────────────────────────────────┤
│ TOP CONSUMERS                          │ GOOGLE DRIVE                               │
│ nextcloud   LXC 108   82.4 GiB  2 bk   │ ████░░░░░░░░░░  200 GiB / 2 TiB            │
│ win-srv     VM  103   48.0 GiB  2 bk   │ 34 objects · last sync 19 Jul 02:14        │
│ docker-host VM  101   16.9 GiB  2 bk   │ ✔ verified 3 days ago                      │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

## 10. Logs

Console-style, monospace, virtualised list. Filter bar: category chips
(`api backup restore upload retention scheduler auth agent notify system`), level, time range,
free text, correlation-id. Clicking a correlation id pivots to every row sharing it — the
fastest path from "the Sunday job failed" to the exact vzdump stderr line.

```
2026-07-19 01:31:44  ERROR  backup   vm/103  vzdump exited 1: VM 103 qmp command 'guest-fsfreeze-freeze' failed  ⧉3f1c…
2026-07-19 01:31:44  INFO   notify   telegram → chat -100123 · backup_failed · sent
2026-07-19 02:14:02  WARN   upload   lxc/108 attempt 2/3 after 429 rate-limit, retry in 60s
```

**Export** offers NDJSON and CSV, streaming the current filter rather than the current page.

Two states this page must show honestly, because both make an empty list mean something other
than "nothing happened": when persistence is switched off the empty state says so instead of
rendering as a quiet night, and when the capture buffer has dropped entries a banner reports
the count, so an operator does not read a gap as evidence.

### Notifications

Reached from the Telegram settings section and from any alert. A table of the outbox — event,
status, when, attempts, and the message as it was actually rendered — with **Resend** on
anything `failed` or `suppressed`. A suppressed row links to the message it repeats, so
"why didn't I get told at 03:14?" is answerable rather than a mystery.

## 11. Settings

Left sub-nav (General · Google Drive · Telegram · Retention · Agent · Users), right form pane.
Every section: dirty-state bar with **Discard** / **Save**, inline validation, and a
**Test connection** action where a remote system is involved. Secrets render as
`••••••••  configured 12 Jul` with **Replace** — never the stored value.

Retention section previews the effect before saving:

> Keeping **2** local and **2** remote per guest. Applying now would delete
> **9 local files (127.3 GiB)** and **9 remote files**. [ Preview list ]

## 12. Component inventory (shadcn/ui)

`button · input · select · checkbox · radio-group · switch · slider · dialog · alert-dialog ·
sheet · drawer · dropdown-menu · command · table · badge · progress · tabs · tooltip ·
popover · toast(sonner) · skeleton · separator · scroll-area · form · calendar · pagination`

Project-specific components: `StatCard`, `StatusBadge`, `UsageBar`, `GuestPicker`,
`CronBuilder`, `LogViewer` (virtualised, ANSI-aware), `TaskProgress`, `ConfirmVmidDialog`,
`ByteSize`, `RelativeTime`, `AgentStatusPill`, `SyncGutter`.

## 13. States and accessibility

Every list has four designed states: **loading** (skeleton rows, never a spinner on a full
page), **empty** (what it is + the one action that fills it), **error** (what failed, why,
retry), **partial** (stale data + a warning that the agent is unreachable).

- WCAG 2.1 AA contrast for all text and status colours on their surfaces.
- Status is never colour-only — every badge pairs a glyph with its colour.
- Full keyboard operation: focus rings on `--accent`, `Esc` closes overlays, arrow-key table
  navigation, focus trapped in dialogs and returned on close.
- Destructive actions are never a single click: confirm dialog, and typed confirmation for
  restores and multi-delete.
- `prefers-reduced-motion` disables the running-pulse and progress transitions.
