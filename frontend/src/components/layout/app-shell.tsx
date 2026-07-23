"use client";

import { useEffect, useState, type ReactNode } from "react";
import { Sidebar } from "./sidebar";
import { Header } from "./header";
import { cn } from "@/lib/utils";

/**
 * The authenticated application shell (UI.md): a fixed sidebar rail, a top header
 * and a scrollable content column.
 *
 * Responsiveness follows the spec's breakpoints. At >=1280px the sidebar is
 * expanded; between 768 and 1280px it auto-collapses to an icon rail; below
 * 768px it becomes an overlay sheet toggled from the header. The manual toggle
 * lets an operator override the auto behaviour at any width. Layout width is
 * driven by the sidebar's own token widths so nothing hard-codes a pixel value
 * that could drift from the design tokens.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [isMobile, setIsMobile] = useState(false);

  // Track the two relevant breakpoints and set the default rail state to match.
  useEffect(() => {
    const mobileQuery = window.matchMedia("(max-width: 767px)");
    const compactQuery = window.matchMedia("(max-width: 1279px)");

    const apply = () => {
      setIsMobile(mobileQuery.matches);
      // Auto-collapse in the compact band; leave the user's manual choice alone
      // once they've toggled by only reacting to the media state here.
      setCollapsed(compactQuery.matches && !mobileQuery.matches);
      if (!mobileQuery.matches) setMobileOpen(false);
    };

    apply();
    mobileQuery.addEventListener("change", apply);
    compactQuery.addEventListener("change", apply);
    return () => {
      mobileQuery.removeEventListener("change", apply);
      compactQuery.removeEventListener("change", apply);
    };
  }, []);

  const toggle = () => {
    if (isMobile) {
      setMobileOpen((open) => !open);
    } else {
      setCollapsed((value) => !value);
    }
  };

  return (
    <div className="flex h-screen overflow-hidden bg-base">
      {/* Desktop / compact rail */}
      {!isMobile && (
        <div className="shrink-0">
          <Sidebar collapsed={collapsed} />
        </div>
      )}

      {/* Mobile overlay sheet */}
      {isMobile && mobileOpen && (
        <div className="fixed inset-0 z-40 flex">
          <div className="shrink-0">
            <Sidebar collapsed={false} />
          </div>
          <button
            type="button"
            className="flex-1 bg-black/50"
            aria-label="Close navigation"
            onClick={() => setMobileOpen(false)}
          />
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <Header onToggleSidebar={toggle} />
        <main
          className={cn("flex-1 overflow-y-auto p-4 md:p-6", "focus:outline-none")}
          tabIndex={-1}
        >
          {children}
        </main>
      </div>
    </div>
  );
}
