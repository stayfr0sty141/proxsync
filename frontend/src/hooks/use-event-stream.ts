"use client";

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { invalidateForEvent } from "@/lib/event-invalidation";
import { progressStore } from "@/lib/progress-store";
import { tokenStore } from "@/lib/api/token-store";
import { EVENT_NAMES, type EventName } from "@/types/events";
import type {
  BackupProgressEvent,
  BackupStateEvent,
  RestoreProgressEvent,
  RestoreStateEvent,
} from "@/types/events";

/**
 * Live connection to the backend SSE stream.
 *
 * `EventSource` cannot set an Authorization header, so the access token is passed
 * as a query param (docs/API.md). On any drop the browser's EventSource retries
 * on its own, but a rotated/expired token would make it retry forever with a 401;
 * we therefore own the lifecycle: close on error, back off, and reopen with a
 * freshly-read token. Terminal state changes invalidate the affected query
 * families; the two high-frequency progress events feed the progress store
 * instead, and a terminal backup/restore state clears its live slot so the
 * mini-progress bar disappears when work stops.
 */

export type StreamStatus = "connecting" | "open" | "closed";

const BASE_RECONNECT_MS = 1000;
const MAX_RECONNECT_MS = 30000;

const TERMINAL_BACKUP_STATES = new Set(["success", "failed", "cancelled", "interrupted"]);
const TERMINAL_RESTORE_STATES = new Set([
  "success",
  "failed",
  "cancelled",
  "interrupted",
  "expired",
]);

export function useEventStream(): StreamStatus {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const reconnectAttempts = useRef(0);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    let disposed = false;

    const scheduleReconnect = () => {
      const attempt = reconnectAttempts.current;
      const delay = Math.min(BASE_RECONNECT_MS * 2 ** attempt, MAX_RECONNECT_MS);
      // Full jitter so a fleet of tabs does not reconnect in lockstep.
      const jittered = Math.random() * delay;
      reconnectTimer.current = setTimeout(connect, jittered);
      reconnectAttempts.current = attempt + 1;
    };

    const handleTypedEvent = (name: EventName, raw: string) => {
      invalidateForEvent(queryClient, name);

      if (name === "backup.progress") {
        progressStore.pushBackup(JSON.parse(raw) as BackupProgressEvent);
      } else if (name === "restore.progress") {
        progressStore.pushRestore(JSON.parse(raw) as RestoreProgressEvent);
      } else if (name === "backup.state") {
        const data = JSON.parse(raw) as BackupStateEvent;
        if (TERMINAL_BACKUP_STATES.has(data.status)) progressStore.clearBackup();
      } else if (name === "run.state") {
        // A run leaving "running" means no backup is in flight for it.
        const data = JSON.parse(raw) as { status: string };
        if (data.status !== "running") progressStore.clearBackup();
      } else if (name === "restore.state") {
        const data = JSON.parse(raw) as RestoreStateEvent;
        if (TERMINAL_RESTORE_STATES.has(data.status)) progressStore.clearRestore();
      }
    };

    const connect = () => {
      if (disposed) return;
      const token = tokenStore.getAccessToken();
      if (!token) {
        // Not authenticated yet; try again shortly without counting it as a
        // failed connection attempt.
        reconnectTimer.current = setTimeout(connect, BASE_RECONNECT_MS);
        return;
      }

      setStatus("connecting");
      const source = new EventSource(`/api/v1/events/stream?token=${encodeURIComponent(token)}`);
      sourceRef.current = source;

      source.onopen = () => {
        if (disposed) return;
        reconnectAttempts.current = 0;
        setStatus("open");
      };

      source.onerror = () => {
        // EventSource fires error on transient drops and on hard failures alike.
        // We always close and own the reconnect so an expired token cannot pin
        // the browser in a 401 retry loop.
        source.close();
        sourceRef.current = null;
        if (disposed) return;
        setStatus("closed");
        scheduleReconnect();
      };

      for (const name of EVENT_NAMES) {
        source.addEventListener(name, (event) => {
          if (disposed) return;
          try {
            handleTypedEvent(name, (event as MessageEvent).data);
          } catch {
            // A malformed frame must never tear down the stream; skip it.
          }
        });
      }
    };

    connect();

    return () => {
      disposed = true;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      sourceRef.current?.close();
      sourceRef.current = null;
    };
  }, [queryClient]);

  return status;
}
