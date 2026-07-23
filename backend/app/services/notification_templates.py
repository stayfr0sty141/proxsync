"""Message text, one template per event.

Rendering is separated from delivery because the rendered text is **frozen at enqueue time**
and stored on the outbox row. A message that sat through a Telegram outage says what was true
when it was written, not what a redeployed template would say an hour later.

Telegram's HTML parse mode is used for the one thing plain text cannot do — a bold first line
that survives a phone's notification preview. Every interpolated value is escaped, without
exception: a guest named `<b>prod` would otherwise either break the message or, worse, be
silently swallowed by the parser.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from typing import Any

from app.schemas.enums import NotificationEvent

MAX_LISTED_GUESTS = 8
"""Beyond this the list becomes "and N more". A message naming forty guests is not read."""


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def human_bytes(value: int | float | None) -> str:
    if value is None:
        return "unknown"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"  # pragma: no cover - the loop always returns


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def human_percent(value: Any) -> str:
    """One decimal, always. `87.99999%` in an alert reads as a bug in the alerting."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.1f}%"
    return esc(value)


def guest_list(guests: Sequence[Any] | None) -> str:
    """`web (vm/101), db (vm/102) and 3 more` — names first, because that is what an operator
    recognises; the identifier is what they need to act on it."""
    if not guests:
        return "none"
    rendered: list[str] = []
    for guest in guests[:MAX_LISTED_GUESTS]:
        if isinstance(guest, Mapping):
            name = guest.get("guest_name") or guest.get("name") or "unnamed"
            vmid = guest.get("vmid")
            guest_type = guest.get("guest_type")
            # `is not None`, not truthiness: an id of 0 is still an id, and a message that
            # silently drops the identifier is the one an operator cannot act on.
            label = (
                f"{esc(name)} ({esc(guest_type)}/{esc(vmid)})" if vmid is not None else esc(name)
            )
        else:
            label = esc(guest)
        rendered.append(label)
    remaining = len(guests) - len(rendered)
    if remaining > 0:
        rendered.append(f"and {remaining} more")
    return ", ".join(rendered)


def _lines(*parts: str | None) -> str:
    return "\n".join(part for part in parts if part)


def _backup_started(v: Mapping[str, Any]) -> str:
    return _lines(
        f"▶️ <b>Backup started</b> · run #{esc(v.get('run_id'))}",
        f"{esc(v.get('guest_total'))} guest(s) · {esc(v.get('trigger', 'manual'))} "
        f"· storage {esc(v.get('storage'))}",
        guest_list(v.get("guests")),
    )


def _backup_success(v: Mapping[str, Any]) -> str:
    return _lines(
        f"✅ <b>Backup finished</b> · run #{esc(v.get('run_id'))}",
        f"{esc(v.get('succeeded'))}/{esc(v.get('guest_total'))} guest(s) · "
        f"{human_bytes(v.get('total_bytes'))} · {human_duration(v.get('duration_seconds'))}",
    )


def _backup_failed(v: Mapping[str, Any]) -> str:
    # `partial` is not a separate notification type: at least one guest has no backup tonight,
    # which is the same thing an operator has to act on.
    headline = "Backup incomplete" if v.get("succeeded") else "Backup failed"
    return _lines(
        f"❌ <b>{headline}</b> · run #{esc(v.get('run_id'))}",
        f"{esc(v.get('succeeded'))}/{esc(v.get('guest_total'))} guest(s) succeeded · "
        f"{human_duration(v.get('duration_seconds'))}",
        f"Failed: {guest_list(v.get('failed_guests'))}",
        f"<code>{esc(v['error'])}</code>" if v.get("error") else None,
    )


def _restore_started(v: Mapping[str, Any]) -> str:
    return _lines(
        f"▶️ <b>Restore started</b> · #{esc(v.get('restore_id'))}",
        f"{esc(v.get('filename'))} → {esc(v.get('target_type'))} "
        f"{esc(v.get('target_vmid'))} on {esc(v.get('target_storage'))}",
        f"Source: {esc(v.get('source'))}"
        + (" · overwriting the existing guest" if v.get("overwrite") else ""),
    )


