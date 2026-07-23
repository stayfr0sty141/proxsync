"use client";

import { useState, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/problem";

/**
 * Application-wide TanStack Query provider.
 *
 * Retry policy is deliberately narrow: a 4xx is a definitive answer from the
 * server (a 401 is already transparently refreshed one level down in the fetch
 * client, and a 403/404/409 will not change on retry), so only transient errors
 * are retried, and only a couple of times. `staleTime` is short because most
 * ProxSync data is live and additionally invalidated by the SSE stream.
 */
export function QueryProvider({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 10_000,
            gcTime: 5 * 60_000,
            refetchOnWindowFocus: false,
            retry: (failureCount, error) => {
              if (error instanceof ApiError && error.status < 500) {
                return false;
              }
              return failureCount < 2;
            },
          },
          mutations: {
            retry: false,
          },
        },
      }),
  );

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
