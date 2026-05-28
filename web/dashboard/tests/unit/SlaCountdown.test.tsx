import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SlaCountdown } from "@/components/reviews/SlaCountdown";

const NOW = new Date("2026-05-27T12:00:00Z").getTime();

beforeEach(() => {
  vi.setSystemTime(NOW);
});

describe("SlaCountdown", () => {
  it("shows 'SLA expired' for past deadlines", () => {
    const past = new Date(NOW - 3_600_000).toISOString();
    render(<SlaCountdown deadline={past} />);
    expect(screen.getByText("SLA expired")).toBeInTheDocument();
  });

  it("shows high urgency for deadlines < 4h away", () => {
    const soon = new Date(NOW + 2 * 3_600_000).toISOString();
    const { container } = render(<SlaCountdown deadline={soon} />);
    // destructive variant applies border-transparent bg-destructive
    expect(container.firstChild).toHaveClass("bg-destructive");
  });

  it("shows medium urgency for deadlines 4–12h away", () => {
    const medium = new Date(NOW + 6 * 3_600_000).toISOString();
    const { container } = render(<SlaCountdown deadline={medium} />);
    expect(container.firstChild).toHaveClass("bg-amber-100");
  });

  it("shows low urgency for deadlines > 12h away", () => {
    const far = new Date(NOW + 24 * 3_600_000).toISOString();
    const { container } = render(<SlaCountdown deadline={far} />);
    expect(container.firstChild).toHaveClass("bg-muted");
  });
});
