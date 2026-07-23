/**
 * Central mapping from a backend status string to its visual treatment.
 *
 * docs/UI.md requires that status is *never* conveyed by colour alone: every
 * badge pairs a token-based colour with a text glyph and a human label. Keeping
 * that mapping in one place means a new status added on the backend fails one
 * lookup here rather than rendering inconsistently across five pages.
 *
 * `tone` names a semantic colour token (success/warning/danger/info/neutral/
 * accent); components translate it to the `--color-*-muted` background and the
 * matching foreground. `glyph` is a short text/emoji marker that survives a
 * greyscale screenshot and a screen reader.
 */

export type StatusTone = "success" | "warning" | "danger" | "info" | "neutral" | "accent";

export interface StatusMeta {
  label: string;
  tone: StatusTone;
  glyph: string;
  /** True for states that should pulse while work is in flight. */
  active?: boolean;
}

const FALLBACK: StatusMeta = { label: "Unknown", tone: "neutral", glyph: "?" };

const RUN_STATUS: Record<string, StatusMeta> = {
  queued: { label: "Queued", tone: "neutral", glyph: "\u25CB" },
  running: { label: "Running", tone: "info", glyph: "\u25D0", active: true },
  success: { label: "Success", tone: "success", glyph: "\u2714" },
  failed: { label: "Failed", tone: "danger", glyph: "\u2717" },
  partial: { label: "Partial", tone: "warning", glyph: "\u25D1" },
  cancelled: { label: "Cancelled", tone: "neutral", glyph: "\u2298" },
  interrupted: { label: "Interrupted", tone: "warning", glyph: "\u26A0" },
};

const UPLOAD_STATUS: Record<string, StatusMeta> = {
  not_uploaded: { label: "Not uploaded", tone: "neutral", glyph: "\u2014" },
  pending: { label: "Pending", tone: "neutral", glyph: "\u25CB" },
  uploading: { label: "Uploading", tone: "info", glyph: "\u2191", active: true },
  uploaded: { label: "Uploaded", tone: "success", glyph: "\u2714" },
  failed: { label: "Failed", tone: "danger", glyph: "\u2717" },
  verifying: { label: "Verifying", tone: "info", glyph: "\u21BB", active: true },
  verified: { label: "Verified", tone: "success", glyph: "\u2714\uFE0E" },
  hash_mismatch: { label: "Hash mismatch", tone: "danger", glyph: "\u2260" },
  hash_unavailable: { label: "Hash unavailable", tone: "warning", glyph: "\u2298" },
};

const RESTORE_STATUS: Record<string, StatusMeta> = {
  preflight: { label: "Preflight", tone: "info", glyph: "\u2699" },
  pending_confirmation: { label: "Awaiting confirmation", tone: "warning", glyph: "\u23F3" },
  queued: { label: "Queued", tone: "neutral", glyph: "\u25CB" },
  running: { label: "Running", tone: "info", glyph: "\u25D0", active: true },
  success: { label: "Success", tone: "success", glyph: "\u2714" },
  failed: { label: "Failed", tone: "danger", glyph: "\u2717" },
  cancelled: { label: "Cancelled", tone: "neutral", glyph: "\u2298" },
  interrupted: { label: "Interrupted", tone: "warning", glyph: "\u26A0" },
  expired: { label: "Expired", tone: "neutral", glyph: "\u231B" },
};

const SYNC_STATUS: Record<string, StatusMeta> = {
  queued: { label: "Queued", tone: "neutral", glyph: "\u25CB" },
  running: { label: "Running", tone: "info", glyph: "\u25D0", active: true },
  success: { label: "Success", tone: "success", glyph: "\u2714" },
  failed: { label: "Failed", tone: "danger", glyph: "\u2717" },
  cancelled: { label: "Cancelled", tone: "neutral", glyph: "\u2298" },
};

const NOTIFICATION_STATUS: Record<string, StatusMeta> = {
  pending: { label: "Pending", tone: "neutral", glyph: "\u25CB" },
  sending: { label: "Sending", tone: "info", glyph: "\u2191", active: true },
  sent: { label: "Sent", tone: "success", glyph: "\u2714" },
  failed: { label: "Failed", tone: "danger", glyph: "\u2717" },
  suppressed: { label: "Suppressed", tone: "neutral", glyph: "\u25CE" },
};

const SYNC_STATE: Record<string, StatusMeta> = {
  in_sync: { label: "In sync", tone: "success", glyph: "=" },
  local_only: { label: "Local only", tone: "warning", glyph: "\u2191" },
  remote_only: { label: "Remote only", tone: "info", glyph: "\u2193" },
  size_mismatch: { label: "Mismatch", tone: "danger", glyph: "\u2260" },
};

const SEVERITY: Record<string, StatusMeta> = {
  ok: { label: "OK", tone: "success", glyph: "\u2714" },
  warning: { label: "Warning", tone: "warning", glyph: "\u26A0" },
  critical: { label: "Critical", tone: "danger", glyph: "\u2717" },
};

const REGISTRIES = {
  run: RUN_STATUS,
  backup: RUN_STATUS,
  upload: UPLOAD_STATUS,
  restore: RESTORE_STATUS,
  sync: SYNC_STATUS,
  notification: NOTIFICATION_STATUS,
  syncState: SYNC_STATE,
  severity: SEVERITY,
} as const;

export type StatusDomain = keyof typeof REGISTRIES;

/** Look up the visual treatment for a status within a domain. */
export function statusMeta(domain: StatusDomain, status: string | null | undefined): StatusMeta {
  if (!status) return FALLBACK;
  return REGISTRIES[domain][status] ?? { ...FALLBACK, label: humanise(status) };
}

/** Turn a snake_case status into a readable fallback label. */
function humanise(value: string): string {
  return value
    .split("_")
    .map((word) => (word.length > 0 ? word[0]!.toUpperCase() + word.slice(1) : word))
    .join(" ");
}
