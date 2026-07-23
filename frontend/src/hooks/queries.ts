"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { queryKeys, queryDomains } from "@/lib/query-keys";
import type {
  AllSettingsResponse,
  AuditListResponse,
  BackupHistoryListResponse,
  BackupHistoryResponse,
  BackupJobListResponse,
  BackupJobResponse,
  BackupRunListResponse,
  BackupRunResponse,
  BrowserListResponse,
  ComparisonResponse,
  DashboardActivityResponse,
  DashboardSummary,
  GuestListResponse,
  HealthDetailResponse,
  JobPreviewResponse,
  LogListResponse,
  NotificationListResponse,
  RemoteQuotaResponse,
  RestoreListResponse,
  RestoreResponse,
  SectionResponse,
  StorageByGuestResponse,
  StorageForecastResponse,
  StorageHistoryResponse,
  StorageStatusResponse,
  SyncQueueStatus,
  SyncTaskListResponse,
} from "@/types/api";

/**
 * Query and mutation hooks, one thin wrapper per endpoint.
 *
 * Every hook derives its key from the shared `queryKeys` factory so the SSE layer
 * can invalidate by domain prefix. Mutations invalidate the domains they affect
 * on success; the SSE stream is the backstop that keeps other clients in sync.
 * Params objects are passed straight through as the query key's tail so distinct
 * filters cache independently.
 */

// ----- Dashboard --------------------------------------------------------------

export function useDashboardSummary() {
  return useQuery({
    queryKey: queryKeys.dashboard.summary(),
    queryFn: () => api.get<DashboardSummary>("/dashboard/summary"),
  });
}

export function useDashboardActivity(limit = 20) {
  return useQuery({
    queryKey: queryKeys.dashboard.activity(limit),
    queryFn: () => api.get<DashboardActivityResponse>("/dashboard/activity", { params: { limit } }),
  });
}

// ----- Health -----------------------------------------------------------------

export function useHealthDetail() {
  return useQuery({
    queryKey: queryKeys.health.detail(),
    queryFn: () => api.get<HealthDetailResponse>("/health/detail"),
    // The agent pill wants reasonably fresh reachability without hammering.
    refetchInterval: 30_000,
  });
}

// ----- Guests -----------------------------------------------------------------

export function useGuests(params?: Record<string, string | number | boolean | undefined>) {
  return useQuery({
    queryKey: queryKeys.guests.list(params),
    queryFn: () => api.get<GuestListResponse>("/guests", { params }),
  });
}

// ----- Backup jobs (schedules) ------------------------------------------------

export function useBackupJobs() {
  return useQuery({
    queryKey: queryKeys.jobs.list(),
    queryFn: () => api.get<BackupJobListResponse>("/backup-jobs"),
  });
}

export function useBackupJob(id: number) {
  return useQuery({
    queryKey: queryKeys.jobs.detail(id),
    queryFn: () => api.get<BackupJobResponse>(`/backup-jobs/${id}`),
    enabled: Number.isFinite(id),
  });
}

export function useJobPreview(id: number) {
  return useQuery({
    queryKey: queryKeys.jobs.preview(id),
    queryFn: () => api.get<JobPreviewResponse>(`/backup-jobs/${id}/preview`),
    enabled: Number.isFinite(id),
  });
}

// ----- Runs -------------------------------------------------------------------

export function useRuns(params?: Record<string, string | number | boolean | undefined>) {
  return useQuery({
    queryKey: queryKeys.runs.list(params),
    queryFn: () => api.get<BackupRunListResponse>("/runs", { params }),
  });
}

export function useRun(id: number) {
  return useQuery({
    queryKey: queryKeys.runs.detail(id),
    queryFn: () => api.get<BackupRunResponse>(`/runs/${id}`),
    enabled: Number.isFinite(id),
  });
}

// ----- Backup history ---------------------------------------------------------

export function useBackups(params?: Record<string, string | number | boolean | undefined>) {
  return useQuery({
    queryKey: queryKeys.backups.list(params),
    queryFn: () => api.get<BackupHistoryListResponse>("/backups", { params }),
  });
}

export function useBackup(id: number) {
  return useQuery({
    queryKey: queryKeys.backups.detail(id),
    queryFn: () => api.get<BackupHistoryResponse>(`/backups/${id}`),
    enabled: Number.isFinite(id),
  });
}

export function useBackupLog(id: number) {
  return useQuery({
    queryKey: queryKeys.backups.log(id),
    queryFn: () => api.get<{ lines: string[] }>(`/backups/${id}/log`),
    enabled: Number.isFinite(id),
  });
}

