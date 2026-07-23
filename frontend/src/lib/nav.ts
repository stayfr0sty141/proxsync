import {
  LayoutDashboard,
  Archive,
  CalendarClock,
  RotateCcw,
  FolderTree,
  RefreshCw,
  HardDrive,
  ScrollText,
  Bell,
  Settings,
  type LucideIcon,
} from "lucide-react";

/**
 * Single source of truth for the primary navigation (docs/UI.md route map).
 *
 * Both the sidebar and the command palette read this list, so a route added here
 * appears in both without drift. `requiredRole` mirrors the API's authorisation
 * floor: a viewer sees monitoring pages, an operator can restore, an admin can
 * change settings and users. Items above the operator's role are hidden rather
 * than shown-and-disabled, since a hidden capability is not a discoverability
 * loss for a tool whose permissions are assigned deliberately.
 */

export interface NavItem {
  label: string;
  href: string;
  icon: LucideIcon;
  requiredRole: "viewer" | "operator" | "admin";
  /** Match child routes too (e.g. /backups/123 highlights Backups). */
  matchPrefix?: boolean;
}

export const NAV_ITEMS: NavItem[] = [
  { label: "Dashboard", href: "/", icon: LayoutDashboard, requiredRole: "viewer" },
  { label: "Backups", href: "/backups", icon: Archive, requiredRole: "viewer", matchPrefix: true },
  {
    label: "Schedules",
    href: "/schedules",
    icon: CalendarClock,
    requiredRole: "viewer",
    matchPrefix: true,
  },
  { label: "Restore", href: "/restore", icon: RotateCcw, requiredRole: "operator" },
  { label: "Browser", href: "/browser", icon: FolderTree, requiredRole: "viewer" },
  { label: "Sync", href: "/sync", icon: RefreshCw, requiredRole: "viewer" },
  { label: "Storage", href: "/storage", icon: HardDrive, requiredRole: "viewer" },
  { label: "Logs", href: "/logs", icon: ScrollText, requiredRole: "viewer", matchPrefix: true },
  { label: "Notifications", href: "/notifications", icon: Bell, requiredRole: "viewer" },
  {
    label: "Settings",
    href: "/settings/general",
    icon: Settings,
    requiredRole: "admin",
    matchPrefix: true,
  },
];

/** Does a nav item match the current pathname? */
export function isActive(item: NavItem, pathname: string): boolean {
  if (item.href === "/") return pathname === "/";
  const base = item.href.split("/").slice(0, 2).join("/");
  return item.matchPrefix ? pathname.startsWith(base) : pathname === item.href;
}
