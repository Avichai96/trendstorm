import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { PipelineProgress } from "@/components/jobs/PipelineProgress";

describe("PipelineProgress", () => {
  it("shows 100% for completed", () => {
    const { container } = render(<PipelineProgress status="completed" refinementLoops={0} />);
    const indicator = container.querySelector("[style]") as HTMLElement;
    expect(indicator.style.transform).toBe("translateX(-0%)");
  });

  it("renders refinement loop badge when loops > 0", () => {
    render(<PipelineProgress status="analyzing" refinementLoops={2} />);
    expect(screen.getByText("Refinement loop 2")).toBeInTheDocument();
  });

  it("hides refinement badge when loops = 0", () => {
    render(<PipelineProgress status="analyzing" refinementLoops={0} />);
    expect(screen.queryByText(/Refinement loop/)).toBeNull();
  });

  it("shows awaiting review status text", () => {
    render(<PipelineProgress status="awaiting_review" refinementLoops={0} />);
    expect(screen.getByText("awaiting review")).toBeInTheDocument();
  });
});