// ----- Restores ---------------------------------------------------------------

export function useRestores(params?: Record<string, string | number | boolean | undefined>) {
  return useQuery({
    queryKey: queryKeys.restores.list(params),
    queryFn: () => api.get<RestoreListResponse>("/restores", { params }),
  });
}

export function useRestore(id: number) {
  return useQuery({
    queryKey: queryKeys.restores.detail(id),
    queryFn: () => api.get<RestoreResponse>(`/restores/${id}`),
    enabled: Number.isFinite(id),
  });
}

// ----- Sync / browser ---------------------------------------------------------

export function useSyncStatus() {
  return useQuery({
    queryKey: queryKeys.sync.status(),
    queryFn: () => api.get<SyncQueueStatus>("/sync/status"),
  });
}

export function useSyncTasks(params?: Record<string, string | number | boolean | undefined>) {
  return useQuery({
    queryKey: queryKeys.sync.tasks(params),
    queryFn: () => api.get<SyncTaskListResponse>("/sync/tasks", { params }),
  });
}

export function useSyncQuota() {
  return useQuery({
    queryKey: queryKeys.sync.quota(),
    queryFn: () => api.get<RemoteQuotaResponse>("/sync/quota"),
  });
}

export function useBrowserCompare() {
  return useQuery({
    queryKey: queryKeys.browser.compare(),
    queryFn: () => api.get<ComparisonResponse>("/browser/compare"),
  });
}

export function useBrowserLocal(path?: string) {
  return useQuery({
    queryKey: queryKeys.browser.local(path),
    queryFn: () => api.get<BrowserListResponse>("/browser/local", { params: { path } }),
  });
}

export function useBrowserRemote(path?: string) {
  return useQuery({
    queryKey: queryKeys.browser.remote(path),
    queryFn: () => api.get<BrowserListResponse>("/browser/remote", { params: { path } }),
  });
}

// ----- Storage ----------------------------------------------------------------

export function useStorageStatus() {
  return useQuery({
    queryKey: queryKeys.storage.status(),
    queryFn: () => api.get<StorageStatusResponse>("/storage"),
  });
}

export function useStorageHistory(days = 30) {
  return useQuery({
    queryKey: queryKeys.storage.history(days),
    queryFn: () => api.get<StorageHistoryResponse>("/storage/history", { params: { days } }),
  });
}

export function useStorageForecast() {
  return useQuery({
    queryKey: queryKeys.storage.forecast(),
    queryFn: () => api.get<StorageForecastResponse>("/storage/forecast"),
  });
}

export function useStorageByGuest() {
  return useQuery({
    queryKey: queryKeys.storage.byGuest(),
    queryFn: () => api.get<StorageByGuestResponse>("/storage/by-guest"),
  });
}

// ----- Notifications ----------------------------------------------------------

export function useNotifications(params?: Record<string, string | number | boolean | undefined>) {
  return useQuery({
    queryKey: queryKeys.notifications.list(params),
    queryFn: () => api.get<NotificationListResponse>("/notifications", { params }),
  });
}

// ----- Logs & audit -----------------------------------------------------------

export function useLogs(params?: Record<string, string | number | boolean | undefined>) {
  return useQuery({
    queryKey: queryKeys.logs.list(params),
    queryFn: () => api.get<LogListResponse>("/logs", { params }),
  });
}

export function useAudit(params?: Record<string, string | number | boolean | undefined>) {
  return useQuery({
    queryKey: queryKeys.logs.audit(params),
    queryFn: () => api.get<AuditListResponse>("/audit", { params }),
  });
}

// ----- Settings ---------------------------------------------------------------

export function useAllSettings() {
  return useQuery({
    queryKey: queryKeys.settings.all(),
    queryFn: () => api.get<AllSettingsResponse>("/settings"),
  });
}

export function useSettingsSection(section: string) {
  return useQuery({
    queryKey: queryKeys.settings.section(section),
    queryFn: () => api.get<SectionResponse>(`/settings/${section}`),
    enabled: Boolean(section),
  });
}

// ----- Shared mutation helper -------------------------------------------------

/**
 * Build a mutation that invalidates one or more query domains on success. Keeps
 * every page's action wiring to a single line and guarantees the affected lists
 * refetch even before the SSE echo arrives.
 */
export function useInvalidatingMutation<TArgs, TResult>(
  mutationFn: (args: TArgs) => Promise<TResult>,
  domains: readonly (readonly string[])[],
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => {
      for (const domain of domains) {
        void queryClient.invalidateQueries({ queryKey: domain });
      }
    },
  });
}

export { queryDomains };
