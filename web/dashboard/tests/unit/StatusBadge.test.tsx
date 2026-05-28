import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { JobStatusBadge, ReviewStatusBadge } from "@/components/shared/StatusBadge";

describe("JobStatusBadge", () => {
  const terminalStatuses = ["completed", "failed", "cancelled", "rejected"] as const;
  const nonTerminal = ["pending", "analyzing", "ingesting"] as const;

  it.each(terminalStatuses)("renders %s status", (status) => {
    render(<JobStatusBadge status={status} />);
    expect(screen.getByText(status.replace(/_/g, " "))).toBeInTheDocument();
  });

  it.each(nonTerminal)("renders non-terminal %s status", (status) => {
    render(<JobStatusBadge status={status} />);
    expect(screen.getByText(status.replace(/_/g, " "))).toBeInTheDocument();
  });

  it("renders awaiting_review with human-readable text", () => {
    render(<JobStatusBadge status="awaiting_review" />);
    expect(screen.getByText("awaiting review")).toBeInTheDocument();
  });
});

describe("ReviewStatusBadge", () => {
  it("renders pending", () => {
    render(<ReviewStatusBadge status="pending" />);
    expect(screen.getByText("pending")).toBeInTheDocument();
  });

  it("renders approved", () => {
    render(<ReviewStatusBadge status="approved" />);
    expect(screen.getByText("approved")).toBeInTheDocument();
  });

  it("renders timed_out with spaces", () => {
    render(<ReviewStatusBadge status="timed_out" />);
    expect(screen.getByText("timed out")).toBeInTheDocument();
  });
});
