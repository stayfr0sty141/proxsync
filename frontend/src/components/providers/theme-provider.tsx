"use client";

import { ThemeProvider as NextThemesProvider } from "next-themes";
import type { ReactNode } from "react";

/**
 * Theme provider wiring next-themes to our token system.
 *
 * Our CSS tokens key off `:root[data-theme="light"]` (src/styles/globals.css), so
 * we tell next-themes to write the active theme onto the `data-theme` attribute
 * rather than a class. `defaultTheme="dark"` matches the dark-first design; the
 * system option lets an operator follow their OS. `disableTransitionOnChange`
 * avoids a flash of half-swapped colours when toggling.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  return (
    <NextThemesProvider
      attribute="data-theme"
      defaultTheme="dark"
      enableSystem
      disableTransitionOnChange
    >
      {children}
    </NextThemesProvider>
  );
}
