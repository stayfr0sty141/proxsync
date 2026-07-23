"use client";

import { useEffect, type ReactNode } from "react";
import { useRouter, usePathname } from "next/navigation";
import { useAuth, hasRole } from "@/components/providers/auth-provider";
import { useEventStream } from "@/hooks/use-event-stream";

/**
 * Gate for every authenticated route.
 *
 * While auth is bootstrapping (a silent refresh from the HttpOnly cookie) it
 * shows a minimal loading state rather than flashing the login screen. Once
 * resolved, an unauthenticated user is redirected to /login carrying a `next`
 * param so they return to where they aimed. The SSE stream is mounted here — not
 * in a provider — so it only runs for authenticated users who have a token to
 * pass to EventSource.
 */
export function AuthGuard({
  children,
  requiredRole,
}: {
  children: ReactNode;
  requiredRole?: "viewer" | "operator" | "admin";
}) {
  const { user, status } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  // Keep the SSE connection alive for the whole authenticated session.
  useEventStream();

  useEffect(() => {
    if (status === "unauthenticated") {
      const next = encodeURIComponent(pathname);
      router.replace(`/login?next=${next}`);
    }
  }, [status, pathname, router]);

  if (status === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base">
        <div className="flex flex-col items-center gap-3">
          <div className="h-6 w-1.5 animate-running-pulse rounded-full bg-accent" />
          <p className="text-xs text-fg-muted">{"Loading ProxSync\u2026"}</p>
        </div>
      </div>
    );
  }

  if (status === "unauthenticated") {
    // The redirect effect will fire; render nothing meanwhile.
    return null;
  }

  if (requiredRole && !hasRole(user, requiredRole)) {
    return (
      <div className="flex min-h-[60vh] flex-col items-center justify-center gap-2 text-center">
        <p className="text-sm font-medium text-fg">Access denied</p>
        <p className="max-w-sm text-xs text-fg-muted">
          Your account role does not have permission to view this page.
        </p>
      </div>
    );
  }

  return <>{children}</>;
}
