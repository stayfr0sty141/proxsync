import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Text input on the inset surface. Focus uses the accent ring shared with every
 * other focusable control (UI.md keyboard operability). `aria-invalid` flips the
 * border to danger so a field error is conveyed by more than colour when paired
 * with the field-level message the forms render.
 */
const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, type, ...props }, ref) => (
    <input
      type={type}
      ref={ref}
      className={cn(
        "flex h-9 w-full rounded-md border border-border-default bg-inset px-3 py-1 text-sm text-fg shadow-sm transition-colors",
        "placeholder:text-fg-subtle",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "aria-[invalid=true]:border-danger",
        className,
      )}
      {...props}
    />
  ),
);
Input.displayName = "Input";

export { Input };
