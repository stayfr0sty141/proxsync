/**
 * TypeScript mirror of the ProxSync backend API contract (docs/API.md).
 *
 * These types are hand-maintained against `GET /api/v1/openapi.json`. They are
 * intentionally exhaustive: every field the UI reads is typed, so a contract
 * drift surfaces as a `tsc` error rather than a runtime `undefined`.
 */

// ---------------------------------------------------------------------------
// Shared primitives
// ---------------------------------------------------------------------------

/** RFC 9457 problem document. Every non-2xx response uses this shape. */
export interface ProblemDetail {
  type: string;
  title: string;
  status: number;
  detail?: string;
  instance?: string;
  /** Some problems carry field-level validation errors. */
  errors?: Record<string, string[]>;
  [key: string]: unknown;
}

export interface Paginated<T> {
  items: T[];
  total: number;
  limit?: number;
  offset?: number;
}

export type GuestType = "vm" | "lxc";
export type BackupMode = "snapshot" | "suspend" | "stop";
export type Compression = "none" | "lzo" | "gzip" | "zstd";

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export type UserRole = "admin" | "operator" | "viewer";

export interface UserResponse {
  id: number;
  username: string;
  email: string | null;
  role: UserRole;
  is_active: boolean;
  must_change_password: boolean;
  last_login_at: string | null;
  created_at: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  csrf_token: string;
  user: UserResponse;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface ChangePasswordRequest {
  current_password: string;
  new_password: string;
}

export interface SessionResponse {
  id: number;
  created_at: string;
  last_used_at: string | null;
  user_agent: string | null;
  ip_address: string | null;
  current: boolean;
}

// ---------------------------------------------------------------------------
// Guests
// ---------------------------------------------------------------------------

export interface GuestResponse {
  id: number;
  vmid: number;
  guest_type: GuestType;
  name: string;
  node: string;
  status: string;
  backup_enabled: boolean;
  tags: string[];
  maxmem_bytes: number | null;
  maxdisk_bytes: number | null;
  last_seen_at: string | null;
}

export interface GuestListResponse {
  items: GuestResponse[];
  total: number;
}

export interface InventorySyncResponse {
  node: string;
  discovered: number;
  added: number;
  updated: number;
  disappeared: number;
}

// ---------------------------------------------------------------------------
// Backup jobs (schedules)
// ---------------------------------------------------------------------------

export type TargetSelector = "all" | "include" | "exclude";

export interface GuestTarget {
  vmid: number;
  guest_type: GuestType;
}

export interface BackupJobResponse {
  id: number;
  name: string;
  enabled: boolean;
  cron_expression: string;
  timezone: string;
  mode: BackupMode;
  compression: Compression;
  zstd_threads: number;
  storage: string;
  target_selector: TargetSelector;
  targets: GuestTarget[];
  keep_local: number;
  keep_remote: number;
  upload_enabled: boolean;
  notify_on_start: boolean;
  notify_on_success: boolean;
  notify_on_failure: boolean;
  bwlimit_kbps: number | null;
  max_parallel: number;
  last_run_at: string | null;
  next_run_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface BackupJobCreate {
  name: string;
  cron_expression: string;
  timezone: string;
  mode: BackupMode;
  compression: Compression;
  zstd_threads?: number;
  storage: string;
  target_selector: TargetSelector;
  targets: GuestTarget[];
  keep_local: number;
  keep_remote: number;
  upload_enabled: boolean;
  notify_on_start?: boolean;
  notify_on_success?: boolean;
  notify_on_failure?: boolean;
  bwlimit_kbps?: number | null;
  max_parallel?: number;
  enabled?: boolean;
}

export interface BackupJobListResponse {
  items: BackupJobResponse[];
  total: number;
}

export interface JobPreviewResponse {
  next_fire_times: string[];
  resolved_targets: number[];
  skipped: { vmid: number; reason: string }[];
}

// ---------------------------------------------------------------------------
// Backup runs
// ---------------------------------------------------------------------------

export type RunStatus =
  "queued" | "running" | "success" | "failed" | "partial" | "cancelled" | "interrupted";

export type RunTrigger = "scheduled" | "manual";

export interface RunProgress {
  vmid: number | null;
  guest_name: string | null;
  percent: number | null;
  bytes_done: number | null;
  bytes_total: number | null;
  rate_bps: number | null;
  eta_seconds: number | null;
}

export interface BackupRunResponse {
  id: number;
  job_id: number | null;
  job_name: string | null;
  trigger: RunTrigger;
  status: RunStatus;
  guest_total: number;
  guest_succeeded: number;
  guest_failed: number;
  targets: number[];
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  error_message: string | null;
  correlation_id: string;
  progress: RunProgress | null;
}

export interface BackupRunListResponse {
  items: BackupRunResponse[];
  total: number;
}

export interface ManualBackupTarget {
  vmid: number;
  guest_type: GuestType;
}

export interface ManualBackupRequest {
  targets: ManualBackupTarget[];
  mode: BackupMode;
  compression: Compression;
  storage: string;
  upload: boolean;
  notes?: string;
}

export interface RunAcceptedResponse {
  run: BackupRunResponse;
  queued_position: number;
}

// ---------------------------------------------------------------------------
// Backup history
// ---------------------------------------------------------------------------

export type BackupStatus = "success" | "failed" | "running" | "cancelled" | "interrupted";

export type UploadStatus =
  | "not_uploaded"
  | "pending"
  | "uploading"
  | "uploaded"
  | "failed"
  | "verifying"
  | "verified"
  | "hash_mismatch"
  | "hash_unavailable";

export interface BackupHistoryResponse {
  id: number;
  run_id: number | null;
  vmid: number;
  guest_type: GuestType;
  guest_name: string;
  node: string;
  storage: string;
  filename: string;
  local_path: string | null;
  size_bytes: number | null;
  mode: BackupMode;
  compression: Compression;
  checksum_sha256: string | null;
  status: BackupStatus;
  agent_task_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  error_message: string | null;
  warnings: string[];
  upload_status: UploadStatus;
  uploaded_at: string | null;
  remote_path: string | null;
  retention_locked: boolean;
  local_deleted_at: string | null;
  remote_deleted_at: string | null;
  correlation_id: string;
}

export interface BackupHistoryListResponse {
  items: BackupHistoryResponse[];
  total: number;
  limit: number;
  offset: number;
}

// ---------------------------------------------------------------------------
// Retention
// ---------------------------------------------------------------------------

export interface RetentionDecision {
  backup_id: number;
  vmid: number;
  filename: string;
  scope: "local" | "remote";
  action: "keep" | "delete";
  reason: string;
  size_bytes: number | null;
}

export interface RetentionSummary {
  evaluated: number;
  local_delete_count: number;
  local_delete_bytes: number;
  remote_delete_count: number;
  remote_delete_bytes: number;
  deleted_local: number;
  deleted_remote: number;
  failed: number;
}

export interface RetentionResponse {
  policy: { keep_local: number; keep_remote: number };
  items: RetentionDecision[];
  summary: RetentionSummary;
}

// ---------------------------------------------------------------------------
// Restore (two-phase)
// ---------------------------------------------------------------------------

export type RestoreMode = "qemu" | "lxc";
export type RestoreStatus =
  | "preflight"
  | "pending_confirmation"
  | "queued"
  | "running"
  | "success"
  | "failed"
  | "cancelled"
  | "interrupted"
  | "expired";

export type PreflightCheckStatus = "pass" | "fail" | "warn";

export interface PreflightCheck {
  name: string;
  status: PreflightCheckStatus;
  detail: string;
}

export interface PreflightReport {
  checks: PreflightCheck[];
  blocking: boolean;
  warnings: string[];
  source: "local" | "remote";
  backup_id: number;
  size_bytes: number | null;
  target_vmid: number;
  target_type: GuestType;
  target_storage: string;
  target_node: string;
}

export interface RestoreRequest {
  backup_id: number;
  restore_mode: RestoreMode;
  target_vmid: number;
  target_storage: string;
  target_node: string;
  overwrite_existing: boolean;
  force_stop: boolean;
  start_after_restore: boolean;
}

export interface RestoreCreatedResponse {
  id: number;
  status: RestoreStatus;
  confirmation_token: string;
  expires_at: string;
  preflight: PreflightReport;
}

export interface RestoreConfirmRequest {
  confirmation_token: string;
  target_vmid: number;
}

export interface RestoreProgress {
  percent: number | null;
  phase: string | null;
}

export interface RestoreResponse {
  id: number;
  backup_id: number;
  vmid: number;
  guest_type: GuestType;
  restore_mode: RestoreMode;
  target_vmid: number;
  target_storage: string;
  target_node: string;
  status: RestoreStatus;
  source: "local" | "remote";
  overwrite_existing: boolean;
  force_stop: boolean;
  start_after_restore: boolean;
  agent_task_id: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  error_message: string | null;
  correlation_id: string;
  progress: RestoreProgress | null;
}

export interface RestoreListResponse {
  items: RestoreResponse[];
  total: number;
}

// ---------------------------------------------------------------------------
// Sync / browser
// ---------------------------------------------------------------------------

export type SyncTaskStatus = "queued" | "running" | "success" | "failed" | "cancelled";

export type SyncOperation = "upload" | "download" | "verify" | "delete";

export interface SyncTaskResponse {
  id: number;
  backup_id: number | null;
  operation: SyncOperation;
  status: SyncTaskStatus;
  filename: string | null;
  bytes_total: number | null;
  bytes_done: number | null;
  percent: number | null;
  rate_bps: number | null;
  attempt: number;
  max_attempts: number;
  next_attempt_at: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface SyncTaskListResponse {
  items: SyncTaskResponse[];
  total: number;
}

export interface SyncQueueStatus {
  enabled: boolean;
  remote_name: string | null;
  remote_path: string | null;
  queued: number;
  running: number;
  failed: number;
  pending_uploads: number;
  current: SyncTaskResponse | null;
}

export interface RemoteQuotaResponse {
  remote: string;
  configured: boolean;
  total_bytes?: number | null;
  quota_bytes?: number | null;
  used_bytes: number | null;
  free_bytes: number | null;
  trashed_bytes: number | null;
  used_percent: number | null;
  detail: string | null;
}

export interface BrowserEntry {
  name: string;
  path: string;
  size_bytes: number | null;
  modified_at: string | null;
  is_dir: boolean;
  backup_id: number | null;
}

export interface BrowserListResponse {
  location: "local" | "remote";
  entries: BrowserEntry[];
  total: number;
  detail: string | null;
}

export type SyncState = "in_sync" | "local_only" | "remote_only" | "size_mismatch";

export interface ComparisonEntry {
  name: string;
  state: SyncState;
  local_size: number | null;
  remote_size: number | null;
  backup_id: number | null;
}

export interface ComparisonResponse {
  in_sync: number;
  local_only: number;
  remote_only: number;
  size_mismatch: number;
  entries: ComparisonEntry[];
  detail: string | null;
}

// ---------------------------------------------------------------------------
// Storage
// ---------------------------------------------------------------------------

export type StorageSeverity = "ok" | "warning" | "critical";

export interface StoragePool {
  name: string;
  type: string;
  active: boolean;
  total_bytes: number;
  used_bytes: number;
  available_bytes: number;
  used_percent: number;
}

export interface StorageStatusResponse {
  captured_at: string;
  local: {
    total_bytes: number;
    used_bytes: number;
    free_bytes: number;
    used_percent: number;
    severity: StorageSeverity;
  };
  storages: StoragePool[];
  remote: RemoteQuotaResponse;
}

export interface StorageSnapshotResponse {
  captured_at: string;
  used_bytes: number;
  total_bytes: number;
  used_percent: number;
}

export interface StorageHistoryResponse {
  days: number;
  items: StorageSnapshotResponse[];
}

export interface StorageForecastResponse {
  sample_count: number;
  growth_bytes_per_day: number | null;
  days_until_full: number | null;
  estimated_full_at: string | null;
  detail: string | null;
}

export interface GuestStorageResponse {
  vmid: number;
  guest_name: string;
  guest_type: GuestType;
  backup_count: number;
  total_bytes: number;
}

export interface StorageByGuestResponse {
  items: GuestStorageResponse[];
  total_bytes: number;
}

// ---------------------------------------------------------------------------
// Notifications
// ---------------------------------------------------------------------------

export type NotificationStatus = "pending" | "sending" | "sent" | "failed" | "suppressed";

export interface NotificationResponse {
  id: number;
  event_type: string;
  channel: string;
  status: NotificationStatus;
  subject: string | null;
  body: string;
  attempts: number;
  max_attempts: number;
  next_attempt_at: string | null;
  error_message: string | null;
  suppressed_by: number | null;
  dedupe_key: string | null;
  created_at: string;
  sent_at: string | null;
}

export interface NotificationListResponse {
  items: NotificationResponse[];
  total: number;
  pending: number;
}

export interface TelegramTestResponse {
  ok: boolean;
  detail: string | null;
  error_code: number | null;
  description: string | null;
}

// ---------------------------------------------------------------------------
// Logs & audit
// ---------------------------------------------------------------------------

export type LogLevel = "debug" | "info" | "warning" | "error" | "critical";
export type LogCategory =
  | "backup"
  | "restore"
  | "sync"
  | "retention"
  | "storage"
  | "auth"
  | "agent"
  | "scheduler"
  | "notify"
  | "system";

export interface LogEntryResponse {
  id: number;
  ts: string;
  level: LogLevel;
  category: LogCategory;
  event: string;
  message: string;
  correlation_id: string | null;
  backup_id: number | null;
  restore_id: number | null;
  context: Record<string, unknown>;
}

export interface LogListResponse {
  items: LogEntryResponse[];
  total: number;
  persisted: number;
  dropped: number;
}

export interface AuditEventResponse {
  id: number;
  ts: string;
  action: string;
  result: "success" | "failure";
  user_id: number | null;
  username: string | null;
  ip_address: string | null;
  correlation_id: string | null;
  detail: Record<string, unknown>;
}

export interface AuditListResponse {
  items: AuditEventResponse[];
  total: number;
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export type SettingsSection = "general" | "gdrive" | "telegram" | "retention" | "agent" | "proxmox";

export interface SecretHint {
  configured: boolean;
  hint: string | null;
  updated_at: string | null;
}

export interface SectionResponse {
  section: SettingsSection;
  values: Record<string, unknown>;
  secrets: Record<string, SecretHint>;
  updated_at: string | null;
}

export interface AllSettingsResponse {
  sections: SectionResponse[];
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export interface ComponentHealth {
  name: string;
  status: "ok" | "degraded" | "down";
  detail: string | null;
  latency_ms: number | null;
}

export interface HealthDetailResponse {
  status: "ok" | "degraded" | "down";
  version: string;
  environment: string;
  started_at: string;
  uptime_seconds: number;
  components: ComponentHealth[];
}

// ---------------------------------------------------------------------------
// Dashboard (aggregate endpoints)
// ---------------------------------------------------------------------------

export interface DashboardSummary {
  guests: {
    vm_total: number;
    vm_running: number;
    lxc_total: number;
    lxc_running: number;
  };
  last_backup: {
    id: number;
    finished_at: string | null;
    status: RunStatus;
    guest_count: number;
    duration_seconds: number | null;
    size_bytes: number | null;
  } | null;
  next_backup: {
    job_id: number;
    name: string;
    next_run_at: string | null;
    cron: string;
    timezone: string;
  } | null;
  storage: {
    local: {
      total_bytes: number;
      used_bytes: number;
      free_bytes: number;
      used_percent: number;
    };
    gdrive: {
      used_bytes: number | null;
      quota_bytes: number | null;
      used_percent: number | null;
    };
  };
  jobs: {
    running: number;
    queued: number;
    failed_24h: number;
    failed_7d: number;
  };
  agent: {
    reachable: boolean;
    version: string | null;
    latency_ms: number | null;
  };
}

export type ActivityKind = "backup" | "restore" | "sync";

export interface ActivityItem {
  kind: ActivityKind;
  id: number;
  title: string;
  status: string;
  ts: string;
  correlation_id: string | null;
}

export interface DashboardActivityResponse {
  items: ActivityItem[];
}
