import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { DecisionForm } from "@/components/reviews/DecisionForm";
import * as reviewQueries from "@/api/queries/reviews";

function Wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

describe("DecisionForm", () => {
  const mockResolve = vi.fn();

  beforeEach(() => {
    vi.spyOn(reviewQueries, "resolveReview").mockImplementation(mockResolve);
  });

  it("renders all three action buttons", () => {
    render(
      <Wrapper>
        <DecisionForm reviewId="rev1" jobId="job1" />
      </Wrapper>,
    );
    expect(screen.getByText("Approve")).toBeInTheDocument();
    expect(screen.getByText("Request Refinement")).toBeInTheDocument();
    expect(screen.getByText("Reject")).toBeInTheDocument();
  });

  it("opens confirmation dialog on click", async () => {
    render(
      <Wrapper>
        <DecisionForm reviewId="rev1" jobId="job1" />
      </Wrapper>,
    );
    fireEvent.click(screen.getByText("Approve"));
    await waitFor(() => {
      expect(screen.getByText("Approve this analysis?")).toBeInTheDocument();
    });
  });

  it("disables confirm for refinement without comment", async () => {
    render(
      <Wrapper>
        <DecisionForm reviewId="rev1" jobId="job1" />
      </Wrapper>,
    );
    fireEvent.click(screen.getByText("Request Refinement"));
    await waitFor(() => {
      expect(screen.getByText("Request refinement?")).toBeInTheDocument();
    });
    const confirmBtn = screen.getAllByText("Confirm")[0];
    expect(confirmBtn).toBeDisabled();
  });

  it("enables confirm for refinement once comment is typed", async () => {
    render(
      <Wrapper>
        <DecisionForm reviewId="rev1" jobId="job1" />
      </Wrapper>,
    );
    fireEvent.click(screen.getByText("Request Refinement"));
    await waitFor(() => screen.getByPlaceholderText("Add context for the record…"));
    fireEvent.change(screen.getByPlaceholderText("Add context for the record…"), {
      target: { value: "Needs more citations" },
    });
    expect(screen.getAllByText("Confirm")[0]).not.toBeDisabled();
  });
});
