"use client";

import Link from "next/link";
import { useTheme } from "next-themes";
import { Menu, Moon, Sun, Bell } from "lucide-react";
import { useAuth } from "@/components/providers/auth-provider";
import { useNotifications } from "@/hooks/queries";
import { AgentStatusPill } from "./agent-status-pill";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * Top bar of the shell (UI.md): sidebar toggle, agent connectivity pill, a
 * notification bell badged with the pending-delivery count, a theme toggle, and
 * the signed-in user with a sign-out affordance. The bell count reads the same
 * `/notifications` pending figure the notifications page shows, so the two never
 * disagree.
 */
export function Header({ onToggleSidebar }: { onToggleSidebar: () => void }) {
  const { user, logout } = useAuth();
  const { theme, setTheme, resolvedTheme } = useTheme();
  const { data: notifications } = useNotifications({ status: "pending", limit: 1 });

  const pending = notifications?.pending ?? 0;
  const isDark = (resolvedTheme ?? theme) !== "light";

  return (
    <header className="flex h-14 items-center gap-3 border-b border-border-default bg-surface px-4">
      <Button variant="ghost" size="icon" onClick={onToggleSidebar} aria-label="Toggle navigation">
        <Menu className="size-4" />
      </Button>

      <div className="flex-1" />

      <AgentStatusPill />

      <Link
        href="/notifications"
        aria-label={`Notifications${pending > 0 ? `, ${pending} pending` : ""}`}
      >
        <Button variant="ghost" size="icon" className="relative">
          <Bell className="size-4" />
          {pending > 0 && (
            <Badge
              variant="accent"
              className="absolute -right-1 -top-1 h-4 min-w-4 justify-center px-1 text-[10px]"
            >
              {pending > 99 ? "99+" : pending}
            </Badge>
          )}
        </Button>
      </Link>

      <Button
        variant="ghost"
        size="icon"
        onClick={() => setTheme(isDark ? "light" : "dark")}
        aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
      >
        <Sun className={cn("size-4", isDark && "hidden")} />
        <Moon className={cn("size-4", !isDark && "hidden")} />
      </Button>

      <div className="flex items-center gap-2 border-l border-border-default pl-3">
        <div className="hidden flex-col items-end sm:flex">
          <span className="text-xs font-medium text-fg">{user?.username}</span>
          <span className="text-[10px] capitalize text-fg-muted">{user?.role}</span>
        </div>
        <Button variant="secondary" size="sm" onClick={() => void logout()}>
          Sign out
        </Button>
      </div>
    </header>
  );
}
