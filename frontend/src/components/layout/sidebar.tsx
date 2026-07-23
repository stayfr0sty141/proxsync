"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuth, hasRole } from "@/components/providers/auth-provider";
import { NAV_ITEMS, isActive } from "@/lib/nav";
import { cn } from "@/lib/utils";
import { MiniProgress } from "./mini-progress";

/**
 * Primary navigation rail.
 *
 * Items are filtered by the user's role (an operator never sees Settings), the
 * active item is derived from the pathname via the shared `isActive`, and the
 * live MiniProgress sits pinned at the bottom (UI.md shell) so in-flight work is
 * visible from every page. Below 1280px the parent collapses this to icons; the
 * labels are hidden with `lg:inline` rather than removed so the DOM (and the
 * accessible name) stays intact.
 */
export function Sidebar({ collapsed }: { collapsed: boolean }) {
  const pathname = usePathname();
  const { user } = useAuth();

  const items = NAV_ITEMS.filter((item) => hasRole(user, item.requiredRole));

  return (
    <nav
      aria-label="Primary"
      className={cn(
        "flex h-full flex-col border-r border-border-default bg-surface transition-all",
        collapsed ? "w-14" : "w-58",
      )}
      style={{ width: collapsed ? "56px" : "232px" }}
    >
      <div className="flex items-center gap-2 px-4 py-4">
        <span
          className="inline-block h-5 w-1.5 shrink-0 rounded-full bg-accent"
          aria-hidden="true"
        />
        {!collapsed && <span className="font-semibold text-fg">ProxSync</span>}
      </div>

      <ul className="flex flex-1 flex-col gap-0.5 px-2">
        {items.map((item) => {
          const active = isActive(item, pathname);
          const Icon = item.icon;
          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={active ? "page" : undefined}
                title={collapsed ? item.label : undefined}
                className={cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
                  active
                    ? "bg-accent-muted text-accent"
                    : "text-fg-muted hover:bg-elevated hover:text-fg",
                  collapsed && "justify-center px-0",
                )}
              >
                <Icon className="size-4 shrink-0" aria-hidden="true" />
                {!collapsed && <span>{item.label}</span>}
              </Link>
            </li>
          );
        })}
      </ul>

      <div className="border-t border-border-default p-2">
        <MiniProgress collapsed={collapsed} />
      </div>
    </nav>
  );
}
