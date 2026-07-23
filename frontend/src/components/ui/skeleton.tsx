import { cn } from "@/lib/utils";

/**
 * Loading placeholder. Every list's loading state (UI.md's four required states)
 * renders rows of these rather than a spinner, so the layout does not jump when
 * real data arrives. The pulse respects prefers-reduced-motion globally.
 */
function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("animate-pulse rounded-md bg-elevated", className)}
      aria-hidden="true"
      {...props}
    />
  );
}

export { Skeleton };
