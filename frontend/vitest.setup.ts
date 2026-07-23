import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// React Testing Library unmounts components between tests so state does not leak.
afterEach(() => {
  cleanup();
});

// jsdom does not implement matchMedia; several components read it for the theme
// and `prefers-reduced-motion`. Provide a no-op, non-matching implementation.
if (typeof window !== "undefined" && !window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }),
  });
}

// jsdom lacks ResizeObserver, which Recharts' ResponsiveContainer and some Radix
// primitives rely on. A minimal stub keeps them from throwing under test.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// jsdom does not implement scrollIntoView, used by some Radix primitives on open.
if (typeof Element !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = vi.fn();
}
