import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Lightweight native select styled to the token system. A native <select> is
 * used for filter controls (rather than a custom Radix listbox) because it is
 * fully keyboard- and screen-reader-native and the option sets here are short.
 */
const Select = React.forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement>>(
  ({ className, children, ...props }, ref) => (
    <select
      ref={ref}
      className={cn(
        "h-9 rounded-md border border-border-default bg-inset px-2 text-sm text-fg",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    >
      {children}
    </select>
  ),
);
Select.displayName = "Select";

export { Select };
