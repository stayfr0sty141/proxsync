import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatusBadge } from "./status-badge";

/**
 * Component test for StatusBadge. This is the component that enforces the UI.md
 * accessibility rule in the DOM: a badge must expose a human label to assistive
 * tech and render a text glyph alongside it, so status is never conveyed by
 * colour alone. These assertions fail if a future change drops either.
 */

describe("StatusBadge", () => {
  it("renders the human label as text", () => {
    render(<StatusBadge domain="run" status="success" />);
    expect(screen.getByText("Success")).toBeInTheDocument();
  });

  it("exposes an accessible label via role=status", () => {
    render(<StatusBadge domain="upload" status="hash_mismatch" />);
    const badge = screen.getByRole("status");
    expect(badge).toHaveAttribute("aria-label", "Hash mismatch");
  });

  it("renders a glyph alongside the label by default", () => {
    const { container } = render(<StatusBadge domain="run" status="failed" />);
    // The glyph is an aria-hidden span; there should be one present.
    const hidden = container.querySelector('[aria-hidden="true"]');
    expect(hidden).not.toBeNull();
    expect(hidden?.textContent?.length).toBeGreaterThan(0);
  });

  it("omits the glyph when hideGlyph is set but keeps the label", () => {
    const { container } = render(<StatusBadge domain="run" status="failed" hideGlyph />);
    expect(container.querySelector('[aria-hidden="true"]')).toBeNull();
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });

  it("degrades to a readable label for an unknown status", () => {
    render(<StatusBadge domain="run" status="brand_new" />);
    expect(screen.getByText("Brand New")).toBeInTheDocument();
  });
});
