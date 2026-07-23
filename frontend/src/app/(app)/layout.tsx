import type { ReactNode } from "react";
import { AuthGuard } from "@/components/layout/auth-guard";
import { AppShell } from "@/components/layout/app-shell";

/**
 * Layout for the authenticated route group. Everything under (app) is gated by
 * AuthGuard (which also mounts the SSE stream) and framed by AppShell. The login
 * route lives outside this group, so it renders without the shell or the guard.
 */
export default function AppGroupLayout({ children }: { children: ReactNode }) {
  return (
    <AuthGuard>
      <AppShell>{children}</AppShell>
    </AuthGuard>
  );
}
