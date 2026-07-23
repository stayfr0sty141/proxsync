"use client";

import { Toaster as SonnerToaster } from "sonner";
import { useTheme } from "next-themes";

/**
 * Global toast host (sonner). Success/error/info toasts are the app's transient
 * feedback channel for mutations (a manual backup queued, a setting saved, a
 * restore refused). Styled through our tokens and following the active theme so
 * a toast never flashes the wrong palette. Mounted once in the root layout.
 */
export function Toaster() {
  const { resolvedTheme } = useTheme();
  return (
    <SonnerToaster
      theme={(resolvedTheme as "light" | "dark" | undefined) ?? "dark"}
      position="bottom-right"
      toastOptions={{
        style: {
          background: "var(--bg-elevated)",
          border: "1px solid var(--border)",
          color: "var(--fg-default)",
        },
      }}
    />
  );
}
