/**
 * Centralised TanStack Query key factory.
 *
 * Every hook derives its key from here so that SSE-driven invalidation (see
 * useEventStream) can target exactly the right slices without stringly-typed
 * keys drifting between the reader and the invalidator. Keys are arrays with a
 * stable domain head followed by any parameters.
 */

export const queryKeys = {
  auth: {
    me: () => ["auth", "me"] as const,
    sessions: () => ["auth", "sessions"] as const,
  },
  dashboard: {
    summary: () => ["dashboard", "summary"] as const,
    activity: (limit: number) => ["dashboard", "activity", limit] as const,
  },
  guests: {
    list: (params?: Record<string, unknown>) => ["guests", "list", params ?? {}] as const,
    detail: (id: number) => ["guests", "detail", id] as const,
  },
  jobs: {
    list: () => ["jobs", "list"] as const,
    detail: (id: number) => ["jobs", "detail", id] as const,
    preview: (id: number) => ["jobs", "preview", id] as const,
  },
  runs: {
    list: (params?: Record<string, unknown>) => ["runs", "list", params ?? {}] as const,
    detail: (id: number) => ["runs", "detail", id] as const,
    backups: (id: number) => ["runs", "backups", id] as const,
  },
  backups: {
    list: (params?: Record<string, unknown>) => ["backups", "list", params ?? {}] as const,
    detail: (id: number) => ["backups", "detail", id] as const,
    log: (id: number) => ["backups", "log", id] as const,
  },
  restores: {
    list: (params?: Record<string, unknown>) => ["restores", "list", params ?? {}] as const,
    detail: (id: number) => ["restores", "detail", id] as const,
    log: (id: number) => ["restores", "log", id] as const,
  },
  sync: {
    status: () => ["sync", "status"] as const,
    tasks: (params?: Record<string, unknown>) => ["sync", "tasks", params ?? {}] as const,
    quota: () => ["sync", "quota"] as const,
  },
  browser: {
    local: (path?: string) => ["browser", "local", path ?? ""] as const,
    remote: (path?: string) => ["browser", "remote", path ?? ""] as const,
    compare: () => ["browser", "compare"] as const,
  },
  storage: {
    status: () => ["storage", "status"] as const,
    history: (days: number) => ["storage", "history", days] as const,
    forecast: () => ["storage", "forecast"] as const,
    byGuest: () => ["storage", "by-guest"] as const,
  },
  notifications: {
    list: (params?: Record<string, unknown>) => ["notifications", "list", params ?? {}] as const,
  },
  logs: {
    list: (params?: Record<string, unknown>) => ["logs", "list", params ?? {}] as const,
    audit: (params?: Record<string, unknown>) => ["logs", "audit", params ?? {}] as const,
  },
  settings: {
    all: () => ["settings", "all"] as const,
    section: (section: string) => ["settings", "section", section] as const,
  },
  health: {
    detail: () => ["health", "detail"] as const,
  },
} as const;

/** Domain heads used by the SSE layer to invalidate whole families at once. */
export const queryDomains = {
  dashboard: ["dashboard"],
  guests: ["guests"],
  jobs: ["jobs"],
  runs: ["runs"],
  backups: ["backups"],
  restores: ["restores"],
  sync: ["sync"],
  storage: ["storage"],
  notifications: ["notifications"],
} as const;
