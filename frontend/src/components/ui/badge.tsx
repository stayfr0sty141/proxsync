import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/**
 * Neutral badge for counts and labels that are NOT a lifecycle status (those use
 * StatusBadge, which enforces the glyph+colour accessibility rule). Variants map
 * to semantic tokens so a count pill can still read as info/warning where useful.
 */
const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium",
  {
    variants: {
      variant: {
        default: "bg-elevated text-fg-muted",
        accent: "bg-accent-muted text-accent",
        info: "bg-info-muted text-info",
        success: "bg-success-muted text-success",
        warning: "bg-warning-muted text-warning",
        danger: "bg-danger-muted text-danger",
        outline: "border border-border-default text-fg-muted",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
