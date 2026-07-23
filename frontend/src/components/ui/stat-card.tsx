import type { ReactNode } from "react";
import { Card } from "./card";
import { Skeleton } from "./skeleton";
import { cn } from "@/lib/utils";

/**
 * Dashboard stat tile (UI.md). Shows a label, a large value, and an optional
 * sub-line/hint. `loading` renders a skeleton in place of the value so the grid
 * does not reflow when data arrives. `tone` tints the value for at-a-glance
 * severity (e.g. failed counts in danger) without relying on colour alone — the
 * label always names what the number is.
 */
export function StatCard({
  label,
  value,
  hint,
  icon,
  tone = "default",
  loading = false,
}: Readonly<{
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  icon?: ReactNode;
  tone?: "default" | "success" | "warning" | "danger" | "accent";
  loading?: boolean;
}>) {
  const toneClass = {
    default: "text-fg",
    success: "text-success",
    warning: "text-warning",
    danger: "text-danger",
    accent: "text-accent",
  }[tone];

  return (
    <Card className="p-4">
      <div className="flex items-start justify-between">
        <p className="text-xs font-medium text-fg-muted">{label}</p>
        {icon && <span className="text-fg-subtle">{icon}</span>}
      </div>
      {loading ? (
        <Skeleton className="mt-2 h-7 w-20" />
      ) : (
        <p className={cn("mt-1 text-2xl font-semibold tabular-nums", toneClass)}>{value}</p>
      )}
      {hint && !loading && <p className="mt-1 text-xs text-fg-subtle">{hint}</p>}
    </Card>
  );
}