def _restore_finished(v: Mapping[str, Any]) -> str:
    return _lines(
        f"✅ <b>Restore finished</b> · #{esc(v.get('restore_id'))}",
        f"{esc(v.get('target_type'))} {esc(v.get('target_vmid'))} · "
        f"{human_duration(v.get('duration_seconds'))}",
        f"⚠️ {esc(v['warning'])}" if v.get("warning") else None,
    )


def _restore_failed(v: Mapping[str, Any]) -> str:
    # `interrupted` reaches this template too. The status word carries the whole difference:
    # "failed" means nothing changed, "interrupted" means nobody knows what changed.
    status = str(v.get("status") or "failed")
    headline = "Restore outcome unknown" if status == "interrupted" else "Restore failed"
    return _lines(
        f"❌ <b>{headline}</b> · #{esc(v.get('restore_id'))}",
        f"{esc(v.get('target_type'))} {esc(v.get('target_vmid'))} · status {esc(status)}",
        f"<code>{esc(v['error'])}</code>" if v.get("error") else None,
    )


def _upload_failed(v: Mapping[str, Any]) -> str:
    return _lines(
        "⚠️ <b>Upload failed</b>",
        f"{esc(v.get('filename'))} ({esc(v.get('guest_type'))}/{esc(v.get('vmid'))}) "
        f"after {esc(v.get('attempts'))} attempt(s)",
        "The local copy is intact; retention will not delete it while it is unreplicated.",
        f"<code>{esc(v['error'])}</code>" if v.get("error") else None,
    )


def _retention_deleted(v: Mapping[str, Any]) -> str:
    return _lines(
        "🧹 <b>Retention applied</b>",
        f"{esc(v.get('guest_type'))}/{esc(v.get('vmid'))} {esc(v.get('guest_name', ''))}".strip(),
        f"{esc(v.get('deleted_local'))} local and {esc(v.get('deleted_remote'))} remote "
        f"copies removed · {human_bytes(v.get('freed_bytes'))} freed",
        f"Keeping {esc(v.get('keep_local'))} local / {esc(v.get('keep_remote'))} remote",
    )


def _storage_threshold(v: Mapping[str, Any]) -> str:
    severity = str(v.get("severity") or "warning")
    glyph = "🔴" if severity == "critical" else "🟠"
    return _lines(
        f"{glyph} <b>Storage {esc(severity)}</b>",
        f"{human_bytes(v.get('used_bytes'))} of {human_bytes(v.get('total_bytes'))} used "
        f"({human_percent(v.get('used_percent'))}) · {human_bytes(v.get('free_bytes'))} free",
        f"Threshold crossed from {esc(v.get('previous_severity', 'healthy'))}.",
    )


def _test(v: Mapping[str, Any]) -> str:
    return _lines(
        "🔔 <b>ProxSync test message</b>",
        "If you can read this, the bot token and chat id are correct.",
        f"Sent by {esc(v['requested_by'])}." if v.get("requested_by") else None,
    )


_TEMPLATES = {
    NotificationEvent.BACKUP_STARTED: _backup_started,
    NotificationEvent.BACKUP_SUCCESS: _backup_success,
    NotificationEvent.BACKUP_FAILED: _backup_failed,
    NotificationEvent.RESTORE_STARTED: _restore_started,
    NotificationEvent.RESTORE_FINISHED: _restore_finished,
    NotificationEvent.RESTORE_FAILED: _restore_failed,
    NotificationEvent.UPLOAD_FAILED: _upload_failed,
    NotificationEvent.RETENTION_DELETED: _retention_deleted,
    NotificationEvent.STORAGE_THRESHOLD: _storage_threshold,
    NotificationEvent.TEST: _test,
}


def render(event: NotificationEvent, variables: Mapping[str, Any]) -> str:
    """Render one event. Every member of the enum has a template; there is no fallback text."""
    return _TEMPLATES[event](variables)
