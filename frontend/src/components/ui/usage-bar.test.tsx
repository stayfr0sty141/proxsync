import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { UsageBar } from "./usage-bar";

/**
 * Component test for UsageBar. It must convey usage as text (used/total and a
 * percentage) alongside the coloured fill, so severity is never colour-only, and
 * it must render an "Unknown" state rather than a misleading 0% when the total is
 * absent. The progressbar role carries the numeric value for assistive tech.
 */

describe("UsageBar", () => {
  it("renders the used/total and percentage as text", () => {
    render(<UsageBar label="Local" usedBytes={512} totalBytes={1024} />);
    expect(screen.getByText(/50%/)).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");
  });

  it("shows Unknown when the total is null rather than 0%", () => {
    render(<UsageBar label="Drive" usedBytes={null} totalBytes={null} />);
    expect(screen.getByText("Unknown")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
  });

  it("caps the reported value at 100 when over budget", () => {
    render(<UsageBar label="Local" usedBytes={2048} totalBytes={1024} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
  });

  it("labels the progressbar for assistive tech", () => {
    render(<UsageBar label="Local pool" usedBytes={1} totalBytes={10} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-label", "Local pool");
  });
});
