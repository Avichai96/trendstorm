import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { CitationPanel } from "@/components/reports/CitationPanel";
import type { Citation } from "@/api/types.generated";

const makeCitation = (overrides: Partial<Citation> = {}): Citation => ({
  chunk_id: "chunk-01",
  source_url: "https://example.com/article",
  excerpt: "This is the cited excerpt.",
  ...overrides,
});

describe("CitationPanel", () => {
  it("renders nothing when citations list is empty", () => {
    const { container } = render(<CitationPanel citations={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders one button per citation", () => {
    const citations = [makeCitation(), makeCitation({ chunk_id: "chunk-02" })];
    render(<CitationPanel citations={citations} />);
    expect(screen.getByLabelText("Citation 1")).toBeInTheDocument();
    expect(screen.getByLabelText("Citation 2")).toBeInTheDocument();
  });

  it("shows detail panel when a citation button is clicked", () => {
    render(<CitationPanel citations={[makeCitation()]} />);
    fireEvent.click(screen.getByLabelText("Citation 1"));
    expect(screen.getByText("This is the cited excerpt.")).toBeInTheDocument();
    expect(screen.getByText("example.com")).toBeInTheDocument();
  });

  it("deselects citation on second click", () => {
    render(<CitationPanel citations={[makeCitation()]} />);
    fireEvent.click(screen.getByLabelText("Citation 1"));
    expect(screen.getByText("This is the cited excerpt.")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Citation 1"));
    expect(screen.queryByText("This is the cited excerpt.")).toBeNull();
  });

  it("handles malformed source_url gracefully", () => {
    const citation = makeCitation({ source_url: "not-a-valid-url" });
    render(<CitationPanel citations={[citation]} />);
    fireEvent.click(screen.getByLabelText("Citation 1"));
    // Should not throw; raw URL is displayed as fallback
    expect(screen.getByText("not-a-valid-url")).toBeInTheDocument();
  });
});
