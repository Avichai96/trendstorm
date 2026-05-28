import { render } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import axe from "axe-core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

// Minimal mock of categoriesListOptions
vi.mock("@/api/queries/categories", () => ({
  categoriesListOptions: () => ({
    queryKey: ["categories", "list", {}],
    queryFn: () => Promise.resolve({ items: [], next_cursor: null, total: 0 }),
    initialPageParam: undefined,
    getNextPageParam: () => undefined,
  }),
}));

import Categories from "@/pages/Categories";

function Wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

describe("Categories accessibility", () => {
  it("has no critical axe violations on the empty state", async () => {
    const { container } = render(
      <Wrapper>
        <Categories />
      </Wrapper>,
    );
    const results = await axe.run(container);
    const critical = results.violations.filter((v) => v.impact === "critical");
    expect(critical).toHaveLength(0);
  });
});
