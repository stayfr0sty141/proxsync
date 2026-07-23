import type { QueryClient } from "@tanstack/react-query";
import { queryDomains } from "./query-keys";
import type { EventName } from "@/types/events";

/**
 * Map each SSE event name to the set of query domains it makes stale.
 *
 * This is the single source of truth for "when the server says X changed, what
 * does the UI need to refetch". Keeping it declarative (event -> domains) rather
 * than scattering `invalidateQueries` through components means a new event wires
 * up in one place, and the same table drives the tests.
 */
const EVENT_INVALIDATIONS: Record<EventName, readonly (readonly string[])[]> = {
  "stream.open": [],
  "backup.progress": [
    // Progress is high-frequency; it drives live UI via the event bus, not a
    // refetch. Deliberately invalidates nothing to avoid a refetch storm.
  ],
  "backup.state": [queryDomains.backups, queryDomains.runs, queryDomains.dashboard],
  "run.state": [queryDomains.runs, queryDomains.dashboard, queryDomains.backups],
  "job.state": [queryDomains.jobs, queryDomains.dashboard],
  "inventory.updated": [queryDomains.guests, queryDomains.dashboard],
  "storage.update": [queryDomains.storage, queryDomains.dashboard],
  "restore.state": [queryDomains.restores, queryDomains.dashboard],
  "restore.progress": [
    // Same rationale as backup.progress: live-only, no refetch.
  ],
  "notification.state": [queryDomains.notifications],
};

/**
 * Invalidate every query family a given event marks stale. Uses the array-prefix
 * match so `["runs", ...]` invalidates every runs query regardless of its params.
 */
export function invalidateForEvent(client: QueryClient, event: EventName): void {
  const domains = EVENT_INVALIDATIONS[event];
  for (const domain of domains) {
    void client.invalidateQueries({ queryKey: domain });
  }
}

/** Exposed for tests: the raw mapping. */
export { EVENT_INVALIDATIONS };
