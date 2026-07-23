/**
 * TypeScript mirror of the SSE event contract (docs/API.md § SSE Event Stream).
 *
 * The backend emits named SSE events; each `data` frame is a JSON object. These
 * types let `useEventStream` dispatch on `event` with a correctly-typed payload,
 * so a renamed field on the wire fails `tsc` rather than silently no-op-ing a
 * cache invalidation.
 */

export interface BackupProgressEvent {
  run_id: number;
  backup_id: number | null;
  vmid: number;
  guest_name: string | null;
  percent: number | null;
  bytes_done: number | null;
  bytes_total: number | null;
  rate_bps: number | null;
  eta_seconds: number | null;
}

export interface BackupStateEvent {
  run_id: number;
  backup_id: number | null;
  vmid: number;
  status: string;
  error_message?: string | null;
}

export interface RunStateEvent {
  run_id: number;
  status: string;
}

export interface JobStateEvent {
  job_id: number;
  status: string;
}

export interface InventoryUpdatedEvent {
  node: string;
  added: number;
  updated: number;
  disappeared: number;
}

export interface StorageUpdateEvent {
  captured_at: string;
  local_used_percent: number;
  severity: string;
}

export interface RestoreStateEvent {
  restore_id: number;
  status: string;
}

export interface RestoreProgressEvent {
  restore_id: number;
  percent: number | null;
  phase: string | null;
}

export interface NotificationStateEvent {
  notification_id: number;
  status: string;
}

/** Map of event name -> payload type. `stream.open` carries no useful data. */
export interface EventPayloadMap {
  "stream.open": Record<string, never>;
  "backup.progress": BackupProgressEvent;
  "backup.state": BackupStateEvent;
  "run.state": RunStateEvent;
  "job.state": JobStateEvent;
  "inventory.updated": InventoryUpdatedEvent;
  "storage.update": StorageUpdateEvent;
  "restore.state": RestoreStateEvent;
  "restore.progress": RestoreProgressEvent;
  "notification.state": NotificationStateEvent;
}

export type EventName = keyof EventPayloadMap;

/** All event names the client subscribes to, with their common envelope keys. */
export const EVENT_NAMES: EventName[] = [
  "stream.open",
  "backup.progress",
  "backup.state",
  "run.state",
  "job.state",
  "inventory.updated",
  "storage.update",
  "restore.state",
  "restore.progress",
  "notification.state",
];

/** Common envelope every event data frame carries alongside its payload. */
export interface EventEnvelope {
  ts?: string;
  correlation_id?: string | null;
}
