import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { SourceForm, DeleteSourceConfirm } from "@/components/sources/SourceForm";
import * as sourceQueries from "@/api/queries/sources";
import type { Source } from "@/api/types.generated";

const CAT_ID = "01HTEST00000000000000000002";

function Wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const MOCK_SOURCE: Source = {
  id: "01HTEST00000000000000000003",
  category_id: CAT_ID,
  url: "https://example.com/feed.rss",
  label: "Example Feed",
  type: "rss",
  enabled: true,
  last_fetch_at: null,
  last_fetch_status: null,
  last_fetch_error: null,
  created_at: new Date().toISOString(),
};

describe("SourceForm", () => {
  const mockRegister = vi.fn();

  beforeEach(() => {
    vi.spyOn(sourceQueries, "registerSource").mockImplementation(mockRegister);
  });

  it("renders URL, label, and type fields", () => {
    render(
      <Wrapper>
        <SourceForm open={true} onOpenChange={() => {}} categoryId={CAT_ID} />
      </Wrapper>,
    );
    expect(screen.getByText("Register Source")).toBeInTheDocument();
    expect(screen.getByLabelText("URL *")).toBeInTheDocument();
    expect(screen.getByLabelText("Label")).toBeInTheDocument();
    expect(screen.getByLabelText("Type")).toBeInTheDocument();
  });

  it("Register button is disabled when URL is empty", () => {
    render(
      <Wrapper>
        <SourceForm open={true} onOpenChange={() => {}} categoryId={CAT_ID} />
      </Wrapper>,
    );
    expect(screen.getByRole("button", { name: "Register" })).toBeDisabled();
  });

  it("enables Register button once URL is entered", () => {
    render(
      <Wrapper>
        <SourceForm open={true} onOpenChange={() => {}} categoryId={CAT_ID} />
      </Wrapper>,
    );
    fireEvent.change(screen.getByLabelText("URL *"), {
      target: { value: "https://example.com/feed.rss" },
    });
    expect(screen.getByRole("button", { name: "Register" })).not.toBeDisabled();
  });

  it("calls registerSource with correct body on submit", async () => {
    mockRegister.mockResolvedValue(MOCK_SOURCE);
    render(
      <Wrapper>
        <SourceForm open={true} onOpenChange={() => {}} categoryId={CAT_ID} />
      </Wrapper>,
    );
    fireEvent.change(screen.getByLabelText("URL *"), {
      target: { value: "https://example.com/feed.rss" },
    });
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Example Feed" } });
    fireEvent.click(screen.getByRole("button", { name: "Register" }));
    await waitFor(() => {
      expect(mockRegister).toHaveBeenCalledWith({
        category_id: CAT_ID,
        url: "https://example.com/feed.rss",
        label: "Example Feed",
        type: "http",
      });
    });
  });
});

describe("DeleteSourceConfirm", () => {
  const mockDelete = vi.fn();

  beforeEach(() => {
    vi.spyOn(sourceQueries, "deleteSource").mockImplementation(mockDelete);
  });

  it("does not render when source is null", () => {
    render(
      <Wrapper>
        <DeleteSourceConfirm source={null} categoryId={CAT_ID} onClose={() => {}} />
      </Wrapper>,
    );
    expect(screen.queryByText("Disable source?")).not.toBeInTheDocument();
  });

  it("renders confirmation with source label when source is set", () => {
    render(
      <Wrapper>
        <DeleteSourceConfirm source={MOCK_SOURCE} categoryId={CAT_ID} onClose={() => {}} />
      </Wrapper>,
    );
    expect(screen.getByText("Disable source?")).toBeInTheDocument();
    expect(screen.getByText("Example Feed")).toBeInTheDocument();
  });

  it("calls deleteSource on confirm", async () => {
    mockDelete.mockResolvedValue(undefined);
    const onClose = vi.fn();
    render(
      <Wrapper>
        <DeleteSourceConfirm source={MOCK_SOURCE} categoryId={CAT_ID} onClose={onClose} />
      </Wrapper>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Disable" }));
    await waitFor(() => {
      expect(mockDelete).toHaveBeenCalledWith(MOCK_SOURCE.id);
    });
  });
});
