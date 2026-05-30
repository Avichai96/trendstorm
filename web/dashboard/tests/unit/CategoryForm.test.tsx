import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { CategoryForm } from "@/components/categories/CategoryForm";
import * as categoryQueries from "@/api/queries/categories";
import type { Category } from "@/api/types.generated";

function Wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const MOCK_CATEGORY: Category = {
  id: "01HTEST00000000000000000001",
  name: "AI Safety",
  description: "Tracks AI safety research",
  keywords: ["alignment", "red-teaming"],
  archived: false,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
};

describe("CategoryForm — create mode", () => {
  const mockCreate = vi.fn();

  beforeEach(() => {
    vi.spyOn(categoryQueries, "createCategory").mockImplementation(mockCreate);
  });

  it("renders name, description, keywords fields in create mode", () => {
    render(
      <Wrapper>
        <CategoryForm open={true} onOpenChange={() => {}} />
      </Wrapper>,
    );
    expect(screen.getByText("New Category")).toBeInTheDocument();
    expect(screen.getByLabelText("Name *")).toBeInTheDocument();
    expect(screen.getByLabelText("Description")).toBeInTheDocument();
    expect(screen.getByLabelText("Keywords")).toBeInTheDocument();
  });

  it("disables Create button when name is empty", () => {
    render(
      <Wrapper>
        <CategoryForm open={true} onOpenChange={() => {}} />
      </Wrapper>,
    );
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
  });

  it("enables Create button once name is entered", () => {
    render(
      <Wrapper>
        <CategoryForm open={true} onOpenChange={() => {}} />
      </Wrapper>,
    );
    fireEvent.change(screen.getByLabelText("Name *"), { target: { value: "AI Safety" } });
    expect(screen.getByRole("button", { name: "Create" })).not.toBeDisabled();
  });

  it("calls createCategory with correct body on submit", async () => {
    mockCreate.mockResolvedValue(MOCK_CATEGORY);
    render(
      <Wrapper>
        <CategoryForm open={true} onOpenChange={() => {}} />
      </Wrapper>,
    );
    fireEvent.change(screen.getByLabelText("Name *"), { target: { value: "AI Safety" } });
    fireEvent.change(screen.getByLabelText("Keywords"), {
      target: { value: "alignment, red-teaming" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => {
      expect(mockCreate).toHaveBeenCalledWith({
        name: "AI Safety",
        description: null,
        keywords: ["alignment", "red-teaming"],
      });
    });
  });
});

describe("CategoryForm — edit mode", () => {
  const mockUpdate = vi.fn();

  beforeEach(() => {
    vi.spyOn(categoryQueries, "updateCategory").mockImplementation(mockUpdate);
  });

  it("pre-populates fields with category data in edit mode", () => {
    render(
      <Wrapper>
        <CategoryForm open={true} onOpenChange={() => {}} category={MOCK_CATEGORY} />
      </Wrapper>,
    );
    expect(screen.getByText("Edit Category")).toBeInTheDocument();
    expect(screen.queryByLabelText("Name *")).not.toBeInTheDocument();
    const descField = screen.getByLabelText("Description") as HTMLTextAreaElement;
    expect(descField.value).toBe("Tracks AI safety research");
    const kwField = screen.getByLabelText("Keywords") as HTMLInputElement;
    expect(kwField.value).toBe("alignment, red-teaming");
  });

  it("Save button is enabled in edit mode (no name required)", () => {
    render(
      <Wrapper>
        <CategoryForm open={true} onOpenChange={() => {}} category={MOCK_CATEGORY} />
      </Wrapper>,
    );
    expect(screen.getByRole("button", { name: "Save" })).not.toBeDisabled();
  });
});
