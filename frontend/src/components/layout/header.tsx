"use client";

import { useState } from "react";
import Link from "next/link";
import { useTheme } from "next-themes";
import { Menu, Moon, Sun, Bell, KeyRound, Zap } from "lucide-react";
import { useAuth } from "@/components/providers/auth-provider";
import { useNotifications } from "@/hooks/queries";
import { AgentStatusPill } from "./agent-status-pill";
import { ChangePasswordDialog } from "@/components/dialogs/change-password-dialog";
import { ManualBackupDialog } from "@/components/dialogs/manual-backup-dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * Top bar of the shell (UI.md): sidebar toggle, agent connectivity pill, a
 * notification bell badged with the pending-delivery count, a theme toggle, and
 * the signed-in user with password change and sign-out affordances. The bell count
 * reads the same `/notifications` pending figure the notifications page shows.
 */
export function Header({ onToggleSidebar }: Readonly<{ onToggleSidebar: () => void }>) {
  const { user, logout } = useAuth();
  const { theme, setTheme, resolvedTheme } = useTheme();
  const { data: notifications } = useNotifications({ status: "pending", limit: 1 });
  const [showPasswordDialog, setShowPasswordDialog] = useState(false);
  const [showBackupModal, setShowBackupModal] = useState(false);

  const pending = notifications?.pending ?? 0;
  const isDark = (resolvedTheme ?? theme) !== "light";
  const mustChangePassword = user?.must_change_password ?? false;

  const bellAriaLabel = pending > 0 ? `Notifications, ${pending} pending` : "Notifications";

  return (
    <header className="flex h-14 items-center gap-3 border-b border-border-default bg-surface px-4">
      <Button variant="ghost" size="icon" onClick={onToggleSidebar} aria-label="Toggle navigation">
        <Menu className="size-4" />
      </Button>

      <div className="flex-1" />

      <Button
        variant="secondary"
        size="sm"
        className="gap-1.5 text-xs font-medium"
        onClick={() => setShowBackupModal(true)}
      >
        <Zap className="size-3.5 text-accent fill-accent" />
        <span>Backup</span>
      </Button>

      <AgentStatusPill />

      <Link
        href="/notifications"
        aria-label={bellAriaLabel}
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
        <Button
          variant="ghost"
          size="icon"
          onClick={() => setShowPasswordDialog(true)}
          title="Change password"
          aria-label="Change password"
        >
          <KeyRound className="size-4 text-fg-muted hover:text-fg" />
        </Button>
        <Button variant="secondary" size="sm" onClick={() => void logout()}>
          Sign out
        </Button>
      </div>

      {(showPasswordDialog || mustChangePassword) && (
        <ChangePasswordDialog
          forced={mustChangePassword}
          onClose={() => setShowPasswordDialog(false)}
        />
      )}

      {showBackupModal && (
        <>
          <button
            type="button"
            aria-label="Close dialog"
            className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm"
            onClick={() => setShowBackupModal(false)}
          />
          <dialog
            open
            aria-labelledby="header-manual-backup-title"
            className="fixed left-1/2 top-1/2 z-50 m-0 w-full max-w-2xl -translate-x-1/2 -translate-y-1/2 rounded-xl border border-border-muted bg-surface p-0 text-fg-default shadow-2xl"
            onKeyDown={(e) => {
              if (e.key === "Escape") setShowBackupModal(false);
            }}
          >
            <div className="flex items-center justify-between border-b border-border-muted px-5 py-4">
              <h2 id="header-manual-backup-title" className="text-base font-semibold">
                Start Manual Backup
              </h2>
              <button
                type="button"
                onClick={() => setShowBackupModal(false)}
                className="text-lg text-fg-muted transition-colors hover:text-fg-default"
                aria-label="Close dialog"
              >
                ✕
              </button>
            </div>
            <div className="max-h-[85vh] overflow-y-auto px-5 py-4">
              <ManualBackupDialog onClose={() => setShowBackupModal(false)} />
            </div>
          </dialog>
        </>
      )}
    </header>
  );
}
